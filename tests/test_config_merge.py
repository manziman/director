"""Acceptance tests for the `config-two-level-merge` node.

`director.config` must support two-level configuration loading:

  * user-level:  ~/.director/config.toml   (resolved via ``Path.home()``)
  * repo-level:  <repo>/.director/config.toml

``load(repo)`` deep-merges the repo config OVER the user config (repo wins,
per sub-key, recursively). It exposes three new module-level helpers:

  * ``_user_config_path()``   -> Path.home() / '.director' / 'config.toml'
  * ``_deep_merge(base, override)`` -> new dict, recursive, non-mutating
  * ``_build_config(data, path)`` -> validate roles + construct Config

``load_file(path)`` must stay a single-file loader (no HOME lookup, no merge).

Run: python3 -m unittest tests.test_config_merge -v
"""

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

# --------------------------------------------------------------------------- #
# TOML fixtures
# --------------------------------------------------------------------------- #

# A complete, valid user-level config: every role bound, plus nested tables we
# expect the deep merge to descend into.
USER_TOML = """
[tiers]
planner     = "user/plan"
test_author = "user/ta"
executor    = "user/exec"
explorer    = "user/expl"
reviewer    = "user/rev"
escalation  = "user/esc"

[gates]
test      = "user-test-cmd"
lint      = "user-lint-cmd"
typecheck = "user-tc-cmd"

[limits]
node_timeout_secs = 100
cost_ceiling_usd  = 5.0
max_attempts      = 2

[providers.local]
base_url = "http://user-host:1234"
api_key  = "user-key"

[sampling.planner]
temperature = 0.1
top_p       = 0.9

[pricing."user/plan"]
input  = 1.0
output = 2.0
"""

# Repo-level config that overrides a SUBSET of keys inside several tables.
# Every table below is a partial override; the unspecified keys must fall back
# to the user values via deep merge.
REPO_OVERRIDE_TOML = """
[tiers]
executor = "repo/exec"

[gates]
lint = "repo-lint-cmd"

[limits]
max_attempts = 9

[providers.local]
api_key = "repo-key"

[sampling.planner]
top_p = 0.5
"""

# A complete, valid repo-only config (distinct values from USER_TOML) used for
# the "repo-only load is identical to today" regression.
REPO_FULL_TOML = """
[tiers]
planner     = "repo/plan"
test_author = "repo/ta"
executor    = "repo/exec"
explorer    = "repo/expl"
reviewer    = "repo/rev"
escalation  = "repo/esc"

[gates]
test = "repo-test-cmd"

[limits]
max_attempts = 4
"""


def _write(dir_path: Path, text: str) -> Path:
    """Write ``text`` to ``<dir_path>/.director/config.toml`` and return the path."""
    cfg_dir = dir_path / ".director"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    p = cfg_dir / "config.toml"
    p.write_text(text, encoding="utf-8")
    return p


class _HomeIsolationMixin:
    """Point HOME (and USERPROFILE) at an isolated, initially-empty temp dir so
    ``Path.home()`` resolves there. Each test that wants a user-level config
    writes one under ``self.home``; otherwise the user side is absent."""

    def setUp(self):
        super().setUp()
        self._saved_home = os.environ.get("HOME")
        self._saved_userprofile = os.environ.get("USERPROFILE")
        self.home = Path(tempfile.mkdtemp(prefix="cfgmerge-home-"))
        self.repo = Path(tempfile.mkdtemp(prefix="cfgmerge-repo-"))
        os.environ["HOME"] = str(self.home)
        os.environ["USERPROFILE"] = str(self.home)

    def tearDown(self):
        for key, val in (("HOME", self._saved_home), ("USERPROFILE", self._saved_userprofile)):
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val
        super().tearDown()


# --------------------------------------------------------------------------- #
# _user_config_path
# --------------------------------------------------------------------------- #
class UserConfigPathTests(_HomeIsolationMixin, unittest.TestCase):
    def test_resolves_under_home(self):
        expected = self.home / ".director" / "config.toml"
        self.assertEqual(config._user_config_path(), expected)


