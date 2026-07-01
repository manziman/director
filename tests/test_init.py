"""Acceptance tests for `director/init.py` (the `director init` flow).

These pin the contract for the module: a pure model-list parser, a
registry-driven model discovery that deduplicates tier strings in stable
(registration) order, interactive prompt helpers (driven here by
monkeypatched `input`/`print`), a pure TOML renderer that must round-trip
through `director.config.load_file`, and the `run_init` orchestrator.

The obsolete DiscoverModelsTests class that mocked init.subprocess has been
removed; that subprocess behavior is tested against OpenCodeRuntime directly.
"""

import builtins
import io
import os
import pathlib
import sys
import tempfile
import tomllib
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import director.runtime as _rt
from director import config, init
from director.config import ROLES


def scripted_input(answers):
    """Return an input() replacement that yields `answers` in order.

    Captures every prompt string passed to input() into `.prompts`.
    Raises AssertionError if input() is called more times than scripted, which
    keeps an accidentally-infinite re-prompt loop from hanging the suite.
    """
    it = iter(answers)
    prompts = []

    def fake_input(prompt=""):
        prompts.append(prompt)
        try:
            return next(it)
        except StopIteration as exc:  # pragma: no cover - defensive
            raise AssertionError(
                f"input() asked for more than the {len(answers)} scripted answers; "
                f"prompts so far: {prompts!r}"
            ) from exc

    fake_input.prompts = prompts
    return fake_input


# --------------------------------------------------------------------------- #
class ParseModelsTests(unittest.TestCase):
    def test_basic_two_models_trailing_newline(self):
        out = init.parse_models("anthropic/claude-opus-4\nopenai/gpt-4o\n")
        self.assertEqual(out, ["anthropic/claude-opus-4", "openai/gpt-4o"])

    def test_skips_header_blank_and_dedupes(self):
        text = (
            "Available models:\nanthropic/claude-opus-4\n\nanthropic/claude-opus-4\nopenai/gpt-4o"
        )
        out = init.parse_models(text)
        self.assertEqual(out, ["anthropic/claude-opus-4", "openai/gpt-4o"])

    def test_strips_surrounding_whitespace(self):
        out = init.parse_models("   anthropic/claude-opus-4   \n\topenai/gpt-4o\t\n")
        self.assertEqual(out, ["anthropic/claude-opus-4", "openai/gpt-4o"])

    def test_lines_without_slash_are_dropped(self):
        out = init.parse_models("header\nnoslash\njust text\n")
        self.assertEqual(out, [])

    def test_empty_input_returns_empty_list(self):
        self.assertEqual(init.parse_models(""), [])

    def test_preserves_first_seen_order_on_dedupe(self):
        text = "b/2\na/1\nb/2\nc/3\na/1\n"
        self.assertEqual(init.parse_models(text), ["b/2", "a/1", "c/3"])


