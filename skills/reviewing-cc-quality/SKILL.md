---
name: reviewing-cc-quality
description: Use when auditing C or C++ paths or Git changes with Reliable C/C++, interpreting text or JSON findings, configuring Clang coverage, troubleshooting audit limits, or explaining completion-gate and Stop-hook behavior.
---

# Reviewing Reliable C/C++ Quality

## Run the Auditor

Resolve `scripts/audit_cc.py` beside this `SKILL.md` and run it from the target
project's working directory. In the plugin source checkout, these are exact
examples:

```bash
python3 skills/reviewing-cc-quality/scripts/audit_cc.py . --format text --fail-on errors
python3 skills/reviewing-cc-quality/scripts/audit_cc.py --git-diff --format json --fail-on none
```

Explicit paths recurse through directories and accept `.c`, `.h`, `.cc`,
`.cpp`, `.cxx`, `.hh`, `.hpp`, and `.hxx`. They can run outside Git.
`--git-diff` requires a Git working tree, audits added lines relative to `HEAD`
plus every line of untracked supported files, and cannot be combined with
paths. Supply either paths or `--git-diff`.

`--format text|json` defaults to `text`. `--fail-on errors|warnings|none`
defaults to `errors` and controls only the process status; it does not hide
findings.

## Exit Status and JSON

| Status | Meaning |
|---|---|
| 0 | No gate-eligible `POT` finding met the selected threshold; always for `--fail-on none`. |
| 1 | An eligible Power of Ten error met `errors`, or an eligible warning/error met `warnings`. |
| 2 | Invalid invocation/configuration or a required Git, path, read, or decode input failed. |

SOLID, `TOOL` findings, and limitations never cause status 1. Optional Clang
failure does not cause status 2 when the source audit completed.

JSON is one deterministic object:

```json
{
  "schema_version": 1,
  "mode": "paths|git-diff",
  "configuration": {
    "format": "json",
    "fail_on": "none",
    "compile_commands": null,
    "clang": "auto|off",
    "compilation_databases": []
  },
  "findings": [{
    "code": "POT01",
    "severity": "error",
    "path": "src/main.c",
    "line": 4,
    "message": "...",
    "remediation": "...",
    "engine": "source|clang",
    "gate_eligible": true
  }],
  "limitations": [{"code": "TOOL_...", "message": "...", "path": null}],
  "truncated": false,
  "summary": {"errors": 1, "warnings": 0, "info": 0}
}
```

Counts cover every reported severity, not only gate-eligible findings.

## Source and Clang Coverage

Dependency-free source checks run without Clang. `RELIABLE_CC_CLANG=auto` is the
default; `off` disables semantic analysis. Other values are configuration
errors. Auto mode requires an exact normalized source entry and never guesses
flags.

`RELIABLE_CC_COMPILE_COMMANDS` may name a database or its directory. It is
authoritative: an invalid value returns status 2; a valid database without an
exact entry produces a limitation and does not fall back. Without it, candidates
are the Git/audit root, `build`, `out`, and immediate `build-*` and
`cmake-build-*` directories. Among exact matches, the shortest path string,
then lexical order, wins. Recorded compiler/language and project flags select
`clang` or `clang++`; output/dependency options are removed or rejected.

Missing Clang, missing exact entries, unsupported commands/languages, parse or
JSON failure, timeout, and output overflow produce `TOOL_CLANG_*` limitations.
Source findings remain. Report this as reduced semantic coverage, never a clean
semantic result, failed source audit, or certification.

Clang execution is available only on POSIX platforms, where the complete
compiler process group can be terminated within the audit bound. Other
platforms retain source coverage and report `TOOL_CLANG_PLATFORM`. Commands
that request native plugins, opaque forwarded options, module/cache building,
artifact output, or crash reproducers are unsupported and remain visible as
Clang limitations without execution. Accepted commands disable automatic crash
diagnostic files and must identify exactly one positional compilation input
matching the database entry.

## Resource Limits

| Scope | Limit |
|---|---|
| Direct audit | 60 seconds total |
| Source | 500 files; 2 MiB/file; 20 MiB total; 500 findings |
| Git discovery | 20 MiB combined stdout/stderr; shares the direct-audit deadline |
| Compilation database | 16 MiB |
| Clang | 10 translation units; 5 seconds/unit; 16 MiB captured output/unit |
| Stop hook | 15-second audit inside a 20-second host timeout; 2 MiB audit capture |
| Stop report | First 12 findings, first 5 limitations, bounded 16,384-character messages |

Every exceeded analysis cap adds a limitation and sets `truncated: true` where
applicable. Never interpret a truncated report as complete coverage.

## Suppressions

Use only a confirmed false positive or approved exception, on the exact finding
line with one exact code and a rationale:

```c
for (;;) { /* quality: ignore[POT02] - top-level scheduler loop */
```

Wildcard, comma-separated, file-wide, off-line, and rationale-free forms fail.
A malformed form emits non-gating `TOOL_SUPPRESSION`; a valid form removes only
that code on that line and does not establish safety or compliance. Use
`$applying-power-of-ten-to-c-cpp` or `$applying-solid-to-cpp` before accepting
an exception.

## Stop Hook Meaning

The hook runs `--git-diff --format json --fail-on none`, then applies
`RELIABLE_CC_GATE=errors|all|off` (`errors` by default). It silently skips a
non-Git directory. Invalid gate values and infrastructure failures fail open
with a message; limitations are non-blocking.

On the first completion attempt, selected eligible `POT` findings can request
one correction pass. When `stop_hook_active` is already true, the hook allows
completion even if findings remain, preventing a loop. **`Stop (completed)`
means the Stop hook command finished; it does not mean the plugin or session
was stopped, disabled, or uninstalled.**

## Review and Troubleshooting

1. Run JSON with `--fail-on none` to inspect findings, limitations, effective
   configuration, selected databases, and truncation without status 1.
2. Confirm the requested scope and Clang coverage. Then classify findings with
   the relevant rule skill, fix confirmed issues, and run project compiler,
   analyzers, and tests.
3. Re-run at the CI threshold and report remaining exceptions and limitations.

| Symptom | Action |
|---|---|
| `--git-diff` says Git is required | Run inside the intended working tree, or audit explicit paths. The Stop hook skips ordinary non-Git directories silently. |
| Status 2 with environment settings | Use `RELIABLE_CC_CLANG=auto|off`; verify the explicit database path, JSON array, entries, and 16 MiB cap. |
| `TOOL_CLANG_DATABASE` | Generate a database containing the source's exact path, or set the authoritative database deliberately. |
| Clang limitation | Fix tool/command/data availability; source results remain valid only for source coverage. |
| `truncated: true` | Narrow the paths or Git diff and rerun; inspect every `TOOL_LIMIT`/timeout. |
| Hook says `gate skipped` | Read the sanitized infrastructure reason; the completion was allowed, not certified. |
