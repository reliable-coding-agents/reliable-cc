"""Behavior tests for the shared Reliable C/C++ hooks."""

from __future__ import annotations

import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
import warnings


ROOT = Path(__file__).resolve().parents[1]
HOOKS = ROOT / "hooks"


def load_hook(name: str):
    """Load one hook module from the package under test."""

    path = HOOKS / f"{name}.py"
    if not path.is_file():
        raise AssertionError(f"missing hook: {path}")
    spec = importlib.util.spec_from_file_location(f"test_{name}", path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load hook: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def pot_finding(
    code: str = "POT01",
    severity: str = "error",
    *,
    line: int = 7,
    gate_eligible: bool = True,
) -> dict[str, object]:
    """Return one complete auditor finding payload."""

    return {
        "code": code,
        "severity": severity,
        "path": "src/main.cpp",
        "line": line,
        "message": f"message for {code}",
        "remediation": "use a bounded alternative",
        "engine": "source",
        "gate_eligible": gate_eligible,
    }


def audit_report(*findings: dict[str, object]) -> dict[str, object]:
    """Return a complete normalized audit report."""

    return {
        "schema_version": 1,
        "mode": "git-diff",
        "configuration": {"format": "json", "fail_on": "none"},
        "findings": list(findings),
        "limitations": [],
        "truncated": False,
        "summary": {
            "errors": sum(item.get("severity") == "error" for item in findings),
            "warnings": sum(item.get("severity") == "warning" for item in findings),
            "info": sum(item.get("severity") == "info" for item in findings),
        },
    }


def limitation(
    code: str = "TOOL_CLANG_MISSING",
    message: str = "Clang driver is unavailable",
    *,
    path: str | None = "src/main.cpp",
) -> dict[str, object]:
    """Return one complete auditor limitation payload."""

    return {"code": code, "message": message, "path": path}


class SessionStartTests(unittest.TestCase):
    def run_hook(self, name: str, payload: dict[str, object]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(HOOKS / name)],
            input=json.dumps(payload),
            check=False,
            capture_output=True,
            text=True,
        )

    def test_session_start_injects_entry_skill(self) -> None:
        result = self.run_hook("session_start.py", {})
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        context = payload["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(
            payload["hookSpecificOutput"]["hookEventName"], "SessionStart"
        )
        self.assertIn("<RELIABLE_CC_POLICY>", context)
        self.assertIn("using-reliable-cc", context)
        self.assertLessEqual(len(context.encode()), 8192 + 1024)

    def test_session_start_missing_skill_fails_open(self) -> None:
        session_start = load_hook("session_start")
        stdout = io.StringIO()
        with (
            mock.patch("pathlib.Path.read_text", side_effect=OSError("missing")),
            mock.patch.object(session_start.sys, "stdout", stdout),
            mock.patch.object(session_start.sys, "stderr", io.StringIO()),
        ):
            self.assertEqual(session_start.main(), 0)
        self.assertEqual(stdout.getvalue(), "")


