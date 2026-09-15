# Reliable C/C++ Power of Ten and SOLID v0.1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the published Reliable C/C++ scaffold with a functional v0.1.0 plugin that audits changed C/C++ code against high-confidence Power of Ten checks and advisory SOLID evidence.

**Architecture:** A dependency-free lexical analyzer always produces normalized findings, while an optional Clang adapter adds diagnostics and AST-backed evidence only for exact compilation-database entries. Shared SessionStart and Stop hooks expose the policy to Codex and Claude Code and allow at most one corrective completion pass.

**Tech Stack:** Python standard library, C/C++ fixture source, optional Clang JSON AST, Git, Codex and Claude Code plugin manifests.

**Spec:** `docs/superpowers/specs/2026-09-15-power-of-ten-solid-v0.1-design.md`

## Global Constraints

- Keep runtime and tests free of third-party Python dependencies.
- Support `.c`, `.h`, `.cc`, `.cpp`, `.cxx`, `.hh`, `.hpp`, and `.hxx`.
- Default `RELIABLE_CC_GATE` to `errors`; SOLID and `TOOL` findings never gate completion or CLI status.
- Run Clang only with an exact normalized `compile_commands.json` entry; never guess project flags.
- Missing/broken Clang and all hook infrastructure failures fail open with visible limitations.
- Completion auditing covers changed lines only, silently skips non-Git directories, blocks the first attempt at most, and always permits the second.
- Enforce limits of 500 files, 2 MiB per file, 20 MiB total source, ten translation units, five seconds and 16 MiB output per unit, 500 findings, 15 seconds per hook audit, and 60 seconds per direct audit.
- Preserve narrow `quality: ignore[RULE] - rationale` suppressions; reject blanket or rationale-free forms.
- The final source repository is one functional root commit tagged `v0.1.0`; replacing the current scaffold history, tag, and release is explicitly authorized.

## File Structure

- `skills/reviewing-cc-quality/scripts/quality_model.py` — immutable finding, limitation, and report values plus serialization and ordering.
- `skills/reviewing-cc-quality/scripts/source_checks.py` — comment/string-safe lexical view, suppressions, function ranges, and portable Power of Ten checks.
- `skills/reviewing-cc-quality/scripts/clang_analysis.py` — compilation-database discovery, command sanitization, bounded Clang execution, diagnostics, AST traversal, and semantic findings.
- `skills/reviewing-cc-quality/scripts/audit_cc.py` — CLI, path/Git discovery, changed-line filtering, engine orchestration, limits, rendering, and exit policy.
- `hooks/session_start.py` — bounded policy injection.
- `hooks/stop_quality_gate.py` — changed-line audit and one-pass completion decision.
- `hooks/hooks.json` — shared SessionStart and Stop registrations.
- `skills/using-reliable-cc/` — compact entry policy and routing metadata.
- `skills/applying-power-of-ten-to-c-cpp/` — detailed rule guidance and references.
- `skills/applying-solid-to-cpp/` — advisory C++ and C design-review guidance.
- `skills/reviewing-cc-quality/` — CLI workflow, limitations, suppressions, and OpenAI metadata.
- `tests/test_quality_model.py` — normalized schema and ordering.
- `tests/test_source_checks.py` — lexer, suppressions, and portable rules.
- `tests/test_clang_analysis.py` — database selection, command execution, diagnostics, AST rules, and limitations.
- `tests/test_audit_cc.py` — CLI, Git scope, filtering, limits, text/JSON, and exit statuses.
- `tests/test_hooks.py` — both hook contracts and fail-open behavior.
- `tests/test_package.py` — manifests, hooks, skills, version, descriptions, and generated-file exclusions.

---

### Task 1: Normalized quality model

**Files:**
- Create: `skills/reviewing-cc-quality/scripts/quality_model.py`
- Create: `tests/test_quality_model.py`

**Interfaces:**
- Consumes: only Python standard-library dataclasses and JSON-compatible values.
- Produces: `Finding`, `Limitation`, `AuditReport`, `finding_sort_key()`, and `to_dict()` methods used by every later task.

- [ ] **Step 1: Write the failing model tests**

```python
class QualityModelTests(unittest.TestCase):
    def test_report_serializes_stable_schema(self) -> None:
        finding = Finding(
            code="POT01", severity="error", path="src/a.c", line=7,
            message="goto obscures control flow", remediation="use structured control flow",
            engine="source", gate_eligible=True,
        )
        report = AuditReport(mode="paths", findings=(finding,))
        payload = report.to_dict()
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["findings"][0]["code"], "POT01")
        self.assertEqual(payload["summary"], {"errors": 1, "warnings": 0, "info": 0})

    def test_findings_sort_by_path_line_code(self) -> None:
        findings = [make_finding("b.c", 1, "POT08"), make_finding("a.c", 4, "POT01")]
        self.assertEqual([f.path for f in sorted(findings, key=finding_sort_key)], ["a.c", "b.c"])
```

