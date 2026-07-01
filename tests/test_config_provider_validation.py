"""Acceptance tests for provider-name validation in director.config."""

import os
import pathlib
import sys
import tempfile
import unittest
from pathlib import Path

os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from director import config  # noqa: E402
from director.config import ROLES  # noqa: E402


def _toml_for(tiers: dict[str, str], pricing_key: str | None = None) -> str:
    lines = ["[tiers]"]
    for role in ROLES:
        lines.append(f'{role} = "{tiers[role]}"')
    if pricing_key is not None:
        lines.extend(["", f'[pricing."{pricing_key}"]', "input = 0.0", "output = 0.0"])
    return "\n".join(lines) + "\n"


def _write(text: str) -> Path:
    d = Path(tempfile.mkdtemp(prefix="cfg-provider-"))
    path = d / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


class ConfigProviderValidationTests(unittest.TestCase):
    def test_legacy_bare_opencode_subprovider_raises_clear_error(self):
        tiers = dict.fromkeys(ROLES, "opencode/anthropic/claude-opus-4-8")
        tiers["executor"] = "lmstudio/qwen3.6-27b-mtp"
        path = _write(_toml_for(tiers))

        with self.assertRaises(ValueError) as ctx:
            config.load_file(path)

        msg = str(ctx.exception)
        self.assertIn("unknown provider 'lmstudio'", msg)
        self.assertIn("tier 'lmstudio/qwen3.6-27b-mtp'", msg)
        self.assertIn("opencode/lmstudio/qwen3.6-27b-mtp", msg)
        self.assertIn("Known providers: claude-code, opencode", msg)

    def test_canonical_claude_code_and_opencode_tiers_load(self):
        tiers = {
            "planner": "claude-code/opus",
            "test_author": "claude-code/opus",
            "executor": "opencode/lmstudio/qwen3.6-27b-mtp",
            "explorer": "opencode/lmstudio/qwen3.6-27b-mtp",
            "reviewer": "claude-code/sonnet",
            "escalation": "claude-code/sonnet",
        }
        path = _write(_toml_for(tiers, pricing_key="opencode/lmstudio/qwen3.6-27b-mtp"))

        cfg = config.load_file(path)

        self.assertEqual(cfg.tiers, tiers)
        self.assertIn("opencode/lmstudio/qwen3.6-27b-mtp", cfg.pricing)

    def test_legacy_pricing_key_raises_clear_error(self):
        tiers = dict.fromkeys(ROLES, "opencode/anthropic/claude-opus-4-8")
        path = _write(_toml_for(tiers, pricing_key="amazon-bedrock/anthropic.claude-sonnet-4-6"))

        with self.assertRaises(ValueError) as ctx:
            config.load_file(path)

        msg = str(ctx.exception)
        self.assertIn("unknown provider 'amazon-bedrock'", msg)
        self.assertIn("pricing key 'amazon-bedrock/anthropic.claude-sonnet-4-6'", msg)
        self.assertIn("opencode/amazon-bedrock/anthropic.claude-sonnet-4-6", msg)


if __name__ == "__main__":
    unittest.main(verbosity=2)
