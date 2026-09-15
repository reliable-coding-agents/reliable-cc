# Reliable C/C++ Power of Ten Profile

## Contents

- Scope and status terms
- Automated rule map
- Rule-by-rule review guide
- C and C++ examples
- Exceptions and suppressions
- Coverage limits
- Sources

## Scope and Status Terms

This profile follows Gerard J. Holzmann's C-oriented Power of Ten rules. It
uses `POT01` through `POT10` for findings, but it does not certify safety,
correctness, or literal compliance. Review the whole program and its operating
assumptions even when the changed-line audit is clean.

The default completion gate blocks only gate-eligible errors. The `all` gate
also blocks gate-eligible warnings. Advisory-only findings never block. A
direct audit uses the equivalent `errors`, `warnings`, or `none` threshold.
SOLID and `TOOL` findings are outside this rule map and never affect a Power of
Ten gate decision.

## Automated Rule Map

| Rule | Original constraint | Reliable C/C++ evidence | Status |
|---|---|---|---|
| POT01 | Use simple control flow; no `goto`, `setjmp`/`longjmp`, or recursion. | Source finds jump constructs. Clang finds only complete direct/indirect call cycles it can confirm. | Error; blocks at the default threshold. |
| POT02 | Give every terminating loop a fixed, statically provable upper bound. | Source warns on empty-condition `for` loops and `while`/`do` loops without a narrowly recognizable constant bound. | Advisory-only warning. |
| POT03 | Do not allocate dynamically after initialization. | Source warns on `malloc`, `calloc`, `realloc`, `aligned_alloc`, `alloca`, and C++ `new` sites. | Advisory-only warning. |
| POT04 | Keep functions at 60 physical source lines or fewer. | Confirmed source or Clang ranges over 60 lines are errors; macro-obscured source ranges are warnings. | Confirmed error blocks by default; uncertain warning is advisory-only. |
| POT05 | Average at least two meaningful assertions per nontrivial function. | Source warns when a function over 10 lines with at least two statements has fewer than two standard `assert(...)` calls. | Advisory-only heuristic warning. |
| POT06 | Declare data at the smallest useful scope. | Clang warns on mutable, non-`extern` file-scope variables. | Warning; blocks only with the `all`/`warnings` threshold. |
| POT07 | Validate parameters and handle every non-void result. | Clang warns on discarded non-void calls; attributed `[[nodiscard]]`/`warn_unused_result` diagnostics are errors. Parameter validation remains manual review. | Ordinary warning blocks only with `all`; attributed error blocks by default. |
| POT08 | Limit preprocessing to includes and simple macros. | Source errors on token pasting, stringification, variadics, embedded directives, and direct self-reference; it warns on function-like macros and non-guard conditionals. | Restricted-form error blocks by default; ordinary warning is advisory-only. |
| POT09 | Use pointers sparingly: no more than one dereference level, no function pointers, and no dereferences hidden in macros or typedefs. | Clang errors on function-pointer types and expressions with more than two raw dereference operations. It does not generally validate declaration/API pointer depth or dereferences hidden by macros or typedefs. | Error; blocks at the default threshold. |
| POT10 | Enable strict compiler warnings and static analysis; finish with zero warnings. | Exact compilation commands let Clang turn other attributable diagnostics on recorded-source changed lines into POT10 errors. Unused-result diagnostics remain POT07. Reliable C/C++ does not add warning flags or run a project-wide analyzer. | Error; blocks at the default threshold. |

## Rule-by-Rule Review Guide

### POT01 — Simple Control Flow

Replace jumps with structured branches and recursion with bounded iteration or
an explicit bounded work stack. The source engine does not see recursion;
Clang reports a cycle only when all required definitions and attributable call
sites are present. Virtual or external calls can leave the call graph
incomplete.

A strict safety profile has no automatic cleanup-`goto`, generated-code, or
bounded-recursion exception. A project may approve a narrow deviation, but the
suppression records that decision rather than proving it safe.

### POT02 — Bounded Loops

Make a preset maximum mechanically visible and define the failure action when
the maximum is exhausted. The source recognizer is deliberately narrow: a
more sophisticated but valid bound may still warn.

The original rule excludes loops intended never to terminate, such as a task
scheduler. Keep the outer scheduler loop explicit, bound each unit of work,
and document the exception on the loop line.

### POT03 — Initialization-Only Allocation

Prefer fixed-capacity storage, pools initialized once, or caller-provided
buffers. The scanner reports allocation syntax wherever it occurs because it
cannot prove that `init`, a constructor, or a startup callback executes only
during initialization.

One-shot allocation before threads or steady-state processing is the intended
exception. Document evidence for that lifecycle boundary; a function name
alone is insufficient. The check reports allocation sites, not leaks, frees,
capacity bounds, or allocator timing.

### POT04 — Small Functions

Split a function over 60 physical source lines into cohesive, independently
reviewable operations. Do not compress several statements onto one line to
meet the count. The plugin's threshold is fixed at 60 even though the original
profile allows a project to select a reasonable value near that size.

Macro-obscured boundaries are uncertain and therefore advisory. Generated or
externally maintained code may justify a project deviation, but ordinary
complexity does not.

### POT05 — Assertion Density

Use side-effect-free checks for invariants, preconditions, postconditions, and
loop invariants, with an explicit recovery path appropriate to the system.
Never add `assert(true)` or duplicate checks to satisfy a count. Standard
`assert` may be disabled by `NDEBUG`, so the safety case must establish its
runtime behavior.

