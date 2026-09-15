"""Optional, bounded Clang analysis using exact compilation database entries."""

from __future__ import annotations

from collections.abc import Container, Mapping
from bisect import bisect_right
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import threading
import time

from quality_model import Finding, Limitation, finding_sort_key
from source_checks import apply_suppressions, parse_suppressions


MAX_UNITS = 10
UNIT_TIMEOUT = 5.0
MAX_OUTPUT_BYTES = 16 * 1024 * 1024
MAX_DATABASE_BYTES = 16 * 1024 * 1024
MAX_FINDINGS = 500
MAX_FUNCTION_LINES = 60
MAX_CLASS_LINES = 300
MAX_INTERFACE_METHODS = 10
PROCESS_REAP_TIMEOUT = 0.25
HIGH_LEVEL_SUFFIXES = ("Service", "Controller", "Manager", "UseCase")
INFRASTRUCTURE_SUFFIXES = ("Database", "Repository", "Socket", "FileSystem", "HttpClient")

_PAIRED_DRIVER_OPTIONS = frozenset({
    "-A", "-B", "-D", "-F", "-I", "-MF", "-MJ", "-MQ", "-MT", "-U",
    "-Xassembler", "-Xclang", "-Xlinker", "-Xpreprocessor", "-add-plugin", "-arch",
    "-cxx-isystem", "-fbuild-session-file", "-fmodule-file", "-fmodule-map-file",
    "-fmodules-cache-path", "-fpass-plugin", "-fplugin", "-fprebuilt-module-path",
    "-gcc-toolchain", "-idirafter", "-iframework", "-iframeworkwithsysroot",
    "-imacros", "-include", "-include-pch", "-include-pth", "-iprefix", "-iquote",
    "-isysroot", "-isystem", "-isystem-after", "-ivfsoverlay", "-iwithprefix",
    "-iwithprefixbefore", "-iwithsysroot", "-load", "-load-pass-plugin", "-mllvm",
    "-o", "-plugin", "-resource-dir", "-stdlib++-isystem", "-target",
    "-working-directory", "-x", "--gcc-toolchain", "--output", "--param", "--sysroot",
    "--target",
})
_SAFE_STANDALONE_DRIVER_OPTIONS = frozenset({
    "-ansi", "-c", "-M", "-MD", "-MG", "-MM", "-MMD", "-MP", "-pedantic",
    "-pedantic-errors", "-pipe", "-pthread", "-Qunused-arguments", "-undef", "-w",
    "-nostdinc", "-nostdinc++", "-nostdlibinc", "-fblocks", "-fexceptions",
    "-ffreestanding", "-fopenmp", "-fPIC", "-fPIE", "-frtti", "-fshort-wchar",
    "-fsyntax-only", "-fvisibility-inlines-hidden", "-ObjC", "-ObjC++",
    "--no-default-config",
})
_SAFE_DEBUG_DRIVER_OPTIONS = frozenset({
    "-g", "-g0", "-g1", "-g2", "-g3", "-ggdb", "-ggdb0", "-ggdb1",
    "-ggdb2", "-ggdb3", "-gcodeview", "-gcodeview-command-line",
    "-gcodeview-ghash", "-gdwarf", "-gdwarf-2", "-gdwarf-3", "-gdwarf-4",
    "-gdwarf-5", "-gdwarf32", "-gdwarf64", "-gembed-source",
    "-gline-directives-only", "-gline-tables-only", "-gno-codeview-command-line",
    "-gno-embed-source", "-gno-inline-line-tables", "-gstrict-dwarf",
})
_SAFE_OPTIMIZATION_DRIVER_OPTIONS = frozenset({
    "-O", "-O0", "-O1", "-O2", "-O3", "-O4", "-Ofast", "-Og", "-Os", "-Oz",
})
_SAFE_JOINED_LANGUAGE_OPTIONS = frozenset({
    "-xc", "-xc-header", "-xcpp-output", "-xc++", "-xc++-header",
    "-xc++-cpp-output", "-xnone",
})
_SAFE_JOINED_DRIVER_PREFIXES = (
    "-B", "-D", "-F", "-I", "-R", "-U", "-W", "-m",
    "-std=", "-stdlib=", "-rtlib=", "-unwindlib=", "-resource-dir=", "-target=",
    "--gcc-toolchain=", "--param=", "--sysroot=", "--target=",
)
_UNSAFE_DRIVER_OPTIONS = frozenset({
    "-cc1", "-fplugin", "-fpass-plugin", "-load", "-load-pass-plugin",
    "-plugin", "-add-plugin", "-mllvm", "-Xassembler", "-Xlinker",
    "--hipspv-pass-plugin",
})
_UNSAFE_DRIVER_PREFIXES = (
    "-fplugin=", "-fplugin-arg-", "-fpass-plugin=", "-load=",
    "-load-pass-plugin=", "-plugin=", "-add-plugin=", "-mllvm=",
    "--hipspv-pass-plugin=", "-Xassembler=", "-Xlinker=", "-Wa,", "-Wl,",
)
_UNSAFE_MODULE_PREFIXES = (
    "-emit-header-unit", "-emit-module", "-fcxx-modules", "-fbuild-session",
    "-fimplicit-module", "-fmodule", "-fmodules", "-fprebuilt-module-path",
    "-gmodules", "-module",
)
_UNSAFE_OUTPUT_PREFIXES = (
    "-fcodegen-data-generate", "-fcrash-diagnostics", "-fproc-stat-report",
    "-fthin-link-bitcode", "-gen-cdb-fragment-path", "-gen-reproducer",
)


