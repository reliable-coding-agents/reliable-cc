"""Tests for the lexical C/C++ source analysis foundation."""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "reviewing-cc-quality" / "scripts"
sys.path.insert(
    0,
    str(SCRIPTS),
)

import source_checks  # noqa: E402
from quality_model import Finding
from source_checks import (
    FunctionRange,
    analyze_source,
    analyze_source_bounded,
    apply_suppressions,
    function_ranges,
    lex_source,
    parse_suppressions,
)


class SourceLexingTests(unittest.TestCase):
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

    def test_directive_continues_through_a_string_literal_backslash(self) -> None:
        text = '#define TEXT "left\\\nright"\n'
        lexed = lex_source(text)
        self.assertEqual(lexed.directives[0].start_line, 1)
        self.assertEqual(lexed.directives[0].end_line, 2)

    def test_lexer_keeps_backslash_continued_line_comments_hidden(self) -> None:
        text = "// goto hidden \\\ngoto still hidden\ngoto visible;\n"
        lexed = lex_source(text)
        self.assertNotIn("goto hidden", lexed.code_lines[0])
        self.assertNotIn("goto still hidden", lexed.code_lines[1])
        self.assertIn("goto visible", lexed.code_lines[2])

    def test_suppression_requires_exact_code_and_rationale(self) -> None:
        valid, malformed = parse_suppressions(
            "for (;;) {} /* quality: ignore[POT02] - scheduler */\n"
            "goto out; /* quality: ignore[*] */\n",
            "main.c",
        )
        self.assertEqual(valid[1], frozenset({"POT02"}))
        self.assertEqual(malformed[0].code, "TOOL_SUPPRESSION")

    def test_suppression_only_removes_the_matching_code_on_the_matching_line(self) -> None:
        findings = (
            _finding("POT02", 2),
            _finding("POT01", 2),
            _finding("POT02", 3),
        )
        remaining = apply_suppressions(findings, {2: frozenset({"POT02"})})
        self.assertEqual([(item.code, item.line) for item in remaining], [("POT01", 2), ("POT02", 3)])

    def test_suppression_reports_every_malformed_directive_in_a_comment(self) -> None:
        valid, malformed = parse_suppressions(
            "goto out; /* quality: ignore[POT01] - cleanup; quality: ignore[*] */\n",
            "main.c",
        )
        self.assertEqual(valid, {1: frozenset({"POT01"})})
        self.assertEqual([(item.code, item.line) for item in malformed], [("TOOL_SUPPRESSION", 1)])

    def test_suppression_in_spliced_line_comment_stays_on_its_physical_line(self) -> None:
        valid, malformed = parse_suppressions(
            "// generated rationale follows \\\nquality: ignore[POT01] - generated code\n",
            "main.c",
        )
        self.assertEqual(valid, {2: frozenset({"POT01"})})
        self.assertEqual(malformed, ())


def _finding(code: str, line: int) -> Finding:
    return Finding(
        code=code,
        severity="warning",
        path="sample.cc",
        line=line,
        message="message",
        remediation="remediation",
        engine="source",
        gate_eligible=False,
    )