- [ ] **Step 2: Run the tests and confirm the missing module failure**

Run: `python3 -m unittest tests.test_quality_model -v`

Expected: import failure for `quality_model`.

- [ ] **Step 3: Implement frozen dataclasses and stable serialization**

```python
@dataclass(frozen=True)
class Finding:
    code: str
    severity: str
    path: str
    line: int
    message: str
    remediation: str
    engine: str
    gate_eligible: bool

    def to_dict(self) -> dict[str, object]:
        return dataclasses.asdict(self)

@dataclass(frozen=True)
class Limitation:
    code: str
    message: str
    path: str | None = None

    def to_dict(self) -> dict[str, object]:
        return dataclasses.asdict(self)

@dataclass(frozen=True)
class AuditReport:
    mode: str
    findings: tuple[Finding, ...] = ()
    limitations: tuple[Limitation, ...] = ()
    truncated: bool = False
    configuration: Mapping[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        ordered = tuple(sorted(self.findings, key=finding_sort_key))
        return {
            "schema_version": 1,
            "mode": self.mode,
            "configuration": dict(self.configuration),
            "findings": [item.to_dict() for item in ordered],
            "limitations": [item.to_dict() for item in self.limitations],
            "truncated": self.truncated,
            "summary": severity_counts(ordered),
        }
```

Validate severity and engine values in `__post_init__`, require positive lines, and cap neither messages nor collections here; orchestration owns those limits.

- [ ] **Step 4: Run model tests**

Run: `python3 -m unittest tests.test_quality_model -v`

Expected: all tests pass.

- [ ] **Step 5: Commit the model**

```bash
git add skills/reviewing-cc-quality/scripts/quality_model.py tests/test_quality_model.py
git commit -m "feat: add cc audit finding model"
```

### Task 2: Lexical source view and suppression contract

**Files:**
- Create: `skills/reviewing-cc-quality/scripts/source_checks.py`
- Create: `tests/test_source_checks.py`

**Interfaces:**
- Consumes: `Finding` from `quality_model.py`.
- Produces: `LexedSource`, `FunctionRange`, `lex_source(text)`, `function_ranges(lexed)`, `parse_suppressions(text, path)`, and `apply_suppressions(findings, suppressions)`.

- [ ] **Step 1: Write failing lexer tests**

```python
def test_lexer_hides_comments_and_literals_but_preserves_lines(self) -> None:
    text = 'const char *s = "goto raw";\n// goto hidden\nlabel: goto done;\n'
    lexed = lex_source(text)
    self.assertNotIn("goto raw", lexed.code_lines[0])
    self.assertNotIn("goto hidden", lexed.code_lines[1])
    self.assertIn("goto done", lexed.code_lines[2])

def test_lexer_handles_cpp_raw_string_and_directive_continuation(self) -> None:
    text = 'auto s = R"tag(goto { #define X })tag";\n#define JOIN(a, b) a \\\n## b\n'
    lexed = lex_source(text)
    self.assertEqual(len(lexed.code_lines), 3)
    self.assertIn("## b", lexed.directives[0].replacement)

def test_suppression_requires_exact_code_and_rationale(self) -> None:
    valid, malformed = parse_suppressions(
        "for (;;) {} /* quality: ignore[POT02] - scheduler */\n"
        "goto out; /* quality: ignore[*] */\n", "main.c"
    )
    self.assertEqual(valid[1], frozenset({"POT02"}))
    self.assertEqual(malformed[0].code, "TOOL_SUPPRESSION")
```

- [ ] **Step 2: Confirm tests fail before implementation**

Run: `python3 -m unittest tests.test_source_checks.SourceLexingTests -v`

Expected: import/name failures for the lexical API.

- [ ] **Step 3: Implement a single-pass lexer**

Use explicit states `CODE`, `LINE_COMMENT`, `BLOCK_COMMENT`, `STRING`, `CHAR`, and `RAW_STRING`. Replace hidden characters with spaces while retaining every newline and character offset. Join backslash-continued directives into `Directive(start_line, end_line, name, replacement)` values without exposing tokens embedded in strings/comments.

```python
@dataclass(frozen=True)
class LexedSource:
    original_lines: tuple[str, ...]
    code_lines: tuple[str, ...]
    directives: tuple[Directive, ...]

SUPPRESSION_RE = re.compile(
    r"quality:\s*ignore\[([A-Z]+\d{2})\]\s*-\s*(\S(?:.*\S)?)"
)
```

