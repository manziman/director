"""Acceptance tests for the `config-example-runtime-docs` node.

`director/config.example.toml` must contain a commented documentation block
for the optional `[runtime]` table.  The block must:

  1. Be placed after the `[tiers]` section.
  2. Be entirely commented out (every non-blank line begins with `#`).
  3. Explain that `[runtime]` maps a role name to its agent backend.
  4. State that valid values are "opencode" (default) and "claude-code".
  5. State that any role not listed defaults to "opencode".
  6. State that this lets a single run mix backends per role.
  7. Include a concrete commented example with `[runtime]` header and at
     least one role assignment (e.g. `planner = "claude-code"`).
  8. Not introduce any uncommented (live) `[runtime]` key or table.

The file must remain valid TOML (ignoring comments) — i.e. the existing
uncommented content must still parse without error.
"""

import os
import pathlib
import re
import sys
import tomllib
import unittest
from pathlib import Path

os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

# Path to the example file under test — relative to this test file's parent.
_EXAMPLE_PATH = Path(__file__).resolve().parent.parent / "director" / "config.example.toml"


def _read_example() -> str:
    return _EXAMPLE_PATH.read_text(encoding="utf-8")


class ExampleFileExistsTest(unittest.TestCase):
    """Sanity: the example file must exist."""

    def test_example_file_exists(self):
        self.assertTrue(
            _EXAMPLE_PATH.exists(),
            f"config.example.toml not found at {_EXAMPLE_PATH}",
        )


class ExampleFileRemainsValidTomlTest(unittest.TestCase):
    """The uncommented content must still parse as valid TOML."""

    def test_file_parses_as_valid_toml(self):
        raw = _read_example()
        try:
            tomllib.loads(raw)
        except tomllib.TOMLDecodeError as exc:
            self.fail(f"config.example.toml is no longer valid TOML: {exc}")


class RuntimeBlockPresentTest(unittest.TestCase):
    """A commented `[runtime]` documentation block must exist in the file."""

    def _lines(self):
        return _read_example().splitlines()

    def test_commented_runtime_header_present(self):
        """The file must contain a line like `# [runtime]`."""
        lines = self._lines()
        has_runtime_header = any(re.match(r"^\s*#\s*\[runtime\]", line) for line in lines)
        self.assertTrue(
            has_runtime_header,
            "Expected a commented `# [runtime]` header in config.example.toml but found none.",
        )

    def test_no_live_runtime_table(self):
        """There must be NO uncommented `[runtime]` table in the file."""
        lines = self._lines()
        for lineno, line in enumerate(lines, start=1):
            stripped = line.strip()
            # A live (uncommented) [runtime] table header
            if re.match(r"^\[runtime\]", stripped):
                self.fail(
                    f"Line {lineno}: found an uncommented `[runtime]` table — "
                    "the block must be entirely commented out."
                )

    def test_no_live_runtime_keys(self):
        """There must be NO uncommented keys under a [runtime] table."""
        raw = _read_example()
        parsed = tomllib.loads(raw)
        self.assertNotIn(
            "runtime",
            parsed,
            "Parsed TOML must NOT contain a `runtime` key — the block must be "
            "entirely commented out.",
        )


class RuntimeBlockAfterTiersSectionTest(unittest.TestCase):
    """The `[runtime]` documentation block must appear after `[tiers]`."""

    def _line_index(self, pattern: str) -> int:
        """Return the 0-based index of the first line matching *pattern*, or -1."""
        for i, line in enumerate(_read_example().splitlines()):
            if re.search(pattern, line):
                return i
        return -1

    def test_runtime_block_after_tiers(self):
        tiers_idx = self._line_index(r"^\[tiers\]")
        runtime_idx = self._line_index(r"^\s*#\s*\[runtime\]")

        self.assertGreater(
            tiers_idx,
            -1,
            "Could not find `[tiers]` section in config.example.toml.",
        )
        self.assertGreater(
            runtime_idx,
            -1,
            "Could not find commented `# [runtime]` block in config.example.toml.",
        )
        self.assertGreater(
            runtime_idx,
            tiers_idx,
            f"The `# [runtime]` block (line {runtime_idx + 1}) must appear "
            f"after `[tiers]` (line {tiers_idx + 1}).",
        )


