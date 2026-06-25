"""Acceptance tests for the runtime documentation in `config.example.toml`.

The new model: the provider segment of a `"<provider>/<model>"` tier string
selects a runtime via a **registry**.  The example file's comment block must
document this — including which providers map to which runtimes and what happens
when an unrecognized provider is used — and must NOT contain a live `[runtime]`
table (that design was removed).
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
        """The old [runtime] table design was removed; only the registry remains.
        No uncommented `[runtime]` header may appear, and the parsed TOML must
        have no `runtime` key at the top level."""
        for line in _read_example().splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            self.assertNotEqual(stripped, "[runtime]", "no live [runtime] table allowed")
        data = tomllib.loads(_read_example())
        self.assertNotIn("runtime", data)


class RegistryBasedResolutionTest(unittest.TestCase):
    """Comments must document the new registry-based provider→runtime lookup.

    Required by spec:
    • The provider segment selects a runtime via a registry (not an implicit default).
    • claude-code/<model>            → Claude Code CLI runtime.
    • anthropic/<model> and other OpenCode-owned providers
      (openai, google, lmstudio, amazon-bedrock, openrouter, …) → OpenCode.
    • Unrecognized/unregistered provider → configuration error surfaced as a
      FAILED run (not a silent fallback).
    """

    def setUp(self):
        self.comments = _comment_text(_read_example())
        self.comments_lower = self.comments.lower()

    # ------------------------------------------------------------------
    # Registry mechanism
    # ------------------------------------------------------------------

    def test_documents_registry_based_resolution(self):
        """Comments must mention 'registry' as the provider→runtime resolution mechanism."""
        self.assertIn(
            "registry",
            self.comments_lower,
            "comments must mention 'registry' to describe the provider→runtime lookup",
        )

    # ------------------------------------------------------------------
    # Documented routes
    # ------------------------------------------------------------------

    def test_documents_anthropic_provider_routes_to_opencode(self):
        """Comments must explicitly show anthropic/<model> as an OpenCode-routed provider."""
        self.assertIn(
            "anthropic/",
            self.comments,
            "comments must document 'anthropic/<model>' as routing to OpenCode",
        )

    def test_documents_openai_and_google_as_opencode_providers(self):
        """Comments must list both openai and google as OpenCode-routed providers.

        Note: the existing 'OpenAI-compatible endpoint' phrase does NOT satisfy this
        — openai must appear in the provider routing docs alongside google, which is
        currently absent entirely.
        """
        lower = self.comments_lower
        # google is entirely absent from the current file; requiring it guarantees
        # we're testing the provider routing docs, not a false-positive substring match
        for provider in ("openai", "google"):
            self.assertIn(
                provider,
                lower,
                f"comments must list '{provider}' as an OpenCode-routed provider",
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
        """Comments must say an unknown provider produces a FAILED run, not a silent fallback."""
        lower = self.comments_lower
        mentions_failure = "failed" in lower or "fail" in lower
        mentions_not_silent = "silent" in lower or "fallback" in lower
        self.assertTrue(
            mentions_failure and mentions_not_silent,
            "comments must state an unknown provider causes a FAILED run "
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
