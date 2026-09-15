"""Tests for the normalized Reliable C/C++ audit quality model."""

from __future__ import annotations

import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "reviewing-cc-quality" / "scripts"))

from quality_model import (
    AuditReport,
    Finding,
    Limitation,
    finding_sort_key,
)


def make_finding(path: str, line: int, code: str) -> Finding:
    return Finding(
        code=code,
        severity="warning",
        path=path,
        line=line,
        message="message",
        remediation="remediation",
        engine="source",
        gate_eligible=False,
    )


class QualityModelTests(unittest.TestCase):
    def test_report_serializes_stable_schema(self) -> None:
        finding = Finding(
            code="POT01", severity="error", path="src/a.c", line=7,
            message="goto obscures control flow", remediation="use structured control flow",
            engine="source", gate_eligible=True,
        )
        report = AuditReport(mode="paths", findings=(finding,))
        payload = report.to_dict()
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["findings"][0]["code"], "POT01")
        self.assertEqual(payload["summary"], {"errors": 1, "warnings": 0, "info": 0})

    def test_findings_sort_by_path_line_code(self) -> None:
        findings = [make_finding("b.c", 1, "POT08"), make_finding("a.c", 4, "POT01")]
        self.assertEqual([f.path for f in sorted(findings, key=finding_sort_key)], ["a.c", "b.c"])

    def test_findings_sort_by_line_then_code(self) -> None:
        findings = [make_finding("a.c", 4, "POT08"), make_finding("a.c", 2, "POT01"), make_finding("a.c", 2, "POT00")]
        self.assertEqual(
            [(f.line, f.code) for f in sorted(findings, key=finding_sort_key)],
            [(2, "POT00"), (2, "POT01"), (4, "POT08")],
        )

    def test_invalid_finding_values_and_line_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            make_finding("a.c", 0, "POT01")
        with self.assertRaises(ValueError):
            Finding("POT01", "fatal", "a.c", 1, "message", "fix", "source", False)
        with self.assertRaises(ValueError):
            Finding("POT01", "error", "a.c", 1, "message", "fix", "other", False)

    def test_limitation_and_configuration_serialize(self) -> None:
        limitation = Limitation(code="CLANG_UNAVAILABLE", message="clang is unavailable")
        report = AuditReport(
            mode="git",
            limitations=(limitation,),
            truncated=True,
            configuration={"fail_on": "warning"},
        )
        self.assertEqual(report.to_dict()["limitations"], [limitation.to_dict()])
        self.assertEqual(report.to_dict()["configuration"], {"fail_on": "warning"})
        self.assertTrue(report.to_dict()["truncated"])


if __name__ == "__main__":
    unittest.main()
