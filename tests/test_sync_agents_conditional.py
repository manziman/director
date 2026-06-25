"""Acceptance tests for setup-conditional-sync: sync_agents is config-aware.

sync_agents(repo, cfg=None) renders .opencode/ ONLY when an OpenCode-owned provider
appears in the tiers. With no cfg, no .director/config.toml, or claude-code-only
tiers it returns [] and leaves .opencode/ untouched. The .director/.gitignore is
always written. A malformed/incomplete on-disk config.toml must not raise.
A pre-existing .opencode/ tree is never deleted (D6).
"""

from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import unittest
from pathlib import Path

os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from director import setup
from director.config import ROLES, Config
from director.setup import AGENT_FILES

# Build TOML content using the actual ROLES so the files satisfy config.load_file.
_OPENCODE_TOML = (
    "[tiers]\n" + "\n".join(f'{r} = "anthropic/claude-3-5-sonnet"' for r in ROLES) + "\n"
)

_CLAUDECODE_TOML = "[tiers]\n" + "\n".join(f'{r} = "claude-code/sonnet"' for r in ROLES) + "\n"

# A TOML that is missing most required roles — load_file raises ValueError.
_INCOMPLETE_TOML = '[tiers]\nplanner = "anthropic/claude-3-5-sonnet"\n'

# A TOML that is syntactically invalid.
_MALFORMED_TOML = "this is not TOML }{\n"


def _cfg(tiers: dict) -> Config:
    return Config(
        path=Path("/dev/null"),
        tiers=tiers,
        gates={},
        pricing={},
        limits={},
        sampling={},
        local={},
        review={},
    )


def _opencode_cfg() -> Config:
    return _cfg(dict.fromkeys(ROLES, "anthropic/claude-3-5-sonnet"))


def _claudecode_cfg() -> Config:
    return _cfg(dict.fromkeys(ROLES, "claude-code/sonnet"))


class OpenCodeCfgTests(unittest.TestCase):
    """sync_agents with an OpenCode-tier Config creates the full .opencode/ tree."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fm-cond-oc-"))

    def test_creates_all_agent_markdown_files(self):
        setup.sync_agents(self.tmp, _opencode_cfg())
        agents_dir = self.tmp / ".opencode" / "agents"
        for name in AGENT_FILES:
            dest = agents_dir / name
            self.assertTrue(dest.is_file(), f"missing agent file: {name}")
            self.assertTrue(dest.read_text().strip(), f"empty agent file: {name}")

    def test_creates_opencode_json(self):
        setup.sync_agents(self.tmp, _opencode_cfg())
        oc = self.tmp / ".opencode" / "opencode.json"
        self.assertTrue(oc.is_file(), ".opencode/opencode.json was not created")

    def test_writes_director_gitignore(self):
        setup.sync_agents(self.tmp, _opencode_cfg())
        gi = self.tmp / ".director" / ".gitignore"
        self.assertTrue(gi.is_file(), ".director/.gitignore was not written")

    def test_returns_nonempty_list(self):
        written = setup.sync_agents(self.tmp, _opencode_cfg())
        self.assertIsInstance(written, list)
        self.assertGreater(len(written), 0, "expected non-empty list of written paths")

    def test_returned_paths_include_all_agents(self):
        written = setup.sync_agents(self.tmp, _opencode_cfg())
        for name in AGENT_FILES:
            self.assertIn(str(Path(".opencode") / "agents" / name), written)

    def test_returned_paths_include_opencode_json(self):
        written = setup.sync_agents(self.tmp, _opencode_cfg())
        self.assertIn(str(Path(".opencode") / "opencode.json"), written)

    def test_returned_paths_are_repo_relative(self):
        written = setup.sync_agents(self.tmp, _opencode_cfg())
        for p in written:
            self.assertFalse(Path(p).is_absolute(), f"expected relative path, got {p!r}")

    def test_existing_opencode_json_preserved_byte_for_byte(self):
        oc = self.tmp / ".opencode" / "opencode.json"
        oc.parent.mkdir(parents=True)
        oc.write_bytes(b"ORIGINAL_BYTES\n")
        setup.sync_agents(self.tmp, _opencode_cfg())
        self.assertEqual(oc.read_bytes(), b"ORIGINAL_BYTES\n", "opencode.json was overwritten")

    def test_existing_opencode_json_not_in_returned_paths(self):
        oc = self.tmp / ".opencode" / "opencode.json"
        oc.parent.mkdir(parents=True)
        oc.write_text("ORIGINAL\n")
        written = setup.sync_agents(self.tmp, _opencode_cfg())
        self.assertNotIn(str(Path(".opencode") / "opencode.json"), written)


class ClaudeCodeCfgTests(unittest.TestCase):
    """sync_agents with claude-code-only tiers writes nothing under .opencode/."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fm-cond-cc-"))

    def test_returns_empty_list(self):
        written = setup.sync_agents(self.tmp, _claudecode_cfg())
        self.assertEqual(written, [], f"expected [] for claude-code tiers, got {written!r}")

    def test_no_opencode_directory_created(self):
        setup.sync_agents(self.tmp, _claudecode_cfg())
        self.assertFalse(
            (self.tmp / ".opencode").exists(),
            ".opencode/ directory must not be created for claude-code tiers",
        )

    def test_writes_director_gitignore(self):
        setup.sync_agents(self.tmp, _claudecode_cfg())
        gi = self.tmp / ".director" / ".gitignore"
        self.assertTrue(
            gi.is_file(), ".director/.gitignore must be written even for claude-code tiers"
        )

    def test_preexisting_opencode_tree_not_deleted(self):
        # D6: existing .opencode/ must never be removed regardless of config
        sentinel = self.tmp / ".opencode" / "sentinel.txt"
        sentinel.parent.mkdir(parents=True)
        sentinel.write_text("keep me\n")
        setup.sync_agents(self.tmp, _claudecode_cfg())
        self.assertTrue(sentinel.exists(), ".opencode/ tree was deleted — violates D6")
        self.assertEqual(sentinel.read_text(), "keep me\n")


