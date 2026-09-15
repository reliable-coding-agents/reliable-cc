"""Safe lexical views shared by the Reliable C/C++ source checks."""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Container
from dataclasses import dataclass
import pathlib
import re
import time

from quality_model import Finding


CODE = "CODE"
LINE_COMMENT = "LINE_COMMENT"
BLOCK_COMMENT = "BLOCK_COMMENT"
STRING = "STRING"
CHAR = "CHAR"
RAW_STRING = "RAW_STRING"

_KNOWN_SUPPRESSION_PREFIXES = frozenset({"POT", "SOLID"})

SUPPRESSION_RE = re.compile(
    r"quality:\s*ignore\[([A-Z]+\d{2})\]\s*-\s*(\S(?:.*\S)?)"
)


@dataclass(frozen=True)
class Directive:
    """One logical preprocessor directive, including continuations."""

    start_line: int
    end_line: int
    name: str
    replacement: str


@dataclass(frozen=True)
class LexedSource:
    """Original source and an offset-preserving view containing only code."""

    original_lines: tuple[str, ...]
    code_lines: tuple[str, ...]
    directives: tuple[Directive, ...]


@dataclass(frozen=True)
class FunctionRange:
    """A possible function body and the confidence of its source boundary."""

    start_line: int
    end_line: int
    confirmed: bool


@dataclass(frozen=True)
class SourceAnalysisResult:
    """Bounded source findings plus explicit resource-limit state."""

    findings: tuple[Finding, ...]
    timed_out: bool = False
    truncated: bool = False


def _check_deadline(deadline: float | None) -> None:
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError("source analysis total time limit reached")


def _raw_string_end(text: str, index: int) -> str | None:
    """Return a raw-string terminator when ``index`` begins one, if valid."""

    if not text.startswith('R"', index):
        return None
    delimiter_end = index + 2
    while delimiter_end < len(text) and text[delimiter_end] != "(":
        character = text[delimiter_end]
        if character in " ()\\\t\r\n" or delimiter_end - (index + 2) >= 16:
            return None
        delimiter_end += 1
    if delimiter_end >= len(text):
        return None
    return ")" + text[index + 2:delimiter_end] + '"'


def _directives(
    original_lines: tuple[str, ...], code_lines: tuple[str, ...],
    *, deadline: float | None = None,
) -> tuple[Directive, ...]:
    directives: list[Directive] = []
    line_index = 0
    pattern = re.compile(r"\s*#\s*([A-Za-z_]\w*)(.*)$")
    while line_index < len(code_lines):
        if (line_index & 255) == 0:
            _check_deadline(deadline)
        match = pattern.match(code_lines[line_index])
        if not match:
            line_index += 1
            continue
        start_index = line_index
        pieces = [match.group(2)]
        while (
            original_lines[line_index].rstrip().endswith("\\")
            and line_index + 1 < len(code_lines)
        ):
            _check_deadline(deadline)
            if code_lines[line_index].rstrip().endswith("\\"):
                pieces[-1] = pieces[-1].rstrip()[:-1]
            else:
                pieces[-1] = pieces[-1].rstrip()
            line_index += 1
            pieces.append(code_lines[line_index])
        directives.append(
            Directive(
                start_line=start_index + 1,
                end_line=line_index + 1,
                name=match.group(1),
                replacement="".join(pieces),
            )
        )
        line_index += 1
    return tuple(directives)


