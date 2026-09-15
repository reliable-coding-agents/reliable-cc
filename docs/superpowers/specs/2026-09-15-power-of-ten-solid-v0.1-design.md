# Reliable C/C++ Power of Ten and SOLID v0.1 Design

## Summary

Reliable C/C++ v0.1.0 will be the first functional release of the plugin. It
will combine dependency-free source checks, optional Clang-backed semantic
analysis, concise session guidance, and a one-pass completion gate for changed
C and C++ code.

The package will implement all ten of Gerard J. Holzmann's Power of Ten rules
as either high-confidence automated checks or explicit review guidance. SOLID
will be presented as a design-review framework with carefully labeled static
evidence, never as something the tool can prove mechanically.

The already-published scaffold must not remain a numbered release. Before
publication, its GitHub release and tag will be replaced, and the completed
plugin will be squashed into the repository's actual initial `v0.1.0` commit.
The marketplace and organization profile will then describe the functional
release rather than a scaffold.

## Goals

- Catch high-confidence violations of the Power of Ten in changed C and C++
  code before an agent completes a turn.
- Keep lower-confidence Power of Ten and SOLID observations useful without
  overstating what static analysis can establish.
- Work without third-party Python packages or a compiler.
- Add semantic checks when Clang and an exact compilation-database entry are
  available.
- Support Codex and Claude Code from one shared implementation.
- Fail open on infrastructure problems and prevent completion-hook loops.
- Provide a direct command-line audit with human-readable and JSON output.

## Non-goals

- Certifying code as safe, secure, correct, or SOLID.
- Replacing a project build, test suite, formatter, compiler, or full static
  analysis system.
- Guessing include paths, language standards, defines, or other compiler
  arguments when no compilation database is available.
- Proving loop termination, identifying an application's initialization phase,
  judging assertion quality, or proving design principles from syntax alone.
- Running an entire project build from a completion hook.
- Shipping or installing Clang, clang-tidy, CMake, or cppcheck.

## Sources and interpretation

The Power of Ten profile follows Holzmann's original C-oriented rules and
rationale:

- <https://spinroot.com/p10/>
- <https://spinroot.com/gerard/pdf/P10.pdf>

The semantic adapter uses documented Clang facilities:

- <https://clang.llvm.org/docs/IntroductionToTheClangAST.html>
- <https://clang.llvm.org/docs/JSONCompilationDatabase.html>

SOLID is adapted for C++ class design and C module/API design. Findings are
review prompts because the principles describe architectural intent rather
than syntax:

- <https://en.wikipedia.org/wiki/SOLID>

## Architecture

The plugin uses a hybrid analyzer with one normalized finding model.

### `skills/reviewing-cc-quality/scripts/audit_cc.py`

This is the public CLI and orchestration layer. It discovers files and changed
lines, loads configuration, calls the source and Clang engines, deduplicates
and sorts findings, applies suppressions, enforces output limits, and renders
text or JSON.

Each finding contains:

- stable rule code;
- `error`, `warning`, or `info` severity;
- source path and one-based line number;
- concise message and remediation;
- `source` or `clang` engine label;
- whether the finding is eligible for completion gating.

### `skills/reviewing-cc-quality/scripts/source_checks.py`

This dependency-free engine tokenizes enough C and C++ to distinguish code
from comments, string literals, character literals, raw strings, and
preprocessor lines while preserving line numbers. It performs only checks that
remain high confidence under this limited model. It does not pretend to be a
complete parser.

### `skills/reviewing-cc-quality/scripts/clang_analysis.py`

This optional engine locates an exact entry in `compile_commands.json`, runs
the matching translation unit through Clang with bounded time and output, and
converts compiler diagnostics and JSON AST evidence into normalized findings.
It never invents compiler flags. Source files without an exact entry and
headers without a reliably associated translation unit retain source-level
coverage and receive a non-blocking limitation notice.

The adapter removes output/dependency-generation arguments from the recorded
command, preserves the remaining project arguments, substitutes the available
Clang driver, and requests syntax-only diagnostics plus JSON AST output.
Unparseable output is an infrastructure limitation, not a code violation.

### Hooks

`hooks/session_start.py` injects the compact entry policy and points the agent
to the focused rule skills. `hooks/stop_quality_gate.py` invokes the auditor in
changed-line JSON mode, selects gate-eligible findings, and emits the common
Codex/Claude Code completion decision shape. `hooks/hooks.json` registers both
events with bounded timeouts.

### Skills

The plugin will contain uniquely named skills so it can coexist with Reliable
Python:

- `using-reliable-cc` — compact injected entry policy and routing guide;
- `applying-power-of-ten-to-c-cpp` — all ten rules, C/C++ interpretation,
  exceptions, and suppression expectations;
- `applying-solid-to-cpp` — C++ class-design and C module/API review guidance;
- `reviewing-cc-quality` — direct audit workflow and tool-limit explanation.

## Audit scope and data flow

Supported extensions are `.c`, `.h`, `.cc`, `.cpp`, `.cxx`, `.hh`, `.hpp`,
and `.hxx`.

