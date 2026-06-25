"""Acceptance tests for the runtime documentation in `config.example.toml`.

Runtime selection is a property of the tier model string: a `claude-code/<model>`
prefix routes to the Claude Code CLI; anything else is an OpenCode provider. The
example file must document this convention in the `[tiers]` section and must NOT
contain a live (uncommented) `[runtime]` table (that design was removed).
"""

import os
import pathlib
import sys
import tomllib
import unittest
from pathlib import Path

os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

_EXAMPLE_PATH = Path(__file__).resolve().parent.parent / "director" / "config.example.toml"


def _read_example() -> str:
    return _EXAMPLE_PATH.read_text(encoding="utf-8")


class ExampleFileTest(unittest.TestCase):
    def test_example_file_exists(self):
        self.assertTrue(_EXAMPLE_PATH.exists(), f"missing {_EXAMPLE_PATH}")

    def test_file_parses_as_valid_toml(self):
        try:
            tomllib.loads(_read_example())
        except tomllib.TOMLDecodeError as exc:
            self.fail(f"config.example.toml is not valid TOML: {exc}")


class RuntimeConventionDocumentedTest(unittest.TestCase):
    def test_documents_claude_code_prefix(self):
        raw = _read_example()
        self.assertIn("claude-code/", raw, "the claude-code/ prefix convention must be documented")

    def test_no_live_runtime_table(self):
        """The old [runtime] table design was removed; only the prefix remains.
        No uncommented `[runtime]` header may appear, and the parsed TOML must
        have no `runtime` table."""
        for line in _read_example().splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            self.assertNotEqual(stripped, "[runtime]", "no live [runtime] table allowed")
        data = tomllib.loads(_read_example())
        self.assertNotIn("runtime", data)


if __name__ == "__main__":
    unittest.main()
