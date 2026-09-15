# Reliable C/C++ Development Guide

## Current contract

Reliable C/C++ 0.1.0 is the functional initial release. It combines
dependency-free source checks, optional Clang analysis from an exact compilation
command, Power of Ten and SOLID guidance, and a changed-line Stop hook that may
request at most one correction pass.

Preserve these boundaries:

- supported suffixes are `.c`, `.h`, `.cc`, `.cpp`, `.cxx`, `.hh`, `.hpp`, and
  `.hxx`;
- only eligible `POT` findings can gate or cause CLI status 1;
- SOLID, `TOOL_*`, and limitations are advisory under every threshold;
- optional Clang coverage never guesses flags and does not replace completed
  source coverage;
- invalid settings and infrastructure failures fail open in the hook;
- the second consecutive completion attempt is always allowed;
- `Stop (completed)` means the hook command finished, not that anything shut
  down;
- no finding, clean report, or suppression certifies safety, correctness,
  security, compliance, Power of Ten adherence, or SOLID design.

Keep the public version exactly synchronized across the Codex manifest, Claude
manifest, Claude marketplace entry, README badge, and package contracts.
Human-facing copy uses `Reliable C/C++`; the package name is `reliable-cc`.

## Structure

- `.codex-plugin/plugin.json` defines Codex metadata and discovers
  `hooks/hooks.json` conventionally; Codex validation rejects a top-level
  `hooks` manifest field.
- `.claude-plugin/plugin.json` defines Claude Code metadata and declares
  `./hooks/hooks.json` according to the Claude schema.
- `.claude-plugin/marketplace.json` supports repository-local Claude testing.
- `hooks/hooks.json` registers SessionStart and Stop; its command targets are
  package-relative and must exist.
- `skills/` contains exactly four uniquely named packages:
  `using-reliable-cc`, `applying-power-of-ten-to-c-cpp`,
  `applying-solid-to-cpp`, and `reviewing-cc-quality`.
- `skills/reviewing-cc-quality/scripts/` contains the normalized model, source
  checks, optional Clang adapter, and direct CLI.
- `tests/` protects analysis, Git scope, hooks, text/JSON rendering, limits,
  manifests, portable paths, and generated-file exclusions.

## Behavior changes

Write a failing regression first. A rule, severity, gate, suppression,
configuration, output, or limit change requires an approved scope and matching
public documentation. Keep runtime and tests free of third-party Python
dependencies. Do not implement post-v0.1.0 backlog items as incidental cleanup.

Never describe the plugin as a compiler, complete parser, build system, full
static analyzer, proof system, or certification mechanism. Report source and
Clang coverage separately, including exact-command and truncation limitations.

## Validation

Run the complete source validation and both CLI smokes before publishing a
change:

```sh
python3 -m unittest discover -s tests -v
python3 "${CODEX_HOME:-$HOME/.codex}/skills/.system/plugin-creator/scripts/validate_plugin.py" .
claude plugin validate .
for skill in skills/*; do
    python3 "${CODEX_HOME:-$HOME/.codex}/skills/.system/skill-creator/scripts/quick_validate.py" "$skill"
done
git diff --check
python3 skills/reviewing-cc-quality/scripts/audit_cc.py tests --format text --fail-on none
python3 skills/reviewing-cc-quality/scripts/audit_cc.py --git-diff --format json --fail-on none
```
