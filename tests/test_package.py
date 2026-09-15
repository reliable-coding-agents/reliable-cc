"""Package contract tests for the functional Reliable C/C++ release."""

from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Iterator


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_SKILLS = [
    "applying-power-of-ten-to-c-cpp",
    "applying-solid-to-cpp",
    "reviewing-cc-quality",
    "using-reliable-cc",
]


def load_json(path: Path) -> dict[str, Any]:
    """Load a JSON object from a package file."""
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise TypeError(f"{path} must contain a JSON object")
    return payload


def host_and_catalog_manifests() -> tuple[dict[str, Any], ...]:
    """Return both host manifests and the Reliable C/C++ catalog entry."""
    catalog = load_json(ROOT / ".claude-plugin" / "marketplace.json")
    return (
        load_json(ROOT / ".claude-plugin" / "plugin.json"),
        load_json(ROOT / ".codex-plugin" / "plugin.json"),
        catalog["plugins"][0],
    )


def nested_strings(value: object) -> Iterator[str]:
    """Yield strings from a JSON-compatible value."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from nested_strings(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from nested_strings(child)


def tracked_files() -> tuple[str, ...]:
    """Return repository paths tracked by Git."""
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return tuple(
        path.decode("utf-8", errors="surrogateescape")
        for path in result.stdout.split(b"\0")
        if path
    )


def is_main_guard(statement: ast.stmt) -> bool:
    """Return whether a top-level statement is the standard main guard."""
    if not isinstance(statement, ast.If):
        return False
    comparison = statement.test
    return (
        isinstance(comparison, ast.Compare)
        and isinstance(comparison.left, ast.Name)
        and comparison.left.id == "__name__"
        and len(comparison.ops) == 1
        and isinstance(comparison.ops[0], ast.Eq)
        and len(comparison.comparators) == 1
        and isinstance(comparison.comparators[0], ast.Constant)
        and comparison.comparators[0].value == "__main__"
    )


def git_ignore_results(paths: tuple[str, ...]) -> dict[str, bool]:
    """Evaluate the tracked ignore file without global or local excludes."""
    with tempfile.TemporaryDirectory() as temporary:
        repository = Path(temporary)
        subprocess.run(
            ["git", "init", "--quiet"], cwd=repository, check=True
        )
        (repository / ".git" / "info" / "exclude").write_text("", encoding="utf-8")
        shutil.copyfile(ROOT / ".gitignore", repository / ".gitignore")
        ignored: dict[str, bool] = {}
        for path in paths:
            result = subprocess.run(
                [
                    "git",
                    "-c",
                    f"core.excludesFile={os.devnull}",
                    "check-ignore",
                    "--quiet",
                    "--no-index",
                    "--",
                    path,
                ],
                cwd=repository,
                check=False,
            )
            if result.returncode not in {0, 1}:
                raise RuntimeError(
                    f"git check-ignore failed with status {result.returncode}"
                )
            ignored[path] = result.returncode == 0
        return ignored


def contradicts_non_certification(
    public_copy: str, approved_negative_boundary: str,
) -> bool:
    """Return whether copy contradicts its approved non-certification claim."""
    normalized_copy = " ".join(public_copy.lower().split())
    normalized_boundary = " ".join(approved_negative_boundary.lower().split())
    if normalized_boundary not in normalized_copy:
        return True
    remainder = normalized_copy.replace(normalized_boundary, "", 1)
    certification = re.compile(
        r"\bcertif(?:y|ies|ied|ying|icate|icates|icated|icating|ication|ications)\b"
    )
    protected_concept = re.compile(
        r"\b(?:safety|correctness|security|compliance|solid)\b|"
        r"\bpower(?:\s+|-)of(?:\s+|-)ten\b"
    )
    return any(
        certification.search(clause) and protected_concept.search(clause)
        for clause in re.split(r"[.!?;]+", remainder)
    )


class PackageContractTests(unittest.TestCase):
    """Protect public metadata and runnable package boundaries."""

    def test_host_manifests_share_identity_and_version(self) -> None:
        claude, codex, catalog_entry = host_and_catalog_manifests()

        self.assertEqual(
            {claude["name"], codex["name"], catalog_entry["name"]},
            {"reliable-cc"},
        )
        self.assertEqual(
            {claude["version"], codex["version"], catalog_entry["version"]},
            {"0.1.0"},
        )
        self.assertEqual(catalog_entry["source"], "./")
        self.assertEqual(codex["skills"], "./skills/")

    def test_package_exposes_hooks_and_four_unique_skills(self) -> None:
        hooks = load_json(ROOT / "hooks" / "hooks.json")["hooks"]
        self.assertEqual(set(hooks), {"SessionStart", "Stop"})
        self.assertEqual(
            sorted(path.name for path in (ROOT / "skills").iterdir()),
            EXPECTED_SKILLS,
        )

        claude = load_json(ROOT / ".claude-plugin" / "plugin.json")
        codex = load_json(ROOT / ".codex-plugin" / "plugin.json")
        self.assertNotIn("hooks", claude)
        self.assertNotIn("hooks", codex)

    def test_every_manifest_describes_functional_v010(self) -> None:
        for manifest in host_and_catalog_manifests():
            with self.subTest(manifest=manifest["name"]):
                self.assertEqual(manifest["version"], "0.1.0")
                description = manifest["description"].lower()
                self.assertNotIn("scaffold", description)
                self.assertNotIn("foundation", description)
                self.assertIn("power of ten", description)
                self.assertIn("advisory solid", description)

    def test_readme_badge_matches_public_v010_version(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        match = re.search(r"badge/version-(\d+\.\d+\.\d+)-", readme)
        self.assertIsNotNone(match)
        assert match is not None
        manifest_versions = {
            manifest["version"] for manifest in host_and_catalog_manifests()
        }
        self.assertEqual({match.group(1)}, manifest_versions)
        self.assertEqual(manifest_versions, {"0.1.0"})
        readme_versions = set(
            re.findall(r"\b(?:version\s+|v)?(\d+\.\d+\.\d+)\b", readme)
        )
        self.assertEqual(readme_versions, manifest_versions)

    def test_readme_separates_installed_agent_and_source_checkout_cli(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("Installed agent usage", readme)
        self.assertIn("Source checkout CLI", readme)
        self.assertIn("$reviewing-cc-quality", readme)

        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary)
            (target / "main.c").write_text("goto out;\n", encoding="utf-8")
            command = [
                sys.executable,
                str(ROOT / "skills/reviewing-cc-quality/scripts/audit_cc.py"),
                "main.c",
                "--format",
                "json",
                "--fail-on",
                "none",
            ]
            result = subprocess.run(
                command,
                cwd=target,
                check=False,
                capture_output=True,
                text=True,
                env={**os.environ, "RELIABLE_CC_CLANG": "off"},
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["findings"][0]["code"], "POT01")

    def test_public_validation_commands_do_not_name_a_contributor_home(self) -> None:
        for name in ("CONTRIBUTING.md", "CLAUDE.md"):
            with self.subTest(name=name):
                copy = (ROOT / name).read_text(encoding="utf-8")
                self.assertNotRegex(copy, r"/home/[^/\s]+/")
                self.assertIn("${CODEX_HOME:-$HOME/.codex}", copy)

    def test_registered_hook_command_targets_exist(self) -> None:
        hooks = load_json(ROOT / "hooks" / "hooks.json")["hooks"]
        commands = [
            hook["command"]
            for event in hooks.values()
            for registration in event
            for hook in registration["hooks"]
            if hook["type"] == "command"
        ]
        self.assertTrue(commands)
        for command in commands:
            with self.subTest(command=command):
                match = re.fullmatch(
                    r'python3 "\$\{CLAUDE_PLUGIN_ROOT\}/([^"\n]+)"', command
                )
                self.assertIsNotNone(match)
                assert match is not None
                self.assertTrue((ROOT / match.group(1)).is_file())

    def test_registered_paths_are_portable(self) -> None:
        payloads = (
            *host_and_catalog_manifests(),
            load_json(ROOT / "hooks" / "hooks.json"),
        )
        for value in nested_strings(payloads):
            with self.subTest(value=value):
                self.assertNotIn(".codex/plugins/cache", value)

    def test_every_executable_python_file_has_a_main_guard(self) -> None:
        result = subprocess.run(
            ["git", "ls-files", "--stage", "-z", "*.py"],
            cwd=ROOT,
            check=True,
            capture_output=True,
        )
        executable_paths: list[str] = []
        for entry in result.stdout.split(b"\0"):
            if not entry:
                continue
            metadata, raw_path = entry.split(b"\t", 1)
            if metadata.split(maxsplit=1)[0] == b"100755":
                executable_paths.append(
                    raw_path.decode("utf-8", errors="surrogateescape")
                )

        self.assertTrue(executable_paths)
        for path in executable_paths:
            with self.subTest(path=path):
                source = (ROOT / path).read_text(encoding="utf-8")
                module = ast.parse(source, filename=path)
                self.assertTrue(any(is_main_guard(item) for item in module.body))

    def test_generated_python_artifacts_are_not_tracked(self) -> None:
        generated = [
            path
            for path in tracked_files()
            if "__pycache__" in Path(path).parts or path.endswith((".pyc", ".pyo"))
        ]
        self.assertEqual(generated, [])

    def test_gitignore_preserves_supported_sources_and_authored_fixtures(self) -> None:
        visible_paths = (
            "widget.c",
            "widget.h",
            "widget.cc",
            "widget.cpp",
            "widget.so.cpp",
            "widget.cxx",
            "widget.hh",
            "widget.hpp",
            "widget.hxx",
            "build-support/main.cpp",
            "tests/fixtures/compile_commands.json",
            "tests/fixtures/build.ninja",
            "tests/fixtures/CMakeFiles/input.hpp",
            "build/main.cpp",
            "tests/fixtures/build/main.h",
            "out/main.cc",
            "tests/fixtures/out/main.hpp",
            "cmake-build-debug/main.cxx",
            "tests/fixtures/cmake-build-debug/main.hxx",
            "CMakeFiles/source.cpp",
            "tests/fixtures/CMakeFiles/source.cc",
            "build.ninja",
            "tests/fixtures/nested/build.ninja",
        )
        ignored = git_ignore_results(visible_paths)
        for path in visible_paths:
            with self.subTest(path=path):
                self.assertFalse(ignored[path])

    def test_gitignore_excludes_representative_generated_outputs(self) -> None:
        ignored_paths = (
            "module.o",
            "module.so",
            "coverage.gcda",
            "hooks/__pycache__/session_start.cpython-313.pyc",
            ".pytest_cache/state",
            ".coverage",
            "htmlcov/index.html",
            "compile_commands.json",
            "CMakeCache.txt",
            "cmake_install.cmake",
            ".ninja_deps",
            ".ninja_log",
        )
        ignored = git_ignore_results(ignored_paths)
        for path in ignored_paths:
            with self.subTest(path=path):
                self.assertTrue(ignored[path])

    def test_public_copy_states_non_certification_boundary(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8").lower()
        skill = (
            ROOT / "skills" / "using-reliable-cc" / "SKILL.md"
        ).read_text(encoding="utf-8").lower()
        normalized_readme = " ".join(readme.split())
        normalized_skill = " ".join(skill.split())

        boundaries = (
            (
                normalized_readme,
                "does not certify safety, correctness, security, compliance",
            ),
            (
                normalized_skill,
                "neither a clean result nor a suppression certifies safety, "
                "correctness, or compliance",
            ),
        )
        for public_copy, negative_boundary in boundaries:
            with self.subTest(copy=public_copy[:40]):
                self.assertIn(negative_boundary, public_copy)
                self.assertFalse(
                    contradicts_non_certification(public_copy, negative_boundary)
                )

    def test_certification_contract_rejects_contradictory_positive_claim(self) -> None:
        boundary = "the audit does not certify safety or correctness"
        contradictory_claims = (
            "the audit certifies safety",
            "the audit certifies correctness and compliance",
            "the audit provides security certification",
            "the audit certifies compliance",
            "the audit provides Power of Ten certification",
            "the audit certifies SOLID design",
            "the audit issues a safety certificate",
            "the audit provides Power-of-Ten certification",
        )

        for claim in contradictory_claims:
            with self.subTest(claim=claim):
                mutated_copy = boundary + ". However, " + claim + "."
                self.assertTrue(
                    contradicts_non_certification(mutated_copy, boundary)
                )


if __name__ == "__main__":
    unittest.main()