Reject `*`, comma-separated codes, missing rationales, unknown prefixes, and comments that do not sit on the finding line. Preserve valid suppressions as `dict[int, frozenset[str]]`.

- [ ] **Step 4: Add and pass function-range tests**

Cover ordinary functions, constructors, destructors, operators, namespace/class methods, lambdas, control statements, prototypes, initializer lists, comments, and preprocessor-obscured braces. Only definitions with a balanced, non-directive body become `confirmed=True`; uncertain candidates are retained with `confirmed=False`.

Run: `python3 -m unittest tests.test_source_checks.SourceLexingTests tests.test_source_checks.FunctionRangeTests -v`

Expected: all lexer, suppression, and range tests pass.

- [ ] **Step 5: Commit the lexical foundation**

```bash
git add skills/reviewing-cc-quality/scripts/source_checks.py tests/test_source_checks.py
git commit -m "feat: add safe cc source lexer"
```

### Task 3: Dependency-free Power of Ten checks

**Files:**
- Modify: `skills/reviewing-cc-quality/scripts/source_checks.py`
- Modify: `tests/test_source_checks.py`

**Interfaces:**
- Consumes: lexical/suppression APIs from Task 2.
- Produces: `analyze_source(path: pathlib.Path, text: str) -> tuple[Finding, ...]`.

- [ ] **Step 1: Add a table-driven failing test for every portable rule**

```python
CASES = (
    ("POT01", "void f(void) { goto done; done: return; }", "error"),
    ("POT02", "void f(void) { for (;;) { tick(); } }", "warning"),
    ("POT03", "void f(void) { void *p = malloc(8); }", "warning"),
    ("POT04", long_function(61), "error"),
    ("POT05", twelve_line_function_without_assertions(), "warning"),
    ("POT08", "#define JOIN(a,b) a ## b\n", "error"),
)

def test_portable_rule_cases(self) -> None:
    for code, source, severity in CASES:
        with self.subTest(code=code):
            finding = next(item for item in analyze_source(Path("sample.c"), source) if item.code == code)
            self.assertEqual(finding.severity, severity)
```

Add negative tests for identifiers such as `gotohome`, allocation names in comments/strings, bounded `for (i = 0; i < 10; ++i)`, 60-line functions, include guards, simple object macros, and suppressed findings.

- [ ] **Step 2: Run the rule tests and observe missing findings**

Run: `python3 -m unittest tests.test_source_checks.PortableRuleTests -v`

Expected: failures because `analyze_source` does not yet emit the rule cases.

- [ ] **Step 3: Implement token-aware rule detectors**

Implement the agreed profile:

- POT01 errors for standalone `goto` and calls to `setjmp`/`longjmp`.
- POT02 warnings for `for` with an empty condition and every `while`/`do` loop without a recognized constant upper-bound pattern.
- POT03 warnings for allocation calls and C++ `new`/`new[]` tokens.
- POT04 errors for confirmed function bodies exceeding 60 inclusive physical lines; uncertain bodies become warnings.
- POT05 warnings for nontrivial functions longer than ten lines with fewer than two standard `assert(...)` calls.
- POT08 errors for `##`, stringification parameters, `...`/`__VA_ARGS__`, directives in replacements, and direct self-reference; warnings for function-like macros and non-include-guard conditionals.

Every detector returns `Finding` values with stable remediation text and a line anchored to the relevant token. Run all findings through exact-line suppression and append malformed-suppression warnings.

- [ ] **Step 4: Run all source tests and inspect false positives**

Run: `python3 -m unittest tests.test_source_checks -v`

Expected: all cases pass with no findings from comments, strings, identifiers, guards, or allowed simple macros.

- [ ] **Step 5: Commit portable checks**

```bash
git add skills/reviewing-cc-quality/scripts/source_checks.py tests/test_source_checks.py
git commit -m "feat: audit portable power of ten rules"
```

### Task 4: Audit CLI, path discovery, and Git changed lines

**Files:**
- Create: `skills/reviewing-cc-quality/scripts/audit_cc.py`
- Create: `tests/test_audit_cc.py`

**Interfaces:**
- Consumes: `AuditReport`, `Finding`, `Limitation`, and `analyze_source()`.
- Produces: `SUPPORTED_SUFFIXES`, `discover_paths()`, `git_changed_lines()`, `run_audit()`, `render_text()`, `build_parser()`, and `main(argv=None) -> int`.

- [ ] **Step 1: Write failing CLI and Git tests**