class StopQualityGateTests(unittest.TestCase):
    def test_review_untrusted_fields_are_one_physical_line_on_both_attempts(self) -> None:
        filename = "safe\nESC\x1b\tC1\x85BIDI\u202e.c"
        source = self.working_tree / filename
        source.write_text("goto out;\n", encoding="utf-8")
        finding = pot_finding()
        finding["path"] = filename
        finding["message"] = "bad\r\nmessage\x00\x7f\x9f\u2066override\u2069"
        report = audit_report(finding)
        report["limitations"] = [
            limitation(message="limited\n\x1b\t\u200fdetail", path=filename)
        ]

        for active in (False, True):
            with self.subTest(active=active):
                payload, _ = self.invoke_stop(
                    {"cwd": str(self.working_tree), "stop_hook_active": active},
                    audit=report,
                )
                rendered = "\n".join(
                    str(payload.get(key, "")) for key in ("reason", "systemMessage")
                )
                finding_lines = [line for line in rendered.split("\n") if line.startswith("- POT01")]
                limitation_lines = [
                    line for line in rendered.split("\n") if line.startswith("- TOOL_CLANG_MISSING")
                ]
                self.assertEqual(len(finding_lines), 1)
                self.assertEqual(len(limitation_lines), 1)
                for line in finding_lines + limitation_lines:
                    self.assertNotRegex(line, r"[\x00-\x1f\x7f-\x9f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]")
                self.assertIn(r"\n", rendered)
                self.assertIn(r"\x1b", rendered)
                self.assertIn(r"\t", rendered)
                self.assertIn(r"\u202e", rendered)

    def setUp(self) -> None:
        self.stop = load_hook("stop_quality_gate")
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.working_tree = Path(self.temporary.name)

    def invoke_stop(
        self,
        hook_input: dict[str, object] | str,
        *,
        audit: dict[str, object] | str | BaseException | None = None,
        gate: str | None = None,
        git_returncode: int = 0,
        git_stdout: str | None = None,
        git_stderr: str = "",
        git_error: BaseException | None = None,
        audit_returncode: int = 0,
        audit_stderr: str = "",
    ) -> tuple[dict[str, object], mock.Mock]:
        stdin_text = hook_input if isinstance(hook_input, str) else json.dumps(hook_input)
        git_result = subprocess.CompletedProcess(
            ["git"],
            git_returncode,
            stdout=("true\n" if git_returncode == 0 else "")
            if git_stdout is None else git_stdout,
            stderr=git_stderr,
        )
        if isinstance(audit, BaseException):
            audit_result = (None, str(audit))
        elif audit_returncode != 0:
            audit_result = (
                None,
                audit_stderr or f"auditor exited {audit_returncode}",
            )
        elif isinstance(audit, str):
            try:
                decoded = json.loads(audit)
            except json.JSONDecodeError:
                audit_result = (None, "auditor returned invalid JSON")
            else:
                audit_result = (
                    (decoded, None)
                    if isinstance(decoded, dict)
                    else (None, "auditor returned a non-object")
                )
        else:
            audit_result = (audit_report() if audit is None else audit, None)

        def run_git(command, *args, **kwargs):
            if git_error is not None:
                raise git_error
            return git_result

        stdout = io.StringIO()
        environment = {} if gate is None else {"RELIABLE_CC_GATE": gate}
        processes = mock.Mock()
        git_run = mock.Mock(side_effect=run_git)
        audit_run = mock.Mock(return_value=audit_result)
        processes.git = git_run
        processes.audit = audit_run
        with (
            mock.patch.object(self.stop.subprocess, "run", git_run),
            mock.patch.object(self.stop, "_run_audit", audit_run),
            mock.patch.object(self.stop.sys, "stdin", io.StringIO(stdin_text)),
            mock.patch.object(self.stop.sys, "stdout", stdout),
            mock.patch.dict(os.environ, environment, clear=True),
        ):
            result = self.stop.main()
        self.assertEqual(result, 0)
        return json.loads(stdout.getvalue()), processes

    def test_default_errors_gate_ignores_warning(self) -> None:
        payload, _ = self.invoke_stop(
            {"cwd": str(self.working_tree), "stop_hook_active": False},
            audit=audit_report(pot_finding("POT02", "warning")),
        )
        self.assertEqual(payload, {})

    def test_all_gate_adds_eligible_pot_warnings(self) -> None:
        payload, _ = self.invoke_stop(
            {"cwd": str(self.working_tree), "stop_hook_active": False},
            gate="all",
            audit=audit_report(pot_finding("POT02", "warning")),
        )
        self.assertEqual(payload["decision"], "block")
        self.assertIn("POT02", payload["reason"])

    def test_off_emits_no_gate_action_without_running_processes(self) -> None:
        payload, run = self.invoke_stop(
            {"cwd": str(self.working_tree), "stop_hook_active": False}, gate="off"
        )
        self.assertEqual(payload, {})
        run.git.assert_not_called()
        run.audit.assert_not_called()

    def test_invalid_gate_value_fails_open_with_message(self) -> None:
        payload, run = self.invoke_stop(
            {"cwd": str(self.working_tree), "stop_hook_active": False}, gate="strict"
        )
        self.assertNotIn("decision", payload)
        self.assertIn("Ignoring invalid RELIABLE_CC_GATE", payload["systemMessage"])
        run.git.assert_not_called()
        run.audit.assert_not_called()

    def test_clean_audit_allows_completion(self) -> None:
        payload, _ = self.invoke_stop(
            {"cwd": str(self.working_tree), "stop_hook_active": False}
        )
        self.assertEqual(payload, {})

    def test_errors_gate_selects_only_eligible_pot_errors(self) -> None:
        report = audit_report(
            pot_finding("POT02", "warning", line=2),
            pot_finding("POT01", "error", line=3),
            pot_finding("POT03", "error", line=4, gate_eligible=False),
        )
        payload, _ = self.invoke_stop(
            {"cwd": str(self.working_tree), "stop_hook_active": False}, audit=report
        )
        self.assertEqual(payload["decision"], "block")
        self.assertIn("POT01", payload["reason"])
        self.assertNotIn("POT02", payload["reason"])
        self.assertNotIn("POT03", payload["reason"])

    def test_solid_and_tool_findings_never_gate(self) -> None:
        report = audit_report(
            pot_finding("SOLID01", "error", gate_eligible=True),
            pot_finding("TOOL_SUPPRESSION", "error", gate_eligible=True),
        )
        payload, _ = self.invoke_stop(
            {"cwd": str(self.working_tree), "stop_hook_active": False},
            gate="all",
            audit=report,
        )
        self.assertEqual(payload, {})

    def test_non_git_directory_is_silent(self) -> None:
        payload, run = self.invoke_stop(
            {"cwd": str(self.working_tree), "stop_hook_active": False},
            git_returncode=128,
            git_stderr="fatal: not a git repository (or any parent): .git",
        )
        self.assertEqual(payload, {})
        run.git.assert_called_once()
        run.audit.assert_not_called()

    def test_git_exit_128_other_than_non_git_is_visible(self) -> None:
        payload, run = self.invoke_stop(
            {"cwd": str(self.working_tree), "stop_hook_active": False},
            git_returncode=128,
            git_stderr="fatal:\ndubious ownership in repository\x00hidden",
        )
        self.assertNotIn("decision", payload)
        self.assertIn("dubious ownership", payload["systemMessage"])
        self.assertNotIn("\n", payload["systemMessage"])
        self.assertNotIn("\x00", payload["systemMessage"])
        run.git.assert_called_once()
        run.audit.assert_not_called()

    def test_git_stdout_false_is_silent(self) -> None:
        payload, _ = self.invoke_stop(
            {"cwd": str(self.working_tree)}, git_stdout="false\n"
        )
        self.assertEqual(payload, {})

    def test_git_nonzero_with_stdout_false_is_visible(self) -> None:
        payload, processes = self.invoke_stop(
            {"cwd": str(self.working_tree)},
            git_returncode=128,
            git_stdout="false\n",
            git_stderr="wrapper configuration rejected",
        )
        self.assertNotIn("decision", payload)
        self.assertIn("systemMessage", payload)
        self.assertIn("wrapper configuration rejected", payload["systemMessage"])
        processes.git.assert_called_once()
        processes.audit.assert_not_called()

    def test_audit_timeout_fails_open(self) -> None:
        payload, run = self.invoke_stop(
            {"cwd": str(self.working_tree), "stop_hook_active": False},
            audit=subprocess.TimeoutExpired(["audit"], 15),
        )
        self.assertNotIn("decision", payload)
        self.assertIn("skipped", payload["systemMessage"])
        run.audit.assert_called_once_with(self.working_tree)

    def test_invalid_audit_json_fails_open(self) -> None:
        payload, _ = self.invoke_stop(
            {"cwd": str(self.working_tree), "stop_hook_active": False},
            audit="not json",
        )
        self.assertNotIn("decision", payload)
        self.assertIn("invalid JSON", payload["systemMessage"])

    def test_malformed_audit_reports_fail_open_without_partial_gate(self) -> None:
        valid = audit_report(pot_finding())
        malformed_reports: list[dict[str, object]] = []
        for key, value in (
            ("schema_version", True),
            ("mode", "git"),
            ("mode", []),
            ("configuration", []),
            ("findings", {}),
            ("limitations", {}),
            ("truncated", 0),
            ("summary", []),
        ):
            report = {**valid, key: value}
            malformed_reports.append(report)
        for missing in (
            "schema_version",
            "mode",
            "configuration",
            "findings",
            "limitations",
            "truncated",
            "summary",
        ):
            report = dict(valid)
            report.pop(missing)
            malformed_reports.append(report)
        bad_findings = (
            {**pot_finding(), "code": "POTATO"},
            {**pot_finding(), "code": "TOOL"},
            {**pot_finding(), "code": "TOOL_lower"},
            {**pot_finding(), "severity": "fatal"},
            {**pot_finding(), "gate_eligible": 1},
            {**pot_finding(), "path": []},
            {**pot_finding(), "message": None},
            {**pot_finding(), "remediation": None},
            {**pot_finding(), "engine": "gcc"},
            {**pot_finding(), "engine": []},
            {**pot_finding(), "line": True},
            {**pot_finding(), "line": 0},
        )
        for missing in (
            "code",
            "severity",
            "path",
            "line",
            "message",
            "remediation",
            "engine",
            "gate_eligible",
        ):
            item = pot_finding()
            item.pop(missing)
            bad_findings += (item,)
        malformed_reports.extend(audit_report(item) for item in bad_findings)
        for bad_limitation in (
            {**limitation(), "code": "CLANG_MISSING"},
            {**limitation(), "code": "TOOL"},
            {**limitation(), "message": []},
            {**limitation(), "path": 3},
        ):
            report = audit_report(pot_finding())
            report["limitations"] = [bad_limitation]
            malformed_reports.append(report)
        for summary in (
            {"errors": 1, "warnings": 0},
            {"errors": 1, "warnings": 0, "info": 0, "total": 1},
            {"errors": True, "warnings": 0, "info": 0},
            {"errors": -1, "warnings": 0, "info": 0},
            {"errors": 0, "warnings": 0, "info": 0},
        ):
            malformed_reports.append({**valid, "summary": summary})

        for report in malformed_reports:
            with self.subTest(report=report):
                payload, _ = self.invoke_stop(
                    {"cwd": str(self.working_tree)}, audit=report
                )
                self.assertNotIn("decision", payload)
                self.assertEqual(
                    payload.get("systemMessage"),
                    "Reliable C/C++ gate skipped: auditor returned malformed report",
                )

    def test_valid_paths_mode_and_nonblocking_extras_are_accepted(self) -> None:
        report = audit_report()
        report["mode"] = "paths"
        report["future_metadata"] = {"version": 2}
        payload, _ = self.invoke_stop({"cwd": str(self.working_tree)}, audit=report)
        self.assertEqual(payload, {})

    def test_malformed_finding_values_fail_open(self) -> None:
        malformed = pot_finding()
        malformed["severity"] = ["error"]
        payload, _ = self.invoke_stop(
            {"cwd": str(self.working_tree), "stop_hook_active": False},
            gate="all",
            audit=audit_report(malformed),
        )
        self.assertNotIn("decision", payload)

    def test_invalid_host_json_fails_open_without_processes(self) -> None:
        payload, run = self.invoke_stop("not json")
        self.assertNotIn("decision", payload)
        self.assertIn("invalid Stop hook input", payload["systemMessage"])
        run.git.assert_not_called()
        run.audit.assert_not_called()

    def test_invalid_host_payload_fields_fail_open_without_processes(self) -> None:
        file_path = self.working_tree / "not-a-directory"
        file_path.write_text("data", encoding="utf-8")
        invalid_payloads = (
            [],
            {},
            {"cwd": ""},
            {"cwd": "   "},
            {"cwd": 7},
            {"cwd": str(self.working_tree / "missing")},
            {"cwd": str(file_path)},
            {"cwd": str(self.working_tree), "stop_hook_active": 1},
        )
        for invalid in invalid_payloads:
            with self.subTest(invalid=invalid):
                payload, run = self.invoke_stop(json.dumps(invalid))
                self.assertNotIn("decision", payload)
                self.assertIn("invalid Stop hook input", payload["systemMessage"])
                run.git.assert_not_called()
                run.audit.assert_not_called()

    def test_missing_stop_hook_active_is_a_first_attempt(self) -> None:
        payload, _ = self.invoke_stop(
            {"cwd": str(self.working_tree)},
            audit=audit_report(pot_finding()),
        )
        self.assertEqual(payload["decision"], "block")

    def test_limitations_are_visible_and_never_gate(self) -> None:
        report = audit_report()
        report["limitations"] = [limitation()]
        clean, _ = self.invoke_stop({"cwd": str(self.working_tree)}, audit=report)
        self.assertNotIn("decision", clean)
        self.assertIn("TOOL_CLANG_MISSING", clean["systemMessage"])

        report = audit_report(pot_finding())
        report["limitations"] = [limitation()]
        blocked, _ = self.invoke_stop({"cwd": str(self.working_tree)}, audit=report)
        self.assertEqual(blocked["decision"], "block")
        self.assertIn("TOOL_CLANG_MISSING", blocked["systemMessage"])

        second, _ = self.invoke_stop(
            {"cwd": str(self.working_tree), "stop_hook_active": True}, audit=report
        )
        self.assertNotIn("decision", second)
        self.assertIn("allowing the turn to complete", second["systemMessage"])
        self.assertIn("TOOL_CLANG_MISSING", second["systemMessage"])

    def test_limitations_display_five_and_remainder(self) -> None:
        report = audit_report()
        report["limitations"] = [
            limitation(f"TOOL_LIMIT_{index}", f"limit {index}", path=None)
            for index in range(1, 8)
        ]
        payload, _ = self.invoke_stop({"cwd": str(self.working_tree)}, audit=report)
        message = payload["systemMessage"]
        for index in range(1, 6):
            self.assertIn(f"TOOL_LIMIT_{index}", message)
        self.assertNotIn("TOOL_LIMIT_6", message)
        self.assertNotIn("TOOL_LIMIT_7", message)
        self.assertIn("and 2 more limitation(s)", message)

    def test_audit_stdout_and_stderr_are_bounded_while_process_runs(self) -> None:
        script = self.working_tree / "oversized_auditor.py"
        with warnings.catch_warnings(record=True) as captured:
            warnings.simplefilter("always")
            for descriptor in (1, 2):
                with self.subTest(descriptor=descriptor):
                    script.write_text(
                        "import os, time\n"
                        f"os.write({descriptor}, b'x' * (2 * 1024 * 1024 + 1))\n"
                        "time.sleep(5)\n",
                        encoding="utf-8",
                    )
                    with (
                        mock.patch.object(self.stop, "AUDITOR", script),
                        mock.patch.object(self.stop, "AUDIT_TIMEOUT_SECONDS", 0.25),
                    ):
                        payload, error = self.stop._run_audit(self.working_tree)
                    self.assertIsNone(payload)
                    self.assertIn("output exceeds", error)
        self.assertFalse(
            [item for item in captured if issubclass(item.category, ResourceWarning)]
        )

    @unittest.skipUnless(os.name == "posix", "process-group cleanup is POSIX-specific")
    def test_audit_parent_exit_cleans_long_lived_inherited_pipe_child(self) -> None:
        child_ready = self.working_tree / "child-ready"
        process_record = self.working_tree / "audit-process.json"
        script = self.working_tree / "inherited_pipe_auditor.py"
        script.write_text(
            "import json, os, pathlib, subprocess, sys, time\n"
            "child = subprocess.Popen([sys.executable, '-c', "
            + repr(
                "import pathlib, signal, time; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                f"pathlib.Path({str(child_ready)!r}).write_text('ready'); "
                "time.sleep(10)"
            )
            + "])\n"
            f"ready = pathlib.Path({str(child_ready)!r})\n"
            "while not ready.exists(): time.sleep(0.005)\n"
            f"pathlib.Path({str(process_record)!r}).write_text("
            "json.dumps({'child': child.pid, 'group': os.getpgrp()}))\n",
            encoding="utf-8",
        )
        probe_source = (
            "import importlib.util, json, pathlib, sys\n"
            "spec = importlib.util.spec_from_file_location('stop_probe', sys.argv[1])\n"
            "module = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(module)\n"
            "module.AUDITOR = pathlib.Path(sys.argv[2])\n"
            "payload, error = module._run_audit(pathlib.Path(sys.argv[3]))\n"
            "print(json.dumps({'payload': payload, 'error': error}))\n"
        )
        probe = subprocess.Popen(
            [
                sys.executable,
                "-c",
                probe_source,
                str(HOOKS / "stop_quality_gate.py"),
                str(script),
                str(self.working_tree),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        started = time.monotonic()
        timed_out = False
        stdout = ""
        stderr = ""
        try:
            try:
                stdout, stderr = probe.communicate(timeout=1.0)
            except subprocess.TimeoutExpired:
                timed_out = True

            record_deadline = time.monotonic() + 0.5
            while not process_record.exists() and time.monotonic() < record_deadline:
                time.sleep(0.005)
            record = json.loads(process_record.read_text(encoding="utf-8"))

            child_stopped = False
            if not timed_out:
                stop_deadline = time.monotonic() + 0.5
                while time.monotonic() < stop_deadline:
                    try:
                        os.kill(record["child"], 0)
                    except ProcessLookupError:
                        child_stopped = True
                        break
                    process_stat = Path(f"/proc/{record['child']}/stat")
                    if process_stat.exists():
                        try:
                            if process_stat.read_text().split()[2] == "Z":
                                child_stopped = True
                                break
                        except (FileNotFoundError, OSError, IndexError):
                            child_stopped = True
                            break
                    time.sleep(0.005)
        finally:
            if process_record.exists():
                record = json.loads(process_record.read_text(encoding="utf-8"))
                try:
                    os.killpg(record["group"], signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if probe.poll() is None:
                os.killpg(probe.pid, signal.SIGKILL)
                stdout, stderr = probe.communicate(timeout=1.0)

        self.assertFalse(timed_out, "capture hung on inherited pipes")
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual(probe.returncode, 0, stderr)
        self.assertIn("invalid JSON", json.loads(stdout)["error"])
        self.assertTrue(child_stopped, "auditor descendant remained live")

    def test_line_rendering_is_field_bounded(self) -> None:
        finding = pot_finding(line=int("9" * 1_001))
        payload, _ = self.invoke_stop(
            {"cwd": str(self.working_tree)}, audit=audit_report(finding)
        )
        self.assertEqual(payload["decision"], "block")
        self.assertNotIn("9" * 1_001, payload["reason"])
        self.assertIn("9" * 1_000, payload["reason"])

    def test_second_attempt_preserves_all_sections_under_exact_bounds(self) -> None:
        long_value = "X" * 4_000
        findings = tuple(
            {
                **pot_finding(f"POT{index:02d}", line=index),
                "path": f"src/{long_value}/file-{index:02d}.cpp",
                "message": f"action {index:02d}: replace the unbounded operation",
            }
            for index in range(1, 15)
        )
        report = audit_report(*findings)
        report["limitations"] = [
            limitation(
                f"TOOL_LIMIT_{index}_" + long_value,
                long_value,
                path=long_value,
            )
            for index in range(1, 6)
        ]
        payload, _ = self.invoke_stop(
            {"cwd": str(self.working_tree), "stop_hook_active": True}, audit=report
        )
        message = payload["systemMessage"]
        self.assertTrue(message.startswith("Reliable C/C++ findings remain"))
        for index in range(1, 6):
            self.assertIn(f"TOOL_LIMIT_{index}_", message)
        for index in range(1, 13):
            self.assertIn(f"POT{index:02d}", message)
            self.assertIn(f"action {index:02d}", message)
            self.assertIn(f":{index}:", message)
            self.assertIn(f"file-{index:02d}.cpp", message)
        self.assertIn("and 2 more finding(s)", message)
        self.assertIn("Run: python3", message)
        self.assertLessEqual(len(message), 16_384)

    def test_interpolated_fields_and_final_messages_are_bounded(self) -> None:
        long_value = "x" * 4_000
        findings = tuple(
            {
                **pot_finding(f"POT{index:02d}", line=index),
                "path": long_value,
                "message": long_value,
            }
            for index in range(1, 15)
        )
        report = audit_report(*findings)
        report["limitations"] = [
            limitation("TOOL_" + long_value.upper(), long_value, path=long_value)
            for _ in range(5)
        ]
        blocked, _ = self.invoke_stop({"cwd": str(self.working_tree)}, audit=report)
        self.assertLessEqual(len(blocked["reason"]), 16_384)
        self.assertLessEqual(len(blocked["systemMessage"]), 16_384)
        self.assertNotIn("x" * 1_001, blocked["reason"])
        self.assertNotIn("x" * 1_001, blocked["systemMessage"])
        self.assertIn("and 2 more finding(s)", blocked["reason"])

        second, _ = self.invoke_stop(
            {"cwd": str(self.working_tree), "stop_hook_active": True}, audit=report
        )
        self.assertLessEqual(len(second["systemMessage"]), 16_384)
        self.assertIn("TOOL_", second["systemMessage"])

    def test_first_completion_attempt_blocks(self) -> None:
        payload, run = self.invoke_stop(
            {"cwd": str(self.working_tree), "stop_hook_active": False},
            audit=audit_report(pot_finding()),
        )
        self.assertEqual(payload["decision"], "block")
        self.assertIn("Reliable C/C++", payload["reason"])
        run.audit.assert_called_once_with(self.working_tree)

    def test_second_completion_attempt_always_proceeds(self) -> None:
        payload, _ = self.invoke_stop(
            {"cwd": str(self.working_tree), "stop_hook_active": True},
            audit=audit_report(pot_finding()),
        )
        self.assertNotIn("decision", payload)
        self.assertIn("allowing the turn to complete", payload["systemMessage"])

    def test_reason_displays_twelve_findings_and_remainder_count(self) -> None:
        findings = tuple(
            pot_finding(f"POT{index:02d}", line=index) for index in range(1, 15)
        )
        payload, _ = self.invoke_stop(
            {"cwd": str(self.working_tree), "stop_hook_active": False},
            audit=audit_report(*findings),
        )
        reason = str(payload["reason"])
        for index in range(1, 13):
            self.assertIn(f"POT{index:02d}", reason)
        self.assertNotIn("POT13", reason)
        self.assertNotIn("POT14", reason)
        self.assertIn("and 2 more finding(s)", reason)

    def test_git_and_auditor_os_errors_fail_open(self) -> None:
        for arguments in (
            {"git_error": OSError("git unavailable")},
            {"audit": OSError("audit unavailable")},
        ):
            with self.subTest(arguments=arguments):
                payload, _ = self.invoke_stop(
                    {"cwd": str(self.working_tree)}, **arguments
                )
                self.assertNotIn("decision", payload)
                self.assertIn("skipped", payload["systemMessage"])


class HookRegistrationTests(unittest.TestCase):
    def test_hooks_register_shared_host_commands_and_timeouts(self) -> None:
        path = HOOKS / "hooks.json"
        self.assertTrue(path.is_file(), f"missing hook registration: {path}")
        payload = json.loads(path.read_text(encoding="utf-8"))["hooks"]
        session = payload["SessionStart"][0]
        stop = payload["Stop"][0]["hooks"][0]
        self.assertEqual(session["matcher"], "startup|resume|clear|compact")
        self.assertEqual(
            session["hooks"][0]["command"],
            'python3 "${CLAUDE_PLUGIN_ROOT}/hooks/session_start.py"',
        )
        self.assertEqual(
            stop["command"],
            'python3 "${CLAUDE_PLUGIN_ROOT}/hooks/stop_quality_gate.py"',
        )
        self.assertEqual(stop["timeout"], 20)
        self.assertNotIn(".cache", json.dumps(payload))


if __name__ == "__main__":
    unittest.main()