# --------------------------------------------------------------------------- #
class DiscoverModelsRegistryTests(unittest.TestCase):
    """Registry-union semantics for discover_models().

    discover_models() must iterate registered runtimes in registration order,
    collect each runtime's discover_models() tier strings, deduplicate by
    exact string (first occurrence wins) without alphabetical re-sorting,
    and return the resulting list.
    """

    def setUp(self):
        # Snapshot and clear the global registry so tests are isolated.
        self._saved = dict(_rt._REGISTRY)
        _rt._REGISTRY.clear()

    def tearDown(self):
        _rt._REGISTRY.clear()
        _rt._REGISTRY.update(self._saved)

    @staticmethod
    def _fake_rt(name, providers, tiers):
        """Build a minimal runtime stub whose discover_models() returns `tiers`."""

        class _Rt:
            pass

        _Rt.name = name
        _Rt.providers = frozenset(providers)
        _returns = list(tiers)

        def discover_models(self_):
            return list(_returns)

        def run(self_, *, agent, model, message, cwd, log_path, timeout):
            return _rt.RunResult(returncode=0)

        def system_prompt_for(self_, agent):
            return None

        _Rt.discover_models = discover_models
        _Rt.run = run
        _Rt.system_prompt_for = system_prompt_for
        return _Rt()

    # -- base cases -----------------------------------------------------------

    def test_empty_registry_returns_empty_list(self):
        self.assertEqual(init.discover_models(), [])

    def test_returns_list_type(self):
        rt = self._fake_rt("rt", ["p"], ["p/a"])
        _rt.register(rt)
        self.assertIsInstance(init.discover_models(), list)

    # -- single runtime -------------------------------------------------------

    def test_single_runtime_returns_its_tiers(self):
        rt = self._fake_rt("r1", ["prov1"], ["prov1/alpha", "prov1/beta"])
        _rt.register(rt)
        self.assertEqual(init.discover_models(), ["prov1/alpha", "prov1/beta"])

    def test_single_runtime_empty_discover_returns_empty(self):
        rt = self._fake_rt("r1", ["p1"], [])
        _rt.register(rt)
        self.assertEqual(init.discover_models(), [])

    # -- union across runtimes ------------------------------------------------

    def test_two_runtimes_unioned_in_registration_order(self):
        r1 = self._fake_rt("r1", ["p1"], ["p1/a", "p1/b"])
        r2 = self._fake_rt("r2", ["p2"], ["p2/c"])
        _rt.register(r1)
        _rt.register(r2)
        self.assertEqual(init.discover_models(), ["p1/a", "p1/b", "p2/c"])

    def test_three_runtimes_merged_in_registration_order(self):
        r1 = self._fake_rt("r1", ["p1"], ["p1/x"])
        r2 = self._fake_rt("r2", ["p2"], ["p2/y"])
        r3 = self._fake_rt("r3", ["p3"], ["p3/z"])
        _rt.register(r1)
        _rt.register(r2)
        _rt.register(r3)
        self.assertEqual(init.discover_models(), ["p1/x", "p2/y", "p3/z"])

    def test_first_runtime_empty_second_runtime_present(self):
        r1 = self._fake_rt("r1", ["p1"], [])
        r2 = self._fake_rt("r2", ["p2"], ["p2/model"])
        _rt.register(r1)
        _rt.register(r2)
        self.assertEqual(init.discover_models(), ["p2/model"])

    def test_all_runtimes_empty_returns_empty(self):
        r1 = self._fake_rt("r1", ["p1"], [])
        r2 = self._fake_rt("r2", ["p2"], [])
        _rt.register(r1)
        _rt.register(r2)
        self.assertEqual(init.discover_models(), [])

    # -- deduplication --------------------------------------------------------

    def test_dedup_across_runtimes_keeps_first_occurrence(self):
        r1 = self._fake_rt("r1", ["p1"], ["shared/x", "p1/only"])
        r2 = self._fake_rt("r2", ["p2"], ["shared/x", "p2/only"])
        _rt.register(r1)
        _rt.register(r2)
        result = init.discover_models()
        self.assertEqual(result.count("shared/x"), 1)
        # The first runtime's "shared/x" must appear before r2's unique models.
        self.assertLess(result.index("shared/x"), result.index("p2/only"))

    def test_dedup_within_single_runtime(self):
        rt = self._fake_rt("rt", ["p"], ["p/a", "p/b", "p/a", "p/c"])
        _rt.register(rt)
        self.assertEqual(init.discover_models(), ["p/a", "p/b", "p/c"])

    def test_full_dedup_across_three_runtimes(self):
        r1 = self._fake_rt("r1", ["p1"], ["common/m", "p1/only"])
        r2 = self._fake_rt("r2", ["p2"], ["common/m", "p2/only"])
        r3 = self._fake_rt("r3", ["p3"], ["common/m", "p3/only"])
        _rt.register(r1)
        _rt.register(r2)
        _rt.register(r3)
        result = init.discover_models()
        self.assertEqual(result.count("common/m"), 1)
        self.assertIn("p1/only", result)
        self.assertIn("p2/only", result)
        self.assertIn("p3/only", result)

    # -- stable (non-alphabetical) ordering -----------------------------------

    def test_order_within_runtime_is_not_sorted(self):
        # z/m/a must stay z/m/a, not be re-sorted to a/m/z.
        rt = self._fake_rt("rt", ["p"], ["p/z-model", "p/m-model", "p/a-model"])
        _rt.register(rt)
        self.assertEqual(init.discover_models(), ["p/z-model", "p/m-model", "p/a-model"])

    def test_registration_order_beats_alphabetical_name_order(self):
        # z-prov registers first; its models must precede a-prov's regardless
        # of alphabetical ordering of provider names.
        rz = self._fake_rt("z-rt", ["z-prov"], ["z-prov/model"])
        ra = self._fake_rt("a-rt", ["a-prov"], ["a-prov/model"])
        _rt.register(rz)
        _rt.register(ra)
        self.assertEqual(init.discover_models(), ["z-prov/model", "a-prov/model"])