class NoCfgNoDiskTests(unittest.TestCase):
    """sync_agents(repo) with no cfg arg and no .director/config.toml writes nothing to .opencode/."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fm-cond-none-"))

    def test_returns_empty_list(self):
        written = setup.sync_agents(self.tmp)
        self.assertEqual(written, [], f"expected [] with no config, got {written!r}")

    def test_no_opencode_directory_created(self):
        setup.sync_agents(self.tmp)
        self.assertFalse(
            (self.tmp / ".opencode").exists(),
            ".opencode/ must not be created when no config is present",
        )

    def test_writes_director_gitignore(self):
        setup.sync_agents(self.tmp)
        gi = self.tmp / ".director" / ".gitignore"
        self.assertTrue(gi.is_file(), ".director/.gitignore must always be written")


class OnDiskConfigTests(unittest.TestCase):
    """sync_agents(repo) reads .director/config.toml when no cfg is passed."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fm-cond-disk-"))
        (self.tmp / ".director").mkdir(parents=True)

    def test_opencode_toml_triggers_full_sync(self):
        (self.tmp / ".director" / "config.toml").write_text(_OPENCODE_TOML)
        written = setup.sync_agents(self.tmp)
        agents_dir = self.tmp / ".opencode" / "agents"
        for name in AGENT_FILES:
            self.assertTrue((agents_dir / name).is_file(), f"missing agent: {name}")
        self.assertIn(str(Path(".opencode") / "opencode.json"), written)

    def test_opencode_toml_returns_nonempty_list(self):
        (self.tmp / ".director" / "config.toml").write_text(_OPENCODE_TOML)
        written = setup.sync_agents(self.tmp)
        self.assertGreater(len(written), 0)

    def test_claudecode_toml_returns_empty_list(self):
        (self.tmp / ".director" / "config.toml").write_text(_CLAUDECODE_TOML)
        written = setup.sync_agents(self.tmp)
        self.assertEqual(written, [])

    def test_claudecode_toml_no_opencode_dir(self):
        (self.tmp / ".director" / "config.toml").write_text(_CLAUDECODE_TOML)
        setup.sync_agents(self.tmp)
        self.assertFalse((self.tmp / ".opencode").exists())

    def test_malformed_toml_does_not_raise(self):
        (self.tmp / ".director" / "config.toml").write_text(_MALFORMED_TOML)
        try:
            written = setup.sync_agents(self.tmp)
        except Exception as exc:
            self.fail(f"sync_agents raised on malformed config.toml: {exc!r}")
        self.assertEqual(written, [])

    def test_malformed_toml_no_opencode_dir(self):
        (self.tmp / ".director" / "config.toml").write_text(_MALFORMED_TOML)
        setup.sync_agents(self.tmp)
        self.assertFalse((self.tmp / ".opencode").exists())

    def test_incomplete_toml_does_not_raise(self):
        # Missing roles cause load_file to raise ValueError — must be swallowed
        (self.tmp / ".director" / "config.toml").write_text(_INCOMPLETE_TOML)
        try:
            written = setup.sync_agents(self.tmp)
        except Exception as exc:
            self.fail(f"sync_agents raised on incomplete config.toml: {exc!r}")
        self.assertEqual(written, [])

    def test_incomplete_toml_no_opencode_dir(self):
        (self.tmp / ".director" / "config.toml").write_text(_INCOMPLETE_TOML)
        setup.sync_agents(self.tmp)
        self.assertFalse((self.tmp / ".opencode").exists())


class MixedTiersTests(unittest.TestCase):
    """A config mixing OpenCode and claude-code tiers IS selected for sync."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fm-cond-mixed-"))

    def test_mixed_providers_triggers_sync(self):
        roles = list(ROLES)
        tiers = {
            r: ("anthropic/claude-3-5-sonnet" if i % 2 == 0 else "claude-code/sonnet")
            for i, r in enumerate(roles)
        }
        written = setup.sync_agents(self.tmp, _cfg(tiers))
        self.assertGreater(len(written), 0, "mixed tiers (some OpenCode) must trigger sync")
        agents_dir = self.tmp / ".opencode" / "agents"
        self.assertTrue(agents_dir.exists())


class NewSignatureTests(unittest.TestCase):
    """sync_agents must accept the new cfg=None second parameter."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fm-cond-sig-"))

    def test_accepts_cfg_as_none_positional(self):
        try:
            setup.sync_agents(self.tmp, None)
        except TypeError as exc:
            self.fail(f"sync_agents raised TypeError with None positional: {exc!r}")

    def test_accepts_cfg_as_none_keyword(self):
        try:
            setup.sync_agents(self.tmp, cfg=None)
        except TypeError as exc:
            self.fail(f"sync_agents raised TypeError with cfg=None keyword: {exc!r}")

    def test_accepts_config_object(self):
        try:
            setup.sync_agents(self.tmp, cfg=_opencode_cfg())
        except TypeError as exc:
            self.fail(f"sync_agents raised TypeError with Config object: {exc!r}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