```python
def test_json_cli_reports_only_changed_line(self) -> None:
    repo = self.make_repo({"main.c": "void f(void) {\n  return;\n}\n"})
    self.write(repo / "main.c", "void f(void) {\n  goto out;\nout: return;\n}\n")
    result = self.run_cli(repo, "--git-diff", "--format", "json", "--fail-on", "none")
    payload = json.loads(result.stdout)
    self.assertEqual(result.returncode, 0)
    self.assertEqual([(f["code"], f["line"]) for f in payload["findings"]], [("POT01", 2)])

def test_untracked_supported_file_has_all_lines_changed(self) -> None:
    repo = self.make_repo({"README.md": "base\n"})
    self.write(repo / "new.cpp", "void f() { goto out; out: return; }\n")
    self.assertEqual(git_changed_lines(repo)[Path("new.cpp")], frozenset({1}))

def test_fail_on_ignores_solid_and_tool_findings(self) -> None:
    report = AuditReport(mode="paths", findings=(solid_warning(),), limitations=(tool_limit(),))
    self.assertEqual(exit_status(report, "warnings"), 0)
```

- [ ] **Step 2: Run tests and verify CLI absence**

Run: `python3 -m unittest tests.test_audit_cc -v`

Expected: import failure for `audit_cc`.

- [ ] **Step 3: Implement deterministic discovery and changed-line parsing**

Use `git rev-parse --show-toplevel`, `git diff --unified=0 --no-ext-diff HEAD --`, `git ls-files --others --exclude-standard`, and an empty-repository fallback that treats tracked/untracked supported files as changed. Parse `@@ -old,count +new,count @@` hunks into one-based new-line sets. Ignore deleted files and reject explicit `--git-diff` outside Git with exit `2`; the hook itself will preflight and silently skip.

Apply the exact file/byte/finding caps from Global Constraints. Sort paths by POSIX-relative name and record every truncation as a `TOOL_LIMIT` limitation.

- [ ] **Step 4: Implement CLI rendering and status policy**

```python
def exit_status(report: AuditReport, fail_on: str) -> int:
    eligible = [item for item in report.findings if item.gate_eligible and item.code.startswith("POT")]
    if fail_on == "none":
        return 0
    if fail_on == "errors":
        return int(any(item.severity == "error" for item in eligible))
    return int(any(item.severity in {"error", "warning"} for item in eligible))
```

Text output prints one `path:line: severity CODE: message` line per finding, followed by limitations and counts. JSON uses `AuditReport.to_dict()`. Invalid arguments/configuration or required input failures return `2` without a traceback.

- [ ] **Step 5: Run CLI and integration tests**

Run: `python3 -m unittest tests.test_quality_model tests.test_source_checks tests.test_audit_cc -v`

Expected: all tests pass, including staged, unstaged, untracked, renamed, deleted, unchanged, and no-HEAD repositories.

- [ ] **Step 6: Commit CLI behavior**

```bash
git add skills/reviewing-cc-quality/scripts/audit_cc.py tests/test_audit_cc.py
git commit -m "feat: add changed-line cc audit cli"
```

### Task 5: Optional Clang semantic adapter

**Files:**
- Create: `skills/reviewing-cc-quality/scripts/clang_analysis.py`
- Create: `tests/test_clang_analysis.py`
- Modify: `skills/reviewing-cc-quality/scripts/audit_cc.py`
- Modify: `tests/test_audit_cc.py`

**Interfaces:**
- Consumes: normalized model, changed-file/line scope, suppressions, environment configuration, and source text.
- Produces: `CompileCommand`, `find_compilation_database()`, `load_compile_command()`, `sanitize_command()`, `run_clang_analysis()`, and `analyze_ast()`.

- [ ] **Step 1: Write failing database and command tests**

```python
def test_selects_shortest_sorted_database_with_exact_entry(self) -> None:
    self.write_db("build/compile_commands.json", "src/main.cpp")
    self.write_db("build-z/compile_commands.json", "src/main.cpp")
    selected = find_compilation_database(self.root, self.root / "src/main.cpp", None)
    self.assertEqual(selected, self.root / "build/compile_commands.json")

def test_sanitize_preserves_project_flags_and_removes_outputs(self) -> None:
    command = CompileCommand(self.root, ("g++", "-Iinc", "-DMODE=1", "-c", "src/a.cpp", "-o", "a.o"), self.root / "src/a.cpp")
    args = sanitize_command(command, clang_driver="clang++")
    self.assertEqual(args[:3], ("clang++", "-Iinc", "-DMODE=1"))
    self.assertNotIn("-o", args)
    self.assertIn("-fsyntax-only", args)
    self.assertIn("-ast-dump=json", args)
```

- [ ] **Step 2: Write failing fake-Clang tests**