class FunctionRangeTests(unittest.TestCase):
    def test_review_include_guard_scan_honors_an_expired_deadline(self) -> None:
        text = "".join(
            f"#ifndef GUARD_{index:05d}_{'X' * 35}\n" for index in range(32_000)
        )
        self.assertGreater(len(text), 1_500_000)
        lexed = lex_source(text)
        started = time.monotonic()
        try:
            source_checks._include_guard_starts(
                Path("near-cap.hpp"), lexed, deadline=started - 1.0
            )
        except TimeoutError:
            outcome = "timed-out"
        except TypeError:
            outcome = "deadline-unsupported"
        else:
            outcome = "completed"
        self.assertEqual(outcome, "timed-out")
        self.assertLess(time.monotonic() - started, 0.25)

    def test_recognizes_ordinary_and_cpp_member_definitions(self) -> None:
        text = (
            "int add(int left, int right) {\n"
            "    return left + right;\n"
            "}\n"
            "Widget::Widget() : value(0) {}\n"
            "Widget::~Widget() {}\n"
            "Widget Widget::operator+(const Widget& other) const { return other; }\n"
            "namespace network { class Server { void start() {} }; }\n"
        )
        self.assertEqual(
            function_ranges(lex_source(text)),
            (
                FunctionRange(1, 3, True),
                FunctionRange(4, 4, True),
                FunctionRange(5, 5, True),
                FunctionRange(6, 6, True),
                FunctionRange(7, 7, True),
            ),
        )

    def test_recognizes_lambdas_but_not_controls_prototypes_or_initializers(self) -> None:
        text = (
            "int declared(int value);\n"
            "void run() {\n"
            "    if (ready) { tick(); }\n"
            "    for (;;) { tick(); }\n"
            "    auto callback = [](int value) { return value; };\n"
            "    Point point{1, 2};\n"
            "}\n"
        )
        self.assertEqual(
            function_ranges(lex_source(text)),
            (FunctionRange(2, 7, True), FunctionRange(5, 5, True)),
        )

    def test_ignores_comments_and_marks_directive_affected_body_uncertain(self) -> None:
        text = (
            "// void fake() { }\n"
            "void configured() { /* } */\n"
            "#if ENABLED\n"
            "    work();\n"
            "#endif\n"
            "}\n"
        )
        self.assertEqual(
            function_ranges(lex_source(text)),
            (FunctionRange(2, 6, False),),
        )

    def test_retains_macro_obscured_function_candidate_as_uncertain(self) -> None:
        text = "void generated(void)\n#define BODY {\nBODY\n    work();\n}\n"
        self.assertEqual(function_ranges(lex_source(text)), (FunctionRange(1, 5, False),))

    def test_uses_constructor_body_after_braced_member_initializer(self) -> None:
        text = (
            "Widget::Widget()\n"
            "    : value{0}\n"
            "{\n"
            "    use(value);\n"
            "}\n"
        )
        self.assertEqual(function_ranges(lex_source(text)), (FunctionRange(1, 5, True),))

    def test_uses_constructor_body_after_nested_braced_member_initializer(self) -> None:
        text = (
            "Widget::Widget()\n"
            "    : values{{1, 2}, {3, 4}}\n"
            "{\n"
            "    use(values);\n"
            "}\n"
        )
        self.assertEqual(function_ranges(lex_source(text)), (FunctionRange(1, 5, True),))

    def test_retains_candidate_when_its_brace_macro_is_defined_earlier(self) -> None:
        text = "#define BODY {\nvoid generated(void) BODY\n    work();\n}\n"
        self.assertEqual(function_ranges(lex_source(text)), (FunctionRange(2, 4, False),))


def _long_function(lines: int) -> str:
    return "void long_function(void) {\n" + "    work();\n" * (lines - 2) + "}\n"


def _twelve_line_function_without_assertions() -> str:
    return (
        "void unchecked(void) {\n"
        "    work();\n"
        "    work();\n"
        "    work();\n"
        "    work();\n"
        "    work();\n"
        "    work();\n"
        "    work();\n"
        "    work();\n"
        "    work();\n"
        "    work();\n"
        "}\n"
    )


