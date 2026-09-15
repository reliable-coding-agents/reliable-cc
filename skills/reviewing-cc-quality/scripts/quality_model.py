"""Normalized values exchanged by the Reliable C/C++ audit engines."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Mapping


_SEVERITIES = frozenset({"error", "warning", "info"})
_ENGINES = frozenset({"source", "clang"})


@dataclass(frozen=True)
class Finding:
    """A normalized quality finding from one audit engine."""

    code: str
    severity: str
    path: str
    line: int
    message: str
    remediation: str
    engine: str
    gate_eligible: bool

    def __post_init__(self) -> None:
        if self.severity not in _SEVERITIES:
            raise ValueError(f"invalid severity: {self.severity!r}")
        if self.engine not in _ENGINES:
            raise ValueError(f"invalid engine: {self.engine!r}")
        if self.line <= 0:
            raise ValueError("line must be positive")

    def to_dict(self) -> dict[str, object]:
        return dataclasses.asdict(self)


@dataclass(frozen=True)
class Limitation:
    """A non-finding limitation encountered while auditing."""

    code: str
    message: str
    path: str | None = None

    def to_dict(self) -> dict[str, object]:
        return dataclasses.asdict(self)


def finding_sort_key(finding: Finding) -> tuple[str, int, str]:
    """Return the stable path, line, and rule ordering key."""

    return (finding.path, finding.line, finding.code)


def severity_counts(findings: tuple[Finding, ...]) -> dict[str, int]:
    """Count findings by the report's stable summary keys."""

    return {
        "errors": sum(item.severity == "error" for item in findings),
        "warnings": sum(item.severity == "warning" for item in findings),
        "info": sum(item.severity == "info" for item in findings),
    }


@dataclass(frozen=True)
class AuditReport:
    """Complete normalized audit output."""

    mode: str
    findings: tuple[Finding, ...] = ()
    limitations: tuple[Limitation, ...] = ()
    truncated: bool = False
    configuration: Mapping[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        ordered = tuple(sorted(self.findings, key=finding_sort_key))
        return {
            "schema_version": 1,
            "mode": self.mode,
            "configuration": dict(self.configuration),
            "findings": [item.to_dict() for item in ordered],
            "limitations": [item.to_dict() for item in self.limitations],
            "truncated": self.truncated,
            "summary": severity_counts(ordered),
        }
