"""Acceptance tests for claude-code/* pricing entries in config.example.toml.

Verifies:
  - All three claude-code/* pricing tables exist with numeric input/output values.
  - The file's prose comments explicitly document that unlisted models cost $0.
"""

import pathlib
import sys
import tomllib
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

_EXAMPLE = pathlib.Path(__file__).resolve().parent.parent / "director" / "config.example.toml"

_CLAUDE_CODE_MODELS = [
    "claude-code/opus",
    "claude-code/sonnet",
    "claude-code/haiku",
]


def _load():
    with open(_EXAMPLE, "rb") as f:
        return tomllib.load(f)


def _text():
    return _EXAMPLE.read_text()


class ClaudeCodePricingTablesExistTests(unittest.TestCase):
    """Each claude-code/* key must appear under [pricing] with numeric values."""

    def setUp(self):
        self.data = _load()
        self.pricing = self.data.get("pricing", {})

    def test_pricing_section_present(self):
        self.assertIn("pricing", self.data, "config.example.toml must have a [pricing] section")

    def test_claude_code_opus_key_exists(self):
        self.assertIn(
            "claude-code/opus",
            self.pricing,
            "pricing table must contain a 'claude-code/opus' entry",
        )

    def test_claude_code_sonnet_key_exists(self):
        self.assertIn(
            "claude-code/sonnet",
            self.pricing,
            "pricing table must contain a 'claude-code/sonnet' entry",
        )

    def test_claude_code_haiku_key_exists(self):
        self.assertIn(
            "claude-code/haiku",
            self.pricing,
            "pricing table must contain a 'claude-code/haiku' entry",
        )

    def _assert_numeric_fields(self, key):
        entry = self.pricing[key]
        self.assertIn("input", entry, f"pricing['{key}'] must have an 'input' field")
        self.assertIn("output", entry, f"pricing['{key}'] must have an 'output' field")
        self.assertIsInstance(
            entry["input"],
            (int, float),
            f"pricing['{key}'].input must be numeric, got {type(entry['input'])}",
        )
        self.assertIsInstance(
            entry["output"],
            (int, float),
            f"pricing['{key}'].output must be numeric, got {type(entry['output'])}",
        )

    def test_claude_code_opus_input_is_numeric(self):
        self.assertIn("claude-code/opus", self.pricing)
        self._assert_numeric_fields("claude-code/opus")

    def test_claude_code_sonnet_input_is_numeric(self):
        self.assertIn("claude-code/sonnet", self.pricing)
        self._assert_numeric_fields("claude-code/sonnet")

    def test_claude_code_haiku_input_is_numeric(self):
        self.assertIn("claude-code/haiku", self.pricing)
        self._assert_numeric_fields("claude-code/haiku")

    def test_all_claude_code_values_are_positive(self):
        for key in _CLAUDE_CODE_MODELS:
            if key not in self.pricing:
                self.skipTest(f"key {key!r} not yet present")
            entry = self.pricing[key]
            for field in ("input", "output"):
                if field in entry:
                    self.assertGreater(
                        entry[field],
                        0,
                        f"pricing['{key}'].{field} should be a positive illustrative number",
                    )


class UnlistedModelsPricedAtZeroDocumentedTests(unittest.TestCase):
    """The file's comments must explicitly state that unlisted models cost $0."""

    def setUp(self):
        self.text = _text()

    def _comment_lines(self):
        return [ln.lstrip() for ln in self.text.splitlines() if ln.lstrip().startswith("#")]

    def test_dollar_zero_mentioned_in_comments(self):
        comment_blob = "\n".join(self._comment_lines()).lower()
        self.assertTrue(
            "$0" in comment_blob or "zero" in comment_blob,
            "A comment must state that unlisted models are priced at $0 (or 'zero'). "
            f"Comment lines found:\n{''.join(self._comment_lines())}",
        )

    def test_unlisted_models_zero_cost_prose_present(self):
        comment_blob = "\n".join(self._comment_lines()).lower()
        # Must mention both "unlisted" (or "not listed") and the zero-cost consequence.
        has_unlisted = "not listed" in comment_blob or "unlisted" in comment_blob
        has_zero = "$0" in comment_blob or "zero" in comment_blob
        self.assertTrue(
            has_unlisted and has_zero,
            "Comments must explicitly say that any model NOT listed is counted at $0. "
            f"has_unlisted={has_unlisted}, has_zero={has_zero}. "
            f"Comment lines:\n{''.join(self._comment_lines())}",
        )

    def test_provider_model_key_format_documented_in_comments(self):
        comment_blob = "\n".join(self._comment_lines()).lower()
        # Must document the "<provider>/<model>" key format used in [tiers].
        self.assertTrue(
            "provider" in comment_blob and ("tier" in comment_blob or "tiers" in comment_blob),
            "Comments must explain that pricing keys use the same '<provider>/<model>' "
            "tier string as [tiers]. "
            f"Comment lines:\n{''.join(self._comment_lines())}",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
