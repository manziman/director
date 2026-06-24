"""Acceptance tests for the `config-runtime-field` node.

`director.config.Config` gains a required `runtime: dict` field (role -> backend
string) and a `backend_for(role)` resolver. `load_file` parses the `[runtime]`
TOML table and passes it to the `Config(...)` constructor.

Semantics of `backend_for` (per spec):
  - absent role  -> "opencode"  (default)
  - present role -> returns the stored value VERBATIM (no coercion here;
    coercion is the responsibility of `set_runtime`, a different module)

The `[runtime]` TOML table is optional; a config file with no `[runtime]` table
must yield `runtime == {}` and `backend_for(<anything>) == "opencode"`.
"""

import os
import pathlib
import sys
import tempfile
import unittest
from pathlib import Path

os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from director import config as config_mod
from director.config import Config, load_file


def _minimal_toml() -> str:
    """A valid config.toml body with every [tiers] role bound and no [runtime]."""
    return (
        "[tiers]\n"
        'planner = "p/m1"\n'
        'test_author = "p/m1"\n'
        'executor = "p/m1"\n'
        'explorer = "p/m1"\n'
        'reviewer = "p/m1"\n'
        'escalation = "p/m1"\n'
    )


def _write_config(tmp: Path, body: str) -> Path:
    cfg_dir = tmp / ".director"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    path = cfg_dir / "config.toml"
    path.write_text(body)
    return path


def _make_config(runtime: dict) -> Config:
    """Construct a Config directly (no TOML needed) with the given runtime dict."""
    return Config(
        path=Path("<test>"),
        tiers=dict.fromkeys(config_mod.ROLES, "p/m1"),
        gates={},
        pricing={},
        limits={},
        sampling={},
        local={},
        review={},
        runtime=runtime,
    )


# ---------------------------------------------------------------------------
# 1. Config dataclass has a `runtime` field
# ---------------------------------------------------------------------------


class RuntimeFieldExistsTests(unittest.TestCase):
    """The `runtime` field must exist on Config and be required (no default)."""

    def test_runtime_field_accepted_in_constructor(self):
        """Config can be constructed with runtime={}."""
        cfg = _make_config({})
        self.assertEqual(cfg.runtime, {})

    def test_runtime_field_stores_mapping(self):
        """Config stores the runtime dict verbatim."""
        rt = {"planner": "claude-code", "executor": "opencode"}
        cfg = _make_config(rt)
        self.assertEqual(cfg.runtime, rt)

    def test_constructor_defaults_runtime_to_empty(self):
        """Omitting `runtime=` is allowed and defaults to {} — adding the field
        must NOT break existing Config(...) callers (backward-compatible)."""
        cfg = Config(
            path=Path("<test>"),
            tiers=dict.fromkeys(config_mod.ROLES, "p/m1"),
            gates={},
            pricing={},
            limits={},
            sampling={},
            local={},
            review={},
            # runtime intentionally omitted → defaults to {}
        )
        self.assertEqual(cfg.runtime, {})

    def test_runtime_field_is_after_review_in_dataclass(self):
        """runtime must be declared after review (field ordering check via
        dataclass __dataclass_fields__)."""
        import dataclasses

        fields = [f.name for f in dataclasses.fields(Config)]
        self.assertIn("runtime", fields)
        self.assertIn("review", fields)
        self.assertGreater(fields.index("runtime"), fields.index("review"))


# ---------------------------------------------------------------------------
# 2. backend_for resolver — happy path
# ---------------------------------------------------------------------------


