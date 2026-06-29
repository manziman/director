"""Acceptance tests for the optional [target] declaration on Config.

These tests exercise the `target` field and the three convenience properties
(`target_language`, `target_test_framework`, `target_toolchain`) on
`director.config.Config`, loaded via `load_file()` from a temporary TOML file.

Run: python3 -m unittest tests.test_config_target -v
"""

import os
import pathlib
import sys
import tempfile
import unittest
from pathlib import Path

os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from director.config import load_file  # noqa: E402

# Minimal valid TOML that satisfies the roles validation in load_file().
_MINIMAL_TIERS = """
[tiers]
planner       = "cloud/plan"
test_author   = "cloud/plan"
executor      = "cloud/exec"
explorer      = "cloud/exec"
reviewer      = "cloud/rev"
escalation    = "cloud/esc"
"""


def _write_toml(tmp: str, extra: str = "") -> Path:
    """Write a minimal config.toml (with optional extra TOML) to *tmp*."""
    p = Path(tmp) / "config.toml"
    p.write_text(_MINIMAL_TIERS + extra, encoding="utf-8")
    return p


class TestTargetAbsent(unittest.TestCase):
    """Config loaded from TOML with NO [target] table."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="dir-cfg-target-")
        self._cfg = load_file(_write_toml(self._tmp))

    def test_target_field_defaults_to_empty_dict(self):
        self.assertEqual(self._cfg.target, {})

    def test_target_language_is_none(self):
        self.assertIsNone(self._cfg.target_language)

    def test_target_test_framework_is_none(self):
        self.assertIsNone(self._cfg.target_test_framework)

    def test_target_toolchain_is_none(self):
        self.assertIsNone(self._cfg.target_toolchain)


class TestTargetFullyPopulated(unittest.TestCase):
    """Config loaded from TOML with a fully-populated [target] table."""

    def setUp(self):
        extra = """
[target]
language       = "rust"
test_framework = "cargo"
toolchain      = "cargo"
"""
        self._tmp = tempfile.mkdtemp(prefix="dir-cfg-target-full-")
        self._cfg = load_file(_write_toml(self._tmp, extra))

    def test_target_field_contains_all_keys(self):
        self.assertEqual(self._cfg.target.get("language"), "rust")
        self.assertEqual(self._cfg.target.get("test_framework"), "cargo")
        self.assertEqual(self._cfg.target.get("toolchain"), "cargo")

    def test_target_language_returns_value(self):
        self.assertEqual(self._cfg.target_language, "rust")

    def test_target_test_framework_returns_value(self):
        self.assertEqual(self._cfg.target_test_framework, "cargo")

    def test_target_toolchain_returns_value(self):
        self.assertEqual(self._cfg.target_toolchain, "cargo")


class TestTargetPartiallyPopulated(unittest.TestCase):
    """Config loaded from TOML with only `language` set in [target]."""

    def setUp(self):
        extra = """
[target]
language = "python"
"""
        self._tmp = tempfile.mkdtemp(prefix="dir-cfg-target-partial-")
        self._cfg = load_file(_write_toml(self._tmp, extra))

    def test_target_language_returns_value(self):
        self.assertEqual(self._cfg.target_language, "python")

    def test_target_test_framework_is_none_when_unset(self):
        self.assertIsNone(self._cfg.target_test_framework)

    def test_target_toolchain_is_none_when_unset(self):
        self.assertIsNone(self._cfg.target_toolchain)


class TestTargetFreeFormKeys(unittest.TestCase):
    """[target] may contain arbitrary string keys beyond the three named ones."""

    def setUp(self):
        extra = """
[target]
language       = "go"
test_framework = "gotest"
toolchain      = "go"
formatter      = "gofmt"
linter         = "golangci-lint"
"""
        self._tmp = tempfile.mkdtemp(prefix="dir-cfg-target-freeform-")
        self._cfg = load_file(_write_toml(self._tmp, extra))

    def test_named_properties_work_alongside_extra_keys(self):
        self.assertEqual(self._cfg.target_language, "go")
        self.assertEqual(self._cfg.target_test_framework, "gotest")
        self.assertEqual(self._cfg.target_toolchain, "go")

    def test_extra_keys_visible_on_target_dict(self):
        self.assertEqual(self._cfg.target.get("formatter"), "gofmt")
        self.assertEqual(self._cfg.target.get("linter"), "golangci-lint")


class TestTargetDoesNotBreakExistingConstruction(unittest.TestCase):
    """The `target` field must have a default so existing code is unaffected."""

    def test_direct_config_construction_without_target_still_works(self):
        from director.config import Config

        # Construct Config with only the fields that existed before this node.
        # If `target` has no default this will raise TypeError.
        cfg = Config(
            path=Path("/fake/config.toml"),
            tiers={
                "planner": "cloud/plan",
                "test_author": "cloud/plan",
                "executor": "cloud/exec",
                "explorer": "cloud/exec",
                "reviewer": "cloud/rev",
                "escalation": "cloud/esc",
            },
            gates={},
            pricing={},
            limits={},
            sampling={},
            local={},
            review={},
        )
        self.assertEqual(cfg.target, {})
        self.assertIsNone(cfg.target_language)
        self.assertIsNone(cfg.target_test_framework)
        self.assertIsNone(cfg.target_toolchain)


if __name__ == "__main__":
    unittest.main(verbosity=2)
