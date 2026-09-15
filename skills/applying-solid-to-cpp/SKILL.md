---
name: applying-solid-to-cpp
description: Use when reviewing or changing C++ classes, hierarchies, interfaces, C modules or APIs, dependency boundaries, substitutability, cohesion, or any C/C++ design described in terms of SOLID principles.
---

# Applying SOLID to C++ and C

## Core Boundary

SOLID is design reasoning. Static findings are evidence, never proof or
certification; intent, client needs, contracts, ownership, and dependency
direction exceed syntax.

Every `SOLID01`–`SOLID05` finding is an advisory warning with
`gate_eligible=false`. SOLID never blocks completion or changes CLI status,
even under `all`/`warnings`; only eligible Power of Ten findings can.

## Implemented Evidence

These are all v0.1.0 SOLID heuristics. They require Clang and an exact
compilation entry; the source scanner emits none.

| ID | Implemented C++ signal |
|---|---|
| SOLID01 | A complete class spans more than 300 physical lines. |
| SOLID02 | No automated heuristic; open/closed review is manual. |
| SOLID03 | An override only throws, or a polymorphic root has a public non-virtual destructor. |
| SOLID04 | An abstract interface declares more than 10 pure virtual methods. |
| SOLID05 | A type ending `Service`, `Controller`, `Manager`, or `UseCase` directly constructs a type ending `Database`, `Repository`, `Socket`, `FileSystem`, or `HttpClient`. |

Thresholds and names are prompts, not definitions. Missing Clang reduces
coverage; C modules receive no automated SOLID findings.

## Principle Map

| Principle | C++ review | C module/API review |
|---|---|---|
| Single responsibility | Keep one cohesive reason to change; separate policy, parsing, persistence, transport, and ownership. | Give each translation unit/header one purpose; separate unrelated state and APIs. |
| Open/closed | Add proven variants through stable seams, not speculative abstractions. | Review repeated tags; use callbacks, function tables, or modules for real variants. |
| Liskov substitution | Preserve pre/postconditions, invariants, lifetime, errors, and exceptions. | Interchangeable function tables must preserve callback, status, buffer, handle, and lifecycle contracts. |
| Interface segregation | Shape interfaces around actual clients; split unusable operations. | Keep headers and function tables limited to client needs. |
| Dependency inversion | Inject infrastructure behind policy-owned abstractions and explicit ownership. | Pass context/opaque handles or focused function tables instead of hard-wired global I/O. |

C callbacks and function tables can conflict with Power of Ten POT09. Treat
them as an explicit design tradeoff, never an automatically compliant refactor.

## Review Workflow

1. Inspect code, interfaces, clients, tests, and initialization. For prose
   alone, review architecture without claiming an audit ran.
2. Run `$reviewing-cc-quality`; report Clang coverage and limitations.
3. Review all principles manually. Tie each signal to a maintenance, contract,
   coupling, or client cost.
4. Make the smallest tested change addressing that cost, not a threshold.
   Report principle/ID, evidence, impact, coverage, change, and disposition:
   “no covered signal found,” not “SOLID compliant.”

An `OrderManager` that parses, validates, writes SQL, and sends email presents
manual SRP and dependency evidence below 300 lines. A raw database pointer is
an ownership question, not proof.

## Suppressions

Suppress only a confirmed false positive on its exact finding line:

```cpp
class GeneratedAdapter { // quality: ignore[SOLID01] - generated vendor surface reviewed separately
```

Exact code and rationale are required; broad or rationale-free forms fail.
Suppression removes one signal, not manual review.
