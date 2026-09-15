---
name: applying-power-of-ten-to-c-cpp
description: Use when writing, changing, or reviewing C or C++ under Reliable C/C++ Power of Ten constraints, especially loops, allocation, recursion, macros, assertions, pointers, callbacks, unchecked results, function size, scope, or compiler warnings.
---

# Applying Power of Ten to C/C++

## Overview

Apply the Power of Ten as review constraints, and report automated evidence
separately from manual judgment. A clean audit is not proof of safety,
correctness, or compliance and is not a certification.

## Workflow

1. Inspect project instructions, the build, and the compilation database.
2. Read [the complete rule profile](references/power-of-ten.md) before
   classifying a finding, exception, suppression, or gate outcome.
3. Review all ten rules. The source scanner implements only conservative
   lexical checks; optional Clang analysis adds semantic evidence only for an
   exact compilation-database entry.
4. For each finding, report the rule ID, evidence, coverage (`source` or
   `clang`), gate status, remediation, and any documented exception.
5. Run the direct audit through `$reviewing-cc-quality`, then run the project's
   compiler, static analyzers, and tests. Fix confirmed findings and re-run.

## Interpretation Contract

Use these terms consistently:

| Term | Meaning |
|---|---|
| Blocking | A gate-eligible Power of Ten finding meets the active threshold. |
| Advisory | Review evidence that does not block under the current threshold. |
| Limitation | Analysis was unavailable or bounded; coverage is reduced, not failed. |
| Suppressed | One exact finding has a same-line rationale; it is not certified safe. |

An intentionally non-terminating scheduler is a POT02 exception only when the
outer loop is explicit and each work cycle is bounded. Initialization-time
allocation is still reported as POT03 because the scanner cannot prove the
lifecycle phase. A function-pointer callback is still POT09 evidence; an ABI or
hardware callback requirement must be documented as a narrow project-approved
deviation. None is automatically acceptable because of a function name.

## Common Mistakes

- Declaring the project compliant because the audit is clean.
- Treating missing Clang or compilation data as a clean semantic result.
- Assuming every warning blocks, or that every Power of Ten finding is
  advisory.
- Adding assertions, bounds, or suppressions only to silence the tool.
- Using a wildcard, file-wide, rationale-free, or off-line suppression.
