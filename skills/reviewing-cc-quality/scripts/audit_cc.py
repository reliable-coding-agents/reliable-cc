#!/usr/bin/env python3
"""Dependency-free Reliable C/C++ audit command."""

from __future__ import annotations

import argparse
import ast
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import sys
import time
from collections.abc import Container, Iterable, Iterator, Mapping, Set as AbstractSet

from clang_analysis import run_clang_analysis, validate_compilation_database
from quality_model import AuditReport, Finding, Limitation, finding_sort_key
from source_checks import analyze_source_bounded


SUPPORTED_SUFFIXES = frozenset(
    {".c", ".h", ".cc", ".cpp", ".cxx", ".hh", ".hpp", ".hxx"}
)

MAX_FILES = 500
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_SOURCE_BYTES = 20 * 1024 * 1024
MAX_GIT_OUTPUT_BYTES = MAX_TOTAL_SOURCE_BYTES
MAX_FINDINGS = 500
MAX_EAGER_CHANGED_LINES = 100_000
PROCESS_REAP_TIMEOUT = 0.25

_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


class AuditInputError(RuntimeError):
    """A required audit input could not be discovered or read."""


class GitWorkingTreeError(AuditInputError):
    """The current path is not usable as a Git working tree."""


class AuditLimitReached(RuntimeError):
    """A bounded discovery operation exhausted the shared audit budget."""

    def __init__(self, code: str, message: str, path: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.path = path


@dataclass(frozen=True, eq=False)
class LineIntervals(AbstractSet[int]):
    """Immutable, compact inclusive-exclusive changed-line intervals."""

    intervals: tuple[tuple[int, int], ...] = ()

    def __post_init__(self) -> None:
        merged: list[tuple[int, int]] = []
        for start, stop in sorted(self.intervals):
            if stop <= start:
                continue
            if merged and start <= merged[-1][1]:
                previous_start, previous_stop = merged[-1]
                merged[-1] = (previous_start, max(previous_stop, stop))
            else:
                merged.append((start, stop))
        object.__setattr__(self, "intervals", tuple(merged))

    @classmethod
    def normalized(cls, intervals: Iterable[tuple[int, int]]) -> LineIntervals:
        return cls(tuple(intervals))

    @classmethod
    def _from_iterable(cls, values: Iterable[int]) -> AbstractSet[int]:
        return frozenset(values)

    def __contains__(self, value: object) -> bool:
        if not isinstance(value, int) or isinstance(value, bool):
            return False
        index = bisect_right(self.intervals, (value, float("inf"))) - 1
        return index >= 0 and value < self.intervals[index][1]

    def __iter__(self) -> Iterator[int]:
        for start, stop in self.intervals:
            yield from range(start, stop)

    def __len__(self) -> int:
        total = 0
        for start, stop in self.intervals:
            total += stop - start
            if total >= sys.maxsize:
                return sys.maxsize
        return total

    def cardinality_at_most(self, limit: int) -> bool:
        remaining = limit
        for start, stop in self.intervals:
            size = stop - start
            if size > remaining:
                return False
            remaining -= size
        return True

    def _cardinality_equals(self, expected: int) -> bool:
        remaining = expected
        for start, stop in self.intervals:
            size = stop - start
            if size > remaining:
                return False
            remaining -= size
        return remaining == 0

    def __eq__(self, other: object) -> bool:
        if isinstance(other, LineIntervals):
            return self.intervals == other.intervals
        if isinstance(other, AbstractSet):
            try:
                size = len(other)
            except OverflowError:
                return False
            return self._cardinality_equals(size) and all(
                value in self for value in other
            )
        return NotImplemented

    __hash__ = None


LineSelection = AbstractSet[int]


@dataclass(frozen=True)
class AllSourceLines:
    """Compact membership for every possible positive line in a bounded file."""

    maximum: int

    def __contains__(self, value: object) -> bool:
        return (
            isinstance(value, int)
            and not isinstance(value, bool)
            and 1 <= value <= self.maximum
        )


ChangedLineSelection = Container[int]


def _supported(path: Path) -> bool:
    return path.suffix in SUPPORTED_SUFFIXES


def _check_deadline(deadline: float | None) -> None:
    if deadline is not None and time.monotonic() >= deadline:
        raise AuditLimitReached("TOOL_TIMEOUT", "audit total time limit reached")


def _discover_paths(
    inputs: Iterable[str | Path], *, deadline: float | None,
) -> tuple[tuple[Path, ...], bool]:
    """Return at most the lexically first file-limit plus one candidates."""

    input_paths = tuple(Path(raw_path) for raw_path in inputs)
    # Required path errors remain configuration failures even for a tiny budget.
    for path in input_paths:
        if not path.exists():
            raise AuditInputError(f"input path does not exist: {path}")

    retained: dict[str, Path] = {}
    ordered_keys: list[str] = []
    timed_out = False

    def retain(candidate: Path) -> None:
        if not _supported(candidate):
            return
        try:
            identity = candidate.resolve()
        except OSError as error:
            raise AuditInputError(
                f"cannot resolve input path {candidate}: {error}"
            ) from error
        key = identity.as_posix()
        if key in retained:
            return
        position = bisect_left(ordered_keys, key)
        if len(ordered_keys) < MAX_FILES + 1:
            ordered_keys.insert(position, key)
            retained[key] = candidate
        elif position < len(ordered_keys):
            removed = ordered_keys.pop()
            retained.pop(removed)
            ordered_keys.insert(position, key)
            retained[key] = candidate

    for path in input_paths:
        if path.is_file():
            retain(path)
        elif path.is_dir():
            try:
                for candidate in path.rglob("*"):
                    if deadline is not None and time.monotonic() >= deadline:
                        timed_out = True
                        break
                    if candidate.is_file():
                        retain(candidate)
            except OSError as error:
                raise AuditInputError(f"cannot inspect input directory {path}: {error}") from error
            if timed_out:
                break
    return tuple(retained[key] for key in ordered_keys), timed_out


def discover_paths(inputs: Iterable[str | Path]) -> tuple[Path, ...]:
    """Return bounded, unique supported files beneath inputs in stable order."""

    paths, timed_out = _discover_paths(
        inputs, deadline=time.monotonic() + 60.0
    )
    if timed_out:
        raise AuditLimitReached("TOOL_TIMEOUT", "audit total time limit reached")
    return paths


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        elif process.poll() is None:
            process.kill()
    except ProcessLookupError:
        pass


def _run_git(
    root: Path,
    arguments: list[str],
    *,
    deadline: float | None = None,
    output_limit: int | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """Run Git with a shared deadline and bounded combined output capture."""

    if deadline is None:
        deadline = time.monotonic() + 60.0
    if output_limit is None:
        output_limit = MAX_GIT_OUTPUT_BYTES
    _check_deadline(deadline)
    command = ["git", *arguments]
    try:
        process = subprocess.Popen(
            command,
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=os.name == "posix",
        )
    except FileNotFoundError as error:
        raise GitWorkingTreeError(f"Git invocation failed: {error}") from error
    except (OSError, ValueError) as error:
        raise AuditInputError(f"Git invocation failed: {error}") from error

    captured = {"stdout": bytearray(), "stderr": bytearray()}
    failure: AuditLimitReached | AuditInputError | None = None
    try:
        with selectors.DefaultSelector() as selector:
            for stream, name in ((process.stdout, "stdout"), (process.stderr, "stderr")):
                assert stream is not None
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, name)
            total = 0
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    failure = AuditLimitReached(
                        "TOOL_TIMEOUT", "audit total time limit reached"
                    )
                    break
                for key, _ in selector.select(remaining):
                    data = os.read(
                        key.fileobj.fileno(), min(65536, output_limit - total + 1)
                    )
                    if not data:
                        selector.unregister(key.fileobj)
                        continue
                    total += len(data)
                    if total > output_limit:
                        failure = AuditLimitReached(
                            "TOOL_GIT_OUTPUT",
                            f"Git output exceeds the {output_limit}-byte capture limit",
                        )
                        break
                    captured[key.data].extend(data)
                if failure is not None:
                    break
    except OSError as error:
        failure = AuditInputError(
            f"Git output capture failed: {error}"
        )
    finally:
        if failure is not None:
            _stop_process(process)
        try:
            process.wait(
                timeout=(
                    PROCESS_REAP_TIMEOUT
                    if failure is not None
                    else max(0.001, deadline - time.monotonic())
                )
            )
        except subprocess.TimeoutExpired:
            if failure is None:
                failure = AuditLimitReached(
                    "TOOL_TIMEOUT", "audit total time limit reached"
                )
            _stop_process(process)
            try:
                process.wait(timeout=PROCESS_REAP_TIMEOUT)
            except subprocess.TimeoutExpired:
                pass
        assert process.stdout is not None and process.stderr is not None
        process.stdout.close()
        process.stderr.close()
    if failure is not None:
        raise failure
    return subprocess.CompletedProcess(
        command,
        process.returncode,
        bytes(captured["stdout"]),
        bytes(captured["stderr"]),
    )


def _git_root(path: Path, *, deadline: float | None = None) -> Path:
    result = _run_git(path, ["rev-parse", "--show-toplevel"], deadline=deadline)
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        suffix = f": {detail}" if detail else ""
        raise GitWorkingTreeError(f"Git working tree is required{suffix}")
    return Path(result.stdout.decode("utf-8", errors="surrogateescape").strip())


def _decode_diff_path(value: str) -> Path | None:
    value = value.strip()
    if value == "/dev/null":
        return None
    if value.startswith('"'):
        try:
            value = ast.literal_eval(value)
        except (SyntaxError, ValueError):
            return None
        try:
            value = value.encode("latin-1").decode("utf-8", errors="surrogateescape")
        except UnicodeEncodeError:
            pass
    if value.startswith("b/"):
        value = value[2:]
    return Path(value)


def _read_bounded(path: Path, *, max_bytes: int | None = None) -> bytes:
    """Read no more than the per-file cap plus one sentinel byte."""

    limit = MAX_FILE_BYTES if max_bytes is None else max_bytes
    try:
        with path.open("rb") as handle:
            return handle.read(limit + 1)
    except OSError as error:
        raise AuditInputError(f"cannot read source file {path}: {error}") from error


def _line_numbers(path: Path, *, deadline: float | None = None) -> LineIntervals:
    _check_deadline(deadline)
    contents = _read_bounded(path)
    _check_deadline(deadline)
    count = (
        contents.count(b"\n")
        + contents.count(b"\r")
        - contents.count(b"\r\n")
    )
    if contents and contents[-1:] not in {b"\r", b"\n"}:
        count += 1
    return LineIntervals(((1, count + 1),)) if count else LineIntervals()


def _listed_paths(
    git_root: Path, arguments: list[str], *, deadline: float | None = None,
) -> tuple[Path, ...]:
    result = _run_git(
        git_root, ["ls-files", *arguments, "-z"], deadline=deadline
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise AuditInputError(f"Git file discovery failed: {detail or 'unknown error'}")
    paths: list[Path] = []
    start = 0
    while len(paths) < MAX_FILES + 1:
        _check_deadline(deadline)
        end = result.stdout.find(b"\0", start)
        if end < 0:
            break
        if end > start:
            path = Path(
                result.stdout[start:end].decode("utf-8", errors="surrogateescape")
            )
            if _supported(path):
                paths.append(path)
        start = end + 1
    return tuple(paths)


def _git_changed_lines(
    root: Path, *, deadline: float | None = None,
) -> tuple[dict[Path, ChangedLineSelection], bool]:
    if deadline is None:
        deadline = time.monotonic() + 60.0
    git_root = _git_root(root, deadline=deadline)
    diff = _run_git(
        git_root,
        ["diff", "--unified=0", "--no-ext-diff", "HEAD", "--"],
        deadline=deadline,
    )
    changed: dict[Path, list[tuple[int, int]]] = {}
    retained_changed: dict[str, Path] = {}
    changed_keys: list[str] = []
    diff_path_limit_exceeded = False
    paths_requiring_counts: set[Path] = set()

    def retain_changed_path(path: Path) -> bool:
        nonlocal diff_path_limit_exceeded
        key = path.as_posix()
        if key in retained_changed:
            return True
        position = bisect_left(changed_keys, key)
        if len(changed_keys) < MAX_FILES + 1:
            changed_keys.insert(position, key)
            retained_changed[key] = path
            return True
        diff_path_limit_exceeded = True
        if position >= len(changed_keys):
            return False
        removed_key = changed_keys.pop()
        removed = retained_changed.pop(removed_key)
        changed.pop(removed, None)
        changed_keys.insert(position, key)
        retained_changed[key] = path
        return True

    if diff.returncode == 0:
        current_path: Path | None = None
        in_file_header = False
        patch = diff.stdout.decode("utf-8", errors="surrogateescape")
        for line in patch.splitlines():
            _check_deadline(deadline)
            if line.startswith("diff --git "):
                current_path = None
                in_file_header = True
                continue
            if in_file_header and line.startswith("+++ "):
                destination = _decode_diff_path(line[4:])
                current_path = (
                    destination
                    if destination is not None and _supported(destination)
                    else None
                )
                if current_path is not None and not retain_changed_path(current_path):
                    current_path = None
                continue
            match = _HUNK_RE.match(line)
            if match is None:
                continue
            in_file_header = False
            if current_path is None:
                continue
            first = int(match.group(1))
            count = int(match.group(2) or "1")
            if count:
                changed.setdefault(current_path, []).append((first, first + count))
    else:
        head = _run_git(
            git_root, ["rev-parse", "--verify", "HEAD"], deadline=deadline
        )
        if head.returncode == 0:
            detail = diff.stderr.decode("utf-8", errors="replace").strip()
            raise AuditInputError(f"Git diff failed: {detail or 'unknown error'}")
        for path in _listed_paths(git_root, ["--cached"], deadline=deadline):
            if _supported(path) and (git_root / path).is_file():
                paths_requiring_counts.add(path)

    for path in _listed_paths(
        git_root, ["--others", "--exclude-standard"], deadline=deadline
    ):
        if _supported(path) and (git_root / path).is_file():
            paths_requiring_counts.add(path)

    ordered_paths = sorted(
        changed.keys() | paths_requiring_counts, key=lambda item: item.as_posix()
    )
    file_limit_exceeded = diff_path_limit_exceeded or len(ordered_paths) > MAX_FILES
    ordered_paths = ordered_paths[:MAX_FILES]
    result: dict[Path, ChangedLineSelection] = {}
    for path in ordered_paths:
        if path in paths_requiring_counts:
            result[path] = AllSourceLines(MAX_FILE_BYTES + 1)
        else:
            result[path] = LineIntervals.normalized(changed[path])
    return result, file_limit_exceeded


def git_changed_lines(root: Path) -> dict[Path, LineSelection]:
    """Return one-based changed lines keyed by paths relative to the Git root.

    Selections through ``MAX_EAGER_CHANGED_LINES`` remain ``frozenset`` values
    for compatibility. Larger selections stay immutable interval sets.
    """

    deadline = time.monotonic() + 60.0
    discovered = _git_changed_lines(root, deadline=deadline)[0]
    git_root: Path | None = None
    result: dict[Path, LineSelection] = {}
    for path, lines in discovered.items():
        if isinstance(lines, AllSourceLines):
            if git_root is None:
                git_root = _git_root(root, deadline=deadline)
            lines = _line_numbers(git_root / path, deadline=deadline)
        eager_compatible = (
            lines.cardinality_at_most(MAX_EAGER_CHANGED_LINES)
            if isinstance(lines, LineIntervals)
            else len(lines) <= MAX_EAGER_CHANGED_LINES
        )
        result[path] = (
            frozenset(lines)
            if eager_compatible else lines
        )
    return result


def _report_path(path: Path, root: Path) -> Path:
    try:
        return path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return path.resolve()


def run_audit(
    paths: Iterable[Path],
    *,
    root: Path | None = None,
    changed_lines: Mapping[Path, ChangedLineSelection] | None = None,
    mode: str = "paths",
    configuration: Mapping[str, object] | None = None,
    file_limit_exceeded: bool = False,
    clang_mode: str = "off",
    compile_commands: Path | None = None,
    budget_seconds: float = 60.0,
    deadline: float | None = None,
) -> AuditReport:
    """Analyze bounded source inputs and return a normalized report."""

    if deadline is None:
        deadline = time.monotonic() + budget_seconds
    if clang_mode not in {"auto", "off"}:
        raise AuditInputError("RELIABLE_CC_CLANG must be auto or off")
    audit_root = (root or Path.cwd()).resolve()
    selected: list[tuple[Path, Path]] = []
    for path in paths:
        report_path = _report_path(Path(path), audit_root)
        if changed_lines is not None and report_path not in changed_lines:
            continue
        selected.append((Path(path), report_path))
    selected.sort(key=lambda item: item[1].as_posix())

    limitations: list[Limitation] = []
    truncated = file_limit_exceeded
    if file_limit_exceeded:
        limitations.append(
            Limitation("TOOL_LIMIT", f"audit is limited to {MAX_FILES} files")
        )
    if len(selected) > MAX_FILES:
        selected = selected[:MAX_FILES]
        truncated = True
        if not file_limit_exceeded:
            limitations.append(
                Limitation("TOOL_LIMIT", f"audit is limited to {MAX_FILES} files")
            )

    findings: list[Finding] = []
    sources: dict[Path, str] = {}
    source_bytes = 0
    timed_out = False

    def expired(path: str | None = None) -> bool:
        nonlocal timed_out, truncated
        if time.monotonic() < deadline:
            return False
        if not timed_out:
            limitations.append(Limitation("TOOL_TIMEOUT", "audit total time limit reached", path))
        timed_out = truncated = True
        return True

    for source_path, report_path in selected:
        path_text = report_path.as_posix()
        if expired(path_text):
            break
        remaining_source_bytes = MAX_TOTAL_SOURCE_BYTES - source_bytes
        if remaining_source_bytes <= 0:
            truncated = True
            limitations.append(
                Limitation(
                    "TOOL_LIMIT",
                    "audit exceeds the 20 MiB total source limit",
                    path_text,
                )
            )
            break
        if remaining_source_bytes < MAX_FILE_BYTES:
            contents = _read_bounded(
                source_path, max_bytes=remaining_source_bytes
            )
        else:
            contents = _read_bounded(source_path)
        if expired(path_text):
            break
        source_bytes += len(contents)
        if len(contents) > min(MAX_FILE_BYTES, remaining_source_bytes):
            truncated = True
            if remaining_source_bytes >= MAX_FILE_BYTES:
                limitations.append(
                    Limitation(
                        "TOOL_LIMIT", "source file exceeds the 2 MiB limit", path_text
                    )
                )
                continue
            limitations.append(
                Limitation(
                    "TOOL_LIMIT",
                    "audit exceeds the 20 MiB total source limit",
                    path_text,
                )
            )
            break
        try:
            text = contents.decode("utf-8")
        except UnicodeDecodeError as error:
            raise AuditInputError(
                f"cannot decode source file {path_text} as UTF-8: {error}"
            ) from error

        if expired(path_text):
            break
        remaining = MAX_FINDINGS - len(findings)
        source_result = analyze_source_bounded(
            report_path,
            text,
            deadline=deadline,
            max_findings=remaining,
            changed_lines=(changed_lines[report_path] if changed_lines is not None else None),
        )
        source_findings = source_result.findings
        if source_result.timed_out:
            if not timed_out:
                limitations.append(
                    Limitation("TOOL_TIMEOUT", "audit total time limit reached", path_text)
                )
            timed_out = truncated = True
        if source_result.truncated:
            truncated = True
            limitations.append(
                Limitation("TOOL_LIMIT", f"audit is limited to {MAX_FINDINGS} findings")
            )
        stop_after_source = source_result.timed_out or source_result.truncated or expired(path_text)
        sources[source_path] = text
        if len(source_findings) > remaining:
            findings.extend(source_findings[:remaining])
            truncated = True
            limitations.append(
                Limitation("TOOL_LIMIT", f"audit is limited to {MAX_FINDINGS} findings")
            )
            break
        findings.extend(source_findings)
        if stop_after_source:
            break

    effective_configuration = dict(configuration or {})
    effective_configuration["clang"] = clang_mode
    effective_configuration["compilation_databases"] = []
    if clang_mode == "auto" and not timed_out and not expired():
        try:
            semantic = run_clang_analysis(
                sources, root=audit_root, changed_lines=changed_lines,
                compile_commands=compile_commands,
                deadline=deadline,
            )
            findings.extend(semantic.findings)
            limitations.extend(semantic.limitations)
            truncated = truncated or semantic.truncated
            effective_configuration["compilation_databases"] = list(semantic.databases)
        except Exception as error:
            limitations.append(Limitation("TOOL_CLANG_FAILURE", f"Clang analysis unavailable: {error}"))
    # An authoritative AST finding supersedes a same-rule source estimate.
    unique: dict[tuple[str, int, str], Finding] = {}
    severity = {"info": 0, "warning": 1, "error": 2}
    for item in findings:
        key = finding_sort_key(item)
        if key not in unique or (severity[item.severity], item.engine == "clang") > (
            severity[unique[key].severity], unique[key].engine == "clang"
        ):
            unique[key] = item
    ordered = sorted(unique.values(), key=finding_sort_key)
    if len(ordered) > MAX_FINDINGS:
        truncated = True
        limitations.append(Limitation("TOOL_LIMIT", f"audit is limited to {MAX_FINDINGS} findings"))

    return AuditReport(
        mode=mode,
        findings=tuple(ordered[:MAX_FINDINGS]),
        limitations=tuple(limitations),
        truncated=truncated,
        configuration=effective_configuration,
    )


def exit_status(report: AuditReport, fail_on: str) -> int:
    """Return whether gate-eligible Power of Ten findings meet a threshold."""

    eligible = [
        item
        for item in report.findings
        if item.gate_eligible and item.code.startswith("POT")
    ]
    if fail_on == "none":
        return 0
    if fail_on == "errors":
        return int(any(item.severity == "error" for item in eligible))
    return int(any(item.severity in {"error", "warning"} for item in eligible))


def _count_label(count: int, singular: str) -> str:
    if singular == "info":
        return f"{count} info"
    return f"{count} {singular if count == 1 else singular + 's'}"


def render_text(report: AuditReport) -> str:
    """Render findings, limitations, and counts as stable plain text."""

    payload = report.to_dict()
    lines = [
        f"{item['path']}:{item['line']}: {item['severity']} {item['code']}: {item['message']}"
        for item in payload["findings"]
    ]
    for limitation in payload["limitations"]:
        prefix = f"{limitation['path']}: " if limitation["path"] is not None else ""
        lines.append(f"{prefix}limitation {limitation['code']}: {limitation['message']}")
    summary = payload["summary"]
    lines.append(
        "summary: "
        + ", ".join(
            (
                _count_label(summary["errors"], "error"),
                _count_label(summary["warnings"], "warning"),
                _count_label(summary["info"], "info"),
            )
        )
        + f"; truncated: {'yes' if report.truncated else 'no'}"
    )
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    """Build the public command-line parser."""

    parser = argparse.ArgumentParser(description="Audit C/C++ source quality")
    parser.add_argument("paths", nargs="*", metavar="PATH")
    parser.add_argument("--git-diff", action="store_true", help="audit changed Git lines")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--fail-on", choices=("errors", "warnings", "none"), default="errors")
    return parser


def _emit_report(report: AuditReport, output_format: str, fail_on: str) -> int:
    if output_format == "json":
        print(json.dumps(report.to_dict(), sort_keys=True))
    else:
        print(render_text(report))
    return exit_status(report, fail_on)


def _limit_report(
    *, mode: str, configuration: Mapping[str, object], error: AuditLimitReached,
) -> AuditReport:
    effective_configuration = dict(configuration)
    effective_configuration.setdefault("clang", "off")
    effective_configuration.setdefault("compilation_databases", [])
    return AuditReport(
        mode=mode,
        limitations=(Limitation(error.code, str(error), error.path),),
        truncated=True,
        configuration=effective_configuration,
    )


def main(argv: list[str] | None = None, *, budget_seconds: float = 60.0) -> int:
    """Run the CLI and return its documented zero, one, or two status."""

    deadline = time.monotonic() + budget_seconds
    parser = build_parser()
    try:
        arguments = parser.parse_args(argv)
    except SystemExit as error:
        return int(error.code)
    if arguments.git_diff and arguments.paths:
        parser.print_usage(sys.stderr)
        print("audit_cc.py: error: --git-diff cannot be combined with paths", file=sys.stderr)
        return 2
    if not arguments.git_diff and not arguments.paths:
        parser.print_usage(sys.stderr)
        print("audit_cc.py: error: provide a path or --git-diff", file=sys.stderr)
        return 2

    mode = "git-diff" if arguments.git_diff else "paths"
    configuration: dict[str, object] = {
        "format": arguments.format,
        "fail_on": arguments.fail_on,
        "compile_commands": None,
    }
    try:
        clang_mode = os.environ.get("RELIABLE_CC_CLANG", "auto")
        if clang_mode not in {"auto", "off"}:
            raise AuditInputError("RELIABLE_CC_CLANG must be auto or off")
        configuration["clang"] = clang_mode
        configuration["compilation_databases"] = []
        explicit = os.environ.get("RELIABLE_CC_COMPILE_COMMANDS")
        compile_commands = None
        if explicit is not None:
            if not explicit.strip():
                raise AuditInputError("RELIABLE_CC_COMPILE_COMMANDS must name a database or directory")
            try:
                compile_commands = validate_compilation_database(
                    Path(explicit), deadline=deadline
                )
            except TimeoutError as error:
                raise AuditLimitReached(
                    "TOOL_TIMEOUT", "audit total time limit reached"
                ) from error
            except (OSError, ValueError, RecursionError) as error:
                raise AuditInputError(f"invalid RELIABLE_CC_COMPILE_COMMANDS: {error}") from error
        configuration["compile_commands"] = str(compile_commands) if compile_commands else None
        if arguments.git_diff:
            root = _git_root(Path.cwd(), deadline=deadline)
            lines, file_limit_exceeded = _git_changed_lines(root, deadline=deadline)
            paths = tuple(root / path for path in lines)
            report = run_audit(
                paths,
                root=root,
                changed_lines=lines,
                mode="git-diff",
                configuration=configuration,
                file_limit_exceeded=file_limit_exceeded,
                clang_mode=clang_mode,
                compile_commands=compile_commands,
                budget_seconds=budget_seconds,
                deadline=deadline,
            )
        else:
            paths, discovery_timed_out = _discover_paths(
                arguments.paths, deadline=deadline
            )
            if discovery_timed_out:
                raise AuditLimitReached(
                    "TOOL_TIMEOUT", "audit total time limit reached"
                )
            try:
                root = _git_root(Path.cwd(), deadline=deadline)
            except GitWorkingTreeError:
                root = Path.cwd()
            report = run_audit(paths, root=root, configuration=configuration,
                               clang_mode=clang_mode, compile_commands=compile_commands,
                               budget_seconds=budget_seconds, deadline=deadline)
    except AuditLimitReached as error:
        report = _limit_report(
            mode=mode, configuration=configuration, error=error
        )
        return _emit_report(report, arguments.format, arguments.fail_on)
    except AuditInputError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    return _emit_report(report, arguments.format, arguments.fail_on)


if __name__ == "__main__":
    raise SystemExit(main())