class BackendForDefaultTests(unittest.TestCase):
    """backend_for returns "opencode" for any role absent from runtime."""

    def test_empty_runtime_returns_opencode_for_any_role(self):
        cfg = _make_config({})
        self.assertEqual(cfg.backend_for("planner"), "opencode")
        self.assertEqual(cfg.backend_for("executor"), "opencode")
        self.assertEqual(cfg.backend_for("reviewer"), "opencode")
        self.assertEqual(cfg.backend_for("test_author"), "opencode")
        self.assertEqual(cfg.backend_for("explorer"), "opencode")
        self.assertEqual(cfg.backend_for("escalation"), "opencode")

    def test_absent_role_returns_opencode(self):
        """A role not in the runtime dict defaults to "opencode"."""
        cfg = _make_config({"planner": "claude-code"})
        self.assertEqual(cfg.backend_for("executor"), "opencode")

    def test_unknown_role_name_returns_opencode(self):
        """A completely unknown role name also defaults to "opencode"."""
        cfg = _make_config({})
        self.assertEqual(cfg.backend_for("not-a-real-role"), "opencode")

    def test_return_type_is_str(self):
        cfg = _make_config({})
        result = cfg.backend_for("planner")
        self.assertIsInstance(result, str)


class BackendForVerbatimReturnTests(unittest.TestCase):
    """backend_for returns the stored value VERBATIM — no coercion."""

    def test_opencode_value_returned_verbatim(self):
        cfg = _make_config({"planner": "opencode"})
        self.assertEqual(cfg.backend_for("planner"), "opencode")

    def test_claude_code_value_returned_verbatim(self):
        cfg = _make_config({"planner": "claude-code"})
        self.assertEqual(cfg.backend_for("planner"), "claude-code")

    def test_unknown_backend_value_returned_verbatim(self):
        """backend_for does NOT coerce unknown values — returns them as-is."""
        cfg = _make_config({"planner": "bogus-backend"})
        self.assertEqual(cfg.backend_for("planner"), "bogus-backend")

    def test_arbitrary_string_returned_verbatim(self):
        cfg = _make_config({"executor": "my-custom-backend"})
        self.assertEqual(cfg.backend_for("executor"), "my-custom-backend")

    def test_mixed_roles_each_returned_verbatim(self):
        cfg = _make_config(
            {
                "planner": "claude-code",
                "executor": "opencode",
                "reviewer": "some-other-backend",
            }
        )
        self.assertEqual(cfg.backend_for("planner"), "claude-code")
        self.assertEqual(cfg.backend_for("executor"), "opencode")
        self.assertEqual(cfg.backend_for("reviewer"), "some-other-backend")

    def test_absent_role_still_defaults_when_others_present(self):
        cfg = _make_config({"planner": "claude-code"})
        # explorer is not in runtime -> default
        self.assertEqual(cfg.backend_for("explorer"), "opencode")


# ---------------------------------------------------------------------------
# 3. load_file populates runtime from [runtime] TOML table
# ---------------------------------------------------------------------------


