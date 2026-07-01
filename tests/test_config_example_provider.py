"""Acceptance tests for provider-tier documentation in `config.example.toml`."""

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


def _comment_text(raw: str) -> str:
    """Return a single string containing all comment-line bodies (# stripped)."""
    lines = []
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            lines.append(stripped.lstrip("#").strip())
    return "\n".join(lines)


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
        """The old [runtime] table design must stay removed."""
        for line in _read_example().splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            self.assertNotEqual(stripped, "[runtime]", "no live [runtime] table allowed")
        data = tomllib.loads(_read_example())
        self.assertNotIn("runtime", data)


class RegistryBasedResolutionTest(unittest.TestCase):
    """Comments must document the provider-as-tool tier format."""

    def setUp(self):
        self.comments = _comment_text(_read_example())
        self.comments_lower = self.comments.lower()

    # ------------------------------------------------------------------
    # Provider mechanism
    # ------------------------------------------------------------------

    def test_documents_first_segment_is_tool(self):
        self.assertIn(
            "first segment",
            self.comments_lower,
            "comments must state that the first segment selects the tool/provider",
        )
        self.assertIn("tool", self.comments_lower)

    # ------------------------------------------------------------------
    # Documented routes
    # ------------------------------------------------------------------

    def test_documents_opencode_anthropic_tier(self):
        """Comments must show OpenCode sub-provider refs under opencode/."""
        self.assertIn(
            "opencode/anthropic/",
            self.comments,
            "comments must document 'opencode/anthropic/<model>' tiers",
        )

    def test_documents_lmstudio_and_bedrock_under_opencode(self):
        lower = self.comments_lower
        for tier in ("opencode/lmstudio/", "opencode/amazon-bedrock/"):
            self.assertIn(
                tier,
                lower,
                f"comments must show {tier!r} as an OpenCode tier prefix",
            )

    # ------------------------------------------------------------------
    # Error on unknown provider
    # ------------------------------------------------------------------

    def test_documents_unrecognized_provider_is_configuration_error(self):
        """Comments must state that an unrecognized provider is a configuration error."""
        lower = self.comments_lower
        has_error_concept = (
            "configuration error" in lower
            or ("unrecognized" in lower and "error" in lower)
            or ("unregistered" in lower and "error" in lower)
        )
        self.assertTrue(
            has_error_concept,
            "comments must state that an unrecognized/unregistered provider is a "
            "configuration error (not a silent fallback)",
        )

    def test_documents_no_silent_fallback_for_unknown_provider(self):
        """Comments must say an unknown provider fails fast, not a silent fallback."""
        lower = self.comments_lower
        mentions_failure = "failed" in lower or "fail" in lower
        mentions_not_silent = "silent" in lower or "fallback" in lower
        self.assertTrue(
            mentions_failure and mentions_not_silent,
            "comments must state an unknown provider causes a failure "
            "(not a silent fallback); need both failure + 'silent'/'fallback' language",
        )

    # ------------------------------------------------------------------
    # Old framing must be gone
    # ------------------------------------------------------------------

    def test_no_implicit_opencode_default_framing(self):
        """The old '→ OpenCode (default)' framing must be removed.

        That text implied unrecognized providers silently fell through to OpenCode.
        The registry model replaces this with an explicit error on unknown providers.
        """
        self.assertNotIn(
            "OpenCode (default)",
            self.comments,
            "the implicit 'OpenCode (default)' fallback framing must be removed",
        )


if __name__ == "__main__":
    unittest.main()
