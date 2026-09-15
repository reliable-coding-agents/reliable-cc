# Contributing

Thanks for helping improve Reliable C/C++.

## Changes to rules and behavior

Open an issue before adding or materially changing an engineering rule,
checker, severity, suppression, completion-gate policy, or resource limit. The
proposal should identify:

- the failure mode and affected C/C++ language scope;
- what evidence the implementation can and cannot establish;
- false-positive and false-negative risks;
- whether the result is advisory or gate eligible;
- deterministic positive, negative, suppression, and boundary tests;
- any effect on source-only and exact-command Clang coverage.

Documentation, implementation, and tests must ship together. Do not describe a
planned check as implemented, treat SOLID or tool evidence as gating, claim
certification, broaden suppressions, or silently relax a published limit.
Version 0.1.1 ideas belong in the post-release backlog until separately
approved; they are not part of the functional v0.1.0 contract.

## Development

Runtime code and tests use only the Python standard library. Keep the source
engine usable without Clang. Semantic analysis must use an exact compilation
database command and fail open as a visible limitation when optional tooling is
unavailable.

Add focused unit and integration tests before implementation. Preserve
deterministic ordering, one-based source locations, changed-line behavior,
explicit status semantics, and all resource caps. Run the direct text and JSON
smokes when changing output, discovery, configuration, or analysis.

Generated Python caches and compiler, binary, and coverage products are
ignored, along with a small set of exact generated control/cache files at the
repository root: `compile_commands.json`, `CMakeCache.txt`,
`cmake_install.cmake`, `.ninja_deps`, and `.ninja_log`. Build, output, and CMake
directories; nested compilation databases; `build.ninja`; user-authored C/C++
source and headers; build definitions; and fixtures remain trackable. Do not
add generated artifacts to commits.

## Branches and releases

The corrected functional `v0.1.0` is the repository's initial release. Future
work follows the same Git Flow variant as Reliable Python:

- `main` contains released code and annotated `vX.Y.Z` tags;
- `develop` integrates work for the next release;
- `feature/*` and `bugfix/*` branches start from and merge into `develop`;
- planned releases merge `develop` into `main` with `--no-ff`;
- urgent `hotfix/X.Y.Z` branches start from `main` and merge into both branches.

Never tag an unreleased branch. Immediately after a planned release, `main` and
`develop` must have identical trees. A release change must keep the Claude
manifest, Codex manifest, Claude marketplace entry, README badge, and package
contract tests on exactly the same semantic version.

Publication, marketplace copying, cache busting, reinstalling, tagging, and
release creation happen only in the release task after source validation; they
are not ordinary feature-branch steps.

## Validation

Run every check before requesting review:

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

Commits and pull-request titles use `type: imperative summary`, such as
`feat: audit bounded loop evidence` or `fix: preserve source audit on clang
failure`.
