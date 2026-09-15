#!/usr/bin/env python3
"""Inject the shared Reliable C/C++ policy into supported hosts."""

from __future__ import annotations

import json
import pathlib
import sys


PLUGIN_ROOT = pathlib.Path(__file__).resolve().parent.parent
ENTRY_SKILL = PLUGIN_ROOT / "skills" / "using-reliable-cc" / "SKILL.md"
MAX_ENTRY_SKILL_BYTES = 8_192

PREAMBLE = (
    "<RELIABLE_CC_POLICY>\n"
    "Apply this policy to C and C++ code written or changed in this session.\n\n"
    "The full `using-reliable-cc` entry skill follows. Invoke the bundled "
    "skills when their workflows apply.\n\n"
)


def main() -> int:
    """Write SessionStart context, failing open if the policy cannot load."""

    try:
        body = ENTRY_SKILL.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        print(f"reliable-cc: cannot read {ENTRY_SKILL}: {error}", file=sys.stderr)
        return 0

    if len(body.encode("utf-8")) > MAX_ENTRY_SKILL_BYTES:
        print(
            f"reliable-cc: entry skill exceeds {MAX_ENTRY_SKILL_BYTES} bytes; "
            "move detail into a rule skill",
            file=sys.stderr,
        )
        return 0

    context = PREAMBLE + body + "\n</RELIABLE_CC_POLICY>"
    try:
        json.dump(
            {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": context,
                }
            },
            sys.stdout,
        )
        sys.stdout.write("\n")
    except OSError:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