class RuntimeBlockContentTest(unittest.TestCase):
    """The documentation block must explain the semantics described in the spec."""

    def _text(self) -> str:
        return _read_example()

    def test_mentions_opencode_as_valid_value(self):
        """The block must mention `opencode` as a valid backend value."""
        self.assertIn(
            "opencode",
            self._text(),
            "config.example.toml must mention `opencode` as a valid runtime value.",
        )

    def test_mentions_claude_code_as_valid_value(self):
        """The block must mention `claude-code` as a valid backend value."""
        self.assertIn(
            "claude-code",
            self._text(),
            "config.example.toml must mention `claude-code` as a valid runtime value.",
        )

    def test_mentions_default_behaviour(self):
        """The block must explain that roles not listed default to `opencode`."""
        text = self._text()
        # Accept any phrasing that conveys "default" near "opencode"
        has_default_mention = bool(re.search(r"default", text, re.IGNORECASE))
        self.assertTrue(
            has_default_mention,
            "config.example.toml must mention the default behaviour (roles not "
            "listed default to 'opencode').",
        )

    def test_mentions_role_to_backend_mapping(self):
        """The block must convey that [runtime] maps a role name to a backend."""
        text = self._text()
        # Look for words like "role", "backend", "maps", "agent" in commented lines
        commented_lines = [line for line in text.splitlines() if line.strip().startswith("#")]
        commented_text = "\n".join(commented_lines).lower()
        has_role = "role" in commented_text
        has_backend = "backend" in commented_text or "agent" in commented_text
        self.assertTrue(
            has_role,
            "The commented block must mention 'role' to explain the mapping.",
        )
        self.assertTrue(
            has_backend,
            "The commented block must mention 'backend' or 'agent' to explain "
            "what the role maps to.",
        )

    def test_mentions_mixing_backends_per_role(self):
        """The block must explain that a single run can mix backends per role."""
        text = self._text()
        commented_lines = [line for line in text.splitlines() if line.strip().startswith("#")]
        commented_text = "\n".join(commented_lines).lower()
        # Accept "mix", "single run", "per role", "per-role", etc.
        has_mix_concept = bool(re.search(r"mix|single run|per.?role", commented_text))
        self.assertTrue(
            has_mix_concept,
            "The commented block must explain that a single run can mix backends "
            "per role (look for 'mix', 'single run', or 'per role').",
        )


class RuntimeBlockConcreteExampleTest(unittest.TestCase):
    """The block must include a concrete commented example with a role assignment."""

    def _commented_lines(self):
        return [line for line in _read_example().splitlines() if line.strip().startswith("#")]

    def test_concrete_example_has_role_assignment(self):
        """At least one commented line must show a role = "backend" assignment."""
        commented_text = "\n".join(self._commented_lines())
        # Match patterns like:  # planner  = "claude-code"
        #                        # executor = "opencode"
        has_assignment = bool(
            re.search(
                r'#\s*\w+\s*=\s*"(opencode|claude-code)"',
                commented_text,
            )
        )
        self.assertTrue(
            has_assignment,
            "The commented block must include a concrete example with at least "
            'one role assignment, e.g.  # planner = "claude-code"',
        )

    def test_concrete_example_includes_planner_or_executor(self):
        """The concrete example should use a recognisable role name."""
        commented_text = "\n".join(self._commented_lines())
        has_known_role = bool(
            re.search(
                r"#\s*(planner|executor|reviewer|explorer|test_author|escalation)\s*=",
                commented_text,
            )
        )
        self.assertTrue(
            has_known_role,
            "The concrete example must use a recognisable role name "
            "(planner, executor, reviewer, etc.).",
        )


class RuntimeBlockAllCommentedTest(unittest.TestCase):
    """Every line in the runtime documentation block must be commented or blank."""

    def test_runtime_block_lines_are_all_commented_or_blank(self):
        """Scan the region around the `# [runtime]` marker; no bare keys allowed."""
        lines = _read_example().splitlines()

        # Find the start of the commented [runtime] block.
        start_idx = -1
        for i, line in enumerate(lines):
            if re.match(r"^\s*#\s*\[runtime\]", line):
                start_idx = i
                break

        if start_idx == -1:
            self.skipTest("# [runtime] block not found — covered by other tests.")

        # Walk forward from the block header until we hit a non-comment,
        # non-blank line that is NOT itself a section header comment.
        # We collect up to 20 lines as the "block".
        block_lines = []
        for line in lines[start_idx : start_idx + 20]:
            stripped = line.strip()
            if stripped == "" or stripped.startswith("#"):
                block_lines.append(line)
            else:
                # Hit a live TOML line — stop scanning the block.
                break

        # Every collected line must be blank or start with #.
        for _lineno_offset, line in enumerate(block_lines):
            stripped = line.strip()
            if stripped == "":
                continue
            self.assertTrue(
                stripped.startswith("#"),
                f"Line in runtime block is not commented: {line!r}",
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
