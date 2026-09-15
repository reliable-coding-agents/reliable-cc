# Reliable C/C++

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Version](https://img.shields.io/badge/version-0.1.0-informational.svg)](.claude-plugin/plugin.json)
[![Languages](https://img.shields.io/badge/languages-C%20%7C%20C%2B%2B-00599C.svg)](https://isocpp.org/)
[![Codex](https://img.shields.io/badge/Codex-compatible-111827.svg)](.codex-plugin/plugin.json)
[![Claude Code](https://img.shields.io/badge/Claude_Code-compatible-D97757.svg)](.claude-plugin/plugin.json)

Reliable C/C++ is a shared Codex and Claude Code plugin for auditing C and C++
against the Power of Ten and reviewing C++ classes and C module APIs through
SOLID design questions. Version 0.1.0 provides dependency-free source checks,
optional exact-command Clang analysis, four focused skills, and a changed-line
completion hook that can request at most one correction pass.

Reliable C/C++ is evidence, not certification. A clean or suppressed report
does not certify safety, correctness, security, compliance, or adherence to the
Power of Ten or SOLID. Keep using the project's compiler, strict warning policy,
static analyzers, tests, and human review.

## Install

### Claude Code

```sh
claude plugin marketplace add reliable-coding-agents/marketplace
claude plugin install reliable-cc@reliable-coding-agents
```

### Codex

```sh
codex plugin marketplace add reliable-coding-agents/marketplace
codex plugin add reliable-cc@reliable-coding-agents
```

Start a new agent session after installing or updating the plugin.

## What is included

- `using-reliable-cc` routes C/C++ work to the relevant specialist guidance.
- `applying-power-of-ten-to-c-cpp` explains POT01 through POT10, including the
  automated boundary, manual review needs, exceptions, and suppressions.
- `applying-solid-to-cpp` applies advisory SOLID review to C++ classes and C
  modules and APIs.
- `reviewing-cc-quality` documents the direct auditor, coverage, limits,
  statuses, and completion-gate behavior.
- SessionStart loads the compact policy. Stop audits changed C/C++ lines and
  may ask the agent for one correction pass.

The auditor recognizes `.c`, `.h`, `.cc`, `.cpp`, `.cxx`, `.hh`, `.hpp`, and
`.hxx`. Explicit path mode recursively audits supported files and works outside
Git. `--git-diff` audits added lines relative to `HEAD` and every line of an
untracked supported file in the current Git worktree.

## Coverage and severity

The source engine runs without third-party Python packages, a compiler, or a
compilation database. Its lexical checks distinguish code from comments and
literals, but they are not a complete C/C++ parse. Optional Clang coverage runs
only when an exact normalized `compile_commands.json` entry supplies the real
project command; the plugin never guesses flags. Missing or broken Clang leaves
source results intact and adds a non-blocking limitation.

Optional Clang execution is enabled only on POSIX platforms, where the auditor
can terminate the complete compiler process group within its bound. Other
platforms retain source coverage and report `TOOL_CLANG_PLATFORM`. Exact
commands that request native plugins, opaque forwarded options, module/cache
building, artifact output, or crash reproducers are rejected before execution
with a visible Clang limitation. Accepted commands also disable automatic crash
diagnostic files and must identify exactly one positional compilation input,
matching the database entry.

Power of Ten findings use `POT01` through `POT10`. Only findings marked
`gate_eligible=true` can affect the direct CLI status or completion gate.
Errors gate under the default threshold; eligible warnings gate only when the
warning/all threshold is selected. Lower-confidence POT evidence remains
advisory at the default threshold.

`SOLID01` through `SOLID05`, `TOOL_*` findings, and limitations are always
advisory. They never block completion or produce CLI status 1, even at the
strongest threshold. A missing finding means only that no covered signal was
found.

## Completion behavior

The Stop hook audits changed lines with `--fail-on none`, then applies
`RELIABLE_CC_GATE`:

| Value | Completion behavior |
|---|---|
| `errors` | Default. Select eligible Power of Ten errors. |
| `all` | Select eligible Power of Ten warnings and errors. |
| `off` | Emit no completion-hook report and never request correction. |

On the first completion attempt, selected findings can request one correction
pass. If `stop_hook_active` is already true, the second consecutive attempt is
always permitted even when findings remain, preventing a hook loop. Invalid
settings and infrastructure failures fail open with a concise limitation;
ordinary non-Git directories are skipped silently.

`Stop (completed)` means the Stop hook command finished. It does not mean the
plugin, agent, or session stopped, shut down, was disabled, or was uninstalled.

## Configuration

| Variable | Values and meaning |
|---|---|
| `RELIABLE_CC_GATE` | `errors` (default), `all`, or `off`; used only by the Stop hook. |
| `RELIABLE_CC_CLANG` | `auto` (default) or `off`; controls optional semantic analysis. |
| `RELIABLE_CC_COMPILE_COMMANDS` | A `compile_commands.json` file or its directory. This selection is authoritative. |

Without an explicit database, auto mode checks the audit/Git root, `build`,
`out`, and immediate `build-*` and `cmake-build-*` directories. A database is
used only for a source file with an exact entry. Invalid direct-CLI settings
return status 2; optional semantic coverage failures are limitations when the
source audit completed.

## Installed agent usage

From the project you want to inspect, ask the installed agent to use
`$reviewing-cc-quality`. The skill resolves `scripts/audit_cc.py` relative to
its installed `SKILL.md` and runs that absolute path with the target project as
the working directory. There is no separately installed stable shell launcher;
do not guess or hard-code a plugin cache path.

## Source checkout CLI

When developing from a Reliable C/C++ source checkout, run these commands from
the checkout root and pass the project paths you want to inspect:

```sh
python3 skills/reviewing-cc-quality/scripts/audit_cc.py src include --format text --fail-on errors
python3 skills/reviewing-cc-quality/scripts/audit_cc.py --git-diff --format json --fail-on none
```

`--format text|json` defaults to `text`.
`--fail-on errors|warnings|none` defaults to `errors` and changes only the
process status; it never hides findings. Supply explicit paths or `--git-diff`,
not both.

A text report is finding lines followed by limitations and one summary:

```text
src/main.c:4: error POT01: simple control flow violation
summary: 1 error, 0 warnings, 0 info; truncated: no
```

JSON is one deterministic object suitable for tooling:

```json
{
  "schema_version": 1,
  "mode": "git-diff",
  "configuration": {
    "format": "json",
    "fail_on": "none",
    "compile_commands": null,
    "clang": "auto",
    "compilation_databases": []
  },
  "findings": [],
  "limitations": [],
  "truncated": false,
  "summary": {"errors": 0, "warnings": 0, "info": 0}
}
```

Exit statuses are:

| Status | Meaning |
|---|---|
| 0 | The selected eligible POT threshold was not met; always for `--fail-on none`. |
| 1 | An eligible POT error met `errors`, or an eligible warning/error met `warnings`. |
| 2 | The invocation/configuration was invalid, or a required Git, path, read, or decode input failed. |

## Suppressions

Use a suppression only for a confirmed false positive or approved exception,
on the finding's exact line, with one exact rule code and a concrete rationale:

```c
for (;;) { /* quality: ignore[POT02] - top-level scheduler loop */
    dispatch_next_event();
}
```

Wildcard, comma-separated, file-wide, off-line, and rationale-free forms do
not suppress a finding. A malformed form produces advisory
`TOOL_SUPPRESSION` evidence. A valid suppression removes only that rule on that
line and does not certify the exception.

## Resource limits

| Scope | Limit |
|---|---|
| Direct audit | 60 seconds total |
| Source | 500 files; 2 MiB per file; 20 MiB total; 500 findings |
| Git discovery | 20 MiB combined output within the audit deadline |
| Compilation database | 16 MiB |
| Clang | 10 translation units; 5 seconds and 16 MiB captured output per unit |
| Stop hook | 15-second audit inside a 20-second host timeout; 2 MiB audit capture |
| Stop report | First 12 findings; first 5 limitations; 16,384-character bounded messages |

Exceeding an analysis cap adds a visible limitation and marks the report
truncated where applicable. Never treat a truncated report as complete
coverage.

## Contributing

Read [CONTRIBUTING.md](CONTRIBUTING.md) before changing rules, severity,
coverage, or gate behavior. Bugs and focused improvements are welcome in the
[issue tracker](https://github.com/reliable-coding-agents/reliable-cc/issues).
