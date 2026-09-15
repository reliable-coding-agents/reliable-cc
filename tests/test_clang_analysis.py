"""Deterministic compilation database and semantic adapter coverage."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/reviewing-cc-quality/scripts"
sys.path.insert(0, str(SCRIPTS))

import clang_analysis as clang  # noqa: E402
from clang_analysis import (  # noqa: E402
    CompileCommand, analyze_ast, find_compilation_database, load_compile_command,
    run_clang_analysis, sanitize_command,
)


class TemporaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "src/main.cpp"
        self.source.parent.mkdir()
        self.source.write_text("int f() { return 0; }\n", encoding="utf-8")

    def write_db(self, name: str, source: str = "src/main.cpp", **fields: object) -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = {"directory": str(self.root), "file": source,
                 "arguments": ["g++", "-c", source], **fields}
        path.write_text(json.dumps([entry]), encoding="utf-8")
        return path


class CompilationDatabaseTests(TemporaryTests):
    def test_review_blocks_native_plugins_and_opaque_backend_loading(self) -> None:
        unsafe_flags = (
            ("-fplugin=project-plugin.so",),
            ("-fplugin", "project-plugin.so"),
            ("-fpass-plugin=project-pass.so",),
            ("-fpass-plugin", "project-pass.so"),
            ("-fplugin-arg-project-key=value",),
            ("--hipspv-pass-plugin=project-pass.so",),
            ("--hipspv-pass-plugin", "project-pass.so"),
            ("-mllvm", "-load=project-pass.so"),
            ("-mllvm=-load=project-pass.so",),
            ("-load", "project-plugin.so"),
            ("-load-pass-plugin=project-pass.so",),
            ("-Xassembler", "--plugin=project-plugin.so"),
            ("-Xlinker", "-plugin=project-plugin.so"),
            ("-Xcuda-ptxas", "--load=project-plugin.so"),
            ("-Wa,--plugin=project-plugin.so",),
            ("-Wl,-plugin,project-plugin.so",),
        )
        for flags in unsafe_flags:
            with self.subTest(flags=flags):
                command = CompileCommand(
                    self.root,
                    ("g++", *flags, "-c", "src/main.cpp"),
                    self.source,
                )
                with self.assertRaises(ValueError):
                    sanitize_command(command, clang_driver="clang++")

    def test_review_rejects_implicit_module_and_cache_building_options(self) -> None:
        unsafe_flags = (
            ("-fmodules",),
            ("-fcxx-modules",),
            ("-fimplicit-modules",),
            ("-fimplicit-module-maps",),
            ("-fmodules-cache-path", "project-cache"),
            ("-fmodules-cache-path=project-cache",),
            ("-fmodule-map-file", "module.modulemap"),
            ("-fmodule-map-file=module.modulemap",),
            ("-fmodule-file", "Foo=Foo.pcm"),
            ("-fmodule-file=Foo=Foo.pcm",),
            ("-fprebuilt-module-path", "modules"),
            ("-fprebuilt-module-path=modules",),
            ("-fbuild-session-file", "session.timestamp"),
            ("-fbuild-session-file=session.timestamp",),
            ("-fmodule-header",),
            ("-fmodule-output=Foo.pcm",),
        )
        for flags in unsafe_flags:
            with self.subTest(flags=flags):
                command = CompileCommand(
                    self.root,
                    ("g++", *flags, "-c", "src/main.cpp"),
                    self.source,
                )
                with self.assertRaises(ValueError):
                    sanitize_command(command, clang_driver="clang++")

    def test_review2_rejects_explicit_config_files_and_directory_controls(self) -> None:
        for flags in (("--config", "project.cfg"), ("--config=project.cfg",),
                      ("--config-system-dir", "config"), ("--config-system-dir=config",),
                      ("--config-user-dir", "config"), ("--config-user-dir=config",)):
            with self.subTest(flags=flags):
                command = CompileCommand(self.root, ("g++", *flags, "-c", "src/main.cpp"), self.source)
                with self.assertRaises(ValueError):
                    sanitize_command(command, clang_driver="clang++")

    def test_review2_disables_default_configs_before_option_terminator(self) -> None:
        command = CompileCommand(self.root, ("g++", "-c", "--", "src/main.cpp"), self.source)
        args = sanitize_command(command, clang_driver="clang++")
        self.assertIn("--no-default-config", args)
        self.assertLess(args.index("--no-default-config"), args.index("--"))

    def test_review_rejects_output_producing_compiler_options(self) -> None:
        for flags in (("-save-temps",), ("-save-temps=obj",), ("--save-temps=cwd",),
                      ("-serialize-diagnostics", "audit.dia"), ("-serialize-diagnostics=audit.dia",),
                      ("-Xclang", "-dependency-file", "-Xclang", "audit.d"),
                      ("-Xclang=-dependency-file", "-Xclang=audit.d"),
                      ("-Xpreprocessor", "-MD", "-Xpreprocessor", "audit.d"),
                      ("-Wp,-DDEBUG,-MD,audit.d",),
                      ("-E",), ("--precompile",), ("-ftime-trace",),
                      ("-fmodule-output=module.pcm",)):
            with self.subTest(flags=flags):
                command = CompileCommand(self.root, ("g++", *flags, "-c", "src/main.cpp"), self.source)
                with self.assertRaises(ValueError):
                    sanitize_command(command, clang_driver="clang++")

    def test_review3_rejects_database_reproducer_and_diagnostic_outputs(self) -> None:
        unsafe_flags = (
            ("-gen-cdb-fragment-path", "audit-cdb"),
            ("-gen-cdb-fragment-path=audit-cdb",),
            ("-gen-reproducer",),
            ("-gen-reproducer=always",),
            ("-fcrash-diagnostics-dir", "crashes"),
            ("-fcrash-diagnostics-dir=crashes",),
            ("-fcrash-diagnostics",),
            ("-fcrash-diagnostics=always",),
            ("-fcodegen-data-generate=project.cgdata",),
            ("-fproc-stat-report=process.json",),
            ("-fproc-stat-reportprocess.json",),
            ("-fthin-link-bitcode=thin.bc",),
            ("-garbitrary-output-control",),
            ("-Output-file",),
            ("-xarbitrary-language",),
        )
        for flags in unsafe_flags:
            with self.subTest(flags=flags):
                command = CompileCommand(
                    self.root,
                    ("g++", *flags, "-c", "src/main.cpp"),
                    self.source,
                )
                with self.assertRaises(ValueError):
                    sanitize_command(command, clang_driver="clang++")

    def test_review3_preserves_narrow_debug_flags_and_disables_crash_outputs(self) -> None:
        command = CompileCommand(
            self.root,
            (
                "g++", "-g", "-gdwarf-5", "-gline-tables-only",
                "-c", "src/main.cpp",
            ),
            self.source,
        )
        args = sanitize_command(command, clang_driver="clang++")
        self.assertEqual(
            args,
            (
                "clang++", "-g", "-gdwarf-5", "-gline-tables-only",
                "src/main.cpp", "--no-default-config", "-fsyntax-only",
                "-fno-crash-diagnostics", "-fdiagnostics-color=never",
                "-Xclang", "-ast-dump=json",
            ),
        )

    def test_selects_shortest_sorted_database_with_exact_entry(self) -> None:
        self.write_db("build/compile_commands.json")
        self.write_db("build-z/compile_commands.json")
        self.write_db("compile_commands.json", "other/main.cpp")
        self.assertEqual(find_compilation_database(self.root, self.source, None),
                         self.root / "build/compile_commands.json")

    def test_explicit_database_directory_and_normalized_file(self) -> None:
        self.write_db("compile_commands.json")
        explicit = self.write_db("custom/compile_commands.json", "../src/./main.cpp",
                                 directory=str(self.root / "custom"))
        self.assertEqual(find_compilation_database(self.root, self.source, explicit.parent),
                         explicit)

    def test_does_not_search_arbitrary_or_nested_build_directories(self) -> None:
        self.write_db("vendor/compile_commands.json")
        self.write_db("build/nested/compile_commands.json")
        self.assertIsNone(find_compilation_database(self.root, self.source, None))

    def test_accepts_posix_command_and_relative_directory(self) -> None:
        db = self.write_db("build/compile_commands.json", "../src/main.cpp",
                           directory=".", arguments=None,
                           command="g++ '-DNAME=hello world' -c ../src/main.cpp")
        command = load_compile_command(db, self.source)
        self.assertEqual(command.directory, self.root / "build")
        self.assertEqual(command.file, self.source)
        self.assertEqual(command.arguments[1], "-DNAME=hello world")

    def test_sanitize_preserves_project_flags_and_removes_outputs(self) -> None:
        command = CompileCommand(self.root, (
            "g++", "-Iinc", "-DMODE=1", "--target=x86_64-linux-gnu", "-std=c++17",
            "-x", "c++", "-c", "src/main.cpp", "-o", "a.o", "-MFdeps.d",
            "-MT", "target", "-MQquoted", "-MMD", "-MP", "-MJ", "entry.json",
        ), self.source)
        args = sanitize_command(command, clang_driver="clang++")
        self.assertEqual(args, (
            "clang++", "-Iinc", "-DMODE=1", "--target=x86_64-linux-gnu", "-std=c++17",
            "-x", "c++", "src/main.cpp", "--no-default-config", "-fsyntax-only",
            "-fno-crash-diagnostics", "-fdiagnostics-color=never", "-Xclang", "-ast-dump=json",
        ))

    def test_sanitize_keeps_flags_effective_before_option_terminator(self) -> None:
        command = CompileCommand(self.root, ("g++", "-c", "--", "src/main.cpp"), self.source)
        args = sanitize_command(command, clang_driver="clang++")
        self.assertLess(args.index("-fsyntax-only"), args.index("--"))
        self.assertEqual(args[-1], "src/main.cpp")

    def test_sanitize_rejects_commands_without_the_recorded_source_operand(self) -> None:
        command = CompileCommand(self.root, ("g++", "-c", "src/other.cpp"), self.source)
        with self.assertRaises(ValueError):
            sanitize_command(command, clang_driver="clang++")

    def test_review3_requires_exactly_one_recorded_source_operand(self) -> None:
        positional_inputs = (
            (),
            ("src/main.cpp", "src/main.cpp"),
            ("src/main.cpp", "src/other.cpp"),
            ("src/other.cpp", "src/main.cpp"),
            ("src/main.cpp", "input.o"),
            ("libhelper.a", "src/main.cpp"),
        )
        for operands in positional_inputs:
            with self.subTest(operands=operands):
                command = CompileCommand(
                    self.root,
                    ("g++", "-I", "include", *operands, "-x", "c++", "-c"),
                    self.source,
                )
                with self.assertRaises(ValueError):
                    sanitize_command(command, clang_driver="clang++")

    def test_review_option_values_cannot_masquerade_as_the_source_operand(self) -> None:
        for option in (
            "-include", "-include-pch", "-imacros", "-isystem", "-cxx-isystem",
            "-stdlib++-isystem", "-isystem-after", "-I", "-iquote", "-idirafter",
            "-iprefix", "-iwithprefix", "-iwithprefixbefore", "-iwithsysroot",
            "-F", "-iframework", "-iframeworkwithsysroot", "-ivfsoverlay",
            "-working-directory", "-resource-dir", "-gcc-toolchain",
            "--gcc-toolchain", "-target", "--target", "--sysroot", "-B",
            "-fmodule-map-file", "-fprebuilt-module-path",
        ):
            with self.subTest(option=option):
                command = CompileCommand(
                    self.root,
                    ("g++", option, "src/main.cpp", "-c", "src/other.cpp"),
                    self.source,
                )
                with self.assertRaises(ValueError):
                    sanitize_command(command, clang_driver="clang++")

    def test_review_unclassified_option_value_is_not_trusted_as_source(self) -> None:
        for option in ("--project-mystery", "-fproject-mode"):
            with self.subTest(option=option):
                command = CompileCommand(
                    self.root,
                    ("g++", option, "src/main.cpp", "-c", "src/other.cpp"),
                    self.source,
                )
                with self.assertRaises(ValueError):
                    sanitize_command(command, clang_driver="clang++")

    def test_review_safe_common_split_flags_and_late_language_are_preserved(self) -> None:
        command = CompileCommand(
            self.root,
            (
                "g++", "-I", "inc", "-isystem", "system-inc", "-D", "MODE=1",
                "-U", "OLD", "-Wall", "-Wextra", "-std=c++20", "-pthread",
                "-fno-exceptions", "-c", "src/main.cpp", "-x", "c++",
            ),
            self.source,
        )
        args = sanitize_command(command, clang_driver="clang++")
        self.assertEqual(
            args,
            (
                "clang++", "-I", "inc", "-isystem", "system-inc", "-D", "MODE=1",
                "-U", "OLD", "-Wall", "-Wextra", "-std=c++20", "-pthread",
                "-fno-exceptions", "src/main.cpp", "-x", "c++", "--no-default-config", "-fsyntax-only",
                "-fno-crash-diagnostics", "-fdiagnostics-color=never", "-Xclang", "-ast-dump=json",
            ),
        )
        self.assertEqual(clang._driver_for(command), "clang++")

    def test_review_language_selection_after_source_is_still_observed(self) -> None:
        command = CompileCommand(
            self.root,
            ("g++", "-c", "src/main.cpp", "-x", "c"),
            self.source,
        )
        self.assertEqual(clang._driver_for(command), "clang")

    def test_malformed_database_and_nonexact_basename_do_not_qualify(self) -> None:
        self.write_db("compile_commands.json", "elsewhere/main.cpp")
        bad = self.root / "build/compile_commands.json"
        bad.parent.mkdir()
        bad.write_text("{broken", encoding="utf-8")
        self.assertIsNone(find_compilation_database(self.root, self.source, None))


def node(kind: str, line: int | None = None, *, inner: list | None = None, **fields: object) -> dict:
    result = {"kind": kind, **fields}
    if line is not None:
        result["loc"] = {"line": line}
        result.setdefault("range", {"begin": {"line": line}, "end": {"line": line}})
    if inner is not None:
        result["inner"] = inner
    return result


def call(target: str, line: int = 3, return_type: str = "int") -> dict:
    return node("CallExpr", line, type={"qualType": return_type}, inner=[
        node("ImplicitCastExpr", inner=[node("DeclRefExpr", referencedDecl={"id": target})])])


def function(identifier: str, body: list, line: int = 1, **fields: object) -> dict:
    return node("FunctionDecl", line, id=identifier, name=identifier,
                inner=[node("CompoundStmt", line + 1, inner=body)], **fields)


class SemanticTests(unittest.TestCase):
    def test_review2_suppressed_self_cycle_keeps_independent_unsuppressed_cycle(self) -> None:
        ast = [function("f", [call("f", 2), call("g", 3)]), function("g", [call("f", 5)], 4)]
        text = "int f(){\nf(); // quality: ignore[POT01] - accepted self recursion\ng();\n}\nint g(){f();}\n"
        for lines, expected in ((None, [3]), (frozenset({2, 3, 5}), [3, 5]),
                                (frozenset({2}), []), (frozenset({3}), [3])):
            with self.subTest(lines=lines):
                results = self.analyze(ast, text=text, lines=lines)
                recursion = [f for f in results if f.code == "POT01"]
                self.assertEqual([f.line for f in recursion], expected)
                for result in recursion:
                    self.assertIn("g", result.message)

    def test_review_conditional_callee_is_not_a_direct_call(self) -> None:
        selected = node("CallExpr", 3, type={"qualType": "int"}, inner=[
            node("ParenExpr", inner=[node("ConditionalOperator", inner=[
                node("CXXBoolLiteralExpr", value=False),
                node("DeclRefExpr", referencedDecl={"id": "f"}),
                node("DeclRefExpr", referencedDecl={"id": "g"}),
            ])])])
        results = self.analyze([function("f", [selected]), function("g", [])])
        self.assertFalse(any(f.code == "POT01" for f in results))

    def test_review_inherited_virtual_member_dispatch_is_not_proven_direct(self) -> None:
        member_call = node("CXXMemberCallExpr", 3, type={"qualType": "int"}, inner=[
            node("MemberExpr", 3, referencedMemberDecl="derived_f")])
        base = node("CXXRecordDecl", 1, id="base", name="Base", completeDefinition=True,
                    inner=[node("CXXMethodDecl", 2, id="base_f", name="f", virtual=True)])
        derived = node("CXXRecordDecl", 4, id="derived", name="Derived", completeDefinition=True,
                       bases=[{"type": {"qualType": "Base"}}], inner=[
                           node("CXXMethodDecl", 5, id="derived_f", name="f", inner=[
                               node("CompoundStmt", 5, inner=[member_call])])])
        self.assertFalse(any(f.code == "POT01" for f in self.analyze([base, derived])))

    def test_review_changed_edge_gets_witness_even_after_dfs_finishes_target(self) -> None:
        ast = [function("f", [call("g", 2), call("h", 3)]),
               function("g", [call("f", 5)], 4), function("h", [call("g", 7)], 6)]
        results = self.analyze(ast, lines=frozenset({3}))
        recursion = [f for f in results if f.code == "POT01"]
        self.assertEqual([(f.code, f.line) for f in recursion], [("POT01", 3)])
        self.assertIn("f -> h -> g -> f", recursion[0].message)
        results = self.analyze(ast, lines=frozenset({2, 3, 5, 7}))
        self.assertEqual([f.line for f in results if f.code == "POT01"], [2, 3, 5, 7])

    def test_review_same_line_nodiscard_error_survives_plain_result_warning(self) -> None:
        declaration = node("FunctionDecl", 1, id="get", inner=[node("WarnUnusedResultAttr")])
        for calls in ([call("get", 3), call("plain", 3)], [call("plain", 3), call("get", 3)]):
            with self.subTest(calls=calls):
                results = self.analyze([declaration, function("f", calls)])
                self.assertEqual([(f.code, f.severity) for f in results], [("POT07", "error")])

    def analyze(self, nodes: list, *, text: str | None = None, lines=None):
        return analyze_ast(node("TranslationUnitDecl", inner=nodes), Path("main.cpp"),
                           text or "\n" * 400, changed_lines=lines)

    def assert_rule(self, nodes: list, code: str, severity: str):
        results = self.analyze(nodes)
        self.assertIn((code, severity), [(f.code, f.severity) for f in results])
        return [f for f in results if f.code == code]

    def test_direct_recursion_and_complete_indirect_cycle(self) -> None:
        direct = self.assert_rule([function("f", [call("f")])], "POT01", "error")
        self.assertEqual(len(direct), 1)
        indirect = self.assert_rule([function("f", [call("g")]),
                                     function("g", [call("f", 8)], 6)], "POT01", "error")
        self.assertEqual(len(indirect), 1)
        self.assertFalse(any(f.code == "POT01" for f in self.analyze([function("f", [call("external")])])))

    def test_recursive_call_site_changed_without_definition_line(self) -> None:
        results = self.analyze([function("f", [call("f", 3)])], lines=frozenset({3}))
        self.assertEqual([(f.code, f.line) for f in results if f.code == "POT01"], [("POT01", 3)])

    def test_forward_declaration_resolves_to_definition(self) -> None:
        results = self.analyze([node("FunctionDecl", 1, id="decl", name="f"),
                                function("def", [call("decl", 4)], 2, previousDecl="decl")])
        self.assertEqual(len([f for f in results if f.code == "POT01"]), 1)

    def test_uninvoked_lambda_body_is_not_an_outer_function_call_edge(self) -> None:
        closure = node("LambdaExpr", 2, inner=[
            node("CXXRecordDecl", 2, completeDefinition=True, inner=[
                node("CXXMethodDecl", 2, id="lambda", name="operator()", inner=[
                    node("CompoundStmt", 2, inner=[call("outer", 3)])])]),
            node("CompoundStmt", 2, inner=[call("outer", 3)]),
        ])
        results = self.analyze([function("outer", [closure])])
        self.assertFalse(any(f.code == "POT01" for f in results))

    def test_unevaluated_calls_and_virtual_targets_do_not_confirm_recursion(self) -> None:
        unevaluated = function("f", [node("UnaryExprOrTypeTraitExpr", 2, name="sizeof", inner=[call("f")])])
        virtual = node("CXXMethodDecl", 1, id="v", name="v", virtual=True, inner=[
            node("CompoundStmt", 1, inner=[node("CXXMemberCallExpr", 2, type={"qualType": "int"}, inner=[
                node("MemberExpr", 2, referencedMemberDecl="v")])])])
        self.assertFalse(any(f.code == "POT01" for f in self.analyze([unevaluated, virtual])))

    def test_long_function_threshold_and_mutable_file_state(self) -> None:
        long = function("long", [], range={"begin": {"line": 1}, "end": {"line": 61}})
        self.assert_rule([long], "POT04", "error")
        long["range"]["end"]["line"] = 60
        self.assertFalse(any(f.code == "POT04" for f in self.analyze([long])))
        self.assert_rule([node("VarDecl", 2, type={"qualType": "int"})], "POT06", "warning")
        self.assertEqual(self.analyze([node("VarDecl", 2, type={"qualType": "const int"}),
                                      function("f", [node("VarDecl", 3, type={"qualType": "int"})])]), ())

    def test_function_length_uses_range_begin_before_function_name(self) -> None:
        definition = function("f", [], 5, range={"begin": {"line": 1}, "end": {"line": 61}})
        results = self.analyze([definition], lines=frozenset({1}))
        self.assertEqual([(f.code, f.line) for f in results], [("POT04", 1)])

    def test_raw_arrow_chain_counts_dereferences_but_smart_pointer_call_does_not(self) -> None:
        expr = node("DeclRefExpr", 3, type={"qualType": "Node *"})
        for _ in range(3):
            expr = node("MemberExpr", 3, isArrow=True, inner=[expr])
        self.assert_rule([function("f", [expr])], "POT09", "error")
        smart = node("CXXOperatorCallExpr", 3, type={"qualType": "Node &"})
        self.assertFalse(any(f.code == "POT09" for f in self.analyze([function("f", [smart])])))

    def test_discarded_nodiscard_and_nonvoid_results(self) -> None:
        declaration = node("FunctionDecl", 1, id="get", inner=[node("WarnUnusedResultAttr")])
        self.assert_rule([declaration, function("f", [call("get")], 2)], "POT07", "error")
        self.assert_rule([function("f", [call("external")])], "POT07", "warning")
        self.assertEqual(self.analyze([function("f", [node("ReturnStmt", 3, inner=[call("external")])])]), ())
        self.assertEqual(self.analyze([function("f", [call("external", return_type="void")])]), ())
        self.assertEqual(self.analyze([function("f", [node("CStyleCastExpr", 3,
                         type={"qualType": "void"}, inner=[call("external")])])]), ())

    def test_function_pointer_type_and_triple_dereference(self) -> None:
        self.assert_rule([node("ParmVarDecl", 2, type={"qualType": "int (*)(int)"})], "POT09", "error")
        expr = node("DeclRefExpr", 3, type={"qualType": "int ***"})
        for _ in range(3):
            expr = node("UnaryOperator", 3, opcode="*", inner=[expr])
        self.assert_rule([function("f", [expr])], "POT09", "error")
        self.assertEqual(self.analyze([node("ParmVarDecl", 2, type={"qualType": "int &"})]), ())

    def test_solid_heuristics_are_advisory_and_contextual(self) -> None:
        large = node("CXXRecordDecl", 1, name="Large", completeDefinition=True,
                     range={"begin": {"line": 1}, "end": {"line": 301}})
        abstract = node("CXXRecordDecl", 1, name="Wide", completeDefinition=True,
                        inner=[node("CXXMethodDecl", i + 2, pure=True) for i in range(11)])
        stub = node("CXXMethodDecl", 2, inner=[node("OverrideAttr"),
                    node("CompoundStmt", 2, inner=[node("CXXThrowExpr", 3)])])
        service = node("CXXRecordDecl", 1, name="OrderService", completeDefinition=True,
                       inner=[node("CXXMethodDecl", 2, inner=[node("CompoundStmt", 2, inner=[
                           node("CXXConstructExpr", 3, type={"qualType": "SqlDatabase"})])])])
        for nodes, rule in (([large], "SOLID01"), ([abstract], "SOLID04"),
                            ([stub], "SOLID03"), ([service], "SOLID05")):
            for result in self.assert_rule(nodes, rule, "warning"):
                self.assertFalse(result.gate_eligible)
        service["name"] = "LowLevelAdapter"
        self.assertFalse(any(f.code == "SOLID05" for f in self.analyze([service])))

    def test_unsafe_polymorphic_destruction_but_not_protected_destructor(self) -> None:
        record = node("CXXRecordDecl", 1, name="Base", tagUsed="struct", completeDefinition=True,
                      inner=[node("CXXMethodDecl", 2, virtual=True),
                             node("CXXDestructorDecl", 3, name="~Base")])
        self.assert_rule([record], "SOLID03", "warning")
        record["inner"].insert(1, node("AccessSpecDecl", 3, access="protected"))
        self.assertFalse(any(f.code == "SOLID03" for f in self.analyze([record])))

    def test_locations_scope_suppression_and_missing_data(self) -> None:
        nodes = [node("VarDecl", 1, type={"qualType": "int"}),
                 node("VarDecl", 2, type={"qualType": "int"}),
                 node("VarDecl", type={"qualType": "int"})]
        result = self.analyze(nodes, text="int x; // quality: ignore[POT06] - required shared state\nint y;\n",
                              lines=frozenset({1}))
        self.assertEqual(result, ())
        nodes[0]["loc"]["file"] = "/outside/header.hpp"
        self.assertEqual(self.analyze(nodes[:1]), ())

    def test_offset_only_locations_and_header_file_elision(self) -> None:
        offset = node("VarDecl", type={"qualType": "int"}, loc={"offset": 7})
        result = self.analyze([offset], text="int a;\nint b;\n")
        self.assertEqual([(f.code, f.line) for f in result], [("POT06", 2)])
        header = node("VarDecl", 1, type={"qualType": "int"})
        header["loc"]["file"] = "/outside/header.hpp"
        hidden = node("VarDecl", 2, type={"qualType": "int"})
        self.assertEqual(self.analyze([header, hidden]), ())


class FakeClangTests(TemporaryTests):
    def fake(self, body: str) -> None:
        binary = self.root / "bin"
        binary.mkdir(exist_ok=True)
        for driver in ("clang", "clang++"):
            executable = binary / driver
            executable.write_text(f"#!{sys.executable}\nimport json, sys, time, os\n{body}\n",
                                  encoding="utf-8")
            executable.chmod(0o755)
        patch = mock.patch.dict(os.environ, {"PATH": str(binary)})
        patch.start()
        self.addCleanup(patch.stop)

    def run_analysis(self, **kwargs: object):
        return run_clang_analysis(
            {self.source: self.source.read_text(encoding="utf-8")}, root=self.root, **kwargs)


class ClangExecutionTests(FakeClangTests):
    def test_review3_multiple_positional_inputs_never_spawn_clang(self) -> None:
        marker = self.root / "clang-started"
        self.write_db(
            "compile_commands.json",
            arguments=["g++", "src/main.cpp", "input.o", "-c"],
        )
        self.fake(
            "open(" + repr(str(marker)) + ", 'w').write('started')\n"
            "print('{\"kind\": \"TranslationUnitDecl\"}')"
        )
        result = self.run_analysis()
        self.assertEqual(
            [item.code for item in result.limitations], ["TOOL_CLANG_FAILURE"]
        )
        self.assertFalse(marker.exists())

    def test_review3_output_and_reproducer_controls_never_spawn_clang(self) -> None:
        marker = self.root / "clang-started"
        self.fake(
            "open(" + repr(str(marker)) + ", 'w').write('started')\n"
            "print('{\"kind\": \"TranslationUnitDecl\"}')"
        )
        for flags in (
            ("-gen-cdb-fragment-path", "audit-cdb"),
            ("-gen-cdb-fragment-path=audit-cdb",),
            ("-gen-reproducer",),
            ("-gen-reproducer=always",),
            ("-fcrash-diagnostics-dir", "crashes"),
            ("-fcrash-diagnostics-dir=crashes",),
            ("-fcrash-diagnostics",),
            ("-fcrash-diagnostics=always",),
            ("-fproc-stat-report=process.json",),
        ):
            with self.subTest(flags=flags):
                marker.unlink(missing_ok=True)
                self.write_db(
                    "compile_commands.json",
                    arguments=["g++", *flags, "-c", "src/main.cpp"],
                )
                result = self.run_analysis()
                self.assertEqual(
                    [item.code for item in result.limitations],
                    ["TOOL_CLANG_FAILURE"],
                )
                self.assertFalse(marker.exists())

    def test_review_module_cache_command_is_a_limitation_without_execution(self) -> None:
        marker = self.root / "clang-started"
        self.write_db(
            "compile_commands.json",
            arguments=[
                "g++", "-fmodules", "-fmodules-cache-path", "cache",
                "-c", "src/main.cpp",
            ],
        )
        self.fake(
            "open(" + repr(str(marker)) + ", 'w').write('started')\n"
            "print('{\"kind\": \"TranslationUnitDecl\"}')"
        )
        result = self.run_analysis()
        self.assertEqual([item.code for item in result.limitations], ["TOOL_CLANG_FAILURE"])
        self.assertFalse(marker.exists())

    def test_review_non_posix_timeout_and_overflow_never_use_process_groups(self) -> None:
        cases = (
            ((sys.executable, "-c", "import time; time.sleep(2)"), 1024, "TOOL_CLANG_TIMEOUT"),
            ((sys.executable, "-c", "import sys; sys.stdout.write('x' * 10000)"), 128, "TOOL_CLANG_OUTPUT"),
        )
        for arguments, limit, expected in cases:
            with (
                self.subTest(expected=expected),
                mock.patch.object(clang.os, "name", "nt"),
                mock.patch.object(clang.os, "killpg", side_effect=AssertionError("POSIX-only kill")),
            ):
                started = time.monotonic()
                _, _, _, failure = clang._execute(arguments, self.root, 0.05, limit)
                self.assertEqual(failure, expected)
                self.assertLess(time.monotonic() - started, 1.0)

    @unittest.skipUnless(os.name == "posix", "process-group behavior is POSIX-only")
    def test_review_posix_timeout_kills_descendants_holding_capture_pipes(self) -> None:
        marker = self.root / "descendant-survived"
        child = (
            "import pathlib,time; time.sleep(.3); "
            f"pathlib.Path({str(marker)!r}).write_text('survived')"
        )
        parent = (
            "import subprocess,sys,time; "
            f"subprocess.Popen([sys.executable, '-c', {child!r}]); time.sleep(2)"
        )
        _, _, _, failure = clang._execute(
            (sys.executable, "-c", parent), self.root, 0.05, 1024
        )
        time.sleep(0.4)
        self.assertEqual(failure, "TOOL_CLANG_TIMEOUT")
        self.assertFalse(marker.exists())

    def test_review_plugin_loading_is_a_visible_limitation_without_execution(self) -> None:
        marker = self.root / "clang-started"
        for flags in (("-fplugin=project-plugin.so",), ("-mllvm", "-load=project-pass.so")):
            with self.subTest(flags=flags):
                marker.unlink(missing_ok=True)
                self.write_db(
                    "compile_commands.json",
                    arguments=["g++", *flags, "-c", "src/main.cpp"],
                )
                self.fake(
                    "open(" + repr(str(marker)) + ", 'w').write('started')\n"
                    "print('{\"kind\": \"TranslationUnitDecl\"}')"
                )
                result = self.run_analysis()
                self.assertEqual([item.code for item in result.limitations], ["TOOL_CLANG_FAILURE"])
                self.assertFalse(marker.exists())

    def test_review2_default_config_cannot_restore_output_options(self) -> None:
        self.write_db("compile_commands.json")
        self.fake("if '--no-default-config' not in sys.argv:\n"
                  "    open('implicit-config-output.dia', 'w').write('default config output')\n"
                  "print('{\"kind\": \"TranslationUnitDecl\"}')")
        result = self.run_analysis()
        self.assertEqual(result.limitations, ())
        self.assertFalse((self.root / "implicit-config-output.dia").exists())

    def test_review2_explicit_config_is_rejected_before_execution(self) -> None:
        self.write_db("compile_commands.json", arguments=["g++", "--config=project.cfg", "-c", "src/main.cpp"])
        self.fake("open('explicit-config-output.dia', 'w').write('explicit config output')\n"
                  "print('{\"kind\": \"TranslationUnitDecl\"}')")
        result = self.run_analysis()
        self.assertEqual([item.code for item in result.limitations], ["TOOL_CLANG_FAILURE"])
        self.assertFalse((self.root / "explicit-config-output.dia").exists())

    def test_review_driver_respects_explicit_language_then_recorded_driver(self) -> None:
        cases = (("main.h", "g++", (), "clang++"),
                 ("main.c", "clang++", (), "clang++"),
                 ("main.cpp", "g++", ("-x", "c"), "clang"),
                 ("main.c", "gcc", ("-xc++",), "clang++"),
                 ("main.c", "gcc", (), "clang"))
        for filename, driver, flags, expected in cases:
            with self.subTest(filename=filename, driver=driver, flags=flags):
                self.source = self.root / filename
                self.source.write_text("int x;\n", encoding="utf-8")
                self.write_db("compile_commands.json", filename, arguments=[driver, *flags, "-c", filename])
                self.fake("assert os.path.basename(sys.argv[0]) == " + repr(expected) + "\n"
                          "print('{\"kind\": \"TranslationUnitDecl\"}')")
                self.assertEqual(self.run_analysis().limitations, ())

    def test_review_unknown_compiler_language_is_a_limitation(self) -> None:
        self.write_db("compile_commands.json", arguments=["project-compiler", "-c", "src/main.cpp"])
        self.fake("raise AssertionError('ambiguous compiler must not execute')")
        result = self.run_analysis()
        self.assertEqual(result.limitations[0].code, "TOOL_CLANG_LANGUAGE")

    def test_expired_ast_deadline_is_a_time_limitation(self) -> None:
        self.write_db("compile_commands.json")
        self.fake("print('{\"kind\": \"TranslationUnitDecl\"}')")
        with mock.patch.object(clang, "analyze_ast", side_effect=TimeoutError("deadline")):
            result = self.run_analysis()
        self.assertEqual(result.limitations[0].code, "TOOL_CLANG_TIMEOUT")
        self.assertTrue(result.truncated)

    def test_relative_ast_filename_uses_compilation_directory(self) -> None:
        self.write_db("compile_commands.json")
        ast = node("TranslationUnitDecl", inner=[node("VarDecl", 1, type={"qualType": "int"})])
        ast["inner"][0]["loc"]["file"] = "src/main.cpp"
        self.fake("print(" + repr(json.dumps(ast)) + ")")
        self.assertEqual([(f.code, f.line) for f in self.run_analysis().findings], [("POT06", 1)])

    def test_combined_stderr_and_stdout_share_one_output_cap(self) -> None:
        self.write_db("compile_commands.json")
        self.fake("sys.stderr.write('x' * 800); sys.stderr.flush(); print('x' * 800)")
        self.assertEqual(self.run_analysis(output_limit=1024).limitations[0].code, "TOOL_CLANG_OUTPUT")

    def test_semantic_units_stop_at_ten(self) -> None:
        sources = {self.root / f"unit{i:02}.cpp": "int x;\n" for i in range(11)}
        entries = []
        for source, text in sources.items():
            source.write_text(text, encoding="utf-8")
            entries.append({"directory": str(self.root), "file": str(source),
                            "arguments": ["g++", "-c", str(source)]})
        (self.root / "compile_commands.json").write_text(json.dumps(entries), encoding="utf-8")
        self.fake("print(sys.argv[1] + ':1:1: warning: fixture', file=sys.stderr)\n"
                  "print('{\"kind\": \"TranslationUnitDecl\"}')")
        result = run_clang_analysis(sources, root=self.root)
        self.assertEqual(len(result.findings), 10)
        self.assertTrue(result.truncated)
        self.assertEqual(result.limitations[-1].code, "TOOL_CLANG_LIMIT")

    def test_malformed_ast_fields_fail_open(self) -> None:
        self.write_db("compile_commands.json")
        self.fake("print('{\"kind\": \"TranslationUnitDecl\", \"inner\": [{\"kind\": \"CXXRecordDecl\", \"completeDefinition\": true, \"name\": null, \"inner\": [{\"kind\": \"CXXConstructExpr\"}]}]}')")
        result = self.run_analysis()
        self.assertEqual(result.limitations[0].code, "TOOL_CLANG_FAILURE")

    def test_nonzero_exit_keeps_attributable_compiler_diagnostic(self) -> None:
        self.write_db("compile_commands.json")
        self.fake("print('src/main.cpp:1:3: error: parse failed', file=sys.stderr); sys.exit(1)")
        result = self.run_analysis()
        self.assertEqual([(f.code, f.severity) for f in result.findings], [("POT10", "error")])
        self.assertEqual(result.limitations[0].code, "TOOL_CLANG_PARSE")

    def test_fake_clang_receives_preserved_flags_in_recorded_directory(self) -> None:
        self.write_db("compile_commands.json", arguments=["g++", "-Iinc", "-c", "src/main.cpp", "-o", "a.o"])
        self.fake("assert os.getcwd() == " + repr(str(self.root)) + "\n"
                  "assert sys.argv[1:] == ['-Iinc', 'src/main.cpp', '--no-default-config', '-fsyntax-only', "
                  "'-fno-crash-diagnostics', '-fdiagnostics-color=never', '-Xclang', '-ast-dump=json']\n"
                  "print(json.dumps({'kind': 'TranslationUnitDecl', 'inner': []}))")
        result = self.run_analysis()
        self.assertEqual(result.limitations, ())
        self.assertEqual(result.databases, (str(self.root / "compile_commands.json"),))
        self.assertFalse((self.root / "a.o").exists())

    def test_failures_are_limitations(self) -> None:
        self.write_db("compile_commands.json")
        for body, options, code in (
            ("time.sleep(2)", {"unit_timeout": 0.05}, "TOOL_CLANG_TIMEOUT"),
            ("print('{}'); sys.exit(1)", {}, "TOOL_CLANG_PARSE"),
            ("print('x' * 20000)", {"output_limit": 1024}, "TOOL_CLANG_OUTPUT"),
            ("print('{bad')", {}, "TOOL_CLANG_JSON"),
            ("print('[]')", {}, "TOOL_CLANG_JSON"),
        ):
            with self.subTest(code=code, body=body):
                self.fake(body)
                result = self.run_analysis(**options)
                self.assertEqual(result.findings, ())
                self.assertEqual(result.limitations[0].code, code)

    def test_missing_clang_and_no_exact_entry_are_limitations(self) -> None:
        with mock.patch.dict(os.environ, {"PATH": ""}):
            result = self.run_analysis()
            self.assertTrue(result.limitations[0].code.startswith("TOOL_CLANG_"))
            self.write_db("compile_commands.json")
            result = self.run_analysis()
            self.assertEqual(result.limitations[0].code, "TOOL_CLANG_MISSING")

    def test_global_deadline_stops_execution(self) -> None:
        self.write_db("compile_commands.json")
        self.fake("time.sleep(2)")
        start = time.monotonic()
        result = self.run_analysis(budget_seconds=0.05)
        self.assertLess(time.monotonic() - start, 1)
        self.assertEqual(result.limitations[0].code, "TOOL_CLANG_TIMEOUT")

    def test_warning_on_changed_line_becomes_pot10(self) -> None:
        self.write_db("compile_commands.json")
        self.fake("print('src/main.cpp:1:3: warning: bad thing', file=sys.stderr)\n"
                  "print('src/main.cpp:2:3: error: unchanged', file=sys.stderr)\n"
                  "print('other.cpp:1:3: warning: unrelated', file=sys.stderr)\n"
                  "print('{\"kind\": \"TranslationUnitDecl\"}')")
        result = self.run_analysis(changed_lines={Path("src/main.cpp"): frozenset({1})})
        self.assertEqual([(f.code, f.severity, f.line) for f in result.findings],
                         [("POT10", "error", 1)])

    def test_unused_result_diagnostic_deduplicates_ast_result(self) -> None:
        self.write_db("compile_commands.json")
        ast = node("TranslationUnitDecl", inner=[
            node("FunctionDecl", 1, id="get", inner=[node("WarnUnusedResultAttr")]),
            function("f", [call("get", 1)])])
        self.fake("print('src/main.cpp:1:3: warning: ignoring return value [-Wunused-result]', file=sys.stderr)\n"
                  "print(" + repr(json.dumps(ast)) + ")")
        result = self.run_analysis()
        self.assertEqual([(f.code, f.severity) for f in result.findings], [("POT07", "error")])


@unittest.skipUnless(shutil.which("clang") and shutil.which("clang++"), "Clang drivers are not installed")
class RealClangSmokeTests(TemporaryTests):
    def test_review3_cdb_fragment_command_cannot_write_target_tree(self) -> None:
        self.source = self.root / "main.c"
        self.source.write_text("int main(void) { return 0; }\n", encoding="utf-8")
        self.write_db(
            "compile_commands.json",
            "main.c",
            arguments=[
                "clang", "-gen-cdb-fragment-path", "audit-cdb",
                "-c", "main.c",
            ],
        )
        before = sorted(path.relative_to(self.root) for path in self.root.rglob("*"))
        result = run_clang_analysis(
            {self.source: self.source.read_text(encoding="utf-8")}, root=self.root
        )
        after = sorted(path.relative_to(self.root) for path in self.root.rglob("*"))
        self.assertEqual(
            [item.code for item in result.limitations], ["TOOL_CLANG_FAILURE"]
        )
        self.assertEqual(after, before)

    def test_review_implicit_modules_cannot_create_target_cache_files(self) -> None:
        include = self.root / "include"
        include.mkdir()
        (include / "module.modulemap").write_text(
            'module Foo { header "foo.h" export * }\n', encoding="utf-8"
        )
        (include / "foo.h").write_text("int foo(void);\n", encoding="utf-8")
        self.source = self.root / "main.c"
        self.source.write_text('#include "foo.h"\nint main(void) { return 0; }\n', encoding="utf-8")
        self.write_db(
            "compile_commands.json",
            "main.c",
            arguments=[
                "clang", "-fmodules", "-fimplicit-module-maps",
                "-fmodules-cache-path", "cache", "-I", "include", "-c", "main.c",
            ],
        )
        before = sorted(path.relative_to(self.root) for path in self.root.rglob("*"))
        result = run_clang_analysis(
            {self.source: self.source.read_text(encoding="utf-8")}, root=self.root
        )
        after = sorted(path.relative_to(self.root) for path in self.root.rglob("*"))
        self.assertEqual([item.code for item in result.limitations], ["TOOL_CLANG_FAILURE"])
        self.assertEqual(after, before)

    def test_review_false_recursion_language_and_disabled_unused_warning(self) -> None:
        cases = (
            ("select.cpp", "int g(){return 0;}\nint f(){return (false ? f : g)();}\n", "g++", set()),
            ("virtual.cpp", "struct Base { virtual int f(); };\nstruct Derived : Base { int f(); };\n"
             "int Derived::f(){return f();}\n", "g++", set()),
            ("header.h", "namespace project { int value; }\n", "g++", {("POT06", "warning")}),
            ("cpp.c", "namespace project { int value; }\n", "g++", {("POT06", "warning")}),
            ("results.cpp", "[[nodiscard]] int get(){return 1;}\nint plain(){return 2;}\n"
             "void f(){get(); plain();}\n", "g++", {("POT07", "error")}),
        )
        for filename, text, driver, expected in cases:
            with self.subTest(filename=filename):
                source = self.root / filename
                source.write_text(text, encoding="utf-8")
                self.write_db("compile_commands.json", filename,
                              arguments=[driver, "-Wno-unused-result", "-c", filename])
                result = run_clang_analysis({source: text}, root=self.root)
                self.assertEqual(result.limitations, ())
                self.assertFalse(any(f.code == "POT01" for f in result.findings), result)
                self.assertTrue(expected.issubset({(f.code, f.severity) for f in result.findings}), result)

    def test_c_and_cpp_diagnostics_ast_and_no_output_artifacts(self) -> None:
        for suffix, text, expected in (
            (".c", "int state;\nint recurse(void) { return recurse(); }\n", {"POT01", "POT06"}),
            (".cpp", "[[nodiscard]] int get() { return 1; }\nvoid f() { get(); }\n"
                      "int recurse() { return recurse(); }\n", {"POT01", "POT07"}),
        ):
            with self.subTest(suffix=suffix):
                source = self.root / ("main" + suffix)
                source.write_text(text, encoding="utf-8")
                self.write_db("compile_commands.json", source.name,
                              arguments=["gcc" if suffix == ".c" else "g++", "-Wall", "-c", source.name, "-o", "main.o"])
                result = run_clang_analysis({source: text}, root=self.root)
                self.assertEqual(result.limitations, ())
                self.assertTrue(expected.issubset({f.code for f in result.findings}), result)
                self.assertFalse((self.root / "main.o").exists())


if __name__ == "__main__":
    unittest.main()
