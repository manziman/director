"""Acceptance tests for the two-level-config docs in README.md.

Node: docs-readme. The Configuration section of the top-level ``README.md`` must
document the two-level configuration model in prose:

  - user-level defaults live at ``~/.director/config.toml`` and repo-local
    overrides live at ``<repo>/.director/config.toml``;
  - the repo file deep-merges over the user file (nested tables merge recursively;
    scalars and arrays replace wholesale), illustrated with a small ``[tiers]``
    example;
  - ``director init`` auto-detects its write target, with ``--user`` / ``--local``
    forcing the user-level / repo-level target respectively.

These are deterministic plain-substring checks against the file text.
"""

import pathlib
import unittest

_README = pathlib.Path(__file__).resolve().parents[1] / "README.md"


def _text():
    return _README.read_text()


class TwoLevelConfigDocumentedTests(unittest.TestCase):
    def setUp(self):
        self.text = _text()

    def test_readme_exists(self):
        self.assertTrue(_README.exists(), f"{_README} must exist")

    def test_user_level_path_present(self):
        self.assertIn(
            "~/.director/config.toml",
            self.text,
            "README must document the user-level defaults path "
            "'~/.director/config.toml'.",
        )

    def test_repo_local_path_present(self):
        self.assertIn(
            "<repo>/.director/config.toml",
            self.text,
            "README must document the repo-local overrides path "
            "'<repo>/.director/config.toml'.",
        )

    def test_tiers_example_present(self):
        self.assertIn(
            "[tiers]",
            self.text,
            "README must illustrate the deep merge with a '[tiers]' example.",
        )

    def test_user_flag_present(self):
        self.assertIn(
            "--user",
            self.text,
            "README must document the '--user' flag forcing the user-level target.",
        )

    def test_local_flag_present(self):
        self.assertIn(
            "--local",
            self.text,
            "README must document the '--local' flag forcing the repo-level target.",
        )

    def test_director_init_mentioned(self):
        self.assertIn(
            "director init",
            self.text,
            "README must describe 'director init' target auto-detection.",
        )

    def test_all_required_tokens_present(self):
        for token in (
            "~/.director/config.toml",
            "<repo>/.director/config.toml",
            "[tiers]",
            "--user",
            "--local",
            "director init",
        ):
            self.assertIn(
                token,
                self.text,
                f"Required token {token!r} missing from README.md.",
            )


if __name__ == "__main__":
    unittest.main()