@dataclass(frozen=True)
class CompileCommand:
    directory: Path
    arguments: tuple[str, ...]
    file: Path


@dataclass(frozen=True)
class ClangResult:
    findings: tuple[Finding, ...] = ()
    limitations: tuple[Limitation, ...] = ()
    databases: tuple[str, ...] = ()
    truncated: bool = False


def _database_path(path: Path) -> Path:
    return (path / "compile_commands.json" if path.is_dir() else path).resolve()


def _read_database(path: Path, *, deadline: float | None = None) -> list[dict]:
    _check_deadline(deadline)
    with path.open("rb") as handle:
        raw = handle.read(MAX_DATABASE_BYTES + 1)
    _check_deadline(deadline)
    if len(raw) > MAX_DATABASE_BYTES:
        raise ValueError("compilation database exceeds the 16 MiB limit")
    entries = json.loads(raw)
    _check_deadline(deadline)
    if not isinstance(entries, list) or any(not isinstance(e, dict) for e in entries):
        raise ValueError("compilation database must be an array of command objects")
    return entries


def validate_compilation_database(
    path: Path, *, deadline: float | None = None,
) -> Path:
    """Validate an explicit setting; caller treats failures as configuration errors."""
    selected = _database_path(path)
    if not selected.is_file():
        raise ValueError(f"compilation database is not a file: {selected}")
    for entry in _read_database(selected, deadline=deadline):
        _check_deadline(deadline)
        if not all(isinstance(entry.get(key), str) and entry[key] and "\0" not in entry[key]
                   for key in ("directory", "file")):
            raise ValueError("compilation entries require nonempty directory and file strings")
        arguments = entry.get("arguments")
        if arguments is None and isinstance(entry.get("command"), str):
            arguments = shlex.split(entry["command"], posix=True)
        if not isinstance(arguments, list) or not arguments or not all(
            isinstance(argument, str) and "\0" not in argument for argument in arguments
        ):
            raise ValueError("compilation entries require arguments or a POSIX command")
    return selected


def load_compile_command(database: Path, source: Path, *, deadline: float | None = None) -> CompileCommand | None:
    """Load the first usable exact normalized entry, never a basename match."""
    source = source.resolve()
    for entry in _read_database(database, deadline=deadline):
        _check_deadline(deadline)
        directory = entry.get("directory")
        filename = entry.get("file")
        if not isinstance(directory, str) or not isinstance(filename, str):
            continue
        working = (database.parent / directory).resolve()
        if (working / filename).resolve() != source:
            continue
        arguments = entry.get("arguments")
        if arguments is None and isinstance(entry.get("command"), str):
            try:
                arguments = shlex.split(entry["command"], posix=True)
            except ValueError:
                continue
        if (not isinstance(arguments, list) or not arguments
                or not all(isinstance(arg, str) and "\0" not in arg for arg in arguments)):
            continue
        return CompileCommand(working, tuple(arguments), source)
    return None


def find_compilation_database(
    root: Path, source: Path, explicit: Path | None, *, deadline: float | None = None,
) -> Path | None:
    """Select a bounded exact match; an explicit setting never falls back."""
    if explicit is not None:
        candidates = [_database_path(explicit)]
    else:
        candidates = [root / "compile_commands.json", root / "build/compile_commands.json",
                      root / "out/compile_commands.json"]
        for pattern in ("build-*", "cmake-build-*"):
            for path in root.glob(pattern):
                _check_deadline(deadline)
                if path.is_dir():
                    candidates.append(path / "compile_commands.json")
    for candidate in sorted(set(candidates), key=lambda path: (len(str(path)), str(path))):
        _check_deadline(deadline)
        try:
            if load_compile_command(candidate, source, deadline=deadline) is not None:
                return candidate.resolve()
        except TimeoutError:
            raise
        except (OSError, ValueError, RecursionError):
            continue
    return None