The implementation's count is only a review heuristic. Trivial accessors and
functions without a meaningful invariant can be legitimate low-density cases;
explain the reasoning rather than manufacturing assertions.

### POT06 — Narrow Scope

Move state into the smallest block, function, object, or translation unit that
needs it. The Clang check covers mutable file-scope variables, not every overly
broad local, declaration, header exposure, alias, or mutation path.

Memory-mapped device state or deliberately shared process state can be a
necessary exception. Minimize its visibility, state its concurrency and
ownership rules, and justify it on the finding line. `extern` or `const` can
avoid this heuristic but does not itself prove proper scope.

### POT07 — Checked Results and Inputs

Handle non-void results, propagate failures, and validate pointer, range,
enum, size, and index parameters at the boundary that owns the contract. An
explicit `(void)call()` is appropriate only when success and failure require
the same response and the decision is documented.

Clang can identify discarded results but cannot generally prove adequate
parameter validation. Missing `[[nodiscard]]` or `warn_unused_result`
contracts reduce the confidence of result checking.

### POT08 — Restricted Preprocessing

Prefer typed functions, `constexpr` values, templates, or ordinary language
configuration. Conventional include guards and simple object-like constants
are accepted. Platform conditionals and simple function-like macros can be
legitimate portability boundaries, but they remain advisory and need manual
review for complete syntactic units and test-matrix growth.

The lexer does not expand macros or prove that every macro is defined only in
a header and expands to a complete syntactic unit.

### POT09 — Restricted Pointers

The strict original rule permits no more than one pointer-dereference level,
forbids function pointers, and forbids hiding dereferences in macros or
typedefs. Prefer direct, named call relationships and manually review pointer
depth across declarations, APIs, aliases, and macro expansions.

Reliable C/C++ v0.1.0 is narrower: its Clang rule finds function-pointer types
and expressions with more than two raw dereference operations. Absence of a
finding at one or two operations does not establish strict POT09 compliance.
The check does not count C++ references or smart pointers and does not
generally reveal macro- or typedef-hidden depth; those still require review.

A C callback, interrupt vector, C ABI, or hardware dispatch table may require
a function pointer. It is not automatically accepted: record and test that
boundary as a narrow project deviation.

### POT10 — Zero Warnings and Static Analysis

Compile every supported configuration with the project's strict warning flags
and run the project's analyzers from the start of development. Resolve
warnings by fixing or clarifying code rather than casually disabling them.

Reliable C/C++ preserves flags from an exact `compile_commands.json` entry; it
does not add `-Wall`, `-Wextra`, `-pedantic`, or a separate project analyzer.
After `unused-result`, `nodiscard`, and `warn_unused_result` diagnostics are
classified as POT07, other attributable warnings/errors on the recorded source
file's analyzed changed lines become POT10. A clean POT10 result therefore
describes only diagnostics emitted in that scope under the recorded command.

## C and C++ Examples

An intentional C scheduler can document the POT02 exception while keeping
bounded work inside one cycle:

```c
void scheduler(void) {
    for (;;) { /* quality: ignore[POT02] - top-level scheduler; run_cycle is bounded */
        run_cycle();
    }
}
```

Initialization-time C allocation remains visible because naming does not prove
the lifecycle:

```c
void transport_init(void) {
    buffer = malloc(BUFFER_CAPACITY); /* quality: ignore[POT03] - one-shot startup before worker launch */
}
```

A required C++ ABI callback is still POT09 evidence at its declaration:

```cpp
using Completion = void (*)(int); // quality: ignore[POT09] - callback type required by vendor C ABI
```

When the ABI does not require a function pointer, prefer a statically resolved
typed callable or direct call and verify that its lifetime and resource use
remain bounded.

## Exceptions and Suppressions

Use a suppression only for a confirmed false positive or an exceptional,
project-approved deviation. It must be a source comment on the exact finding
line, name exactly one rule code, and contain a non-empty rationale:

```c
for (;;) { /* quality: ignore[POT02] - top-level process scheduler */
    dispatch_one_bounded_event();
}
```

The accepted form is:

```text
quality: ignore[POT02] - concrete rationale
```

Wildcards, comma-separated codes, file-wide directives, missing rationales,
and comments on another line do not suppress the finding. A malformed form
produces a non-gating `TOOL_SUPPRESSION` warning. A valid suppression removes
only the matching code on that line; it does not suppress other rules, waive
manual review, or certify the exception.

## Coverage Limits

- Source analysis is a conservative lexer, not a C/C++ parser.
- Clang analysis requires the driver and an exact normalized compilation-
  database entry; it never guesses includes, defines, targets, or standards.
- Missing or unusable Clang produces a non-blocking limitation while source
  findings remain available. This is reduced semantic coverage, not proof that
  semantic rules pass.
- Direct audits can cover explicit paths; the completion hook considers
  changed C/C++ lines and asks for at most one correction pass.
- Generated code, macro expansions, headers without an attributable
  translation unit, virtual dispatch, external callees, and build variants can
  remain outside semantic coverage.

## Sources

The rule names and original constraints come from Gerard J. Holzmann's
[Power of Ten rule index](https://spinroot.com/p10/) and
[published paper](https://spinroot.com/gerard/pdf/P10.pdf). The status and
coverage columns above describe Reliable C/C++ v0.1.0's implementation, not an
expansion of the original rules.