def lex_source(text: str, *, deadline: float | None = None) -> LexedSource:
    """Hide comments and literals without changing physical source positions."""

    masked = [character if character in "\r\n" else " " for character in text]
    state = CODE
    raw_terminator = ""
    index = 0
    while index < len(text):
        if index & 1023 == 0:
            _check_deadline(deadline)
        character = text[index]
        if state == CODE:
            if text.startswith("//", index):
                state = LINE_COMMENT
                index += 2
                continue
            if text.startswith("/*", index):
                state = BLOCK_COMMENT
                index += 2
                continue
            terminator = _raw_string_end(text, index)
            if terminator is not None:
                state = RAW_STRING
                raw_terminator = terminator
                index += 2
                continue
            if character == '"':
                state = STRING
                index += 1
                continue
            if character == "'":
                state = CHAR
                index += 1
                continue
            masked[index] = character
            index += 1
            continue

        if state == LINE_COMMENT:
            if character == "\\" and index + 1 < len(text):
                if text[index + 1] == "\n":
                    index += 2
                    continue
                if text.startswith("\r\n", index + 1):
                    index += 3
                    continue
            if character in "\r\n":
                state = CODE
            index += 1
            continue

        if state == BLOCK_COMMENT:
            if text.startswith("*/", index):
                state = CODE
                index += 2
            else:
                index += 1
            continue

        if state == RAW_STRING:
            if text.startswith(raw_terminator, index):
                state = CODE
                index += len(raw_terminator)
            else:
                index += 1
            continue

        if character == "\\":
            index += 2
            continue
        if (state == STRING and character == '"') or (state == CHAR and character == "'"):
            state = CODE
        index += 1

    masked_text = "".join(masked)
    original_lines = tuple(text.splitlines())
    code_lines = tuple(masked_text.splitlines())
    return LexedSource(
        original_lines,
        code_lines,
        _directives(original_lines, code_lines, deadline=deadline),
    )


def _looks_like_function_header(header: str) -> bool:
    """Conservatively recognize a definition header immediately before ``{``."""

    compact = " ".join(header.split())
    if not compact:
        return False
    if re.match(r"(?:else\s+)?(?:if|for|while|switch|catch)\b", compact):
        return False
    if re.search(r"\[[^\]]*\]\s*(?:\([^)]*\))?\s*$", compact):
        return True
    if "=" in compact:
        return False
    if "(" not in compact or ")" not in compact:
        return False
    return re.search(
        r"(?:~?[A-Za-z_]\w*|operator\s*(?:\[\]|[^\s(]+))\s*\(", compact
    ) is not None


def _line_starts(
    text: str, *, deadline: float | None = None
) -> tuple[int, ...]:
    starts = [0]
    for match in re.finditer("\n", text):
        if (len(starts) & 255) == 0:
            _check_deadline(deadline)
        starts.append(match.end())
    return tuple(starts)


def _line_for_offset(line_starts: tuple[int, ...], offset: int) -> int:
    return bisect_right(line_starts, offset)


def _header_start_line(
    code_text: str,
    boundary: int,
    brace: int,
    line_starts: tuple[int, ...],
    *,
    deadline: float | None = None,
) -> int:
    for offset in range(boundary + 1, brace):
        if (offset & 1023) == 0:
            _check_deadline(deadline)
        if not code_text[offset].isspace():
            return _line_for_offset(line_starts, offset)
    return _line_for_offset(line_starts, brace)


def _starts_braced_member_initializer(header: str) -> bool:
    """Return whether a constructor initializer, rather than a body, opens here."""

    compact = " ".join(header.split())
    return bool(
        re.search(r"(?<!:):(?!:)", compact)
        and re.search(r"[A-Za-z_]\w*\s*$", compact)
    )


def _has_body_macro_suffix(line: str) -> bool:
    """Recognize the common ``void f() BODY`` macro-provided body opener."""

    return re.search(r"\)\s*[A-Za-z_]\w*\s*$", line) is not None