# --------------------------------------------------------------------------- #
# _deep_merge (pure function)
# --------------------------------------------------------------------------- #
class DeepMergeTests(unittest.TestCase):
    def test_returns_new_dict_not_base(self):
        base = {"a": 1}
        override = {"b": 2}
        result = config._deep_merge(base, override)
        self.assertIsNot(result, base)
        self.assertIsNot(result, override)
        self.assertEqual(result, {"a": 1, "b": 2})

    def test_does_not_mutate_inputs(self):
        base = {"t": {"x": 1, "y": 2}}
        override = {"t": {"y": 20, "z": 30}}
        config._deep_merge(base, override)
        self.assertEqual(base, {"t": {"x": 1, "y": 2}})
        self.assertEqual(override, {"t": {"y": 20, "z": 30}})

    def test_nested_tables_merge_recursively(self):
        base = {"t": {"x": 1, "y": 2}}
        override = {"t": {"y": 20, "z": 30}}
        result = config._deep_merge(base, override)
        self.assertEqual(result, {"t": {"x": 1, "y": 20, "z": 30}})

    def test_arbitrary_depth_recursion(self):
        base = {"a": {"b": {"c": 1, "keep": True}}}
        override = {"a": {"b": {"c": 2}}}
        result = config._deep_merge(base, override)
        self.assertEqual(result, {"a": {"b": {"c": 2, "keep": True}}})

    def test_scalar_replaces_wholesale(self):
        result = config._deep_merge({"k": 1}, {"k": 2})
        self.assertEqual(result, {"k": 2})

    def test_array_replaces_and_is_not_concatenated(self):
        result = config._deep_merge({"k": [1, 2, 3]}, {"k": [9]})
        self.assertEqual(result, {"k": [9]})

    def test_dict_replaced_by_non_dict(self):
        result = config._deep_merge({"k": {"nested": 1}}, {"k": "scalar"})
        self.assertEqual(result, {"k": "scalar"})

    def test_non_dict_replaced_by_dict(self):
        result = config._deep_merge({"k": "scalar"}, {"k": {"nested": 1}})
        self.assertEqual(result, {"k": {"nested": 1}})

    def test_keys_only_in_base_pass_through(self):
        result = config._deep_merge({"only_base": 1}, {"only_override": 2})
        self.assertEqual(result, {"only_base": 1, "only_override": 2})


