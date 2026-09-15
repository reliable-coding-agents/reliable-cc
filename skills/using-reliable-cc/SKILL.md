---
name: using-reliable-cc
description: Use when a user asks about Reliable C/C++ or reliable-cc, or when writing, changing, reviewing, auditing, or troubleshooting C and C++ under the installed plugin policy.
---

# Using Reliable C/C++

## Entry Policy

Apply this policy to C and C++ code written or changed in the session. Inspect
project instructions, language standards, build configuration, compiler,
analyzers, formatter, and tests before editing; project-specific requirements
remain authoritative.

Reliable C/C++ combines dependency-free source evidence with optional Clang
analysis from an exact compilation command. Neither a clean result nor a
suppression certifies safety, correctness, or compliance.

## Route by Task

| Need | Required route |
|---|---|
| Write, change, or review C/C++ against Power of Ten constraints; interpret POT01–POT10, exceptions, or suppressions | Use `$applying-power-of-ten-to-c-cpp`. |
| Review C++ classes/interfaces or C modules/APIs using SOLID design questions | Use `$applying-solid-to-cpp`. |
| Run the audit CLI; interpret JSON/status, gate behavior, Clang coverage, limits, or troubleshooting | Use `$reviewing-cc-quality`. |

Invoke every relevant route for a mixed request. Route instead of reproducing
specialist rule tables or command details here.

## Invariants

- Gate-eligible Power of Ten evidence can request at most one correction pass;
  a later completion attempt proceeds to prevent a loop.
- SOLID findings and tool limitations are advisory and never block completion.
- Missing Clang means reduced semantic coverage, not a clean semantic result.
- `Stop (completed)` means the Stop hook command finished. It does not mean the
  plugin or session stopped, was disabled, or was uninstalled.

## Finish

Run the applicable Reliable C/C++ audit, then the project's compiler with its
strict warning policy, static analyzers, and relevant tests. Inspect the diff,
fix confirmed findings, and report remaining exceptions and coverage
limitations without claiming certification.