For explicit paths, the CLI recursively audits supported files beneath those
paths while respecting hard file and byte limits. For `--git-diff`, it uses
the Git working tree rooted at the current directory, audits tracked changes
against `HEAD`, and treats every line of an untracked supported file as
changed. Findings outside changed lines are omitted in diff mode.

The completion hook:

1. reads the host payload and working directory;
2. exits successfully without output outside a Git working tree;
3. runs `audit_cc.py --git-diff --format json --fail-on none`;
4. filters findings according to the configured gate level;
5. returns the first completion attempt for one correction pass when necessary;
6. always permits the second consecutive completion attempt when
   `stop_hook_active` is already true.

This behavior never stops the plugin, agent, or user session. It only asks the
agent for at most one additional correction pass.

## Rule model

Power of Ten findings use `POT01` through `POT10`. SOLID findings use
`SOLID01` through `SOLID05`. Tool limitations use a separate `TOOL` prefix and
are never gate eligible.

### Gate-eligible Power of Ten findings

- **POT01 — simple control flow:** the source engine reports `goto`, `setjmp`,
  and `longjmp` as errors. The Clang engine reports direct or indirect
  recursion as an error only when a complete analyzed call cycle confirms it.
- **POT04 — small functions:** a function confirmed to exceed 60 physical
  source lines is an error. Clang source ranges are authoritative. The source
  engine may report an obvious brace-balanced definition, but uncertain or
  macro-obscured boundaries are warnings instead.
- **POT08 — restricted preprocessing:** token pasting, stringification,
  variadic macros, directives embedded in macro replacements, and confirmed
  recursive macro expansion are errors. Include guards and simple object-like
  constants are allowed. General conditional compilation and function-like
  macros are warnings because legitimate portability uses are common.
- **POT09 — restricted pointers:** Clang-confirmed function-pointer types and
  expressions requiring more than two pointer dereferences are errors.
  References and smart pointers are not counted as raw pointer indirection.
- **POT10 — zero compiler warnings:** compiler warnings or errors attributed to
  changed lines are errors when produced from an exact compilation-database
  command. Diagnostics elsewhere are excluded from diff-mode gating.
- **POT07 — checked results:** a changed-line diagnostic for a discarded
  `[[nodiscard]]` or `warn_unused_result` value is an error. Other discarded
  non-void results remain warnings.

### Advisory Power of Ten findings

- **POT02:** loops without a mechanically visible upper bound are warnings.
  The tool cannot prove termination or determine whether an intentional
  infinite scheduler is appropriate.
- **POT03:** explicit `malloc`, `calloc`, `realloc`, `aligned_alloc`, `alloca`,
  `new`, and `new[]` sites are warnings. The tool cannot reliably distinguish
  initialization from steady-state execution.
- **POT05:** the auditor reports assertion counts and low assertion density as
  warnings, excluding obvious accessors and trivial functions. Assertion
  usefulness remains a review judgment.
- **POT06:** mutable file-scope state and unnecessarily broad declarations are
  warnings when Clang can establish them.
- **POT07:** unchecked non-void returns and missing input validation are
  warnings unless a compiler attribute makes the contract definite.
- **POT08:** ordinary function-like macros and non-guard conditional
  compilation are warnings.

### SOLID findings

SOLID findings are always advisory, including when the gate level is `all`.
They describe evidence to review, not proven design violations.

- **SOLID01 — single responsibility:** unusually large classes, modules, or
  public APIs and clusters of unrelated member dependencies may prompt review.
- **SOLID02 — open/closed:** repeated concrete-type branching or modification
  points may prompt review.
- **SOLID03 — Liskov substitution:** suspicious override behavior, hidden base
  overloads, unsupported-operation stubs, and unsafe polymorphic destruction
  may prompt review.
- **SOLID04 — interface segregation:** broad abstract interfaces and
  implementers that ignore or reject operations may prompt review.
- **SOLID05 — dependency inversion:** high-level code directly constructing or
  depending on concrete infrastructure may prompt review.

For C, the skill maps these questions to translation-unit responsibilities,
header/API width, opaque types, callbacks, function tables, and dependency
direction. The scanner does not manufacture class-oriented findings for C.

## Severity and gate policy

`RELIABLE_CC_GATE` accepts:

- `errors` — default; block only gate-eligible Power of Ten errors;
- `all` — block gate-eligible Power of Ten errors and warnings;
- `off` — report nothing from the completion hook and never block.

Invalid values fail open with a concise system message. SOLID and `TOOL`
findings never block under any setting.

`RELIABLE_CC_CLANG` accepts `auto` or `off` and defaults to `auto`.
`RELIABLE_CC_COMPILE_COMMANDS` may name a compilation database or its
directory. In `auto` mode, the analyzer checks, in order, the Git root, its
`build` and `out` directories, and immediate `build-*` and `cmake-build-*`
directories. A candidate must contain an exact normalized entry for the source
file. If several candidates match, the shortest lexically sorted path wins and
the selected path is reported. Missing Clang, missing entries, and unusable
commands preserve source checks and produce a non-blocking limitation summary.