# --------------------------------------------------------------------------- #
# load(): user-only present
# --------------------------------------------------------------------------- #
class LoadUserOnlyTests(_HomeIsolationMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        _write(self.home, USER_TOML)  # user present; repo absent

    def test_config_built_from_user_data(self):
        cfg = config.load(self.repo)
        self.assertEqual(cfg.tiers["planner"], "user/plan")
        self.assertEqual(cfg.tiers["executor"], "user/exec")
        self.assertEqual(cfg.gates["lint"], "user-lint-cmd")
        self.assertEqual(cfg.local["base_url"], "http://user-host:1234")
        self.assertEqual(cfg.limits["max_attempts"], 2)

    def test_active_path_is_user_path(self):
        cfg = config.load(self.repo)
        self.assertEqual(cfg.path, self.home / ".director" / "config.toml")


# --------------------------------------------------------------------------- #
# load(): repo-only present -> identical to today's single-file load
# --------------------------------------------------------------------------- #
class LoadRepoOnlyRegressionTests(_HomeIsolationMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.repo_cfg = _write(self.repo, REPO_FULL_TOML)  # repo present; user absent

    def test_identical_to_load_file(self):
        merged = config.load(self.repo)
        single = config.load_file(self.repo_cfg)
        self.assertEqual(merged, single)

    def test_active_path_is_repo_path(self):
        cfg = config.load(self.repo)
        self.assertEqual(cfg.path, self.repo_cfg)
        self.assertEqual(cfg.tiers["planner"], "repo/plan")


# --------------------------------------------------------------------------- #
# load(): both present -> repo deep-merged over user, per sub-key
# --------------------------------------------------------------------------- #
class LoadBothMergeTests(_HomeIsolationMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        _write(self.home, USER_TOML)
        self.repo_cfg = _write(self.repo, REPO_OVERRIDE_TOML)
        self.cfg = config.load(self.repo)

    def test_active_path_is_repo_path(self):
        self.assertEqual(self.cfg.path, self.repo_cfg)

    def test_tiers_override_and_fallback(self):
        self.assertEqual(self.cfg.tiers["executor"], "repo/exec")  # overridden
        self.assertEqual(self.cfg.tiers["planner"], "user/plan")  # fallback

    def test_gates_override_and_fallback(self):
        self.assertEqual(self.cfg.gates["lint"], "repo-lint-cmd")  # overridden
        self.assertEqual(self.cfg.gates["test"], "user-test-cmd")  # fallback
        self.assertEqual(self.cfg.gates["typecheck"], "user-tc-cmd")  # fallback

    def test_limits_override_and_fallback(self):
        self.assertEqual(self.cfg.limits["max_attempts"], 9)  # overridden
        self.assertEqual(self.cfg.limits["node_timeout_secs"], 100)  # fallback
        self.assertEqual(self.cfg.limits["cost_ceiling_usd"], 5.0)  # fallback

    def test_providers_local_override_and_fallback(self):
        self.assertEqual(self.cfg.local["api_key"], "repo-key")  # overridden
        self.assertEqual(self.cfg.local["base_url"], "http://user-host:1234")  # fallback

    def test_sampling_role_override_and_fallback(self):
        self.assertEqual(self.cfg.sampling["planner"]["top_p"], 0.5)  # overridden
        self.assertEqual(self.cfg.sampling["planner"]["temperature"], 0.1)  # fallback

    def test_user_only_table_passes_through(self):
        # pricing exists only in the user config; it must survive the merge.
        self.assertEqual(self.cfg.pricing["user/plan"], {"input": 1.0, "output": 2.0})


# --------------------------------------------------------------------------- #
# load(): merged tiers completeness
# --------------------------------------------------------------------------- #
class LoadMergedTiersCompletenessTests(_HomeIsolationMixin, unittest.TestCase):
    def test_valid_via_union_of_user_and_repo(self):
        # User binds all roles EXCEPT executor; repo supplies executor. The
        # UNION is complete, so the merged config must load without error.
        user_missing_executor = """
[tiers]
planner     = "user/plan"
test_author = "user/ta"
explorer    = "user/expl"
reviewer    = "user/rev"
escalation  = "user/esc"
"""
        repo_supplies_executor = """
[tiers]
executor = "repo/exec"
"""
        _write(self.home, user_missing_executor)
        _write(self.repo, repo_supplies_executor)
        cfg = config.load(self.repo)
        for role in ROLES:
            self.assertIn(role, cfg.tiers)
        self.assertEqual(cfg.tiers["executor"], "repo/exec")

    def test_merged_missing_role_raises_valueerror_naming_it(self):
        # Neither side binds "escalation" -> the merged tiers are incomplete.
        user_missing = """
[tiers]
planner     = "user/plan"
test_author = "user/ta"
executor    = "user/exec"
explorer    = "user/expl"
reviewer    = "user/rev"
"""
        repo_partial = """
[tiers]
executor = "repo/exec"
"""
        _write(self.home, user_missing)
        _write(self.repo, repo_partial)
        with self.assertRaises(ValueError) as ctx:
            config.load(self.repo)
        self.assertIn("escalation", str(ctx.exception))


# --------------------------------------------------------------------------- #
# load_file(): stays a single-file loader (no HOME, no merge)
# --------------------------------------------------------------------------- #
class LoadFileIgnoresUserConfigTests(_HomeIsolationMixin, unittest.TestCase):
    def test_load_file_does_not_merge_user_level_config(self):
        # A user-level config exists under HOME with a gates.lint entry...
        _write(self.home, USER_TOML)
        # ...but load_file() on a repo file that has NO gates.lint must not
        # pull it in. load_file reads exactly one file.
        repo_cfg = _write(self.repo, REPO_FULL_TOML)
        cfg = config.load_file(repo_cfg)
        self.assertEqual(cfg.tiers["planner"], "repo/plan")
        self.assertNotIn("lint", cfg.gates)  # user's lint not merged
        self.assertEqual(cfg.local, {})  # user's providers.local not merged
        self.assertNotIn("user/plan", cfg.pricing)  # user's pricing not merged


if __name__ == "__main__":
    unittest.main(verbosity=2)