Provide an executable fixture that emits controlled stderr diagnostics and JSON AST on stdout. Cover timeout, nonzero parse failure, oversized output, malformed JSON, and no exact database entry. Each failure must return a `TOOL_CLANG_*` limitation and leave source findings intact.

Run: `python3 -m unittest tests.test_clang_analysis.CompilationDatabaseTests tests.test_clang_analysis.ClangExecutionTests -v`

Expected: failures because the adapter is absent.

- [ ] **Step 3: Implement bounded database selection and execution**

Normalize each entry's `file` relative to its `directory`; accept either `arguments` or POSIX-shell-split `command`. Search only the explicit setting, root, `build`, `out`, `build-*`, and `cmake-build-*`. Select a database only if it contains the requested source.

Sanitize `-o`, `-MF`, `-MT`, `-MQ`, dependency flags, and `-c`; keep project includes, defines, target, standard, and language flags. Use `clang` for C and `clang++` for C++. Append `-fsyntax-only -fdiagnostics-color=never -Xclang -ast-dump=json`. Enforce the per-unit timeout/output cap and global deadline without raising into the caller.

- [ ] **Step 4: Write failing semantic finding tests**

Use minimal JSON AST fixtures for:

```python
EXPECTED = {
    "direct_recursion": ("POT01", "error"),
    "indirect_recursion_cycle": ("POT01", "error"),
    "long_function_range": ("POT04", "error"),
    "mutable_file_scope_variable": ("POT06", "warning"),
    "discarded_nodiscard_call": ("POT07", "error"),
    "discarded_nonvoid_call": ("POT07", "warning"),
    "function_pointer_type": ("POT09", "error"),
    "triple_dereference": ("POT09", "error"),
    "large_cpp_class": ("SOLID01", "warning"),
    "suspicious_override": ("SOLID03", "warning"),
    "broad_abstract_interface": ("SOLID04", "warning"),
    "concrete_infrastructure_construction": ("SOLID05", "warning"),
}
```

Also parse `path:line:column: warning|error:` diagnostics on changed lines into POT10 errors and deduplicate a compiler unused-result diagnostic against POT07.

- [ ] **Step 5: Implement AST traversal and call-cycle detection**

Walk nested `inner` arrays iteratively. Index function definitions and referenced declaration IDs, build only edges whose definitions are present in the analyzed AST set, and use depth-first search with active/finished states to report each complete cycle once. Read source locations/ranges defensively; nodes without an attributable changed source line do not yield findings.

Implement the semantic rules in small pure functions, apply changed-line filtering before returning, set every SOLID finding `gate_eligible=False`, and run all results through the same exact-line suppressions.

- [ ] **Step 6: Integrate Clang mode into the auditor**

Resolve `RELIABLE_CC_CLANG=auto|off` and `RELIABLE_CC_COMPILE_COMMANDS`, reject invalid values with direct CLI status `2`, and pass a 60-second direct or 15-second hook budget into the adapter. Merge, sort, deduplicate, and cap source/Clang findings without letting adapter limitations change exit status.

- [ ] **Step 7: Run deterministic and optional real-Clang tests**

Run: `python3 -m unittest tests.test_clang_analysis tests.test_audit_cc -v`

Run when available: `python3 -m unittest tests.test_clang_analysis.RealClangSmokeTests -v`

Expected: deterministic tests always pass; real smoke passes or is explicitly skipped when Clang is absent.

- [ ] **Step 8: Commit semantic analysis**

```bash
git add skills/reviewing-cc-quality/scripts/clang_analysis.py skills/reviewing-cc-quality/scripts/audit_cc.py tests/test_clang_analysis.py tests/test_audit_cc.py
git commit -m "feat: add optional clang semantic checks"
```

### Task 6: Shared session and completion hooks

**Files:**
- Create: `hooks/hooks.json`
- Create: `hooks/session_start.py`
- Create: `hooks/stop_quality_gate.py`
- Create: `tests/test_hooks.py`

**Interfaces:**
- Consumes: `using-reliable-cc/SKILL.md`, `audit_cc.py`, host JSON on stdin, and `RELIABLE_CC_GATE`.
- Produces: SessionStart `additionalContext` and Stop `{decision, reason}`/`systemMessage` JSON payloads.

- [ ] **Step 1: Write failing SessionStart tests**

```python
def test_session_start_injects_entry_skill(self) -> None:
    result = self.run_hook("session_start.py", {})
    payload = json.loads(result.stdout)
    context = payload["hookSpecificOutput"]["additionalContext"]
    self.assertIn("<RELIABLE_CC_POLICY>", context)
    self.assertIn("using-reliable-cc", context)
    self.assertLessEqual(len(context.encode()), 8192 + 1024)

def test_session_start_missing_skill_fails_open(self) -> None:
    with mock.patch("pathlib.Path.read_text", side_effect=OSError("missing")):
        self.assertEqual(session_start.main(), 0)
```

