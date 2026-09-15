#!/usr/bin/env python3
"""Run the Reliable C/C++ changed-line gate once before completion."""

from __future__ import annotations

import json
import os
import pathlib
import re
import signal
import subprocess
import sys
import threading
import time
from typing import Any


PLUGIN_ROOT = pathlib.Path(__file__).resolve().parent.parent
AUDITOR = PLUGIN_ROOT / "skills" / "reviewing-cc-quality" / "scripts" / "audit_cc.py"
GATE_ENV_VAR = "RELIABLE_CC_GATE"
VALID_GATE_LEVELS = frozenset({"all", "errors", "off"})
DEFAULT_GATE_LEVEL = "errors"
MAX_REPORTED_FINDINGS = 12
MAX_REPORTED_LIMITATIONS = 5
AUDIT_TIMEOUT_SECONDS = 15
GIT_TIMEOUT_SECONDS = 3
MAX_AUDIT_OUTPUT_BYTES = 2 * 1024 * 1024
MAX_FIELD_CHARS = 1_000
MAX_SYSTEM_MESSAGE_CHARS = 16_384
SECOND_ATTEMPT_LIMITATIONS_CHARS = 6_000
AUDIT_TERMINATION_GRACE_SECONDS = 0.1
AUDIT_FORCE_KILL_SECONDS = 0.5

_FINDING_CODE = re.compile(r"(?:POT|SOLID)\d{2}|TOOL_[A-Z0-9_]+")
_LIMITATION_CODE = re.compile(r"TOOL_[A-Z0-9_]+")
_VALID_SEVERITIES = frozenset({"error", "warning", "info"})
_VALID_ENGINES = frozenset({"source", "clang"})
_VALID_MODES = frozenset({"paths", "git-diff"})
_SUMMARY_KEYS = frozenset({"errors", "warnings", "info"})
_BIDI_CONTROLS = frozenset({
    0x061C, 0x200E, 0x200F, *range(0x202A, 0x202F), *range(0x2066, 0x206A),
})


def _read_input() -> dict[str, Any] | None:
    try:
        value = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError, UnicodeError):
        return None
    return value if isinstance(value, dict) else None


def _emit(value: dict[str, Any]) -> int:
    bounded = dict(value)
    for key in ("reason", "systemMessage"):
        if isinstance(bounded.get(key), str):
            bounded[key] = _bounded_final_message(bounded[key])
    try:
        json.dump(bounded, sys.stdout)
        sys.stdout.write("\n")
    except OSError:
        pass
    return 0


def _one_line(value: str) -> str:
    """Encode controls so one untrusted field cannot create or spoof a line."""

    pieces: list[str] = []
    named = {"\t": r"\t", "\n": r"\n", "\r": r"\r"}
    for character in value:
        point = ord(character)
        if character in named:
            pieces.append(named[character])
        elif point < 0x20 or 0x7F <= point <= 0x9F:
            pieces.append(f"\\x{point:02x}")
        elif point in _BIDI_CONTROLS:
            pieces.append(f"\\u{point:04x}")
        else:
            pieces.append(character)
    return "".join(pieces)


def _field(value: str) -> str:
    return _one_line(value)[:MAX_FIELD_CHARS]


def _middle_field(value: str, max_chars: int) -> str:
    value = _one_line(value)
    limit = min(MAX_FIELD_CHARS, max(0, max_chars))
    if len(value) <= limit:
        return value
    marker = "..."
    if limit <= len(marker) + 1:
        return value[:limit]
    left_chars = (limit - len(marker) + 1) // 2
    right_chars = limit - len(marker) - left_chars
    return value[:left_chars] + marker + value[-right_chars:]


def _bounded_final_message(value: str) -> str:
    if len(value) <= MAX_SYSTEM_MESSAGE_CHARS:
        return value
    marker = "\n... output truncated ...\n"
    head_chars = (MAX_SYSTEM_MESSAGE_CHARS - len(marker)) // 2
    tail_chars = MAX_SYSTEM_MESSAGE_CHARS - len(marker) - head_chars
    return value[:head_chars] + marker + value[-tail_chars:]