# --------------------------------------------------------------------------- #
class PromptModelMenuTests(unittest.TestCase):
    MODELS = ["anthropic/claude-opus-4", "openai/gpt-4o", "local/exec"]

    def _run(self, answers):
        fi = scripted_input(answers)
        buf = io.StringIO()
        with mock.patch.object(builtins, "input", fi), redirect_stdout(buf):
            result = init.prompt_model("planner", self.MODELS)
        return result, buf.getvalue(), fi

    def test_valid_numeric_selection_returns_model(self):
        result, out, _ = self._run(["2"])
        self.assertEqual(result, "openai/gpt-4o")

    def test_first_selection_one_indexed(self):
        result, _, _ = self._run(["1"])
        self.assertEqual(result, "anthropic/claude-opus-4")

    def test_last_selection(self):
        result, _, _ = self._run(["3"])
        self.assertEqual(result, "local/exec")

    def test_menu_lists_every_model_numbered(self):
        _, out, _ = self._run(["1"])
        self.assertIn("1)", out)
        self.assertIn("anthropic/claude-opus-4", out)
        self.assertIn("2)", out)
        self.assertIn("openai/gpt-4o", out)
        self.assertIn("3)", out)
        self.assertIn("local/exec", out)

    def test_role_name_appears_in_prompt(self):
        _, out, fi = self._run(["1"])
        blob = out + "".join(fi.prompts)
        self.assertIn("planner", blob)

    def test_empty_input_reprompts_then_accepts(self):
        result, _, fi = self._run(["", "2"])
        self.assertEqual(result, "openai/gpt-4o")
        self.assertGreaterEqual(len(fi.prompts), 2)

    def test_out_of_range_high_reprompts(self):
        result, _, fi = self._run(["9", "1"])
        self.assertEqual(result, "anthropic/claude-opus-4")
        self.assertGreaterEqual(len(fi.prompts), 2)

    def test_zero_is_invalid_and_reprompts(self):
        result, _, fi = self._run(["0", "1"])
        self.assertEqual(result, "anthropic/claude-opus-4")
        self.assertGreaterEqual(len(fi.prompts), 2)

    def test_non_integer_reprompts(self):
        result, _, fi = self._run(["abc", "2"])
        self.assertEqual(result, "openai/gpt-4o")
        self.assertGreaterEqual(len(fi.prompts), 2)

    def test_whitespace_padded_number_is_accepted(self):
        result, _, _ = self._run(["  2  "])
        self.assertEqual(result, "openai/gpt-4o")


# --------------------------------------------------------------------------- #
class PromptModelFreeTextTests(unittest.TestCase):
    def _run(self, answers):
        fi = scripted_input(answers)
        buf = io.StringIO()
        with mock.patch.object(builtins, "input", fi), redirect_stdout(buf):
            result = init.prompt_model("executor", [])
        return result, buf.getvalue(), fi

    def test_returns_nonempty_freetext_verbatim(self):
        result, _, _ = self._run(["provider/custom-model"])
        self.assertEqual(result, "provider/custom-model")

    def test_strips_surrounding_whitespace(self):
        result, _, _ = self._run(["   provider/x   "])
        self.assertEqual(result, "provider/x")

    def test_empty_reprompts_until_nonempty(self):
        result, _, fi = self._run(["", "  ", "local/exec"])
        self.assertEqual(result, "local/exec")
        self.assertGreaterEqual(len(fi.prompts), 3)

    def test_role_name_appears(self):
        _, out, fi = self._run(["x/y"])
        blob = out + "".join(fi.prompts)
        self.assertIn("executor", blob)


