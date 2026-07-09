"""Documentation contract for Pi tiers."""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class TestPiDocs(unittest.TestCase):
    def test_config_example_documents_pi_tiers_and_pricing(self):
        text = (ROOT / "director" / "config.example.toml").read_text()
        for expected in (
            "pi/<provider>/<model>",
            "pi/anthropic/claude-sonnet-4-5",
            '[pricing."pi/anthropic/claude-sonnet-4-5"]',
        ):
            self.assertIn(expected, text)

    def test_readme_documents_install_auth_and_manual_smoke(self):
        text = (ROOT / "README.md").read_text()
        for expected in (
            "npm install -g --ignore-scripts @mariozechner/pi-coding-agent",
            "BYOK",
            "Manual Pi smoke test",
            ".director/logs/",
        ):
            self.assertIn(expected, text)


if __name__ == "__main__":
    unittest.main()
