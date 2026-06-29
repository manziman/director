"""Acceptance tests for language-neutral config example and planner template.

Spec: neutral-examples
  1. director/config.example.toml must round-trip through load_file with gates intact.
  2. config.example.toml must contain stack-neutral comment markers: a Python label,
     commented alternative stacks, a commented ignore key, and a commented [target] block.
  3. director/agent_templates/planner.md must have no .py extension in its JSON
     schema example path placeholders.

Run: python3 -m unittest tests.test_examples_neutral -v
"""

import pathlib
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from director.config import load_file  # noqa: E402

_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_EXAMPLE = _ROOT / "director" / "config.example.toml"
_PLANNER_MD = _ROOT / "director" / "agent_templates" / "planner.md"


class ConfigExampleRoundTripTest(unittest.TestCase):
    """config.example.toml must load successfully with gate commands preserved."""

    def setUp(self):
        self.cfg = load_file(_CONFIG_EXAMPLE)

    def test_load_file_succeeds(self):
        self.assertIsNotNone(self.cfg)

    def test_gate_test_command_unchanged(self):
        self.assertEqual(
            self.cfg.gates.get("test"),
            "pytest -q",
            "gates.test must remain 'pytest -q' after adding neutrality comments",
        )

    def test_gate_lint_command_unchanged(self):
        self.assertEqual(
            self.cfg.gates.get("lint"),
            "ruff check .",
            "gates.lint must remain 'ruff check .' after adding neutrality comments",
        )

    def test_gate_typecheck_command_unchanged(self):
        self.assertEqual(
            self.cfg.gates.get("typecheck"),
            "mypy .",
            "gates.typecheck must remain 'mypy .' after adding neutrality comments",
        )


class ConfigExampleNeutralityCommentsTest(unittest.TestCase):
    """config.example.toml must have comments marking Python and alternative stacks."""

    def setUp(self):
        self.text = _CONFIG_EXAMPLE.read_text(encoding="utf-8")

    def test_python_example_label_in_comment(self):
        """A comment near [gates] must mark the values as a Python example to replace."""
        # Spec: "add a leading comment marking them as an EXAMPLE for a Python project
        # that the user should REPLACE with their own stack's commands"
        self.assertIn(
            "python",
            self.text.lower(),
            "config.example.toml must contain a comment marking the [gates] values as a "
            "Python project example that users should replace (case-insensitive 'python')",
        )

    def test_go_alternative_commented(self):
        """A commented Go alternative must appear (e.g. 'go test ./...')."""
        self.assertIn(
            "go test",
            self.text,
            "config.example.toml must contain a commented Go test alternative: 'go test'",
        )

    def test_js_alternative_commented(self):
        """A commented JS alternative must appear (e.g. 'npm test' or 'eslint .')."""
        self.assertTrue(
            "npm test" in self.text or "eslint" in self.text,
            "config.example.toml must contain a commented JS/Node alternative "
            "('npm test' or 'eslint')",
        )

    def test_rust_alternative_commented(self):
        """A commented Rust alternative must appear (e.g. 'cargo test')."""
        self.assertIn(
            "cargo test",
            self.text,
            "config.example.toml must contain a commented Rust alternative: 'cargo test'",
        )

    def test_commented_ignore_key_documented(self):
        """A commented ignore key must document the optional gates.ignore list."""
        # Spec: "Add a commented line documenting the new optional list-valued key
        # inside [gates]: # ignore = [...]  # extra byproduct globs ..."
        self.assertIn(
            "# ignore",
            self.text,
            "config.example.toml must contain a commented '# ignore' line "
            "documenting the optional gates.ignore key",
        )

    def test_commented_target_block_present(self):
        """A commented [target] block must exist in the file."""
        # Spec: "Add a commented [target] example block documenting the optional
        # free-form keys (language, test_framework, toolchain)"
        self.assertIn(
            "# [target]",
            self.text,
            "config.example.toml must contain a commented '# [target]' example block",
        )

    def test_target_block_not_active(self):
        """The [target] block must remain commented — no active [target] section."""
        # Spec: "The [target] block MUST stay commented out so the file round-trips
        # through load_file unchanged."
        for line in self.text.splitlines():
            if line.strip() == "[target]":
                self.fail(
                    "Found an active '[target]' section in config.example.toml — "
                    "it must remain commented so load_file round-trips cleanly"
                )


class PlannerTemplateNeutralityTest(unittest.TestCase):
    """planner.md must have no .py extension in JSON schema example paths."""

    def setUp(self):
        self.text = _PLANNER_MD.read_text(encoding="utf-8")

    def test_planner_md_exists(self):
        self.assertTrue(_PLANNER_MD.exists(), f"missing {_PLANNER_MD}")

    def test_no_py_extension_in_json_example_paths(self):
        """The JSON schema example must not contain .py file extensions in path strings."""
        # Spec: "remove the .py suffix from the example edit-path and test-path
        # placeholders so no hardcoded Python filename remains"
        # The pattern .py" identifies a Python filename inside a JSON string value.
        self.assertNotIn(
            '.py"',
            self.text,
            "planner.md JSON schema example must not contain .py file extensions "
            "in path placeholder strings (pattern: 'some/path.py\"'); "
            "remove the .py suffix from the 'files' and 'tests' example paths",
        )

    def test_files_placeholder_has_no_py_suffix(self):
        """The 'files' example array must not contain a .py path."""
        match = re.search(r'"files"\s*:\s*\[([^\]]+)\]', self.text)
        if match:
            self.assertNotIn(
                ".py",
                match.group(1),
                "planner.md 'files' example array must not contain .py suffix in paths",
            )

    def test_tests_placeholder_has_no_py_suffix(self):
        """The 'tests' example array must not contain a .py path."""
        match = re.search(r'"tests"\s*:\s*\[([^\]]+)\]', self.text)
        if match:
            self.assertNotIn(
                ".py",
                match.group(1),
                "planner.md 'tests' example array must not contain .py suffix in paths",
            )

    def test_repo_stack_steer_preserved(self):
        """The steer to use 'this repo's stack (from the recon summary)' must remain."""
        # Spec: "Keep the existing steer to use 'this repo's stack (from the recon
        # summary)' intact."
        self.assertIn(
            "this repo",
            self.text,
            "planner.md must still contain the steer to use 'this repo's stack'",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