# --------------------------------------------------------------------------- #
class PromptGateTests(unittest.TestCase):
    def _run(self, answer):
        fi = scripted_input([answer])
        buf = io.StringIO()
        with mock.patch.object(builtins, "input", fi), redirect_stdout(buf):
            result = init.prompt_gate("test")
        return result, buf.getvalue(), fi

    def test_returns_stripped_command(self):
        result, _, _ = self._run("  ruff check .  ")
        self.assertEqual(result, "ruff check .")

    def test_blank_is_valid_returns_empty_string(self):
        result, _, fi = self._run("   ")
        self.assertEqual(result, "")
        # blank is accepted on the first ask - no looping
        self.assertEqual(len(fi.prompts), 1)

    def test_name_appears_in_prompt(self):
        _, out, fi = self._run("")
        blob = out + "".join(fi.prompts)
        self.assertIn("test", blob)


# --------------------------------------------------------------------------- #
class RenderConfigTests(unittest.TestCase):
    def test_has_tiers_and_gates_tables(self):
        text = init.render_config({"planner": "anthropic/claude-opus-4"}, {"lint": "ruff check ."})
        self.assertIn("[tiers]", text)
        self.assertIn("[gates]", text)

    def test_roundtrips_through_tomllib(self):
        tiers = {"planner": "anthropic/claude-opus-4"}
        gates = {"test": "", "lint": "ruff check ."}
        data = tomllib.loads(init.render_config(tiers, gates))
        self.assertEqual(data["tiers"]["planner"], "anthropic/claude-opus-4")
        self.assertEqual(data["gates"]["test"], "")
        self.assertEqual(data["gates"]["lint"], "ruff check .")

    def test_preserves_insertion_order(self):
        tiers = {r: f"prov/{r}" for r in ROLES}
        gates = {"test": "", "lint": "", "typecheck": ""}
        text = init.render_config(tiers, gates)
        positions = [text.index(f"{r} =") for r in ROLES]
        self.assertEqual(positions, sorted(positions))

    def test_escapes_backslash_and_quote(self):
        # value containing a quote and a backslash must round-trip exactly.
        gates = {"test": 'echo "hi" \\ done'}
        text = init.render_config({"planner": "p/m"}, gates)
        data = tomllib.loads(text)
        self.assertEqual(data["gates"]["test"], 'echo "hi" \\ done')

    def test_backslash_escaped_before_quote(self):
        # a literal backslash-then-quote in the value must survive intact.
        gates = {"test": 'a\\"b'}
        text = init.render_config({"planner": "p/m"}, gates)
        self.assertEqual(tomllib.loads(text)["gates"]["test"], 'a\\"b')

    def test_blank_line_between_tables(self):
        text = init.render_config({"planner": "p/m"}, {"test": ""})
        ti = text.index("[tiers]")
        gi = text.index("[gates]")
        self.assertIn("\n\n", text[ti:gi])

    def test_trailing_comment_block_mentions_example(self):
        text = init.render_config({"planner": "p/m"}, {"test": ""})
        after_gates = text[text.index("[gates]") :]
        comment_lines = [ln for ln in after_gates.splitlines() if ln.startswith("# ")]
        self.assertTrue(comment_lines, "expected a trailing '# ' comment block")
        self.assertIn("config.example.toml", after_gates)