def _sanitized(value: str) -> str:
    """Collapse process diagnostics to one bounded, non-injectable line."""

    return _field(" ".join(value.replace("\x00", " ").split()))


def _validated_host_input(
    value: dict[str, Any] | None,
) -> tuple[pathlib.Path | None, bool, str | None]:
    if value is None:
        return None, False, "invalid Stop hook input: expected a JSON object"
    cwd_value = value.get("cwd")
    if not isinstance(cwd_value, str) or not cwd_value.strip():
        return None, False, "invalid Stop hook input: cwd must be a nonempty string"
    active = value.get("stop_hook_active", False)
    if type(active) is not bool:
        return None, False, "invalid Stop hook input: stop_hook_active must be a bool"
    try:
        cwd = pathlib.Path(cwd_value)
        if not cwd.is_dir():
            return None, False, "invalid Stop hook input: cwd must name an existing directory"
    except (OSError, ValueError):
        return None, False, "invalid Stop hook input: cwd must name an existing directory"
    return cwd, active, None


def _inside_git_work_tree(cwd: pathlib.Path) -> tuple[bool | None, str | None]:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        return None, f"Git preflight failed: {_sanitized(str(error))}"
    stdout = result.stdout.strip()
    stderr = result.stderr.strip()
    if (result.returncode == 0 and stdout == "false") or (
        "not a git repository" in stderr.lower()
    ):
        return False, None
    if result.returncode == 0 and stdout == "true":
        return True, None
    detail = _sanitized(stderr) or f"git exited {result.returncode}"
    return None, f"Git preflight failed: {detail}"


def _signal_audit_process(
    process: subprocess.Popen[bytes], *, force: bool
) -> bool:
    """Stop the audit process group, with a direct-process fallback."""

    try:
        if os.name == "posix":
            signal_number = signal.SIGKILL if force else signal.SIGTERM
            os.killpg(process.pid, signal_number)
            return True
        if process.poll() is not None:
            return False
        if force:
            process.kill()
        else:
            process.terminate()
        return True
    except (OSError, ValueError):
        pass
    if process.poll() is not None:
        return False
    try:
        process.kill() if force else process.terminate()
    except (OSError, ValueError):
        return False
    return True