- [ ] **Step 2: Write failing Stop behavior tests**

Cover `errors`, `all`, `off`, invalid values, clean audit, error-only selection, advisory SOLID exclusion, non-Git silence, timeout, invalid JSON, first attempt blocking, second attempt allowing, twelve-finding display, and remainder count.

```python
def test_second_completion_attempt_always_proceeds(self) -> None:
    payload = self.invoke_stop({"cwd": str(self.repo), "stop_hook_active": True}, audit=pot_error_report())
    self.assertNotIn("decision", payload)
    self.assertIn("allowing the turn to complete", payload["systemMessage"])
```

- [ ] **Step 3: Implement fail-open hooks and hook registration**

Mirror the proven Reliable Python host payload shapes but use only Reliable C/C++ paths and copy. `session_start.py` reads and size-checks the entry skill. `stop_quality_gate.py` preflights Git, invokes the auditor with JSON/`--fail-on none` under 15 seconds, filters only gate-eligible POT findings, and uses `stop_hook_active` as the loop guard.

Register SessionStart for `startup|resume|clear|compact` and Stop with a 20-second host timeout. Commands resolve `${CLAUDE_PLUGIN_ROOT}` and never contain installation-cache paths.

- [ ] **Step 4: Run hook tests**

Run: `python3 -m unittest tests.test_hooks -v`

Expected: both host-neutral payload contracts and every fail-open path pass.

- [ ] **Step 5: Commit hooks**

```bash
git add hooks tests/test_hooks.py
git commit -m "feat: gate changed cc code once"
```

### Task 7: Author and behavior-test the rule skills

**Files:**
- Modify: `skills/using-reliable-cc/SKILL.md`
- Modify: `skills/using-reliable-cc/agents/openai.yaml`
- Create: `skills/applying-power-of-ten-to-c-cpp/SKILL.md`
- Create: `skills/applying-power-of-ten-to-c-cpp/agents/openai.yaml`
- Create: `skills/applying-power-of-ten-to-c-cpp/references/power-of-ten.md`
- Create: `skills/applying-solid-to-cpp/SKILL.md`
- Create: `skills/applying-solid-to-cpp/agents/openai.yaml`
- Create: `skills/reviewing-cc-quality/SKILL.md`
- Create: `skills/reviewing-cc-quality/agents/openai.yaml`
- Test: temporary skill-behavior transcripts outside the repository plus package validation in Task 8.

**Interfaces:**
- Consumes: implemented rule IDs, CLI, suppression syntax, and source limitations.
- Produces: discoverable, uniquely named agent workflows whose claims match actual enforcement.

- [ ] **Step 1: Invoke the required skill-authoring workflow**

Read `superpowers:writing-skills` completely before editing any skill. Follow its RED/GREEN/REFACTOR behavior-testing procedure, including fresh-agent baseline and post-skill scenarios when it requires them.

- [ ] **Step 2: Record baseline failures for the entry and specialist behaviors**

Use scenarios that ask an agent to review a C scheduler loop, an allocation in initialization, a function pointer callback, a C++ class with broad responsibilities, and a direct CLI audit. Baseline transcripts must show the missing Reliable C/C++ rule routing or overconfident SOLID conclusion before the new skills are written.

- [ ] **Step 3: Write the minimal entry and specialist skills**

The entry skill must fit the 8,192-byte injection ceiling and route rather than duplicate detail. The Power of Ten reference must list POT01–POT10, blocking/advisory status, C/C++ examples, legitimate exceptions, and narrow suppression rules. The SOLID skill must explicitly distinguish evidence from proof and map the principles to both C++ classes and C module/API design. The review skill must document exact CLI commands, exit statuses, JSON shape, limits, Clang selection, and troubleshooting.

- [ ] **Step 4: Run post-skill scenarios and refine loopholes**

Confirm fresh agents now select the correct skill, distinguish completion from plugin shutdown, avoid claiming certification, never block on SOLID, and explain missing Clang as reduced coverage rather than failure.

- [ ] **Step 5: Validate all skill packages**

Run the validator across each concrete skill directory:

```bash
for skill in skills/applying-power-of-ten-to-c-cpp skills/applying-solid-to-cpp skills/reviewing-cc-quality skills/using-reliable-cc
do
    python3 /home/jihohan/.codex/skills/.system/skill-creator/scripts/quick_validate.py "$skill"
done
```

Expected: all four skills validate.

- [ ] **Step 6: Commit the skills**

```bash
git add skills
git commit -m "docs: add cc reliability workflows"
```

### Task 8: Package contracts, README, and contributor documentation