def function_ranges(
    lexed: LexedSource, *, deadline: float | None = None
) -> tuple[FunctionRange, ...]:
    """Return conservative ranges for likely source-level function definitions.

    The lexical view cannot expand macros or parse C++. A brace-balanced body
    without directive lines is consequently the only confirmed range. Other
    plausible candidates remain visible to callers as uncertain ranges.
    """

    code_text = "\n".join(lexed.code_lines)
    line_starts = _line_starts(code_text, deadline=deadline)
    directive_lines = {
        line
        for directive in lexed.directives
        for line in range(directive.start_line, directive.end_line + 1)
    }
    ranges: list[tuple[int, FunctionRange]] = []
    candidates: list[list[int | bool]] = []
    boundary = -1
    initializer_depth = 0
    brace_depth = 0
    current_line = 1

    for brace, character in enumerate(code_text):
        if (brace & 1023) == 0:
            _check_deadline(deadline)
        brace_line = current_line
        if character == "\n":
            current_line += 1
        if brace_line in directive_lines:
            for candidate in candidates:
                candidate[3] = True
            continue
        if initializer_depth:
            if character == "{":
                initializer_depth += 1
                brace_depth += 1
            elif character == "}":
                initializer_depth -= 1
                brace_depth -= 1
            continue
        if character in ";}":
            boundary = brace
            if character == "}":
                brace_depth -= 1
                completed = [item for item in candidates if item[2] > brace_depth]
                candidates = [item for item in candidates if item[2] <= brace_depth]
                for opening, start_line, _, uncertain in completed:
                    ranges.append(
                        (
                            int(opening),
                            FunctionRange(int(start_line), brace_line, not bool(uncertain)),
                        )
                    )
            continue
        if character != "{":
            continue
        header = code_text[boundary + 1:brace]
        _check_deadline(deadline)
        if not _looks_like_function_header(header):
            boundary = brace
            brace_depth += 1
            continue
        if _starts_braced_member_initializer(header):
            initializer_depth += 1
            brace_depth += 1
            continue

        start_line = _header_start_line(
            code_text, boundary, brace, line_starts, deadline=deadline
        )
        brace_depth += 1
        candidates.append([brace, start_line, brace_depth, False])
        boundary = brace

    for opening, start_line, _, _ in candidates:
        ranges.append(
            (
                int(opening),
                FunctionRange(int(start_line), len(lexed.code_lines), False),
            )
        )

    # A definition body can be supplied by a macro, leaving no trustworthy
    # opening brace in the lexical view. Preserve that candidate as uncertain.
    last_directive_line = max(directive_lines, default=0)
    for line_number, line in enumerate(lexed.code_lines, start=1):
        if (line_number & 255) == 0:
            _check_deadline(deadline)
        if line_number in directive_lines or ";" in line or "{" in line:
            continue
        if not _looks_like_function_header(line):
            continue
        _check_deadline(deadline)
        following_directive = last_directive_line > line_number
        if following_directive or _has_body_macro_suffix(line):
            ranges.append(
                (len(code_text) + line_number, FunctionRange(line_number, len(lexed.code_lines), False))
            )

    return tuple(item for _, item in sorted(ranges, key=lambda item: item[0]))


def _comment_fragments(
    text: str, *, deadline: float | None = None
) -> dict[int, list[str]]:
    """Return comment contents by their one-based physical source line."""

    comments: dict[int, list[str]] = {}
    state = CODE
    raw_terminator = ""
    index = 0
    line = 1
    fragment_start: int | None = None

    def add_fragment(start: int, end: int, fragment_line: int) -> None:
        comments.setdefault(fragment_line, []).append(text[start:end])

    while index < len(text):
        if (index & 1023) == 0:
            _check_deadline(deadline)
        character = text[index]
        if state == CODE:
            if text.startswith("//", index):
                fragment_start = index + 2
                state = LINE_COMMENT
                index += 2
                continue
            if text.startswith("/*", index):
                fragment_start = index + 2
                state = BLOCK_COMMENT
                index += 2
                continue
            terminator = _raw_string_end(text, index)
            if terminator is not None:
                raw_terminator = terminator
                state = RAW_STRING
                index += 2
                continue
            if character == '"':
                state = STRING
            elif character == "'":
                state = CHAR
            if character == "\n":
                line += 1
            index += 1
            continue

        if state == LINE_COMMENT:
            if character == "\\" and index + 1 < len(text):
                if text[index + 1] == "\n":
                    assert fragment_start is not None
                    add_fragment(fragment_start, index, line)
                    line += 1
                    fragment_start = index + 2
                    index += 2
                    continue
                if text.startswith("\r\n", index + 1):
                    assert fragment_start is not None
                    add_fragment(fragment_start, index, line)
                    line += 1
                    fragment_start = index + 3
                    index += 3
                    continue
            if character == "\n":
                assert fragment_start is not None
                add_fragment(fragment_start, index, line)
                line += 1
                state = CODE
            index += 1
            continue

        if state == BLOCK_COMMENT:
            if text.startswith("*/", index):
                assert fragment_start is not None
                add_fragment(fragment_start, index, line)
                state = CODE
                index += 2
                continue
            if character == "\n":
                assert fragment_start is not None
                add_fragment(fragment_start, index, line)
                line += 1
                fragment_start = index + 1
            index += 1
            continue

        if state == RAW_STRING:
            if text.startswith(raw_terminator, index):
                state = CODE
                index += len(raw_terminator)
                continue
        elif character == "\\":
            if index + 1 < len(text) and text[index + 1] == "\n":
                line += 1
            index += 2
            continue
        elif (state == STRING and character == '"') or (state == CHAR and character == "'"):
            state = CODE
        if character == "\n":
            line += 1
        index += 1

    if state == LINE_COMMENT and fragment_start is not None:
        add_fragment(fragment_start, len(text), line)
    elif state == BLOCK_COMMENT and fragment_start is not None:
        add_fragment(fragment_start, len(text), line)
    return comments


