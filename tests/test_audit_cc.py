"""Integration tests for the Reliable C/C++ audit command."""

from __future__ import annotations

import builtins
import io
import json
import os
import signal
import subprocess
import sys
import time
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "reviewing-cc-quality" / "scripts"
CLI = SCRIPTS / "audit_cc.py"
sys.path.insert(0, str(SCRIPTS))

import audit_cc  # noqa: E402
import clang_analysis as clang  # noqa: E402
import source_checks  # noqa: E402
from audit_cc import (  # noqa: E402
    AuditInputError,
    discover_paths,
    exit_status,
    git_changed_lines,
    render_text,
    run_audit,
)
from quality_model import AuditReport, Finding, Limitation  # noqa: E402
from tests.test_clang_analysis import FakeClangTests, call, function, node  # noqa: E402


def finding(
    code: str = "POT02",
    severity: str = "warning",
    *,
    path: str = "main.c",
    line: int = 2,
    gate_eligible: bool = True,
) -> Finding:
    return Finding(
        code=code,
        severity=severity,
        path=path,
        line=line,
        message="message",
        remediation="remediation",
        engine="source",
        gate_eligible=gate_eligible,
    )


class TemporaryDirectoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    @staticmethod
    def write(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


class DiscoveryAndLimitTests(TemporaryDirectoryTests):
    def invoke_main(
        self,
        arguments: list[str],
        *,
        budget_seconds: float,
        environment: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, object], str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        variables = {"RELIABLE_CC_CLANG": "off", **(environment or {})}
        with (
            mock.patch.object(Path, "cwd", return_value=self.root),
            mock.patch.object(audit_cc.sys, "stdout", stdout),
            mock.patch.object(audit_cc.sys, "stderr", stderr),
            mock.patch.dict(os.environ, variables, clear=True),
        ):
            status = audit_cc.main(
                [*arguments, "--format", "json", "--fail-on", "none"],
                budget_seconds=budget_seconds,
            )
        return status, json.loads(stdout.getvalue()), stderr.getvalue()

    def test_cli_budget_includes_recursive_path_discovery(self) -> None:
        self.write(self.root / "src/main.c", "goto out;\n")
        original = Path.rglob

        def slow_rglob(path: Path, pattern: str):
            time.sleep(0.03)
            yield from original(path, pattern)

        with mock.patch.object(Path, "rglob", slow_rglob):
            status, payload, stderr = self.invoke_main(
                [str(self.root / "src")], budget_seconds=0.01
            )

        self.assertEqual(status, 0)
        self.assertEqual(stderr, "")
        self.assertTrue(payload["truncated"])
        self.assertEqual(payload["limitations"][0]["code"], "TOOL_TIMEOUT")
        self.assertEqual(payload["findings"], [])

    def test_missing_required_path_remains_status_two_when_budget_elapsed(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        missing = self.root / "missing.c"
        with (
            mock.patch.object(Path, "cwd", return_value=self.root),
            mock.patch.object(audit_cc.sys, "stdout", stdout),
            mock.patch.object(audit_cc.sys, "stderr", stderr),
            mock.patch.dict(os.environ, {"RELIABLE_CC_CLANG": "off"}, clear=True),
        ):
            status = audit_cc.main(
                [str(missing), "--format", "json"], budget_seconds=0.0
            )

        self.assertEqual(status, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("missing.c", stderr.getvalue())

    def test_missing_required_database_remains_status_two_when_budget_elapsed(self) -> None:
        source = self.root / "main.cpp"
        self.write(source, "int main() { return 0; }\n")
        stdout = io.StringIO()
        stderr = io.StringIO()
        missing = self.root / "missing-compile-commands.json"
        with (
            mock.patch.object(Path, "cwd", return_value=self.root),
            mock.patch.object(audit_cc.sys, "stdout", stdout),
            mock.patch.object(audit_cc.sys, "stderr", stderr),
            mock.patch.dict(
                os.environ,
                {
                    "RELIABLE_CC_CLANG": "auto",
                    "RELIABLE_CC_COMPILE_COMMANDS": str(missing),
                },
                clear=True,
            ),
        ):
            status = audit_cc.main(
                [str(source), "--format", "json"], budget_seconds=0.0
            )

        self.assertEqual(status, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("missing-compile-commands.json", stderr.getvalue())

    def test_cli_budget_includes_git_discovery(self) -> None:
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        subprocess.run(
            ["git", "config", "user.email", "tests@example.invalid"],
            cwd=self.root,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Reliable C/C++ Tests"],
            cwd=self.root,
            check=True,
        )
        self.write(self.root / "main.c", "return;\n")
        subprocess.run(["git", "add", "main.c"], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=self.root, check=True)
        self.write(self.root / "main.c", "goto out;\n")
        original = audit_cc._git_changed_lines

        def slow_discovery(root: Path, **kwargs):
            time.sleep(0.03)
            return original(root, **kwargs)

        with mock.patch.object(audit_cc, "_git_changed_lines", slow_discovery):
            status, payload, stderr = self.invoke_main(
                ["--git-diff"], budget_seconds=0.01
            )

        self.assertEqual(status, 0)
        self.assertEqual(stderr, "")
        self.assertTrue(payload["truncated"])
        self.assertEqual(payload["limitations"][0]["code"], "TOOL_TIMEOUT")
        self.assertEqual(payload["findings"], [])

    def test_cli_budget_includes_explicit_database_validation(self) -> None:
        source = self.root / "main.cpp"
        self.write(source, "int main() { return 0; }\n")
        database = self.root / "compile_commands.json"
        database.write_text(
            json.dumps(
                [{
                    "directory": str(self.root),
                    "file": str(source),
                    "arguments": ["c++", str(source)],
                }]
            ),
            encoding="utf-8",
        )
        original = audit_cc.validate_compilation_database

        def slow_validation(path: Path, **kwargs):
            time.sleep(0.03)
            return original(path, **kwargs)

        with mock.patch.object(
            audit_cc, "validate_compilation_database", slow_validation
        ):
            status, payload, stderr = self.invoke_main(
                [str(source)],
                budget_seconds=0.01,
                environment={
                    "RELIABLE_CC_CLANG": "auto",
                    "RELIABLE_CC_COMPILE_COMMANDS": str(database),
                },
            )

        self.assertEqual(status, 0)
        self.assertEqual(stderr, "")
        self.assertTrue(payload["truncated"])
        self.assertEqual(payload["limitations"][0]["code"], "TOOL_TIMEOUT")
        self.assertEqual(payload["findings"], [])

    def test_recursive_discovery_retains_only_file_limit_plus_sentinel(self) -> None:
        for number in reversed(range(503)):
            self.write(self.root / f"{number:03}.c", "")

        paths = discover_paths([self.root])

        self.assertEqual(len(paths), 501)
        self.assertEqual(paths[0].name, "000.c")
        self.assertEqual(paths[-1].name, "500.c")

    def test_git_output_cap_is_a_visible_nonblocking_limitation(self) -> None:
        binary = self.root / "bin"
        binary.mkdir()
        git = binary / "git"
        git.write_text(
            f"#!{sys.executable}\n"
            "import pathlib\n"
            "import sys\n"
            f"root = {str(self.root)!r}\n"
            "arguments = sys.argv[1:]\n"
            "if arguments[:2] == ['rev-parse', '--show-toplevel']:\n"
            "    print(root)\n"
            "elif arguments and arguments[0] == 'diff':\n"
            "    sys.stdout.write('x' * 1024)\n",
            encoding="utf-8",
        )
        git.chmod(0o755)
        path = str(binary) + os.pathsep + os.environ.get("PATH", "")

        with mock.patch.object(audit_cc, "MAX_GIT_OUTPUT_BYTES", 128, create=True):
            status, payload, stderr = self.invoke_main(
                ["--git-diff"],
                budget_seconds=1.0,
                environment={"PATH": path},
            )

        self.assertEqual(status, 0)
        self.assertEqual(stderr, "")
        self.assertTrue(payload["truncated"])
        self.assertEqual(payload["limitations"][0]["code"], "TOOL_GIT_OUTPUT")
        self.assertEqual(payload["findings"], [])

    @unittest.skipUnless(os.name == "posix", "process-group cleanup is POSIX-specific")
    def test_git_parent_exit_cleans_long_lived_inherited_pipe_child(self) -> None:
        child_ready = self.root / "child-ready"
        process_record = self.root / "git-process.json"
        binary = self.root / "bin"
        binary.mkdir()
        git = binary / "git"
        git.write_text(
            f"#!{sys.executable}\n"
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
        git.chmod(0o755)
        path = str(binary) + os.pathsep + os.environ.get("PATH", "")

        record: dict[str, int] | None = None
        child_stopped = False
        try:
            with mock.patch.dict(os.environ, {"PATH": path}):
                with self.assertRaises(audit_cc.AuditLimitReached):
                    audit_cc._run_git(
                        self.root,
                        ["status"],
                        deadline=time.monotonic() + 0.15,
                    )
            record = json.loads(process_record.read_text(encoding="utf-8"))
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
            if record is not None:
                try:
                    os.killpg(record["group"], signal.SIGKILL)
                except ProcessLookupError:
                    pass

        self.assertTrue(child_stopped, "Git descendant remained live")

    def test_git_capture_io_failure_is_status_two_without_success_json(self) -> None:
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        self.write(self.root / "main.c", "return;\n")

        class BrokenSelector:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def register(self, *_args):
                return None

            def get_map(self):
                return {1: 1}

            def select(self, _timeout):
                raise OSError("capture broke")

        for scope in (["--git-diff"], [str(self.root / "main.c")]):
            with self.subTest(scope=scope):
                stdout = io.StringIO()
                stderr = io.StringIO()
                with (
                    mock.patch.object(Path, "cwd", return_value=self.root),
                    mock.patch.object(audit_cc.sys, "stdout", stdout),
                    mock.patch.object(audit_cc.sys, "stderr", stderr),
                    mock.patch.object(
                        audit_cc.selectors,
                        "DefaultSelector",
                        return_value=BrokenSelector(),
                    ),
                    mock.patch.dict(
                        os.environ, {"RELIABLE_CC_CLANG": "off"}, clear=True
                    ),
                ):
                    status = audit_cc.main(
                        [*scope, "--format", "json", "--fail-on", "none"]
                    )

                self.assertEqual(status, 2)
                self.assertEqual(stdout.getvalue(), "")
                self.assertIn("Git output capture failed", stderr.getvalue())
                self.assertNotIn("Traceback", stderr.getvalue())

    def test_review_total_deadline_stops_after_slow_source_analyzer(self) -> None:
        paths = [self.root / name for name in ("a.c", "b.c", "c.c")]
        for path in paths:
            self.write(path, "goto done;\n")
        analyzed = []
        original = audit_cc.analyze_source_bounded

        def slow_analyzer(path, text, **kwargs):
            analyzed.append(path.name)
            time.sleep(0.03)
            return original(path, text, **kwargs)

        with mock.patch.object(audit_cc, "analyze_source_bounded", side_effect=slow_analyzer):
            report = run_audit(paths, root=self.root, clang_mode="auto", budget_seconds=0.01)
        self.assertEqual(analyzed, ["a.c"])
        self.assertEqual(report.findings, ())
        self.assertTrue(report.truncated)
        self.assertEqual([item.code for item in report.limitations], ["TOOL_TIMEOUT"])

    def test_review_total_deadline_stops_after_slow_source_read(self) -> None:
        path = self.root / "a.c"
        self.write(path, "goto done;\n")
        original = audit_cc._read_bounded

        def slow_read(path):
            time.sleep(0.03)
            return original(path)

        with mock.patch.object(audit_cc, "_read_bounded", side_effect=slow_read):
            report = run_audit([path], root=self.root, budget_seconds=0.01)
        self.assertEqual(report.findings, ())
        self.assertEqual([item.code for item in report.limitations], ["TOOL_TIMEOUT"])

    def test_discovery_is_recursive_deduplicated_supported_and_sorted(self) -> None:
        for name in (
            "z.HPP",
            "src/z.hxx",
            "src/a.c",
            "src/b.cc",
            "src/c.cpp",
            "src/d.cxx",
            "src/e.h",
            "src/f.hh",
            "src/g.hpp",
            "src/readme.md",
        ):
            self.write(self.root / name, "")

        paths = discover_paths([self.root / "src", self.root / "src/a.c", self.root])

        self.assertEqual(
            [path.relative_to(self.root).as_posix() for path in paths],
            [
                "src/a.c",
                "src/b.cc",
                "src/c.cpp",
                "src/d.cxx",
                "src/e.h",
                "src/f.hh",
                "src/g.hpp",
                "src/z.hxx",
            ],
        )

    def test_missing_explicit_path_is_a_required_input_failure(self) -> None:
        with self.assertRaisesRegex(AuditInputError, "does not exist"):
            discover_paths([self.root / "missing.c"])

    def test_file_count_is_capped_at_500_with_visible_limitation(self) -> None:
        for number in range(501):
            self.write(self.root / f"{number:03}.c", "")

        report = run_audit(discover_paths([self.root]), root=self.root)

        self.assertTrue(report.truncated)
        self.assertEqual(len(report.limitations), 1)
        self.assertEqual(report.limitations[0].code, "TOOL_LIMIT")
        self.assertIn("500 files", report.limitations[0].message)

    def test_oversized_source_is_skipped_with_visible_limitation(self) -> None:
        path = self.root / "large.c"
        path.write_bytes(b" " * (2 * 1024 * 1024 + 1))

        report = run_audit([path], root=self.root)

        self.assertTrue(report.truncated)
        self.assertEqual(report.findings, ())
        self.assertEqual(report.limitations[0].code, "TOOL_LIMIT")
        self.assertEqual(report.limitations[0].path, "large.c")
        self.assertIn("2 MiB", report.limitations[0].message)

    def test_oversized_reads_debit_aggregate_budget_before_next_file(self) -> None:
        paths = [self.root / name for name in ("a.c", "b.c", "c.c")]
        for path in paths:
            self.write(path, " " * 10)
        reads: list[tuple[str, int, int | None]] = []
        original = audit_cc._read_bounded

        def observed_read(path: Path, *, max_bytes: int | None = None):
            contents = (
                original(path)
                if max_bytes is None
                else original(path, max_bytes=max_bytes)
            )
            reads.append((path.name, len(contents), max_bytes))
            return contents

        with (
            mock.patch.object(audit_cc, "MAX_FILE_BYTES", 4),
            mock.patch.object(audit_cc, "MAX_TOTAL_SOURCE_BYTES", 10),
            mock.patch.object(audit_cc, "_read_bounded", side_effect=observed_read),
        ):
            report = run_audit(paths, root=self.root)

        self.assertEqual(reads, [("a.c", 5, None), ("b.c", 5, None)])
        self.assertTrue(report.truncated)
        self.assertEqual(
            [(item.path, item.code) for item in report.limitations],
            [
                ("a.c", "TOOL_LIMIT"),
                ("b.c", "TOOL_LIMIT"),
                ("c.c", "TOOL_LIMIT"),
            ],
        )
        self.assertIn("2 MiB", report.limitations[0].message)
        self.assertIn("2 MiB", report.limitations[1].message)
        self.assertIn("20 MiB", report.limitations[2].message)

    def test_total_source_cap_stops_before_the_file_that_exceeds_it(self) -> None:
        first = self.root / "a.c"
        second = self.root / "b.c"
        self.write(first, "    ")
        self.write(second, "goto out;\n")

        with mock.patch.object(audit_cc, "MAX_TOTAL_SOURCE_BYTES", 4):
            report = run_audit([second, first], root=self.root)

        self.assertTrue(report.truncated)
        self.assertEqual(report.findings, ())
        self.assertEqual(report.limitations[0].code, "TOOL_LIMIT")
        self.assertEqual(report.limitations[0].path, "b.c")
        self.assertIn("20 MiB", report.limitations[0].message)

    def test_findings_are_capped_at_500_with_visible_limitation(self) -> None:
        path = self.root / "many.c"
        self.write(path, "\n".join("goto out;" for _ in range(501)) + "\n")

        report = run_audit([path], root=self.root)

        self.assertTrue(report.truncated)
        self.assertEqual(len(report.findings), 500)
        self.assertEqual(report.limitations[-1].code, "TOOL_LIMIT")
        self.assertIn("500 findings", report.limitations[-1].message)

    def test_review_adversarial_source_honors_total_deadline(self) -> None:
        path = self.root / "many.c"
        self.write(path, "goto out;\n" * 150_000)
        started = time.monotonic()
        report = run_audit([path], root=self.root, budget_seconds=0.01)
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertTrue(report.truncated)
        self.assertIn("TOOL_TIMEOUT", [item.code for item in report.limitations])

    def test_review_directive_heavy_timeout_keeps_completed_prior_evidence(self) -> None:
        first = self.root / "a.c"
        header = self.root / "b.hpp"
        self.write(first, "goto out;\n")
        self.write(
            header,
            "".join(
                f"#ifndef GUARD_{index}{' ' * 40}\n" for index in range(32_000)
            ),
        )
        self.assertGreater(header.stat().st_size, 1_500_000)
        clock = {"armed": False, "now": 0.0}
        actual_monotonic = time.monotonic
        original_guard = source_checks._include_guard_starts

        def monotonic() -> float:
            now = clock["now"]
            if clock["armed"]:
                clock["now"] += 0.02
            return now

        def enter_guard_near_deadline(*args, **kwargs):
            clock.update(armed=True, now=0.29)
            return original_guard(*args, **kwargs)

        started = actual_monotonic()
        with (
            mock.patch.object(time, "monotonic", side_effect=monotonic),
            mock.patch.object(
                source_checks,
                "_include_guard_starts",
                side_effect=enter_guard_near_deadline,
            ),
        ):
            report = run_audit(
                [header, first], root=self.root, budget_seconds=0.3, clang_mode="off"
            )
        self.assertLess(actual_monotonic() - started, 0.5)
        self.assertTrue(clock["armed"])
        self.assertEqual(
            [(item.path, item.code, item.line) for item in report.findings],
            [("a.c", "POT01", 1)],
        )
        self.assertTrue(report.truncated)
        self.assertEqual([item.code for item in report.limitations], ["TOOL_TIMEOUT"])

    def test_changed_line_filter_is_applied_before_finding_cap(self) -> None:
        path = self.root / "many.c"
        self.write(path, "\n".join("goto out;" for _ in range(501)) + "\n")

        report = run_audit(
            [path], root=self.root, changed_lines={Path("many.c"): frozenset({501})}, mode="git"
        )

        self.assertEqual([(item.code, item.line) for item in report.findings], [("POT01", 501)])
        self.assertFalse(report.truncated)

    def test_invalid_utf8_is_a_required_input_failure(self) -> None:
        path = self.root / "bad.c"
        path.write_bytes(b"\xff")

        with self.assertRaisesRegex(AuditInputError, "bad.c"):
            run_audit([path], root=self.root)

    def test_source_reader_never_requests_more_than_file_cap_plus_one(self) -> None:
        path = self.root / "main.c"
        self.write(path, "placeholder")

        class GuardedReader(io.BytesIO):
            def read(self, size: int = -1) -> bytes:
                if size < 0 or size > audit_cc.MAX_FILE_BYTES + 1:
                    raise AssertionError(f"unbounded read requested: {size}")
                return super().read(size)

        reader = GuardedReader(b"goto out;\n")
        with mock.patch.object(Path, "open", return_value=reader):
            report = run_audit([path], root=self.root)

        self.assertEqual(
            [(item.code, item.line) for item in report.findings],
            [("POT01", 1)],
        )


class GitChangedLineTests(TemporaryDirectoryTests):
    def git(self, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *arguments], cwd=self.root, text=True, capture_output=True, check=check
        )

    def make_repo(self, files: dict[str, str], *, commit: bool = True) -> Path:
        self.git("init", "-q")
        self.git("config", "user.email", "tests@example.invalid")
        self.git("config", "user.name", "Reliable C/C++ Tests")
        for name, text in files.items():
            self.write(self.root / name, text)
        self.git("add", ".")
        if commit:
            self.git("commit", "-qm", "base")
        return self.root

    def run_cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(CLI), *arguments],
            cwd=self.root,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_json_cli_reports_only_changed_line(self) -> None:
        self.make_repo({"main.c": "void f(void) {\n  return;\n}\n"})
        self.write(self.root / "main.c", "void f(void) {\n  goto out;\nout: return;\n}\n")

        result = self.run_cli("--git-diff", "--format", "json", "--fail-on", "none")

        payload = json.loads(result.stdout)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            [(item["code"], item["line"]) for item in payload["findings"]],
            [("POT01", 2)],
        )
        self.assertEqual(payload["mode"], "git-diff")

    def test_staged_and_unstaged_lines_are_combined(self) -> None:
        self.make_repo({"main.c": "one\ntwo\nthree\n"})
        self.write(self.root / "main.c", "goto staged;\ntwo\nthree\n")
        self.git("add", "main.c")
        self.write(self.root / "main.c", "goto staged;\ntwo\ngoto unstaged;\n")

        self.assertEqual(git_changed_lines(self.root)[Path("main.c")], frozenset({1, 3}))

    def test_modified_unsupported_tracked_file_is_excluded(self) -> None:
        self.make_repo({"notes.md": "one\ntwo\n"})
        self.write(self.root / "notes.md", "one\ngoto out;\n")

        self.assertEqual(git_changed_lines(self.root), {})

    def test_added_content_resembling_patch_header_does_not_change_file_state(self) -> None:
        self.make_repo(
            {"main.cpp": "counter;\ntwo;\nthree;\nfour;\nfive;\nreturn;\n"}
        )
        self.write(
            self.root / "main.cpp",
            "++ counter;\ntwo;\nthree;\nfour;\nfive;\ngoto out;\n",
        )

        self.assertEqual(
            git_changed_lines(self.root),
            {Path("main.cpp"): frozenset({1, 6})},
        )

    def test_untracked_supported_file_has_all_lines_changed(self) -> None:
        self.make_repo({"README.md": "base\n"})
        self.write(self.root / "new.cpp", "void f() { goto out; out: return; }\n")

        self.assertEqual(git_changed_lines(self.root)[Path("new.cpp")], frozenset({1}))

    def test_untracked_physical_lines_include_cr_only_and_crlf(self) -> None:
        self.make_repo({"README.md": "base\n"})
        (self.root / "cr-only.c").write_bytes(b"return;\rgoto done;\r")
        (self.root / "crlf.c").write_bytes(b"return;\r\ngoto done;\r\n")

        changed = git_changed_lines(self.root)

        self.assertIn(2, changed[Path("cr-only.c")])
        self.assertIn(2, changed[Path("crlf.c")])

    def test_unsupported_untracked_paths_do_not_hide_later_supported_file(self) -> None:
        self.make_repo({"README.md": "base\n"})
        for number in range(502):
            self.write(self.root / f"early-{number:03}.txt", "ignored\n")
        self.write(self.root / "later.c", "goto done;\n")

        changed = git_changed_lines(self.root)
        result = self.run_cli(
            "--git-diff", "--format", "json", "--fail-on", "none"
        )
        payload = json.loads(result.stdout)

        self.assertEqual(changed, {Path("later.c"): frozenset({1})})
        self.assertEqual(result.returncode, 0)
        self.assertFalse(payload["truncated"])
        self.assertEqual(
            [(item["path"], item["code"]) for item in payload["findings"]],
            [("later.c", "POT01")],
        )

    def test_unsupported_no_head_cached_paths_do_not_hide_supported_file(self) -> None:
        files = {
            **{f"early-{number:03}.txt": "ignored\n" for number in range(502)},
            "later.cpp": "goto done;\n",
        }
        self.make_repo(files, commit=False)

        self.assertEqual(
            git_changed_lines(self.root),
            {Path("later.cpp"): frozenset({1})},
        )

    def test_untracked_line_counter_uses_bounded_reader(self) -> None:
        self.make_repo({"README.md": "base\n"})
        self.write(self.root / "new.c", "placeholder")

        class GuardedReader(io.BytesIO):
            def read(self, size: int = -1) -> bytes:
                if size < 0 or size > audit_cc.MAX_FILE_BYTES + 1:
                    raise AssertionError(f"unbounded read requested: {size}")
                return super().read(size)

        reader = GuardedReader(b"one\ntwo\n")
        with mock.patch.object(Path, "open", return_value=reader):
            changed = git_changed_lines(self.root)

        self.assertEqual(changed[Path("new.c")], frozenset({1, 2}))

    def test_huge_hunk_uses_compact_line_membership(self) -> None:
        patch = (
            b"diff --git a/main.c b/main.c\n"
            b"--- a/main.c\n"
            b"+++ b/main.c\n"
            b"@@ -0,0 +1,1000000000 @@\n"
        )

        def git_result(_root: Path, arguments: list[str], **_kwargs):
            if arguments == ["rev-parse", "--show-toplevel"]:
                return subprocess.CompletedProcess(arguments, 0, str(self.root).encode(), b"")
            if arguments and arguments[0] == "diff":
                return subprocess.CompletedProcess(arguments, 0, patch, b"")
            return subprocess.CompletedProcess(arguments, 0, b"", b"")

        amplified: list[int] = []

        def guarded_range(start: int, stop: int):
            if stop - start > 1000:
                amplified.append(stop - start)
                return ()
            return builtins.range(start, stop)

        with (
            mock.patch.object(audit_cc, "_run_git", side_effect=git_result),
            mock.patch.object(audit_cc, "range", guarded_range, create=True),
        ):
            changed, truncated = audit_cc._git_changed_lines(self.root)

        self.assertEqual(amplified, [])
        self.assertFalse(truncated)
        self.assertIn(1, changed[Path("main.c")])
        self.assertIn(1_000_000_000, changed[Path("main.c")])

    def test_hunk_above_index_limit_uses_saturating_cardinality(self) -> None:
        count = sys.maxsize + 10
        patch = (
            b"diff --git a/main.c b/main.c\n"
            b"--- a/main.c\n"
            b"+++ b/main.c\n"
            + f"@@ -0,0 +1,{count} @@\n".encode()
        )

        def git_result(_root: Path, arguments: list[str], **_kwargs):
            if arguments == ["rev-parse", "--show-toplevel"]:
                return subprocess.CompletedProcess(arguments, 0, str(self.root).encode(), b"")
            if arguments and arguments[0] == "diff":
                return subprocess.CompletedProcess(arguments, 0, patch, b"")
            return subprocess.CompletedProcess(arguments, 0, b"", b"")

        eager_iterations: list[int] = []

        def guarded_range(start: int, stop: int):
            eager_iterations.append(stop - start)
            return ()

        with (
            mock.patch.object(audit_cc, "_run_git", side_effect=git_result),
            mock.patch.object(audit_cc, "range", guarded_range, create=True),
        ):
            try:
                lines = git_changed_lines(self.root)[Path("main.c")]
            except OverflowError:
                lines = None

        self.assertIsNotNone(lines)
        assert lines is not None
        self.assertEqual(eager_iterations, [])
        self.assertEqual(len(lines), sys.maxsize)
        self.assertIn(count, lines)
        self.assertNotIn(count + 1, lines)

    def test_dense_untracked_files_reach_source_cap_without_line_expansion(self) -> None:
        self.make_repo({"README.md": "base\n"})
        self.write(self.root / "a.c", "\n" * 100)
        self.write(self.root / "b.c", "\n" * 100)
        amplified: list[int] = []

        def guarded_range(start: int, stop: int):
            if stop - start > 50:
                amplified.append(stop - start)
            return builtins.range(start, stop)

        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch.object(audit_cc, "MAX_FILE_BYTES", 200),
            mock.patch.object(audit_cc, "MAX_TOTAL_SOURCE_BYTES", 150),
            mock.patch.object(audit_cc, "range", guarded_range, create=True),
            mock.patch.object(Path, "cwd", return_value=self.root),
            mock.patch.object(audit_cc.sys, "stdout", stdout),
            mock.patch.object(audit_cc.sys, "stderr", stderr),
            mock.patch.dict(os.environ, {"RELIABLE_CC_CLANG": "off"}, clear=True),
        ):
            status = audit_cc.main(
                ["--git-diff", "--format", "json", "--fail-on", "none"],
                budget_seconds=1.0,
            )
        payload = json.loads(stdout.getvalue())

        self.assertEqual(status, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(amplified, [])
        self.assertTrue(payload["truncated"])
        self.assertEqual(payload["limitations"][0]["code"], "TOOL_LIMIT")
        self.assertIn("20 MiB", payload["limitations"][0]["message"])

    def test_cli_reads_untracked_sources_once_with_aggregate_sentinel(self) -> None:
        self.make_repo({"README.md": "base\n"})
        self.write(self.root / "a.c", " " * 100)
        self.write(self.root / "b.c", " " * 100)
        reads: list[tuple[str, int, int | None]] = []
        original = audit_cc._read_bounded

        def observed_read(path: Path, *, max_bytes: int | None = None):
            contents = (
                original(path)
                if max_bytes is None
                else original(path, max_bytes=max_bytes)
            )
            reads.append((path.name, len(contents), max_bytes))
            return contents

        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch.object(audit_cc, "MAX_FILE_BYTES", 200),
            mock.patch.object(audit_cc, "MAX_TOTAL_SOURCE_BYTES", 150),
            mock.patch.object(audit_cc, "_read_bounded", side_effect=observed_read),
            mock.patch.object(Path, "cwd", return_value=self.root),
            mock.patch.object(audit_cc.sys, "stdout", stdout),
            mock.patch.object(audit_cc.sys, "stderr", stderr),
            mock.patch.dict(os.environ, {"RELIABLE_CC_CLANG": "off"}, clear=True),
        ):
            status = audit_cc.main(
                ["--git-diff", "--format", "json", "--fail-on", "none"]
            )
        payload = json.loads(stdout.getvalue())

        self.assertEqual(status, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(
            reads,
            [("a.c", 100, 150), ("b.c", 51, 50)],
        )
        self.assertTrue(payload["truncated"])
        self.assertEqual(payload["limitations"][0]["code"], "TOOL_LIMIT")

    def test_untracked_file_cap_is_applied_before_line_reads(self) -> None:
        self.make_repo({"README.md": "base\n"})
        for number in range(501):
            self.write(self.root / f"{number:03}.c", "line\n")

        with mock.patch.object(
            audit_cc, "_line_numbers", return_value=frozenset({1})
        ) as line_numbers:
            changed = git_changed_lines(self.root)

        self.assertEqual(len(changed), 500)
        self.assertIn(Path("000.c"), changed)
        self.assertIn(Path("499.c"), changed)
        self.assertNotIn(Path("500.c"), changed)
        self.assertEqual(line_numbers.call_count, 500)

        result = self.run_cli(
            "--git-diff", "--format", "json", "--fail-on", "none"
        )
        payload = json.loads(result.stdout)
        self.assertEqual(result.returncode, 0)
        self.assertTrue(payload["truncated"])
        self.assertEqual(payload["limitations"][0]["code"], "TOOL_LIMIT")
        self.assertIn("500 files", payload["limitations"][0]["message"])

    def test_rename_uses_new_path_and_only_modified_lines(self) -> None:
        self.make_repo({"old.c": "one\ntwo\nthree\n"})
        self.git("mv", "old.c", "new.c")
        self.write(self.root / "new.c", "one\ngoto out;\nthree\n")

        changed = git_changed_lines(self.root)

        self.assertNotIn(Path("old.c"), changed)
        self.assertEqual(changed[Path("new.c")], frozenset({2}))

    def test_git_quoted_non_ascii_path_is_decoded(self) -> None:
        self.make_repo({"한글.c": "one\ntwo\n"})
        self.write(self.root / "한글.c", "one\ngoto out;\n")

        self.assertEqual(git_changed_lines(self.root), {Path("한글.c"): frozenset({2})})

    def test_deleted_and_unchanged_files_are_ignored(self) -> None:
        self.make_repo({"deleted.c": "goto out;\n", "unchanged.c": "goto out;\n"})
        (self.root / "deleted.c").unlink()

        changed = git_changed_lines(self.root)

        self.assertNotIn(Path("deleted.c"), changed)
        self.assertNotIn(Path("unchanged.c"), changed)

    def test_repository_without_head_treats_tracked_and_untracked_files_as_changed(self) -> None:
        self.make_repo({"tracked.c": "one\ntwo\n"}, commit=False)
        self.write(self.root / "untracked.cpp", "one\n")

        self.assertEqual(
            git_changed_lines(self.root),
            {Path("tracked.c"): frozenset({1, 2}), Path("untracked.cpp"): frozenset({1})},
        )

    def test_git_diff_outside_repository_returns_two_without_traceback(self) -> None:
        result = self.run_cli("--git-diff")

        self.assertEqual(result.returncode, 2)
        self.assertNotIn("Traceback", result.stderr)
        self.assertIn("Git", result.stderr)


class ClangIntegrationTests(FakeClangTests):
    def cli(self, **environment: str):
        return subprocess.run([sys.executable, str(CLI), str(self.source), "--format", "json"],
                              cwd=self.root, text=True, capture_output=True,
                              env={**os.environ, **environment}, check=False)

    def test_cli_auto_merges_semantics_and_records_selected_database(self) -> None:
        self.write_db("compile_commands.json")
        self.source.write_text("int f() { goto done; done: return f(); }\n", encoding="utf-8")
        self.fake("print(" + repr(json.dumps(node("TranslationUnitDecl", inner=[
            function("f", [call("f", 1)])]))) + ")")
        result = self.cli(RELIABLE_CC_CLANG="auto")
        payload = json.loads(result.stdout)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(payload["configuration"]["clang"], "auto")
        self.assertEqual(payload["configuration"]["compilation_databases"],
                         [str(self.root / "compile_commands.json")])
        self.assertEqual(len([f for f in payload["findings"] if f["code"] == "POT01"]), 1)
        self.assertTrue(any(f["engine"] == "clang" for f in payload["findings"]))

    def test_cli_clang_off_keeps_only_source_results(self) -> None:
        self.source.write_text("goto done;\n", encoding="utf-8")
        result = self.cli(RELIABLE_CC_CLANG="off")
        payload = json.loads(result.stdout)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(payload["limitations"], [])
        self.assertEqual([(f["code"], f["engine"]) for f in payload["findings"]], [("POT01", "source")])

    def test_review_non_posix_clang_is_disabled_before_descendant_spawn(self) -> None:
        self.write_db("compile_commands.json")
        self.source.write_text("goto done;\n", encoding="utf-8")
        marker = self.root / "descendant-marker"
        child = (
            "import pathlib,time; time.sleep(.3); "
            f"pathlib.Path({str(marker)!r}).write_text('spawned')"
        )
        self.fake(
            "import subprocess\n"
            f"subprocess.Popen([sys.executable, '-c', {child!r}])"
        )
        fallback_os = mock.Mock(wraps=os)
        fallback_os.name = "nt"
        with mock.patch.object(clang, "os", fallback_os):
            report = run_audit(
                [self.source], root=self.root, clang_mode="auto", budget_seconds=1
            )
        time.sleep(0.4)
        self.assertEqual(
            [(item.code, item.engine) for item in report.findings],
            [("POT01", "source")],
        )
        self.assertEqual(
            [item.code for item in report.limitations], ["TOOL_CLANG_PLATFORM"]
        )
        self.assertFalse(marker.exists())

    def test_cli_invalid_settings_are_status_two(self) -> None:
        invalid = self.root / "invalid.json"
        invalid.write_text("{invalid", encoding="utf-8")
        schema = self.root / "schema.json"
        schema.write_text('[{"file": "src/main.cpp"}]', encoding="utf-8")
        for env in ({"RELIABLE_CC_CLANG": "yes"},
                    {"RELIABLE_CC_COMPILE_COMMANDS": str(invalid)},
                    {"RELIABLE_CC_COMPILE_COMMANDS": str(schema)},
                    {"RELIABLE_CC_COMPILE_COMMANDS": str(self.root / "missing.json")},
                    {"RELIABLE_CC_COMPILE_COMMANDS": ""}):
            with self.subTest(env=env):
                result = self.cli(**env)
                self.assertEqual(result.returncode, 2)
                self.assertNotIn("Traceback", result.stderr)

    def test_explicit_path_from_git_subdirectory_finds_root_database(self) -> None:
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True, capture_output=True)
        self.write_db("compile_commands.json")
        self.fake("print('{\"kind\": \"TranslationUnitDecl\"}')")
        # Keep Git available while the fake compiler remains first on PATH.
        result = subprocess.run([sys.executable, str(CLI), "main.cpp", "--format", "json"],
                                cwd=self.source.parent, text=True, capture_output=True,
                                env={**os.environ, "PATH": os.environ["PATH"] + ":/usr/bin:/bin"})
        payload = json.loads(result.stdout)
        self.assertEqual(payload["configuration"]["compilation_databases"],
                         [str(self.root / "compile_commands.json")])

    def test_timeout_and_output_limit_keep_source_results(self) -> None:
        self.write_db("compile_commands.json")
        self.source.write_text("goto done;\n", encoding="utf-8")
        for body, budget, expected in (("time.sleep(2)", 0.05, "TOOL_CLANG_TIMEOUT"),
                                       ("print('x' * (17 * 1024 * 1024))", 5, "TOOL_CLANG_OUTPUT")):
            with self.subTest(expected=expected):
                self.fake(body)
                result = run_audit([self.source], root=self.root, clang_mode="auto", budget_seconds=budget)
                self.assertEqual([(f.code, f.engine) for f in result.findings], [("POT01", "source")])
                self.assertEqual(result.limitations[0].code, expected)

    def test_explicit_no_match_is_limitation_without_fallback(self) -> None:
        self.write_db("compile_commands.json")
        explicit = self.write_db("custom/compile_commands.json", "other.cpp")
        result = self.cli(RELIABLE_CC_COMPILE_COMMANDS=str(explicit))
        payload = json.loads(result.stdout)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(payload["limitations"][0]["code"], "TOOL_CLANG_DATABASE")
        self.assertEqual(payload["configuration"]["compilation_databases"], [])

    def test_adapter_failure_preserves_source_findings(self) -> None:
        self.write_db("compile_commands.json")
        self.source.write_text("goto done;\n", encoding="utf-8")
        for body in ("print('{bad')", "sys.exit(1)"):
            with self.subTest(body=body):
                self.fake(body)
                result = self.cli(RELIABLE_CC_CLANG="auto")
                payload = json.loads(result.stdout)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(payload["findings"][0]["code"], "POT01")
                self.assertTrue(payload["limitations"][0]["code"].startswith("TOOL_CLANG_"))

    def test_unexpected_adapter_failure_cannot_abort_source_audit(self) -> None:
        self.source.write_text("goto done;\n", encoding="utf-8")
        with mock.patch.object(audit_cc, "run_clang_analysis", side_effect=RuntimeError("unavailable")):
            report = run_audit([self.source], root=self.root, clang_mode="auto")
        self.assertEqual([(f.code, f.engine) for f in report.findings], [("POT01", "source")])
        self.assertEqual(report.limitations[0].code, "TOOL_CLANG_FAILURE")

    def test_changed_cycle_site_and_exact_suppression(self) -> None:
        self.write_db("compile_commands.json")
        self.source.write_text("int f();\nint g();\nint f(){return g();}\nint g(){return f();}\n", encoding="utf-8")
        ast = node("TranslationUnitDecl", inner=[function("f", [call("g", 3)]),
                                               function("g", [call("f", 4)], 2)])
        self.fake("print(" + repr(json.dumps(ast)) + ")")
        result = run_audit([self.source], root=self.root, clang_mode="auto",
                           changed_lines={Path("src/main.cpp"): frozenset({4})})
        self.assertIn(("POT01", 4), [(f.code, f.line) for f in result.findings])
        self.source.write_text(self.source.read_text().rstrip() + " // quality: ignore[POT01] - bounded cycle\n")
        result = run_audit([self.source], root=self.root, clang_mode="auto",
                           changed_lines={Path("src/main.cpp"): frozenset({4})})
        self.assertFalse(any(f.code == "POT01" for f in result.findings))

    def test_semantic_merge_is_sorted_and_capped_after_deduplication(self) -> None:
        self.write_db("compile_commands.json")
        self.source.write_text("goto done;\nint value;\n", encoding="utf-8")
        self.fake("print(" + repr(json.dumps(node("TranslationUnitDecl", inner=[
            node("VarDecl", 2, type={"qualType": "int"})]))) + ")")
        with mock.patch.object(audit_cc, "MAX_FINDINGS", 1):
            report = run_audit([self.source], root=self.root, clang_mode="auto")
        self.assertEqual([(f.code, f.line) for f in report.findings], [("POT01", 1)])
        self.assertTrue(report.truncated)


class LineIntervalTests(unittest.TestCase):
    def test_equivalent_partitions_normalize_and_compare_equal(self) -> None:
        partitioned = audit_cc.LineIntervals(((3, 5), (1, 3), (8, 8)))
        merged = audit_cc.LineIntervals(((1, 5),))

        self.assertEqual(partitioned, merged)
        self.assertEqual(partitioned.intervals, ((1, 5),))
        with self.assertRaises(TypeError):
            hash(partitioned)

    def test_equality_with_frozenset_does_not_expand_large_interval(self) -> None:
        ordinary = audit_cc.LineIntervals(((1, 3), (4, 5)))
        huge = audit_cc.LineIntervals(((1, sys.maxsize + 10),))
        eager_iterations: list[int] = []

        def guarded_range(start: int, stop: int):
            eager_iterations.append(stop - start)
            return ()

        with mock.patch.object(audit_cc, "range", guarded_range, create=True):
            self.assertEqual(ordinary, frozenset({1, 2, 4}))
            self.assertEqual(frozenset({1, 2, 4}), ordinary)
            self.assertNotEqual(huge, frozenset({1}))

        self.assertEqual(eager_iterations, [])


class RenderingAndStatusTests(unittest.TestCase):
    def test_fail_on_ignores_solid_and_tool_findings(self) -> None:
        report = AuditReport(
            mode="paths",
            findings=(
                finding("SOLID01", gate_eligible=False),
                finding("TOOL_SUPPRESSION", gate_eligible=False),
            ),
            limitations=(Limitation("TOOL_LIMIT", "limited"),),
        )
        self.assertEqual(exit_status(report, "warnings"), 0)

    def test_fail_on_applies_only_selected_power_of_ten_threshold(self) -> None:
        report = AuditReport(
            mode="paths",
            findings=(finding("POT02", "warning"), finding("POT01", "error", line=3)),
        )
        self.assertEqual(exit_status(report, "none"), 0)
        self.assertEqual(exit_status(report, "errors"), 1)
        self.assertEqual(exit_status(report, "warnings"), 1)
        warning_only = AuditReport(mode="paths", findings=(finding(),))
        self.assertEqual(exit_status(warning_only, "errors"), 0)

    def test_text_rendering_orders_findings_then_limitations_then_counts(self) -> None:
        report = AuditReport(
            mode="paths",
            findings=(finding("POT01", "error", path="z.c", line=4), finding(path="a.c", line=2)),
            limitations=(Limitation("TOOL_LIMIT", "limited", "z.c"),),
            truncated=True,
        )

        lines = render_text(report).splitlines()

        self.assertEqual(lines[0], "a.c:2: warning POT02: message")
        self.assertEqual(lines[1], "z.c:4: error POT01: message")
        self.assertEqual(lines[2], "z.c: limitation TOOL_LIMIT: limited")
        self.assertEqual(lines[3], "summary: 1 error, 1 warning, 0 info; truncated: yes")

    def test_cli_rejects_missing_paths_and_conflicting_git_scope(self) -> None:
        for arguments in ((), ("--git-diff", "main.c")):
            with self.subTest(arguments=arguments):
                result = subprocess.run(
                    [sys.executable, str(CLI), *arguments],
                    cwd=ROOT,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 2)
                self.assertNotIn("Traceback", result.stderr)

    def test_cli_required_input_failure_returns_two_without_traceback(self) -> None:
        result = subprocess.run(
            [sys.executable, str(CLI), "missing.c"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 2)
        self.assertNotIn("Traceback", result.stderr)
        self.assertIn("missing.c", result.stderr)


if __name__ == "__main__":
    unittest.main()