def _wait_for_audit_process(
    process: subprocess.Popen[bytes], timeout: float
) -> bool:
    try:
        process.wait(timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return True


def _join_audit_readers(
    readers: tuple[threading.Thread, ...], timeout: float
) -> bool:
    deadline = time.monotonic() + timeout
    for reader in readers:
        reader.join(max(0.0, deadline - time.monotonic()))
    return not any(reader.is_alive() for reader in readers)


def _stop_and_reap(process: subprocess.Popen[bytes]) -> None:
    signaled_group = _signal_audit_process(process, force=False)
    reaped = _wait_for_audit_process(process, AUDIT_TERMINATION_GRACE_SECONDS)
    if (os.name == "posix" and signaled_group) or not reaped:
        _signal_audit_process(process, force=True)
    if not reaped:
        _wait_for_audit_process(process, AUDIT_FORCE_KILL_SECONDS)


def _close_audit_pipes(process: subprocess.Popen[bytes]) -> None:
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass


def _bounded_pipe_reader(
    stream,
    output: bytearray,
    oversized: threading.Event,
    failures: list[str],
) -> None:
    try:
        while not oversized.is_set():
            remaining = MAX_AUDIT_OUTPUT_BYTES - len(output)
            chunk = stream.read(min(64 * 1024, remaining + 1))
            if not chunk:
                return
            output.extend(chunk[:remaining])
            if len(chunk) > remaining:
                oversized.set()
                return
    except OSError as error:
        if not oversized.is_set():
            failures.append(str(error))


def _capture_audit(
    cwd: pathlib.Path,
) -> tuple[int | None, bytes, bytes, str | None]:
    command = [
        sys.executable,
        str(AUDITOR),
        "--git-diff",
        "--format",
        "json",
        "--fail-on",
        "none",
    ]
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=os.name == "posix",
        )
    except (OSError, ValueError) as error:
        return None, b"", b"", _sanitized(str(error))
    if process.stdout is None or process.stderr is None:
        _stop_and_reap(process)
        _close_audit_pipes(process)
        return None, b"", b"", "auditor pipes are unavailable"

    stdout = bytearray()
    stderr = bytearray()
    oversized = threading.Event()
    failures: list[str] = []
    readers = (
        threading.Thread(
            target=_bounded_pipe_reader,
            args=(process.stdout, stdout, oversized, failures),
            daemon=True,
        ),
        threading.Thread(
            target=_bounded_pipe_reader,
            args=(process.stderr, stderr, oversized, failures),
            daemon=True,
        ),
    )
    for reader in readers:
        reader.start()

    deadline = time.monotonic() + AUDIT_TIMEOUT_SECONDS
    timed_out = False
    while process.poll() is None and not oversized.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
            break
        oversized.wait(min(0.05, remaining))

    signaled_group = _signal_audit_process(process, force=False)
    reaped = _wait_for_audit_process(process, AUDIT_TERMINATION_GRACE_SECONDS)
    readers_finished = _join_audit_readers(
        readers, AUDIT_TERMINATION_GRACE_SECONDS
    )
    if (os.name == "posix" and signaled_group) or not reaped or not readers_finished:
        _signal_audit_process(process, force=True)
    if not reaped:
        reaped = _wait_for_audit_process(process, AUDIT_FORCE_KILL_SECONDS)
    if not readers_finished:
        readers_finished = _join_audit_readers(readers, AUDIT_FORCE_KILL_SECONDS)

    # BufferedReader.close() waits for an active read lock. Leave daemon-owned
    # handles alone on a fallback platform if an inherited pipe cannot be
    # interrupted; the hook must still return within its inner deadline.
    if readers_finished:
        _close_audit_pipes(process)

    if oversized.is_set():
        return None, bytes(stdout), bytes(stderr), (
            "auditor output exceeds the 2 MiB capture limit"
        )
    if timed_out:
        return None, bytes(stdout), bytes(stderr), (
            f"auditor exceeded the {AUDIT_TIMEOUT_SECONDS}-second timeout"
        )
    if not reaped:
        return None, bytes(stdout), bytes(stderr), "auditor process could not be reaped"
    if failures or not readers_finished:
        detail = _sanitized(failures[0]) if failures else "reader did not finish"
        return None, bytes(stdout), bytes(stderr), f"auditor capture failed: {detail}"
    return process.returncode, bytes(stdout), bytes(stderr), None


def _run_audit(cwd: pathlib.Path) -> tuple[dict[str, Any] | None, str | None]:
    returncode, stdout_bytes, stderr_bytes, capture_error = _capture_audit(cwd)
    if capture_error:
        return None, capture_error
    try:
        stdout = stdout_bytes.decode("utf-8")
        stderr = stderr_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return None, "auditor returned invalid UTF-8"
    if returncode != 0:
        detail = _sanitized(stderr) or f"auditor exited {returncode}"
        return None, detail
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return None, "auditor returned invalid JSON"
    if not isinstance(payload, dict):
        return None, "auditor returned a non-object"
    return payload, None