def _malformed_suppression(path: str, line: int) -> Finding:
    return Finding(
        code="TOOL_SUPPRESSION",
        severity="warning",
        path=path,
        line=line,
        message="malformed quality suppression",
        remediation="use quality: ignore[RULE] - rationale on the finding line",
        engine="source",
        gate_eligible=False,
    )


def parse_suppressions(
    text: str,
    path: str,
    *,
    deadline: float | None = None,
    max_malformed: int | None = None,
    changed_lines: Container[int] | None = None,
) -> tuple[dict[int, frozenset[str]], tuple[Finding, ...]]:
    """Parse narrow same-line quality suppressions from source comments."""

    suppressions: dict[int, set[str]] = {}
    malformed: list[Finding] = []
    for line, fragments in _comment_fragments(text, deadline=deadline).items():
        _check_deadline(deadline)
        for fragment in fragments:
            starts = iter(re.finditer(r"quality:\s*ignore\[", fragment))
            start = next(starts, None)
            while start is not None:
                _check_deadline(deadline)
                following = next(starts, None)
                end = following.start() if following is not None else len(fragment)
                directive = fragment[start.start():end]
                match = SUPPRESSION_RE.search(directive)
                if match is None:
                    if changed_lines is None or line in changed_lines:
                        malformed.append(_malformed_suppression(path, line))
                else:
                    code = match.group(1)
                    prefix = re.match(r"[A-Z]+", code)
                    if prefix is None or prefix.group(0) not in _KNOWN_SUPPRESSION_PREFIXES:
                        if changed_lines is None or line in changed_lines:
                            malformed.append(_malformed_suppression(path, line))
                    else:
                        suppressions.setdefault(line, set()).add(code)
                if max_malformed is not None and len(malformed) >= max_malformed:
                    break
                start = following
            if max_malformed is not None and len(malformed) >= max_malformed:
                break
        if max_malformed is not None and len(malformed) >= max_malformed:
            break
    return (
        {line: frozenset(codes) for line, codes in suppressions.items()},
        tuple(malformed),
    )


def apply_suppressions(
    findings: tuple[Finding, ...], suppressions: dict[int, frozenset[str]]
) -> tuple[Finding, ...]:
    """Drop only findings whose exact code is suppressed on their exact line."""

    return tuple(
        finding
        for finding in findings
        if finding.code not in suppressions.get(finding.line, frozenset())
    )


_CONDITIONAL_DIRECTIVES = frozenset({"if", "ifdef", "ifndef"})
_EMBEDDED_DIRECTIVE_RE = re.compile(
    r"#\s*(?:if|ifdef|ifndef|elif|else|endif|define|include|undef|pragma|line|error|warning)\b"
)


def _source_finding(
    code: str,
    severity: str,
    path: str,
    line: int,
    message: str,
    remediation: str,
    gate_eligible: bool = False,
) -> Finding:
    """Build a normalized source-level finding with stable field values."""

    return Finding(
        code=code,
        severity=severity,
        path=path,
        line=line,
        message=message,
        remediation=remediation,
        engine="source",
        gate_eligible=gate_eligible,
    )


