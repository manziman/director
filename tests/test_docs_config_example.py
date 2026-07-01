"""Acceptance tests for the two-level-config doc header in config.example.toml.

Node: docs-config-example. The file must carry a top-of-file comment block that
documents the two-level configuration model in prose:

  - user-level defaults live at ``~/.director/config.toml``;
  - repo-local overrides live at ``<repo>/.director/config.toml``;
  - the repo file overrides the user file via a recursive per-sub-key deep merge
    (tables merge recursively; scalars and arrays replace wholesale — arrays are
    NOT concatenated);
  - ``bench``/``setup`` profiles loaded via an explicit file path are single-file
    and do NOT participate in the merge.

All added doc lines must be valid TOML comments (each starts with ``#``). These
are deterministic plain-substring checks against the file text.
"""

import pathlib
import unittest

_EXAMPLE = pathlib.Path(__file__).resolve().parents[1] / "director" / "config.example.toml"


def _text():
    return _EXAMPLE.read_text()


def _comment_blob():
    """Concatenation of only the comment lines (lines whose lstrip starts with '#')."""
    return "\n".join(ln for ln in _text().splitlines() if ln.lstrip().startswith("#"))


class TwoLevelLookupDocumentedTests(unittest.TestCase):
    def setUp(self):
        self.text = _text()
        self.comments = _comment_blob()

    def test_file_exists(self):
        self.assertTrue(_EXAMPLE.exists(), f"{_EXAMPLE} must exist")

    def test_user_level_path_present(self):
        self.assertIn(
            "~/.director/config.toml",
            self.text,
            "Header must document the user-level defaults path '~/.director/config.toml'.",
        )

    def test_user_level_path_is_a_comment(self):
        self.assertIn(
            "~/.director/config.toml",
            self.comments,
            "'~/.director/config.toml' must appear inside a TOML comment (# ...).",
        )

    def test_repo_local_path_present(self):
        self.assertIn(
            "<repo>/.director/config.toml",
            self.text,
            "Header must document the repo-local overrides path '<repo>/.director/config.toml'.",
        )

    def test_repo_local_path_is_a_comment(self):
        self.assertIn(
            "<repo>/.director/config.toml",
            self.comments,
            "'<repo>/.director/config.toml' must appear inside a TOML comment (# ...).",
        )

    def test_override_notion_present(self):
        self.assertIn(
            "override",
            self.comments.lower(),
            "Header comments must state the repo file overrides the user file ('override').",
        )

    def test_merge_notion_present(self):
        self.assertIn(
            "merge",
            self.comments.lower(),
            "Header comments must describe the deep merge ('merge').",
        )

    def test_arrays_replace_not_concatenate_present(self):
        self.assertIn(
            "arrays",
            self.comments.lower(),
            "Header comments must call out array handling ('arrays' replace, not concatenate).",
        )

    def test_bench_profiles_single_file_present(self):
        blob = self.comments.lower()
        self.assertIn(
            "bench",
            blob,
            "Header comments must mention path-loaded 'bench' profiles.",
        )
        self.assertIn(
            "single",
            blob,
            "Header comments must state path-loaded profiles are 'single'-file and skip the merge.",
        )

    def test_all_documented_tokens_live_in_comments(self):
        """Every required literal path token must sit on a '#' comment line."""
        for token in ("~/.director/config.toml", "<repo>/.director/config.toml"):
            self.assertIn(
                token,
                self.comments,
                f"{token!r} must be documented as a TOML comment, not bare TOML.",
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