def _source_operands(arguments: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    """Return positional inputs without mistaking paired-option values for inputs."""
    operands: list[str] = []
    index = 1
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--":
            operands.extend(arguments[index + 1:])
            break
        if argument in _PAIRED_DRIVER_OPTIONS:
            index += 2
            continue
        if not argument.startswith("-"):
            operands.append(argument)
        index += 1
    return tuple(operands)


def _is_classified_driver_option(argument: str) -> bool:
    return (
        argument in _PAIRED_DRIVER_OPTIONS
        or argument in _SAFE_STANDALONE_DRIVER_OPTIONS
        or argument in _SAFE_DEBUG_DRIVER_OPTIONS
        or argument in _SAFE_OPTIMIZATION_DRIVER_OPTIONS
        or argument in _SAFE_JOINED_LANGUAGE_OPTIONS
        or argument.startswith(_SAFE_JOINED_DRIVER_PREFIXES)
        or argument.startswith("-fno-")
        or (argument.startswith("-f") and "=" in argument)
    )


def sanitize_command(command: CompileCommand, *, clang_driver: str) -> tuple[str, ...]:
    """Keep project flags while removing compilation/dependency output requests."""
    arguments = command.arguments
    if not arguments:
        raise ValueError("empty compiler command")
    result = [clang_driver]
    paired = {"-o", "-MF", "-MT", "-MQ", "-MJ", "--output"}
    switches = {"-c", "-M", "-MM", "-MD", "-MMD", "-MP", "-MG"}
    index = 1
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--":
            result.extend(arguments[index:])
            break
        if argument.startswith("@"):
            raise ValueError("response-file commands cannot be safely sanitized")
        if argument.startswith("--config"):
            raise ValueError("opaque Clang configuration files and directory controls are not supported")
        if (
            argument in _UNSAFE_DRIVER_OPTIONS
            or argument.startswith(_UNSAFE_DRIVER_PREFIXES)
            or argument.startswith(_UNSAFE_MODULE_PREFIXES)
            or argument.startswith("-X")
        ):
            raise ValueError(
                f"native plugin, module-building, or opaque backend option is not supported: {argument}"
            )
        if (argument in {"-E", "-S", "--precompile", "-emit-ast", "-emit-llvm", "-Xclang", "-Xpreprocessor"}
                or argument.startswith(("-save-temps", "--save-temps", "-serialize-diagnostics",
                                        "--serialize-diagnostics", "-dependency-file", "-ftime-trace",
                                        "-fmodule-output", "-foptimization-record-file", "-fsave-optimization-record",
                                        "-Xclang=", "-Xpreprocessor=", "-Wp,", *_UNSAFE_OUTPUT_PREFIXES))):
            raise ValueError(f"output-producing or opaque forwarded option is not supported: {argument}")
        if argument in paired:
            if index + 1 == len(arguments):
                raise ValueError(f"missing argument for {argument}")
            index += 2
            continue
        if argument in _PAIRED_DRIVER_OPTIONS and index + 1 == len(arguments):
            raise ValueError(f"missing argument for {argument}")
        if (argument in switches or argument.startswith(("-MF", "-MT", "-MQ", "-MJ", "--output="))
                or (argument.startswith("-o") and len(argument) > 2
                    and not argument.startswith("-objc"))):
            index += 1
            continue
        if argument.startswith("-") and not _is_classified_driver_option(argument):
            raise ValueError(f"unclassified compiler option is not supported: {argument}")
        result.append(argument)
        index += 1
    operands = _source_operands(arguments)
    if len(operands) != 1:
        raise ValueError("command must have exactly one positional compilation input")
    if (command.directory / operands[0]).resolve() != command.file:
        raise ValueError("command input does not match its recorded source file")
    position = result.index("--") if "--" in result else len(result)
    result[position:position] = [
        "--no-default-config", "-fsyntax-only", "-fno-crash-diagnostics",
        "-fdiagnostics-color=never", "-Xclang", "-ast-dump=json",
    ]
    return tuple(result)


def _driver_for(command: CompileCommand) -> str | None:
    """Keep the recorded language selection; suffixes never override a C++ driver."""
    language = None
    index = 1
    while index < len(command.arguments):
        argument = command.arguments[index]
        if argument == "--":
            break
        if argument == "-x":
            index += 1
            if index == len(command.arguments):
                return None
            language = command.arguments[index]
        elif argument.startswith("-x"):
            language = argument[2:]
        elif argument in _PAIRED_DRIVER_OPTIONS:
            index += 1
        index += 1
    if language not in {None, "none"}:
        if language in {"c++", "c++-header", "c++-cpp-output"}:
            return "clang++"
        if language in {"c", "c-header", "cpp-output"}:
            return "clang"
        return None
    executable = Path(command.arguments[0]).name
    if re.search(r"(?:^|-)(?:g\+\+|clang\+\+|c\+\+)(?:-\d[\d.]*)?$", executable):
        return "clang++"
    if re.search(r"(?:^|-)(?:gcc|clang|cc)(?:-\d[\d.]*)?$", executable):
        return "clang"
    return None


def _execute(arguments: tuple[str, ...], directory: Path, timeout: float,
             output_limit: int) -> tuple[int, bytes, bytes, str | None]:
    """Drain both pipes under one cap and terminate portably within the deadline."""
    process = subprocess.Popen(arguments, cwd=directory, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, start_new_session=os.name == "posix")
    if process.stdout is None or process.stderr is None:
        raise OSError("Clang capture pipes are unavailable")
    captured = {"stdout": bytearray(), "stderr": bytearray()}
    end = time.monotonic() + timeout
    lock = threading.Lock()
    oversized = threading.Event()
    reader_failures: list[OSError] = []

    def read_pipe(stream, name: str) -> None:
        try:
            while not oversized.is_set():
                data = stream.read(64 * 1024)
                if not data:
                    return
                with lock:
                    total = len(captured["stdout"]) + len(captured["stderr"])
                    remaining = max(0, output_limit - total)
                    captured[name].extend(data[:remaining])
                    if len(data) > remaining:
                        oversized.set()
                        return
        except OSError as error:
            reader_failures.append(error)

    readers = tuple(
        threading.Thread(target=read_pipe, args=(stream, name), daemon=True)
        for stream, name in ((process.stdout, "stdout"), (process.stderr, "stderr"))
    )
    for reader in readers:
        reader.start()

    failure: str | None = None
    while process.poll() is None and not oversized.is_set():
        remaining = end - time.monotonic()
        if remaining <= 0:
            failure = "TOOL_CLANG_TIMEOUT"
            break
        oversized.wait(min(0.01, remaining))
    if oversized.is_set():
        failure = "TOOL_CLANG_OUTPUT"

    def terminate_and_reap() -> None:
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
        elif process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass
        try:
            process.wait(timeout=PROCESS_REAP_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired):
            pass

    if failure:
        terminate_and_reap()
    else:
        try:
            process.wait(timeout=max(0.001, end - time.monotonic()))
        except subprocess.TimeoutExpired:
            failure = "TOOL_CLANG_TIMEOUT"
            terminate_and_reap()

    join_deadline = time.monotonic() + PROCESS_REAP_TIMEOUT
    for reader in readers:
        reader.join(max(0.0, join_deadline - time.monotonic()))
    if any(reader.is_alive() for reader in readers):
        failure = failure or "TOOL_CLANG_TIMEOUT"
        terminate_and_reap()
    else:
        process.stdout.close()
        process.stderr.close()
    if reader_failures and failure is None:
        failure = "TOOL_CLANG_FAILURE"
    return process.returncode or 0, bytes(captured["stdout"]), bytes(captured["stderr"]), failure


_DIAGNOSTIC = re.compile(r"^(.+):(\d+):(\d+): (?:fatal error|error|warning): (.+)$")


def _finding(code: str, severity: str, path: str, line: int, message: str,
             remediation: str) -> Finding:
    return Finding(code, severity, path, line, message, remediation, "clang", code.startswith("POT"))


def _severity(finding: Finding) -> int:
    return {"info": 0, "warning": 1, "error": 2}[finding.severity]


def _diagnostics(stderr: bytes, command: CompileCommand, report_path: str) -> tuple[Finding, ...]:
    result = []
    for line in stderr.decode("utf-8", errors="replace").splitlines():
        match = _DIAGNOSTIC.match(line)
        if not match or (command.directory / match[1]).resolve() != command.file:
            continue
        number = int(match[2])
        if number <= 0:
            continue
        message = match[4]
        contract = "unused-result" in message or "nodiscard" in message or "warn_unused_result" in message
        result.append(_finding("POT07" if contract else "POT10", "error", report_path,
                               number, message, "Handle the result explicitly." if contract
                               else "Resolve the compiler diagnostic using the project build."))
    return tuple(result)


_FUNCTIONS = frozenset({"FunctionDecl", "CXXMethodDecl", "CXXConstructorDecl", "CXXDestructorDecl",
                        "CXXConversionDecl"})
_CALLS = frozenset({"CallExpr", "CXXMemberCallExpr", "CXXOperatorCallExpr"})
_TRANSPARENT = frozenset({"ImplicitCastExpr", "ExprWithCleanups", "MaterializeTemporaryExpr",
                          "CXXBindTemporaryExpr", "ParenExpr"})
_POINTER_DECLS = frozenset({"VarDecl", "ParmVarDecl", "FieldDecl", "TypedefDecl", "TypeAliasDecl"})
_FUNCTION_POINTER = re.compile(r"\(\s*\*[^)]*\)\s*\(")
_UNEVALUATED = frozenset({"UnaryExprOrTypeTraitExpr", "CXXNoexceptExpr", "TypeTraitExpr",
                         "DecltypeType", "TypeOfExprType", "RequiresExpr"})


def _children(node: dict) -> list[dict]:
    inner = node.get("inner", [])
    return [child for child in inner if isinstance(child, dict)] if isinstance(inner, list) else []


def _type(node: dict) -> str:
    value = node.get("type", {})
    if not isinstance(value, dict):
        return ""
    result = value.get("desugaredQualType", value.get("qualType", ""))
    return result if isinstance(result, str) else ""


def _location(value: object) -> dict:
    if not isinstance(value, dict):
        return {}
    # Expansion sites are attributable to the invocation; spelling sites alone are not.
    if "expansionLoc" in value:
        return _location(value["expansionLoc"])
    if "spellingLoc" in value:
        return {}
    return value


def _check_deadline(deadline: float | None) -> None:
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError("semantic total time limit reached")


@dataclass
class _AstNode:
    raw: dict
    parent: _AstNode | None
    file: Path
    line: int | None
    end_line: int | None
    function: str | None
    record: dict | None


def _walk_ast(ast: dict, source: Path, text: str, deadline: float | None,
              directory: Path) -> list[_AstNode]:
    """Read Clang's elided filenames and byte offsets without recursive traversal."""
    encoded = text.encode("utf-8")
    newlines = [i for i, byte in enumerate(encoded) if byte == 10]
    current_file = source
    records = []
    stack = [(ast, None, None, None)]

    def line_of(point: dict, file: Path) -> int | None:
        if file != source or "includedFrom" in point:
            return None
        number = point.get("line")
        if isinstance(number, int) and not isinstance(number, bool) and number > 0:
            return number
        offset = point.get("offset")
        if isinstance(offset, int) and not isinstance(offset, bool) and 0 <= offset < len(encoded):
            return bisect_right(newlines, offset - 1) + 1
        return None

    while stack:
        _check_deadline(deadline)
        raw, parent, owner, record = stack.pop()
        source_range = raw.get("range", {})
        if not isinstance(source_range, dict):
            source_range = {}
        point = _location(raw.get("loc")) or _location(source_range.get("begin"))
        if isinstance(point.get("file"), str):
            current_file = (directory / point["file"]).resolve()
        file = current_file
        if raw.get("kind") in _FUNCTIONS or raw.get("kind") == "CXXRecordDecl":
            begin = _location(source_range.get("begin"))
            begin_file = (directory / begin["file"]).resolve() if isinstance(begin.get("file"), str) else file
            if line_of(begin, begin_file) is not None:
                point = begin
                file = begin_file
        end = _location(source_range.get("end"))
        end_file = (directory / end["file"]).resolve() if isinstance(end.get("file"), str) else file
        item = _AstNode(raw, parent, file, line_of(point, file), line_of(end, end_file), owner, record)
        records.append(item)
        kind = raw.get("kind")
        if kind in _FUNCTIONS:
            owner = raw.get("id")
        elif kind == "LambdaExpr" or kind in _UNEVALUATED:
            # Clang repeats a lambda's body after its closure declaration. It is
            # not executed as part of the enclosing function just by declaration.
            owner = None
        if kind == "CXXRecordDecl" and raw.get("completeDefinition"):
            record = raw
        stack.extend((child, item, owner, record) for child in reversed(_children(raw)))
    return records


def _callee(call: dict) -> str | None:
    children = _children(call)
    if not children:
        return None
    node = children[0]
    while node.get("kind") in _TRANSPARENT:
        children = _children(node)
        if len(children) != 1:
            return None
        node = children[0]
    if node.get("kind") == "DeclRefExpr":
        reference = node.get("referencedDecl", {})
        identifier = reference.get("id") if isinstance(reference, dict) else None
    elif node.get("kind") == "MemberExpr":
        identifier = node.get("referencedMemberDecl")
    else:
        return None  # A conditional, selection, or other expression is not one direct callee.
    return identifier if isinstance(identifier, str) else None


def _discarded(item: _AstNode) -> bool:
    parent = item.parent
    while parent is not None and parent.raw.get("kind") in _TRANSPARENT:
        parent = parent.parent
    return parent is not None and parent.raw.get("kind") == "CompoundStmt"


def _raw_dereferences(raw: dict) -> int:
    depth = 0
    while True:
        kind = raw.get("kind")
        if kind == "UnaryOperator" and raw.get("opcode") == "*":
            depth += 1
        elif kind == "MemberExpr" and raw.get("isArrow"):
            depth += 1
        elif kind == "MemberExpr":
            pass  # A dot does not dereference, but its base can contain raw dereferences.
        elif kind not in _TRANSPARENT:
            return depth
        children = _children(raw)
        if len(children) != 1:
            return depth
        raw = children[0]


def _single_throw_override(raw: dict) -> bool:
    children = _children(raw)
    if not any(child.get("kind") == "OverrideAttr" for child in children):
        return False
    bodies = [child for child in children if child.get("kind") == "CompoundStmt"]
    if len(bodies) != 1:
        return False
    statements = _children(bodies[0])
    return len(statements) == 1 and statements[0].get("kind") == "CXXThrowExpr"


def _unsafe_destructor(raw: dict) -> bool:
    children = _children(raw)
    if not any(child.get("virtual") or child.get("pure") for child in children):
        return False
    # A base can supply a virtual destructor whose flag is elided in this AST.
    if raw.get("bases"):
        return False
    access = "public" if raw.get("tagUsed") == "struct" else "private"
    for child in children:
        if child.get("kind") == "AccessSpecDecl":
            access = child.get("access", access)
        if child.get("kind") == "CXXDestructorDecl":
            if child.get("virtual") or child.get("explicitlyDeleted"):
                return False
            return child.get("isImplicit", False) or child.get("access", access) == "public"
    return True  # An implicitly declared destructor is public and nonvirtual.


def _cycles(edges: dict[str, dict[str, list[_AstNode]]], changed: Container[int] | None,
            deadline: float | None, suppressed_lines: frozenset[int]):
    """Find SCCs iteratively, then witness every changed recursive call edge."""
    visited: set[str] = set()
    finished: list[str] = []
    reverse: dict[str, set[str]] = {owner: set() for owner in edges}
    for owner, targets in edges.items():
        for target in targets:
            reverse[target].add(owner)
    for start in sorted(edges):
        if start in visited:
            continue
        visited.add(start)
        stack = [(start, iter(sorted(edges[start])))]
        while stack:
            _check_deadline(deadline)
            owner, targets = stack[-1]
            target = next(targets, None)
            if target is None:
                stack.pop()
                finished.append(owner)
            elif target not in visited:
                visited.add(target)
                stack.append((target, iter(sorted(edges[target]))))
    assigned: set[str] = set()
    for start in reversed(finished):
        if start in assigned:
            continue
        component: set[str] = set()
        pending = [start]
        assigned.add(start)
        while pending:
            _check_deadline(deadline)
            owner = pending.pop()
            component.add(owner)
            for predecessor in reverse[owner] - assigned:
                assigned.add(predecessor)
                pending.append(predecessor)
        if len(component) == 1 and start not in edges[start]:
            continue
        sites = [(site, owner, target) for owner in sorted(component)
                 for target in sorted(edges[owner]) if target in component
                 for site in edges[owner][target]
                 if site.line is not None and site.line not in suppressed_lines
                 and (changed is None or site.line in changed)]
        sites.sort(key=lambda entry: (entry[0].line, entry[1], entry[2]))
        # An unrestricted audit needs one representative per recursive component;
        # a diff audit must not lose a changed edge merely because DFS finished its target.
        if changed is None:
            sites = sites[:1]
        for site, owner, target in sites:
            predecessors: dict[str, str | None] = {target: None}
            pending = [target]
            for current in pending:
                _check_deadline(deadline)
                if current == owner:
                    break
                for following in sorted(edges[current]):
                    if following in component and following not in predecessors:
                        predecessors[following] = current
                        pending.append(following)
            path = [owner]
            while predecessors[path[-1]] is not None:
                path.append(predecessors[path[-1]])
            yield site, [owner] + list(reversed(path))[:-1]


def analyze_ast(ast: dict, path: Path, text: str, *,
                changed_lines: Container[int] | None = None,
                deadline: float | None = None,
                directory: Path | None = None) -> tuple[Finding, ...]:
    """Extract conservative semantic evidence from one parsed translation unit."""
    path_text = path.as_posix()
    records = _walk_ast(ast, path.resolve(), text, deadline, directory or Path.cwd())
    definitions = {item.raw["id"]: item for item in records
                   if item.raw.get("kind") in _FUNCTIONS and isinstance(item.raw.get("id"), str)
                   and any(child.get("kind") == "CompoundStmt" for child in _children(item.raw))}
    aliases = {item.raw["id"]: item.raw["previousDecl"] for item in records
               if isinstance(item.raw.get("id"), str) and isinstance(item.raw.get("previousDecl"), str)}
    canonical: dict[str, str] = {}
    for identifier in definitions:
        current = identifier
        visited = set()
        while current not in visited:
            visited.add(current)
            canonical[current] = identifier
            if current not in aliases:
                break
            current = aliases[current]
    contracts = {item.raw.get("id") for item in records
                 if any(child.get("kind") == "WarnUnusedResultAttr" for child in _children(item.raw))}
    contracts.update(canonical.get(identifier, identifier) for identifier in tuple(contracts))
    virtual_targets = {canonical.get(item.raw.get("id"), item.raw.get("id")) for item in records
                       if item.raw.get("virtual") or any(child.get("kind") == "OverrideAttr"
                                                       for child in _children(item.raw))}
    direct_members = {canonical.get(item.raw.get("id"), item.raw.get("id")) for item in records
                      if item.raw.get("kind") == "CXXMethodDecl"
                      and (item.raw.get("storageClass") == "static"
                           or (item.record is not None and item.record.get("completeDefinition")
                               and not item.record.get("bases")))} - virtual_targets
    edges: dict[str, dict[str, list[_AstNode]]] = {identifier: {} for identifier in definitions}
    findings = []

    def emit(item: _AstNode, code: str, severity: str, message: str, remediation: str) -> None:
        if item.line is not None and (changed_lines is None or item.line in changed_lines):
            findings.append(_finding(code, severity, path_text, item.line, message, remediation))

    for item in records:
        _check_deadline(deadline)
        raw = item.raw
        kind = raw.get("kind")
        if kind in _CALLS:
            target = _callee(raw)
            target = canonical.get(target, target)
            member_dispatch = kind in {"CXXMemberCallExpr", "CXXOperatorCallExpr"} and target in definitions and definitions[target].raw.get("kind") == "CXXMethodDecl"
            if (item.function in definitions and target in definitions and target not in virtual_targets
                    and (not member_dispatch or target in direct_members)):
                edges[item.function].setdefault(target, []).append(item)
            if _discarded(item) and _type(raw) not in {"", "void"}:
                emit(item, "POT07", "error" if target in contracts else "warning",
                     "discarded result of a nodiscard call" if target in contracts else "discarded non-void call result",
                     "Check the result or explicitly document why discarding it is safe.")
        if kind in _FUNCTIONS and raw.get("id") in definitions:
            if item.line is not None and item.end_line is not None and item.end_line - item.line + 1 > MAX_FUNCTION_LINES:
                emit(item, "POT04", "error", "function exceeds 60 physical source lines",
                     "Split the function into smaller cohesive operations.")
        if kind == "VarDecl" and item.function is None and item.record is None:
            value_type = _type(raw)
            # Pointee constness does not make the pointer itself immutable.
            top_level = value_type.rsplit("*", 1)[-1] if "*" in value_type else value_type
            if value_type and "const" not in top_level.split() and raw.get("storageClass") != "extern":
                emit(item, "POT06", "warning", "mutable file-scope state",
                     "Restrict the state to its smallest useful scope.")
        if kind in _POINTER_DECLS and _FUNCTION_POINTER.search(_type(raw)):
            emit(item, "POT09", "error", "function-pointer type",
                 "Prefer explicit direct calls or document the required callback boundary.")
        if kind in {"UnaryOperator", "MemberExpr"} and _raw_dereferences(raw) > 2:
            emit(item, "POT09", "error", "expression requires more than two raw pointer dereferences",
                 "Reduce pointer indirection and expose a simpler data structure.")
        if kind == "CXXRecordDecl" and raw.get("completeDefinition"):
            if item.line is not None and item.end_line is not None and item.end_line - item.line + 1 > MAX_CLASS_LINES:
                emit(item, "SOLID01", "warning", "class exceeds 300 physical lines; review its responsibilities",
                     "Review whether distinct responsibilities can be separated.")
            if sum(child.get("kind") == "CXXMethodDecl" and bool(child.get("pure")) for child in _children(raw)) > MAX_INTERFACE_METHODS:
                emit(item, "SOLID04", "warning", "abstract interface has more than 10 pure virtual methods",
                     "Review whether clients need smaller focused interfaces.")
            if _unsafe_destructor(raw):
                emit(item, "SOLID03", "warning", "polymorphic class has a public nonvirtual destructor",
                     "Review deletion through base pointers and the destructor contract.")
        if kind == "CXXMethodDecl" and _single_throw_override(raw):
            emit(item, "SOLID03", "warning", "override consists only of a throw; review substitutability",
                 "Check whether the override honors the base operation contract.")
        if kind == "CXXConstructExpr" and item.record is not None:
            owner_name = item.record.get("name", "")
            concrete = _type(raw).split("::")[-1]
            if owner_name.endswith(HIGH_LEVEL_SUFFIXES) and concrete.endswith(INFRASTRUCTURE_SUFFIXES):
                emit(item, "SOLID05", "warning", "high-level type directly constructs concrete infrastructure",
                     "Review injecting an abstraction at the infrastructure boundary.")
    suppressions, _ = parse_suppressions(text, path_text)
    suppressed_recursion_lines = frozenset(line for line, codes in suppressions.items() if "POT01" in codes)
    for site, cycle in _cycles(edges, changed_lines, deadline, suppressed_recursion_lines):
        names = [str(definitions[identifier].raw.get("name", identifier)) for identifier in cycle]
        emit(site, "POT01", "error", "confirmed recursion cycle: " + " -> ".join(names + names[:1]),
             "Replace recursion with bounded iteration or an explicit bounded work stack.")
    unique = {}
    for item in findings:
        key = finding_sort_key(item)
        if key not in unique or _severity(item) > _severity(unique[key]):
            unique[key] = item
    return apply_suppressions(tuple(sorted(unique.values(), key=finding_sort_key)), suppressions)


def run_clang_analysis(
    sources: Mapping[Path, str], *, root: Path,
    changed_lines: Mapping[Path, Container[int]] | None = None,
    compile_commands: Path | None = None, budget_seconds: float = 60.0,
    unit_timeout: float = UNIT_TIMEOUT, output_limit: int = MAX_OUTPUT_BYTES,
    deadline: float | None = None,
) -> ClangResult:
    """Return semantic findings and fail-open limitations for bounded source inputs."""
    if sources and os.name != "posix":
        return ClangResult(
            limitations=(
                Limitation(
                    "TOOL_CLANG_PLATFORM",
                    "optional Clang analysis is disabled because bounded process-tree cleanup is unavailable on this platform",
                ),
            )
        )
    if deadline is None:
        deadline = time.monotonic() + budget_seconds
    findings: list[Finding] = []
    limitations: list[Limitation] = []
    databases: set[str] = set()
    units = 0
    truncated = False
    for source, text in sorted(sources.items(), key=lambda pair: str(pair[0])):
        source = source.resolve()
        try:
            report_path = source.relative_to(root.resolve())
        except ValueError:
            report_path = source
        path_text = report_path.as_posix()
        if changed_lines is not None and report_path not in changed_lines:
            continue
        if time.monotonic() >= deadline or units >= MAX_UNITS:
            limitations.append(Limitation("TOOL_CLANG_LIMIT", "semantic unit or total time limit reached"))
            truncated = True
            break
        try:
            database = find_compilation_database(root, source, compile_commands, deadline=deadline)
            if database is None:
                limitations.append(Limitation("TOOL_CLANG_DATABASE", "no usable exact compilation database entry", path_text))
                continue
            databases.add(str(database))
            command = load_compile_command(database, source, deadline=deadline)
            if command is None:
                raise ValueError("compilation command disappeared")
            driver_name = _driver_for(command)
            if driver_name is None:
                limitations.append(Limitation("TOOL_CLANG_LANGUAGE", "recorded compiler language cannot be determined reliably", path_text))
                continue
            driver = shutil.which(driver_name)
            if driver is None:
                limitations.append(Limitation("TOOL_CLANG_MISSING", "Clang driver is unavailable", path_text))
                continue
            args = sanitize_command(command, clang_driver=driver)
            units += 1
            remaining = min(unit_timeout, deadline - time.monotonic())
            if remaining <= 0:
                limitations.append(Limitation("TOOL_CLANG_LIMIT", "semantic total time limit reached", path_text))
                truncated = True
                break
            status, stdout, stderr, failure = _execute(args, command.directory, remaining, output_limit)
            if failure:
                message = (
                    "Clang exceeded its time or captured output limit"
                    if failure in {"TOOL_CLANG_TIMEOUT", "TOOL_CLANG_OUTPUT"}
                    else "Clang output capture failed"
                )
                limitations.append(Limitation(failure, message, path_text))
                truncated = True
                continue
            unit_findings = list(_diagnostics(stderr, command, path_text))
            if status != 0:
                limitations.append(Limitation("TOOL_CLANG_PARSE", "Clang could not parse the translation unit", path_text))
            else:
                try:
                    ast = json.loads(stdout)
                    if not isinstance(ast, dict) or ast.get("kind") != "TranslationUnitDecl":
                        raise ValueError("expected a translation unit AST")
                    lines = changed_lines.get(report_path, frozenset()) if changed_lines is not None else None
                    unit_findings.extend(analyze_ast(ast, source, text, changed_lines=lines,
                                                     deadline=deadline, directory=command.directory))
                except (ValueError, RecursionError):
                    limitations.append(Limitation("TOOL_CLANG_JSON", "Clang returned an unusable JSON AST", path_text))
            lines = changed_lines.get(report_path, frozenset()) if changed_lines is not None else None
            suppressions, _ = parse_suppressions(text, path_text)
            normalized = tuple(Finding(f.code, f.severity, path_text, f.line, f.message,
                                       f.remediation, f.engine, f.gate_eligible)
                               for f in unit_findings if lines is None or f.line in lines)
            findings.extend(apply_suppressions(normalized, suppressions))
        except TimeoutError as error:
            limitations.append(Limitation("TOOL_CLANG_TIMEOUT", str(error), path_text))
            truncated = True
            break
        except Exception as error:
            # This is the optional engine boundary: malformed compiler output and
            # unexpected adapter failures must preserve the completed source audit.
            limitations.append(Limitation("TOOL_CLANG_FAILURE", f"Clang analysis unavailable: {error}", path_text))
    unique: dict[tuple[str, int, str], Finding] = {}
    for finding in findings:
        key = finding_sort_key(finding)
        if key not in unique or _severity(finding) > _severity(unique[key]):
            unique[key] = finding
    ordered = sorted(unique.values(), key=finding_sort_key)
    if len(ordered) > MAX_FINDINGS:
        truncated = True
        limitations.append(Limitation("TOOL_CLANG_LIMIT", "semantic findings exceed the 500 finding limit"))
    return ClangResult(tuple(ordered[:MAX_FINDINGS]), tuple(limitations), tuple(sorted(databases)), truncated)