def _matching_paren(
    text: str, opening: int, *, deadline: float | None = None
) -> int | None:
    """Find the matching parenthesis in code-only text."""

    depth = 0
    for index in range(opening, len(text)):
        if (index & 1023) == 0:
            _check_deadline(deadline)
        if text[index] == "(":
            depth += 1
        elif text[index] == ")":
            depth -= 1
            if depth == 0:
                return index
    return None


def _for_condition_is_empty(
    header: str, *, deadline: float | None = None
) -> bool:
    """Return whether a ``for`` header has an empty middle condition."""

    depth = 0
    separators: list[int] = []
    for index, character in enumerate(header):
        if (index & 1023) == 0:
            _check_deadline(deadline)
        if character in "([{":
            depth += 1
        elif character in ")]}":
            depth -= 1
        elif character == ";" and depth == 0:
            separators.append(index)
    return len(separators) >= 2 and not header[separators[0] + 1:separators[1]].strip()


def _has_constant_upper_bound(
    condition: str, *, deadline: float | None = None
) -> bool:
    """Recognize the narrow, mechanical loop-bound patterns this engine trusts."""

    operand = r"(?:0[xX][0-9A-Fa-f]+|\d+(?:[uUlL]+)?|[A-Z_][A-Z0-9_]*)"
    identifier = r"[A-Za-z_]\w*"
    condition = condition.strip()
    while condition.startswith("(") and condition.endswith(")"):
        _check_deadline(deadline)
        closing = _matching_paren(condition, 0, deadline=deadline)
        if closing != len(condition) - 1:
            break
        condition = condition[1:-1].strip()
    return bool(
        re.fullmatch(rf"{identifier}\s*(?:<|<=|>|>=)\s*{operand}", condition)
        or re.fullmatch(rf"{operand}\s*(?:<|<=|>|>=)\s*{identifier}", condition)
    )


def _include_guard_starts(
    path: pathlib.Path,
    lexed: LexedSource,
    *,
    deadline: float | None = None,
) -> frozenset[int]:
    """Return opening ``#ifndef`` lines for conventional include guards."""

    if path.suffix.lower() not in {".h", ".hh", ".hpp", ".hxx"}:
        return frozenset()

    _check_deadline(deadline)
    first_nonblank = 0
    last_nonblank = 0
    for line_number, line in enumerate(lexed.code_lines, start=1):
        if (line_number & 255) == 0:
            _check_deadline(deadline)
        if line.strip():
            if first_nonblank == 0:
                first_nonblank = line_number
            last_nonblank = line_number

    directives = lexed.directives
    matches: dict[int, Directive] = {}
    stack: list[int] = []
    for index, directive in enumerate(directives):
        if (index & 255) == 0:
            _check_deadline(deadline)
        if directive.name in _CONDITIONAL_DIRECTIVES:
            stack.append(index)
        elif directive.name == "endif" and stack:
            matches[stack.pop()] = directive

    starts: set[int] = set()
    for index, directive in enumerate(directives[:-1]):
        if (index & 255) == 0:
            _check_deadline(deadline)
        if directive.name != "ifndef" or directive.start_line != first_nonblank:
            continue
        replacement = directive.replacement.strip()
        name = replacement.split(maxsplit=1)[0] if replacement else ""
        following = directives[index + 1]
        if (
            not name
            or following.name != "define"
            or following.replacement.strip() != name
        ):
            continue
        matching_endif = matches.get(index)
        if matching_endif is None:
            continue
        if matching_endif.end_line < last_nonblank:
            continue
        starts.add(directive.start_line)
    _check_deadline(deadline)
    return frozenset(starts)


def _macro_finding(
    path: str, directive: Directive, *, deadline: float | None = None
) -> Finding | None:
    """Classify one macro definition without expanding or evaluating it."""

    _check_deadline(deadline)
    match = re.match(r"\s*([A-Za-z_]\w*)(\(([^)]*)\))?(.*)$", directive.replacement, re.S)
    _check_deadline(deadline)
    if match is None:
        return None
    name, parentheses, parameters, body = match.groups()
    parameter_names = frozenset(re.findall(r"[A-Za-z_]\w*", parameters or ""))
    has_error = (
        "##" in body
        or bool(re.search(r"(?<!#)#\s*[A-Za-z_]\w*", body))
        or "..." in (parameters or "")
        or "__VA_ARGS__" in body
        or bool(_EMBEDDED_DIRECTIVE_RE.search(body))
        or (
            name not in parameter_names
            and bool(re.search(rf"\b{re.escape(name)}\b", body))
        )
    )
    if has_error:
        return _source_finding(
            "POT08",
            "error",
            path,
            directive.start_line,
            "restricted preprocessor construct",
            "replace the macro construct with ordinary language features",
            True,
        )
    if parentheses is not None:
        return _source_finding(
            "POT08",
            "warning",
            path,
            directive.start_line,
            "function-like macro requires review",
            "prefer a typed function, constexpr value, or template",
        )
    return None


