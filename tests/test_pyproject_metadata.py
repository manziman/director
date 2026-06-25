"""Acceptance tests for pyproject.toml metadata neutralization.

Asserts that pyproject.toml presents OpenCode and Claude Code as peer
runtimes rather than framing OpenCode as the sole runtime.
"""

import pathlib
import sys
import tomllib
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

PYPROJECT = pathlib.Path(__file__).resolve().parent.parent / "pyproject.toml"


def _load():
    with open(PYPROJECT, "rb") as f:
        return tomllib.load(f)


class TestDescriptionNeutrality(unittest.TestCase):
    def test_description_does_not_contain_thin_orchestrator_over_opencode(self):
        data = _load()
        desc = data["project"]["description"]
        self.assertNotIn(
            "a thin orchestrator over OpenCode",
            desc,
            msg=f"Description still frames OpenCode as the sole runtime: {desc!r}",
        )

    def test_description_mentions_claude_code(self):
        data = _load()
        desc = data["project"]["description"]
        self.assertIn(
            "Claude Code",
            desc,
            msg=(f"Description does not mention Claude Code as a peer runtime: {desc!r}"),
        )


class TestKeywordPeerRule(unittest.TestCase):
    def test_claude_code_keyword_present_alongside_opencode(self):
        data = _load()
        keywords = data["project"]["keywords"]
        if "opencode" in keywords:
            self.assertIn(
                "claude-code",
                keywords,
                msg=(
                    f"'opencode' is in keywords but 'claude-code' is absent; "
                    f"runtimes must be represented as peers. keywords={keywords!r}"
                ),
            )

    def test_opencode_keyword_still_present(self):
        data = _load()
        keywords = data["project"]["keywords"]
        self.assertIn(
            "opencode",
            keywords,
            msg=f"'opencode' keyword was removed; it should be kept. keywords={keywords!r}",
        )


if __name__ == "__main__":
    unittest.main()
