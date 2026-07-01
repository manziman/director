"""Acceptance tests for the `setup-conditional-sync` node.

sync_agents(repo, cfg=None) is now config-aware: it renders .opencode/ only when
an OpenCode-owned provider appears in the tiers. Tests that previously called the
bare sync_agents(repo) and asserted .opencode/ exists have been migrated to pass an
OpenCode-tier Config. The .director/.gitignore is always written regardless of config.

Pins retained from the previous node (setup-remove-seeding):
- sync_agents never seeds .director/config.toml.
- Dead code (_example_config, CONFIG_EXAMPLE) remains absent.
- The module docstring still mentions 'agent' and omits 'config.toml' / 'ready-to-edit'.
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


def _opencode_cfg() -> Config:
    """Construct a minimal Config with all roles bound to an OpenCode-owned provider."""
    return Config(
        path=Path("/dev/null"),
        tiers=dict.fromkeys(ROLES, "opencode/anthropic/claude-3-5-sonnet"),
        gates={},
        pricing={},
        limits={},
        sampling={},
        review={},
    )


class SyncAgentsNoSeedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fm-setup-noseed-"))

    def test_renders_every_agent_markdown(self):
        setup.sync_agents(self.tmp, _opencode_cfg())
        agents_dir = self.tmp / ".opencode" / "agents"
        for name in AGENT_FILES:
            dest = agents_dir / name
            self.assertTrue(dest.is_file(), f"missing rendered agent: {name}")
            self.assertTrue(dest.read_text().strip(), f"empty agent file: {name}")

    def test_writes_starter_opencode_json(self):
        setup.sync_agents(self.tmp, _opencode_cfg())
        oc = self.tmp / ".opencode" / "opencode.json"
        self.assertTrue(oc.is_file(), "starter opencode.json was not written")

    def test_writes_director_gitignore(self):
        # .director/.gitignore is ALWAYS written regardless of config
        setup.sync_agents(self.tmp)
        gi = self.tmp / ".director" / ".gitignore"
        self.assertTrue(gi.is_file(), ".director/.gitignore was not written")

    def test_does_not_write_config_toml(self):
        # Even with an OpenCode config, sync_agents must never seed config.toml
        setup.sync_agents(self.tmp, _opencode_cfg())
        cfg = self.tmp / ".director" / "config.toml"
        self.assertFalse(cfg.exists(), ".director/config.toml must NOT be seeded")

    def test_returned_paths_do_not_reference_config_toml(self):
        written = setup.sync_agents(self.tmp, _opencode_cfg())
        self.assertFalse(
            any("config.toml" in p for p in written),
            f"returned paths must not include config.toml; got {written!r}",
        )

    def test_returned_paths_include_agents_and_opencode_json(self):
        written = setup.sync_agents(self.tmp, _opencode_cfg())
        for name in AGENT_FILES:
            self.assertIn(str(Path(".opencode") / "agents" / name), written)
        self.assertIn(str(Path(".opencode") / "opencode.json"), written)

    def test_returned_paths_are_repo_relative(self):
        written = setup.sync_agents(self.tmp, _opencode_cfg())
        for p in written:
            self.assertFalse(Path(p).is_absolute(), f"expected repo-relative path, got {p!r}")

    def test_existing_opencode_json_not_clobbered(self):
        oc = self.tmp / ".opencode" / "opencode.json"
        oc.parent.mkdir(parents=True)
        oc.write_text("ORIGINAL\n")
        written = setup.sync_agents(self.tmp, _opencode_cfg())
        self.assertEqual(oc.read_text(), "ORIGINAL\n")
        self.assertNotIn(str(Path(".opencode") / "opencode.json"), written)

    def test_idempotent_still_no_config_toml(self):
        setup.sync_agents(self.tmp, _opencode_cfg())
        setup.sync_agents(self.tmp, _opencode_cfg())
        cfg = self.tmp / ".director" / "config.toml"
        self.assertFalse(cfg.exists(), ".director/config.toml must NOT appear on rerun")

    def test_accepts_str_repo_path(self):
        setup.sync_agents(str(self.tmp), _opencode_cfg())
        cfg = self.tmp / ".director" / "config.toml"
        self.assertFalse(cfg.exists())
        self.assertTrue((self.tmp / ".opencode" / "agents" / AGENT_FILES[0]).is_file())


class DeadCodeRemovedTests(unittest.TestCase):
    def test_config_example_constant_removed(self):
        self.assertFalse(
            hasattr(setup, "CONFIG_EXAMPLE"),
            "CONFIG_EXAMPLE module constant must be deleted",
        )

    def test_example_config_helper_removed(self):
        self.assertFalse(
            hasattr(setup, "_example_config"),
            "_example_config() helper must be deleted",
        )

    def test_template_helper_still_present(self):
        # _template stays; it is what renders the agent files.
        self.assertTrue(hasattr(setup, "_template"))


class DocstringTests(unittest.TestCase):
    def test_docstring_no_longer_advertises_config_seeding(self):
        doc = setup.__doc__ or ""
        self.assertNotIn("config.toml", doc)
        self.assertNotIn("ready-to-edit", doc)

    def test_docstring_still_describes_agent_rendering(self):
        doc = setup.__doc__ or ""
        self.assertIn("agent", doc.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