def _is_jump_declaration(
    text: str, start: int, opening: int, *, deadline: float | None = None
) -> bool:
    """Recognize only unambiguous setjmp/longjmp prototype declarations."""

    closing = _matching_paren(text, opening, deadline=deadline)
    if closing is None:
        return False
    suffix = text[closing + 1:].lstrip()
    _check_deadline(deadline)
    if not suffix.startswith(";"):
        return False
    boundary = max(text.rfind(character, 0, start) for character in ";{}")
    prefix = text[boundary + 1:start].strip()
    if (
        not prefix
        or prefix.endswith("::")
        or re.search(r"\b(?:return|if|while|for|switch|case|goto|sizeof)\b", prefix)
    ):
        return False
    return re.fullmatch(r"(?:[A-Za-z_]\w*|::|\*|&|\s)+", prefix) is not None


class _FindingLimitReached(RuntimeError):
    pass


def analyze_source_bounded(
    path: pathlib.Path,
    text: str,
    *,
    deadline: float | None,
    max_findings: int,
    changed_lines: Container[int] | None = None,
) -> SourceAnalysisResult:
    """Analyze one source while retaining deterministic partial bounded results."""

    if max_findings < 0:
        raise ValueError("max_findings must be nonnegative")
    path_text = str(path)
    findings: list[Finding] = []
    timed_out = False
    truncated = False
    try:
        suppressions, malformed = parse_suppressions(
            text,
            path_text,
            deadline=deadline,
            max_malformed=max_findings + 1,
            changed_lines=changed_lines,
        )
    except TimeoutError:
        return SourceAnalysisResult((), timed_out=True)

    def emit(finding: Finding, *, suppressible: bool = True) -> None:
        nonlocal truncated
        _check_deadline(deadline)
        if changed_lines is not None and finding.line not in changed_lines:
            return
        if suppressible and finding.code in suppressions.get(finding.line, frozenset()):
            return
        if len(findings) >= max_findings:
            truncated = True
            raise _FindingLimitReached
        findings.append(finding)

    try:
        _check_deadline(deadline)
        for finding in malformed:
            emit(finding, suppressible=False)
        lexed = lex_source(text, deadline=deadline)
        _check_deadline(deadline)
        directive_lines = {
            line
            for directive in lexed.directives
            for line in range(directive.start_line, directive.end_line + 1)
        }
        detector_lines: list[str] = []
        for line, code in enumerate(lexed.code_lines, start=1):
            if (line & 255) == 0:
                _check_deadline(deadline)
            detector_lines.append("" if line in directive_lines else code)
        detector_text = "\n".join(detector_lines)
        line_starts = _line_starts(detector_text, deadline=deadline)

        for match in re.finditer(r"\bgoto\b|\b(?:setjmp|longjmp)\s*\(", detector_text):
            _check_deadline(deadline)
            if match.group().lstrip().startswith(("setjmp", "longjmp")):
                opening = detector_text.find("(", match.start(), match.end())
                if opening != -1 and _is_jump_declaration(
                    detector_text, match.start(), opening, deadline=deadline
                ):
                    continue
            emit(
                _source_finding(
                    "POT01", "error", path_text,
                    _line_for_offset(line_starts, match.start()),
                    "simple control flow violation",
                    "replace goto, setjmp, or longjmp with structured control flow", True,
                )
            )

        for match in re.finditer(r"\bfor\b", detector_text):
            _check_deadline(deadline)
            opening = detector_text.find("(", match.end())
            if opening == -1:
                continue
            closing = _matching_paren(detector_text, opening, deadline=deadline)
            if closing is not None and _for_condition_is_empty(
                detector_text[opening + 1:closing], deadline=deadline
            ):
                emit(
                    _source_finding(
                        "POT02", "warning", path_text,
                        _line_for_offset(line_starts, match.start()),
                        "loop has no mechanically visible upper bound",
                        "add a visible bound or document a narrowly scoped exception",
                    )
                )
        for match in re.finditer(r"\bwhile\b", detector_text):
            _check_deadline(deadline)
            opening = detector_text.find("(", match.end())
            if opening == -1:
                continue
            closing = _matching_paren(detector_text, opening, deadline=deadline)
            if closing is None:
                continue
            if not _has_constant_upper_bound(
                detector_text[opening + 1:closing], deadline=deadline
            ):
                emit(
                    _source_finding(
                        "POT02", "warning", path_text,
                        _line_for_offset(line_starts, match.start()),
                        "loop has no mechanically visible upper bound",
                        "add a visible bound or document a narrowly scoped exception",
                    )
                )

        for match in re.finditer(
            r"\b(?:malloc|calloc|realloc|aligned_alloc|alloca)\s*\(|\bnew\b",
            detector_text,
        ):
            _check_deadline(deadline)
            emit(
                _source_finding(
                    "POT03", "warning", path_text,
                    _line_for_offset(line_starts, match.start()),
                    "dynamic allocation site requires review",
                    "prefer bounded, initialization-time, or caller-provided storage",
                )
            )

        assertion_counts = [0] * (len(lexed.code_lines) + 1)
        for match in re.finditer(r"\bassert\s*\(", detector_text):
            _check_deadline(deadline)
            assertion_counts[_line_for_offset(line_starts, match.start())] += 1
        assertion_prefix = [0]
        statement_prefix = [0]
        for line_number, line in enumerate(lexed.code_lines, start=1):
            if (line_number & 255) == 0:
                _check_deadline(deadline)
            assertion_prefix.append(assertion_prefix[-1] + assertion_counts[line_number])
            statement_prefix.append(statement_prefix[-1] + line.count(";"))

        for function in function_ranges(lexed, deadline=deadline):
            _check_deadline(deadline)
            line_count = function.end_line - function.start_line + 1
            if line_count > 60:
                emit(
                    _source_finding(
                        "POT04", "error" if function.confirmed else "warning", path_text,
                        function.start_line, "function exceeds 60 physical source lines",
                        "split the function into smaller cohesive operations", function.confirmed,
                    )
                )
            if line_count <= 10:
                continue
            assertions = assertion_prefix[function.end_line] - assertion_prefix[function.start_line - 1]
            statements = statement_prefix[function.end_line] - statement_prefix[function.start_line - 1]
            if statements >= 2 and assertions < 2:
                emit(
                    _source_finding(
                        "POT05", "warning", path_text, function.start_line,
                        "nontrivial function has fewer than two assertions",
                        "add meaningful standard assert checks or split the function",
                    )
                )

        include_guards = _include_guard_starts(path, lexed, deadline=deadline)
        for directive in lexed.directives:
            _check_deadline(deadline)
            if directive.name == "define":
                macro = _macro_finding(path_text, directive, deadline=deadline)
                if macro is not None:
                    emit(macro)
            elif directive.name in _CONDITIONAL_DIRECTIVES and directive.start_line not in include_guards:
                emit(
                    _source_finding(
                        "POT08", "warning", path_text, directive.start_line,
                        "conditional compilation requires review",
                        "prefer ordinary language-level configuration where practical",
                    )
                )
    except TimeoutError:
        timed_out = True
    except _FindingLimitReached:
        pass

    ordered = tuple(sorted(findings, key=lambda item: (item.line, item.code)))
    return SourceAnalysisResult(ordered, timed_out=timed_out, truncated=truncated)


def analyze_source(path: pathlib.Path, text: str) -> tuple[Finding, ...]:
    """Return portable findings through the compatibility unbounded interface."""

    return analyze_source_bounded(
        path, text, deadline=None, max_findings=2**63 - 1
    ).findings