class PortableRuleTests(unittest.TestCase):
    def test_review_finding_capacity_returns_deterministic_partial_results(self) -> None:
        source = "".join(f"goto label_{line};\n" for line in range(1, 10_001))
        started = time.monotonic()
        result = analyze_source_bounded(
            Path("many.c"), source, deadline=None, max_findings=7
        )
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual([item.line for item in result.findings], list(range(1, 8)))
        self.assertTrue(result.truncated)
        self.assertFalse(result.timed_out)

    def test_review_deadline_retains_partial_findings_at_bounded_checkpoints(self) -> None:
        source = "".join(f"goto label_{line};\n" for line in range(1, 10_001))
        calls = 0

        def clock() -> float:
            nonlocal calls
            calls += 1
            return 0.0 if calls < 600 else 2.0

        with mock.patch.object(source_checks.time, "monotonic", side_effect=clock):
            result = analyze_source_bounded(
                Path("many.c"), source, deadline=1.0, max_findings=500
            )
        self.assertTrue(result.timed_out)
        self.assertFalse(result.truncated)
        self.assertGreater(len(result.findings), 0)
        self.assertLess(len(result.findings), 500)
        self.assertEqual(
            [item.line for item in result.findings],
            list(range(1, len(result.findings) + 1)),
        )

    def test_review_off_diff_malformed_suppressions_do_not_hide_valid_changed_one(self) -> None:
        source = (
            "// quality: ignore[\n" * 501
            + "goto out; // quality: ignore[POT01] - reviewed exception\n"
        )
        result = analyze_source_bounded(
            Path("changed.c"),
            source,
            deadline=None,
            max_findings=500,
            changed_lines=frozenset({502}),
        )
        self.assertEqual(result.findings, ())
        self.assertFalse(result.truncated)
        self.assertFalse(result.timed_out)

    CASES = (
        ("POT01", "void f(void) { goto done; done: return; }", "error"),
        ("POT02", "void f(void) { for (;;) { tick(); } }", "warning"),
        ("POT03", "void f(void) { void *p = malloc(8); }", "warning"),
        ("POT04", _long_function(61), "error"),
        ("POT05", _twelve_line_function_without_assertions(), "warning"),
        ("POT08", "#define JOIN(a,b) a ## b\n", "error"),
    )

    def test_portable_rule_cases(self) -> None:
        for code, source, severity in self.CASES:
            with self.subTest(code=code):
                finding = next(
                    item
                    for item in analyze_source(Path("sample.c"), source)
                    if item.code == code
                )
                self.assertEqual(finding.severity, severity)

    def test_pot01_reports_only_control_flow_tokens(self) -> None:
        findings = analyze_source(
            Path("sample.c"),
            "void f(void) { gotohome(); setjmp(env); longjmp(env, 1); }\n",
        )
        self.assertEqual(
            [(item.code, item.line) for item in findings],
            [("POT01", 1), ("POT01", 1)],
        )

    def test_pot01_ignores_setjmp_and_longjmp_declarations(self) -> None:
        findings = analyze_source(
            Path("sample.h"),
            "int setjmp(void *env);\nvoid longjmp(void *env, int value);\n",
        )
        self.assertNotIn("POT01", [item.code for item in findings])

    def test_pot01_reports_qualified_jump_calls(self) -> None:
        findings = analyze_source(
            Path("sample.cc"),
            "void f(void) { std::longjmp(env, 1); ::setjmp(env); }\n",
        )
        self.assertEqual([item.code for item in findings].count("POT01"), 2)

    def test_pot02_allows_visibly_bounded_for_loop(self) -> None:
        findings = analyze_source(
            Path("sample.c"), "void f(void) { for (i = 0; i < 10; ++i) { tick(); } }\n"
        )
        self.assertNotIn("POT02", [item.code for item in findings])

    def test_pot02_reports_unbounded_while_and_do_loops(self) -> None:
        findings = analyze_source(
            Path("sample.c"), "void f(void) { while (ready) { tick(); } do { tick(); } while (ready); }\n"
        )
        self.assertEqual([item.code for item in findings].count("POT02"), 2)

    def test_pot02_rejects_disjunction_with_only_a_partial_bound(self) -> None:
        findings = analyze_source(
            Path("sample.c"), "void f(void) { while (ready || i < 10) { tick(); } }\n"
        )
        self.assertIn("POT02", [item.code for item in findings])

    def test_pot03_ignores_comments_strings_and_identifiers(self) -> None:
        findings = analyze_source(
            Path("sample.c"),
            'void f(void) { allocation(); const char *s = "malloc(8) new"; /* calloc(4) */ }\n',
        )
        self.assertNotIn("POT03", [item.code for item in findings])

    def test_token_rules_ignore_preprocessor_replacement_text(self) -> None:
        findings = analyze_source(
            Path("sample.h"),
            "#define ESCAPE goto done\n#define ALLOC malloc(8)\n",
        )
        self.assertNotIn("POT01", [item.code for item in findings])
        self.assertNotIn("POT03", [item.code for item in findings])

    def test_pot03_reports_allocation_calls_and_cpp_new(self) -> None:
        findings = analyze_source(
            Path("sample.cc"),
            "void f(void) { calloc(1, 2); auto x = new Item; auto y = new[] int[2]; }\n",
        )
        self.assertEqual([item.code for item in findings].count("POT03"), 3)

    def test_pot04_allows_sixty_line_function_and_marks_uncertain_range_warning(self) -> None:
        allowed = analyze_source(Path("sample.c"), _long_function(60))
        uncertain = analyze_source(
            Path("sample.c"), "void generated(void)\n#define BODY {\nBODY\n" + "work();\n" * 59 + "}\n"
        )
        self.assertNotIn("POT04", [item.code for item in allowed])
        warning = next(item for item in uncertain if item.code == "POT04")
        self.assertEqual(warning.severity, "warning")

    def test_pot05_allows_trivial_and_asserted_functions(self) -> None:
        findings = analyze_source(
            Path("sample.c"),
            "int value(void) { return current; }\n"
            + "void checked(void) {\n"
            + "    assert(one);\n"
            + "    assert(two);\n"
            + "    work();\n" * 8
            + "}\n",
        )
        self.assertNotIn("POT05", [item.code for item in findings])

    def test_pot05_reports_nontrivial_uncertain_function_without_assertions(self) -> None:
        findings = analyze_source(
            Path("sample.c"),
            "void generated(void)\n#define BODY {\nBODY\n" + "    work();\n" * 12 + "}\n",
        )
        finding = next(item for item in findings if item.code == "POT05")
        self.assertEqual(finding.severity, "warning")

    def test_pot08_allows_include_guards_and_simple_object_macro(self) -> None:
        findings = analyze_source(
            Path("sample.h"),
            "#ifndef SAMPLE_H\n#define SAMPLE_H\n#define LIMIT 10\n#endif\n",
        )
        self.assertNotIn("POT08", [item.code for item in findings])

    def test_pot08_reports_default_definition_conditional_outside_include_guard(self) -> None:
        findings = analyze_source(
            Path("sample.h"),
            "#ifndef DEFAULT_LIMIT\n#define DEFAULT_LIMIT 10\n#endif\nvoid use(int);\n",
        )
        self.assertIn("POT08", [item.code for item in findings])

    def test_pot08_reports_other_macro_and_conditional_forms(self) -> None:
        findings = analyze_source(
            Path("sample.h"),
            "#define CALL(x) x\n"
            "#define TEXT(x) #x\n"
            "#define VAR(...) __VA_ARGS__\n"
            "#define SELF SELF\n"
            "#if ENABLED\n#endif\n",
        )
        self.assertEqual(
            [(item.code, item.severity) for item in findings],
            [
                ("POT08", "warning"),
                ("POT08", "error"),
                ("POT08", "error"),
                ("POT08", "error"),
                ("POT08", "warning"),
            ],
        )

    def test_pot08_treats_macro_parameter_named_like_macro_as_nonrecursive(self) -> None:
        findings = analyze_source(Path("sample.h"), "#define FOO(FOO) FOO\n")
        finding = next(item for item in findings if item.code == "POT08")
        self.assertEqual(finding.severity, "warning")

    def test_analyzer_applies_exact_line_suppression_and_keeps_malformed_warning(self) -> None:
        findings = analyze_source(
            Path("sample.c"),
            "goto done; /* quality: ignore[POT01] - generated switch */\n"
            "for (;;) {} /* quality: ignore[*] */\n",
        )
        self.assertEqual(
            [(item.code, item.line) for item in findings],
            [("POT02", 2), ("TOOL_SUPPRESSION", 2)],
        )


if __name__ == "__main__":
    unittest.main()