**Files:**
- Modify: `.claude-plugin/plugin.json`
- Modify: `.codex-plugin/plugin.json`
- Modify: `.claude-plugin/marketplace.json`
- Modify: `README.md`
- Modify: `CONTRIBUTING.md`
- Modify: `CLAUDE.md`
- Modify: `tests/test_package.py`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: all implemented scripts, hooks, skills, configuration, and v0.1.0 behavior.
- Produces: honest public package metadata and validators that protect it.

- [ ] **Step 1: Replace scaffold assertions with failing functional package contracts**

```python
def test_package_exposes_hooks_and_four_unique_skills(self) -> None:
    hooks = load_json(ROOT / "hooks" / "hooks.json")["hooks"]
    self.assertEqual(set(hooks), {"SessionStart", "Stop"})
    self.assertEqual(
        sorted(path.name for path in (ROOT / "skills").iterdir()),
        ["applying-power-of-ten-to-c-cpp", "applying-solid-to-cpp", "reviewing-cc-quality", "using-reliable-cc"],
    )

def test_every_manifest_describes_functional_v010(self) -> None:
    for manifest in host_and_catalog_manifests():
        self.assertEqual(manifest["version"], "0.1.0")
        self.assertNotIn("scaffold", manifest["description"].lower())
```

Also assert every registered command target exists, no path contains `.codex/plugins/cache`, every Python executable has a main guard, no `__pycache__`/`.pyc` is tracked, and public copy contains the non-certification boundary.

- [ ] **Step 2: Run package tests and confirm scaffold-copy failures**

Run: `python3 -m unittest tests.test_package -v`

Expected: failures for missing hooks/skills and scaffold descriptions.

- [ ] **Step 3: Update manifests and public documentation**

Keep version `0.1.0`, add `hooks: "./hooks/hooks.json"` where required by host schema, and replace all foundation/scaffold language with implemented capability descriptions. Document installation, supported suffixes, source versus Clang coverage, POT/SOLID severity boundaries, first/second completion behavior, environment variables, suppression example, direct CLI text/JSON examples, limits, exit statuses, and the absence of safety certification.

Update contribution/release instructions to reflect the corrected functional initial release and normal future Git Flow. Ignore Python caches and generated compiler/build outputs without ignoring user source.

- [ ] **Step 4: Run the full source validation suite**

```bash
python3 -m unittest discover -s tests -v
python3 /home/jihohan/.codex/skills/.system/plugin-creator/scripts/validate_plugin.py .
claude plugin validate .
for skill in skills/*; do python3 /home/jihohan/.codex/skills/.system/skill-creator/scripts/quick_validate.py "$skill"; done
git diff --check
```

Expected: all tests and validators pass with no whitespace errors.

- [ ] **Step 5: Run source-tree audit smoke tests**

```bash
python3 skills/reviewing-cc-quality/scripts/audit_cc.py tests --format text --fail-on none
python3 skills/reviewing-cc-quality/scripts/audit_cc.py --git-diff --format json --fail-on none
```

Expected: valid output, no traceback, and status `0`.

- [ ] **Step 6: Commit package completion**

```bash
git add .claude-plugin .codex-plugin .gitignore CLAUDE.md CONTRIBUTING.md README.md tests
git commit -m "docs: publish reliable cc v0.1 behavior"
```

### Task 9: Review the completed implementation

**Files:**
- Review: every file changed since `eb54c8c`
- Modify: only files required by verified review findings.

**Interfaces:**
- Consumes: completed source branch and approved spec.
- Produces: spec-conformant, reviewed, fully verified release tree.

- [ ] **Step 1: Invoke the required review workflow**

Use `superpowers:requesting-code-review` and provide the spec, plan, base commit `eb54c8c`, and current branch head. Address only evidence-backed findings; use `superpowers:receiving-code-review` before applying reviewer feedback.

- [ ] **Step 2: Re-run focused tests for every accepted review fix**

Run the exact relevant test module first, then:

```bash
python3 -m unittest discover -s tests -v
```

Expected: all tests pass.

- [ ] **Step 3: Commit any review fixes**

```bash
git add -A
git diff --cached --name-only
git commit -m "fix: address reliable cc review"
```

Skip this commit when review requires no changes.

- [ ] **Step 4: Invoke verification-before-completion and capture fresh evidence**

Run the complete validation commands from Task 8 plus a real-Clang smoke test when available. Confirm `git status --short` is empty and inspect `git diff --stat eb54c8c..HEAD` against the spec.

### Task 10: Replace public v0.1.0 and publish verified copies