class LoadFileRuntimeTests(unittest.TestCase):
    """`load_file` parses [runtime] and feeds it into Config."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cfg-runtime-"))

    def test_no_runtime_table_yields_empty_dict(self):
        """A config file with no [runtime] table must yield runtime == {}."""
        path = _write_config(self.tmp, _minimal_toml())
        cfg = load_file(path)
        self.assertEqual(cfg.runtime, {})

    def test_no_runtime_table_backend_for_returns_opencode(self):
        """With no [runtime] table, backend_for returns "opencode" for all roles."""
        path = _write_config(self.tmp, _minimal_toml())
        cfg = load_file(path)
        for role in config_mod.ROLES:
            self.assertEqual(
                cfg.backend_for(role), "opencode", f"expected 'opencode' for role {role!r}"
            )

    def test_runtime_table_with_claude_code(self):
        """[runtime] planner = "claude-code" is parsed and returned verbatim."""
        body = _minimal_toml() + ('\n[runtime]\nplanner = "claude-code"\n')
        path = _write_config(self.tmp, body)
        cfg = load_file(path)
        self.assertEqual(cfg.backend_for("planner"), "claude-code")

    def test_runtime_table_other_roles_still_default(self):
        """Roles not in [runtime] still default to "opencode"."""
        body = _minimal_toml() + ('\n[runtime]\nplanner = "claude-code"\n')
        path = _write_config(self.tmp, body)
        cfg = load_file(path)
        self.assertEqual(cfg.backend_for("executor"), "opencode")
        self.assertEqual(cfg.backend_for("reviewer"), "opencode")

    def test_runtime_table_verbatim_unknown_value(self):
        """load_file stores unknown backend values verbatim (no coercion)."""
        body = _minimal_toml() + ('\n[runtime]\nplanner = "bogus"\n')
        path = _write_config(self.tmp, body)
        cfg = load_file(path)
        # backend_for returns verbatim — "bogus", not "opencode"
        self.assertEqual(cfg.backend_for("planner"), "bogus")

    def test_runtime_table_multiple_roles(self):
        """Multiple entries in [runtime] are all parsed."""
        body = _minimal_toml() + (
            "\n[runtime]\n"
            'planner = "claude-code"\n'
            'executor = "opencode"\n'
            'reviewer = "claude-code"\n'
        )
        path = _write_config(self.tmp, body)
        cfg = load_file(path)
        self.assertEqual(cfg.backend_for("planner"), "claude-code")
        self.assertEqual(cfg.backend_for("executor"), "opencode")
        self.assertEqual(cfg.backend_for("reviewer"), "claude-code")
        # Untouched role defaults.
        self.assertEqual(cfg.backend_for("explorer"), "opencode")

    def test_empty_runtime_table(self):
        """An empty [runtime] table yields runtime == {}."""
        body = _minimal_toml() + "\n[runtime]\n"
        path = _write_config(self.tmp, body)
        cfg = load_file(path)
        self.assertEqual(cfg.runtime, {})
        self.assertEqual(cfg.backend_for("planner"), "opencode")

    def test_runtime_dict_is_accessible_directly(self):
        """cfg.runtime is the raw dict from the TOML [runtime] table."""
        body = _minimal_toml() + ('\n[runtime]\nexecutor = "claude-code"\n')
        path = _write_config(self.tmp, body)
        cfg = load_file(path)
        self.assertIsInstance(cfg.runtime, dict)
        self.assertEqual(cfg.runtime.get("executor"), "claude-code")


# ---------------------------------------------------------------------------
# 4. Existing behaviour is not broken
# ---------------------------------------------------------------------------


class ExistingBehaviourUnchangedTests(unittest.TestCase):
    """Adding runtime must not break existing Config fields or validation."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cfg-runtime-compat-"))

    def test_missing_tier_role_still_raises_valueerror(self):
        """[tiers] validation is unchanged."""
        body = (
            '[tiers]\nplanner = "p/m1"\n'
            # omit the other required roles
        )
        path = _write_config(self.tmp, body)
        with self.assertRaises(ValueError):
            load_file(path)

    def test_existing_fields_still_present(self):
        """All pre-existing Config fields remain accessible."""
        path = _write_config(self.tmp, _minimal_toml())
        cfg = load_file(path)
        # These must not raise AttributeError.
        _ = cfg.tiers
        _ = cfg.gates
        _ = cfg.pricing
        _ = cfg.limits
        _ = cfg.sampling
        _ = cfg.local
        _ = cfg.review
        _ = cfg.runtime  # the new field

    def test_model_for_still_works(self):
        """model_for resolver is unaffected."""
        path = _write_config(self.tmp, _minimal_toml())
        cfg = load_file(path)
        self.assertEqual(cfg.model_for("planner"), "p/m1")

    def test_roles_tuple_unchanged(self):
        """ROLES tuple must not be modified."""
        expected = ("planner", "test_author", "executor", "explorer", "reviewer", "escalation")
        self.assertEqual(config_mod.ROLES, expected)

    def test_mk_config_helper_in_test_director_still_works(self):
        """A Config built without runtime= (like test_director.py's mk_config)
        must still construct and behave as all-opencode by default."""
        cfg = Config(
            path=Path("<test>"),
            tiers=dict.fromkeys(config_mod.ROLES, "p/m1"),
            gates={},
            pricing={},
            limits={},
            sampling={},
            local={},
            review={},
            # runtime omitted → defaults to {}
        )
        self.assertEqual(cfg.runtime, {})
        self.assertEqual(cfg.backend_for("planner"), "opencode")


if __name__ == "__main__":
    unittest.main(verbosity=2)