# --------------------------------------------------------------------------- #
class RunInitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fm-init-"))
        self.cfg_path = self.tmp / ".director" / "config.toml"
        # run_init now auto-detects its target: repo path inside a git repo,
        # else the user path. Fake a git repo (a plain .git dir — no real git
        # is ever run) so auto-detect deterministically writes under self.tmp,
        # and isolate HOME so the user-path fallback can never touch the real
        # user config.
        (self.tmp / ".git").mkdir()
        self._saved_env = {k: os.environ.get(k) for k in ("HOME", "USERPROFILE")}
        self._home = Path(tempfile.mkdtemp(prefix="fm-init-home-"))
        os.environ["HOME"] = str(self._home)
        os.environ["USERPROFILE"] = str(self._home)

    def tearDown(self):
        for key, val in self._saved_env.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val

    def _run(self, *, models, answers):
        """Drive run_init with discovery + interactive builtins stubbed."""
        fi = scripted_input(answers)
        buf = io.StringIO()
        with (
            mock.patch.object(init, "discover_models", return_value=models),
            mock.patch.object(builtins, "input", fi),
            redirect_stdout(buf),
        ):
            path = init.run_init(str(self.tmp))
        return path, buf.getvalue(), fi

    def _menu_answers(self, gate_answers):
        # one numeric selection per role (always pick model #1) then the gates.
        return ["1"] * len(ROLES) + list(gate_answers)

    def test_menu_flow_writes_loadable_config(self):
        models = ["anthropic/claude-opus-4", "openai/gpt-4o"]
        path, _, _ = self._run(
            models=models,
            answers=self._menu_answers(["", "ruff check .", ""]),
        )
        self.assertEqual(path, self.cfg_path)
        self.assertTrue(self.cfg_path.exists())
        cfg = config.load_file(self.cfg_path)
        for role in ROLES:
            self.assertEqual(cfg.tiers[role], "anthropic/claude-opus-4")
        self.assertEqual(cfg.gates["test"], "")
        self.assertEqual(cfg.gates["lint"], "ruff check .")
        self.assertEqual(cfg.gates["typecheck"], "")

    def test_all_six_tiers_present(self):
        path, _, _ = self._run(models=["a/b"], answers=self._menu_answers(["", "", ""]))
        cfg = config.load_file(self.cfg_path)
        self.assertEqual(set(cfg.tiers), set(ROLES))

    def test_creates_parent_director_dir(self):
        self.assertFalse((self.tmp / ".director").exists())
        self._run(models=["a/b"], answers=self._menu_answers(["", "", ""]))
        self.assertTrue((self.tmp / ".director").is_dir())

    def test_freetext_flow_when_discovery_empty(self):
        # empty models -> free-text per role, then 3 gates.
        answers = [f"prov/{r}" for r in ROLES] + ["pytest", "", ""]
        path, out, _ = self._run(models=[], answers=answers)
        cfg = config.load_file(self.cfg_path)
        for role in ROLES:
            self.assertEqual(cfg.tiers[role], f"prov/{role}")
        self.assertEqual(cfg.gates["test"], "pytest")
        # warning must be provider-neutral: must mention free-text fallback and
        # must NOT name any specific runtime command like "opencode".
        self.assertIn(
            "free-text",
            out,
            f"expected 'free-text' in discovery warning, got: {out!r}",
        )
        self.assertNotIn(
            "opencode",
            out,
            f"'opencode' must not appear in the discovery warning, got: {out!r}",
        )

    def test_clobber_declined_does_not_overwrite(self):
        self.cfg_path.parent.mkdir(parents=True)
        self.cfg_path.write_text("ORIGINAL\n")
        path, out, fi = self._run(models=["a/b"], answers=["n"])
        self.assertEqual(path, self.cfg_path)
        # untouched
        self.assertEqual(self.cfg_path.read_text(), "ORIGINAL\n")
        # only the overwrite question was asked (no role/gate prompts).
        self.assertEqual(len(fi.prompts), 1)

    def test_clobber_declined_by_default_empty_answer(self):
        self.cfg_path.parent.mkdir(parents=True)
        self.cfg_path.write_text("ORIGINAL\n")
        self._run(models=["a/b"], answers=[""])
        self.assertEqual(self.cfg_path.read_text(), "ORIGINAL\n")

    def test_clobber_accepted_overwrites(self):
        self.cfg_path.parent.mkdir(parents=True)
        self.cfg_path.write_text("ORIGINAL\n")
        self._run(
            models=["a/b"],
            answers=["y"] + self._menu_answers(["", "", ""]),
        )
        self.assertNotEqual(self.cfg_path.read_text(), "ORIGINAL\n")
        cfg = config.load_file(self.cfg_path)
        self.assertEqual(cfg.tiers["planner"], "a/b")

    def test_clobber_accepted_yes_word(self):
        self.cfg_path.parent.mkdir(parents=True)
        self.cfg_path.write_text("ORIGINAL\n")
        self._run(
            models=["a/b"],
            answers=["YES"] + self._menu_answers(["", "", ""]),
        )
        cfg = config.load_file(self.cfg_path)
        self.assertEqual(cfg.tiers["planner"], "a/b")

    def test_gates_table_has_all_three_keys(self):
        self._run(models=["a/b"], answers=self._menu_answers(["t", "l", "tc"]))
        cfg = config.load_file(self.cfg_path)
        self.assertEqual(cfg.gates, {"test": "t", "lint": "l", "typecheck": "tc"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