**Files:**
- Rewrite: Reliable C/C++ `main`, `develop`, and `v0.1.0` history/tag.
- Modify in marketplace repository: catalog entry, README copy, and vendored `plugins/reliable-cc/` subtree.
- Modify in organization `.github` repository: `profile/README.md`.
- Update remotely: GitHub release and Reliable C/C++ repository description if needed.

**Interfaces:**
- Consumes: the fully verified release tree and explicit user authorization to replace scaffold history/tag/release.
- Produces: one functional root commit, matching public tag/release, matching marketplace copy, and accurate organization Overview.

- [ ] **Step 1: Create a recoverable local backup reference and record remote state**

```bash
git branch backup/scaffold-v0.1 eb54c8c
git ls-remote --heads --tags origin
gh release view v0.1.0 --repo reliable-coding-agents/reliable-cc
```

Expected: the backup resolves to `eb54c8c`, and the old public tag/release is recorded before replacement.

- [ ] **Step 2: Build a single functional root commit without deleting the working branch**

Use Git's plumbing to commit the verified tree directly with no parent. This
does not alter or delete the feature worktree:

```bash
release_tree=$(git rev-parse feature/power-of-ten-solid-v0.1^{tree})
release_commit=$(git commit-tree "$release_tree" -m "feat: release reliable cc v0.1.0")
git branch -f release/v0.1.0-functional "$release_commit"
```

Confirm the new commit has no parents and its tree exactly matches the verified feature head:

```bash
test "$(git rev-list --parents -n 1 release/v0.1.0-functional | wc -w)" -eq 1
test "$(git rev-parse release/v0.1.0-functional^{tree})" = "$(git rev-parse feature/power-of-ten-solid-v0.1^{tree})"
```

- [ ] **Step 3: Replace branches, tag, and GitHub release**

Resolve both expected remote objects, delete only the named release, and use
explicit force-with-lease expectations for every rewritten ref:

```bash
old_main=$(git rev-parse origin/main)
old_develop=$(git rev-parse origin/develop)
old_tag_object=$(git ls-remote --tags origin refs/tags/v0.1.0 | awk '{print $1}')
release_commit=$(git rev-parse release/v0.1.0-functional)
gh release delete v0.1.0 --repo reliable-coding-agents/reliable-cc --yes
git push --force-with-lease=refs/heads/main:"$old_main" origin "$release_commit":refs/heads/main
git push --force-with-lease=refs/heads/develop:"$old_develop" origin "$release_commit":refs/heads/develop
git tag -f -a v0.1.0 "$release_commit" -m "Reliable C/C++ v0.1.0"
git push --force-with-lease=refs/tags/v0.1.0:"$old_tag_object" origin refs/tags/v0.1.0
gh release create v0.1.0 --repo reliable-coding-agents/reliable-cc --verify-tag --title "Reliable C/C++ v0.1.0" --notes "First functional release: Power of Ten checks, advisory SOLID analysis, optional Clang semantics, and a one-pass completion gate."
git fetch origin main develop --tags
```

Verify:

```bash
test "$(git rev-parse origin/main)" = "$(git rev-parse origin/develop)"
test "$(git rev-parse origin/main^{tree})" = "$(git rev-parse v0.1.0^{tree})"
test "$(git rev-list --parents -n 1 origin/main | wc -w)" -eq 1
```

- [ ] **Step 4: Replace and validate the marketplace subtree**

Use the marketplace repository's established subtree/update workflow. Exclude `.git`, development-only backup refs, and generated caches. Keep catalog version `0.1.0`, replace scaffold wording, and assert file-by-file equality between the source release tree and vendored plugin tree for all packaged files.

Run the complete plugin, skill, and unit validation suite inside the vendored copy before pushing marketplace `main`.

- [ ] **Step 5: Update organization Overview and repository metadata**

Change the Reliable C/C++ row/current-focus copy from scaffold/foundation to functional Power of Ten gates plus advisory SOLID analysis. Keep classic badges and avoid emoji. Update the repository description only if it still says scaffold.

- [ ] **Step 6: Verify fresh public clones and release state**

Clone Reliable C/C++, marketplace, and `.github` into separate `mktemp -d` directories. Verify:

- Reliable C/C++ has one root commit on equal `main`/`develop` trees;
- `v0.1.0` peels to that tree and the GitHub release targets it;
- source and marketplace packaged trees are identical;
- source and marketplace tests/validators pass;
- hooks resolve only package-relative files;
- organization Overview and repository metadata contain no scaffold wording.

- [ ] **Step 7: Report the destructive replacement and recoverability**

Report the old scaffold commit `eb54c8c`, the local `backup/scaffold-v0.1` reference, the new root commit/tag, verification counts, release link, marketplace commit, and Overview commit. State clearly that public history/tag/release were replaced at the user's request and the local backup preserves recovery.