def _valid_finding(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    code = value.get("code")
    line = value.get("line")
    return (
        isinstance(code, str)
        and _FINDING_CODE.fullmatch(code) is not None
        and isinstance(value.get("severity"), str)
        and value["severity"] in _VALID_SEVERITIES
        and type(value.get("gate_eligible")) is bool
        and isinstance(value.get("path"), str)
        and isinstance(value.get("message"), str)
        and isinstance(value.get("remediation"), str)
        and isinstance(value.get("engine"), str)
        and value.get("engine") in _VALID_ENGINES
        and type(line) is int
        and line > 0
    )


def _valid_limitation(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    code = value.get("code")
    path = value.get("path")
    return (
        isinstance(code, str)
        and _LIMITATION_CODE.fullmatch(code) is not None
        and isinstance(value.get("message"), str)
        and (path is None or isinstance(path, str))
    )


def _validated_report(
    payload: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]] | None:
    findings = payload.get("findings")
    limitations = payload.get("limitations")
    summary = payload.get("summary")
    if (
        type(payload.get("schema_version")) is not int
        or payload["schema_version"] != 1
        or not isinstance(payload.get("mode"), str)
        or payload.get("mode") not in _VALID_MODES
        or not isinstance(payload.get("configuration"), dict)
        or not isinstance(findings, list)
        or not isinstance(limitations, list)
        or type(payload.get("truncated")) is not bool
        or not isinstance(summary, dict)
        or not all(_valid_finding(item) for item in findings)
        or not all(_valid_limitation(item) for item in limitations)
    ):
        return None
    if set(summary) != _SUMMARY_KEYS or any(
        type(summary[key]) is not int or summary[key] < 0 for key in _SUMMARY_KEYS
    ):
        return None
    expected_summary = {
        "errors": sum(item["severity"] == "error" for item in findings),
        "warnings": sum(item["severity"] == "warning" for item in findings),
        "info": sum(item["severity"] == "info" for item in findings),
    }
    if summary != expected_summary:
        return None
    return findings, limitations


def _selected_findings(
    findings: list[dict[str, Any]], gate_level: str
) -> list[dict[str, Any]]:
    eligible = [
        item
        for item in findings
        if item["gate_eligible"] and str(item["code"]).startswith("POT")
    ]
    if gate_level == "errors":
        return [item for item in eligible if item.get("severity") == "error"]
    return [
        item
        for item in eligible
        if str(item.get("severity", "")) in {"error", "warning"}
    ]


def _bounded_section(
    headers: list[str], entries: list[str], tail: list[str], max_chars: int
) -> str:
    lines = headers + entries + tail
    complete = "\n".join(lines)
    if len(complete) <= max_chars:
        return complete
    fixed_lines = headers + tail
    fixed_chars = sum(len(line) for line in fixed_lines) + max(0, len(lines) - 1)
    available = max(0, max_chars - fixed_chars)
    entry_chars, extra = divmod(available, max(1, len(entries)))
    clipped = [
        line[: entry_chars + int(index < extra)]
        for index, line in enumerate(entries)
    ]
    return "\n".join(headers + clipped + tail)[:max_chars]


def _section_entry_budgets(
    headers: list[str], tail: list[str], count: int, max_chars: int
) -> list[int]:
    if count == 0:
        return []
    line_count = len(headers) + count + len(tail)
    fixed_chars = sum(len(line) for line in headers + tail) + line_count - 1
    available = max(0, max_chars - fixed_chars)
    per_entry, extra = divmod(available, count)
    return [per_entry + int(index < extra) for index in range(count)]


def _finding_entry(finding: dict[str, Any], max_chars: int) -> str:
    code = _field(finding["code"])
    severity = _field(finding["severity"])
    prefix = f"- {code} {severity} at "
    line_room = max(1, max_chars - len(prefix) - len(":: ") - 2)
    line = _middle_field(_field(str(finding["line"])), line_room)
    location_separator = f":{line}: "
    variable_chars = max(2, max_chars - len(prefix) - len(location_separator))
    path_chars = max(1, min(MAX_FIELD_CHARS, variable_chars // 2))
    path = _middle_field(finding["path"] or ".", path_chars)
    message_chars = max(1, variable_chars - len(path))
    message = (_field(finding["message"]) or "(no message)")[:message_chars]
    return (prefix + path + location_separator + message)[:max_chars]


def _reason(
    findings: list[dict[str, Any]], max_chars: int = MAX_SYSTEM_MESSAGE_CHARS
) -> str:
    headers = [
        "The Reliable C/C++ gate found changed C/C++ code that needs another pass.",
        "Fix confirmed Power of Ten findings, or add a narrow suppression with a "
        "rationale only for a proven false positive, then rerun the audit.",
    ]
    tail = []
    remainder = len(findings) - MAX_REPORTED_FINDINGS
    if remainder > 0:
        tail.append(f"- ... and {remainder} more finding(s)")
    tail.append(f"Run: python3 {_field(str(AUDITOR))} --git-diff")
    displayed = findings[:MAX_REPORTED_FINDINGS]
    budgets = _section_entry_budgets(headers, tail, len(displayed), max_chars)
    entries = [
        _finding_entry(finding, budget)
        for finding, budget in zip(displayed, budgets)
    ]
    return _bounded_section(headers, entries, tail, max_chars)


def _limitations_message(
    limitations: list[dict[str, Any]], max_chars: int = MAX_SYSTEM_MESSAGE_CHARS
) -> str:
    if not limitations:
        return ""
    headers = ["Reliable C/C++ audit limitations (non-blocking):"]
    entries = []
    for limitation in limitations[:MAX_REPORTED_LIMITATIONS]:
        code = _field(limitation["code"])
        message = _field(limitation["message"])
        path = limitation.get("path")
        location = f" ({_field(path)})" if isinstance(path, str) else ""
        entries.append(f"- {code}{location}: {message}")
    tail = []
    remainder = len(limitations) - MAX_REPORTED_LIMITATIONS
    if remainder > 0:
        tail.append(f"- ... and {remainder} more limitation(s)")
    return _bounded_section(headers, entries, tail, max_chars)


def _skipped(error: str | None) -> dict[str, str]:
    detail = _sanitized(error or "unknown infrastructure failure")
    return {"systemMessage": f"Reliable C/C++ gate skipped: {detail}"}


def main() -> int:
    """Read a Stop payload and emit a one-pass, fail-open gate decision."""

    hook_input = _read_input()
    cwd, stop_hook_active, input_error = _validated_host_input(hook_input)
    if input_error or cwd is None:
        return _emit(_skipped(input_error))

    requested_gate = os.environ.get(GATE_ENV_VAR, DEFAULT_GATE_LEVEL)
    gate_level = requested_gate.lower()
    if gate_level not in VALID_GATE_LEVELS:
        return _emit(
            {
                "systemMessage": (
                    f"Ignoring invalid {GATE_ENV_VAR}={_field(requested_gate)!r}; "
                    "the Reliable C/C++ gate is disabled for this completion."
                )
            }
        )
    if gate_level == "off":
        return _emit({})

    inside_git, git_error = _inside_git_work_tree(cwd)
    if inside_git is False:
        return _emit({})
    if inside_git is None:
        return _emit(_skipped(git_error))

    payload, audit_error = _run_audit(cwd)
    if audit_error or payload is None:
        return _emit(_skipped(audit_error))
    validated = _validated_report(payload)
    if validated is None:
        return _emit(_skipped("auditor returned malformed report"))
    audit_findings, limitations = validated
    findings = _selected_findings(audit_findings, gate_level)
    limitation_message = _limitations_message(limitations)
    if not findings:
        return _emit({"systemMessage": limitation_message} if limitation_message else {})

    reason = _reason(findings)
    if stop_hook_active:
        completion = (
            "Reliable C/C++ findings remain after one correction pass; "
            "allowing the turn to complete to avoid a hook loop."
        )
        bounded_limitations = _limitations_message(
            limitations, SECOND_ATTEMPT_LIMITATIONS_CHARS
        )
        separators = 1 + int(bool(bounded_limitations))
        reason_budget = (
            MAX_SYSTEM_MESSAGE_CHARS
            - len(completion)
            - len(bounded_limitations)
            - separators
        )
        bounded_reason = _reason(findings, reason_budget)
        parts = [completion]
        if bounded_limitations:
            parts.append(bounded_limitations)
        parts.append(bounded_reason)
        return _emit({"systemMessage": "\n".join(parts)})
    result = {"decision": "block", "reason": reason}
    if limitation_message:
        result["systemMessage"] = limitation_message
    return _emit(result)


if __name__ == "__main__":
    raise SystemExit(main())