The CLI also accepts `--format text|json`,
`--fail-on errors|warnings|none`, `--git-diff`, and explicit paths. CLI failure
policy is independent of the completion hook so CI can choose its own
threshold.

JSON output is a single object containing a schema version, audit mode,
effective configuration, sorted findings, limitations, truncation state, and
summary counts. Exit status `0` means the selected threshold was not met,
status `1` means it was met, and status `2` means the requested audit could not
be performed because of invalid invocation, invalid configuration, or a
required source/input failure. Optional Clang limitations do not cause status
`2` because the source audit still completed. `--fail-on errors` considers
gate-eligible Power of Ten errors; `--fail-on warnings` also considers
gate-eligible Power of Ten warnings. SOLID and `TOOL` findings never determine
the exit status.

## Suppressions

An exceptional or false-positive finding may be suppressed on the relevant
line with a narrow comment:

```c
for (;;) { /* quality: ignore[POT02] - top-level scheduler loop */
    dispatch_next_event();
}
```

The syntax requires an exact rule code and a non-empty rationale. Blanket,
file-wide, wildcard, or rationale-free suppressions are rejected. A malformed
suppression is reported as a non-gate-eligible warning so it cannot silently
disable checks.

## Limits and failure handling

The auditor examines at most 500 files, 2 MiB per source file, and 20 MiB of
source text per run. Semantic analysis covers at most ten changed translation
units, permits five seconds and 16 MiB of captured output per unit, and records
at most 500 diagnostics/findings. The completion hook gives the entire auditor
15 seconds inside its host-level 20-second timeout; a direct audit receives a
60-second total budget. Exceeding any cap stops that portion of analysis and
adds a limitation instead of silently dropping work. The completion hook
reports no more than twelve actionable findings and includes the remaining
count.

Source read failures, missing executables, timeouts, malformed JSON, invalid
compilation databases, and unexpected analyzer failures all fail open in the
hook with a short limitation message. The direct CLI reports the same problem
and returns a distinct infrastructure-error status where appropriate. Code
findings and tool failures are never conflated.

## Testing strategy

All tests remain dependency-free and use the Python standard library.

- Tokenizer tests cover comments, escaped strings and characters, raw strings,
  preprocessor continuations, braces, and preserved line numbers.
- Every source rule has positive, negative, suppression, and false-positive
  regression cases in both C and C++ where applicable.
- Clang tests use a deterministic fake executable and fixture compilation
  databases for commands, diagnostics, AST evidence, timeouts, malformed
  output, missing entries, and argument sanitization.
- A real-Clang smoke test runs when Clang is installed but is not required for
  the portable test suite.
- Git integration tests cover staged, unstaged, untracked, renamed, deleted,
  and unchanged files plus repositories without `HEAD`.
- Hook tests cover clean results, `errors`, `all`, `off`, invalid settings,
  non-Git directories, first and second completion attempts, truncation, and
  every fail-open path.
- Package tests validate manifests, hook commands, executable paths, skill
  uniqueness, skill metadata, version consistency, and absence of generated
  artifacts.
- Source and vendored marketplace copies must pass the Codex plugin validator,
  Claude Code plugin validator, skill validator, and complete test suite.

## Documentation and packaging

The README will explain installation, supported languages, the distinction
between source and Clang coverage, exact blocking behavior, configuration,
suppression policy, CLI examples, and the non-certification boundary. The
manifests will describe actual auditing and completion-gate capabilities.

The package will include hook scripts directly and use only paths relative to
the resolved plugin root, preventing cache/install-path assumptions. Session
and completion hooks must work identically from source, an installed Codex
plugin, an installed Claude Code plugin, and the marketplace-vendored copy.

## Release and history replacement

The functional plugin is `v0.1.0`; the public scaffold must not consume that
version number. After implementation and verification:

1. squash the scaffold, design, and implementation into one root commit whose
   tree is the completed functional plugin;
2. force-update `main` and `develop` to that root commit;
3. delete and recreate the public `v0.1.0` tag and GitHub release;
4. replace the marketplace subtree with the verified source tree while keeping
   its catalog version at `0.1.0`;
5. update the organization Overview and repository metadata to describe the
   functional gates;
6. verify clean fresh clones, installation paths, manifests, hooks, skills,
   tests, tag contents, marketplace equality, and release assets/notes.

This destructive history replacement is explicitly authorized because the
scaffold should never have been published as a numbered release. No later
functional release history exists to preserve.

## Acceptance criteria

- All ten Power of Ten rules are documented and represented honestly as
  blocking checks, advisory checks, or review guidance.
- SOLID guidance covers C++ and C without claiming mechanical proof.
- The completion hook audits only changed C/C++ lines, blocks at most once, and
  silently skips non-Git directories.
- Dependency-free checks run without Clang; semantic checks run only with an
  exact compilation command.
- Missing or broken analysis infrastructure cannot block completion.
- Direct text and JSON audits are deterministic, bounded, and suppressible only
  with narrow rationales.
- Codex and Claude Code packages expose the same behavior and pass their
  validators.
- The final source repository is one functional root commit tagged `v0.1.0`,
  with matching marketplace and organization documentation.
