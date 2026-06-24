"""Acceptance tests for `director/init.py` (the `director init` flow).

These pin the contract for the new module: a pure model-list parser, a
subprocess-backed model discovery that degrades to free-text, interactive
prompt helpers (driven here by monkeypatched `input`/`print`), a pure TOML
renderer that must round-trip through `director.config.load_file`, and the
`run_init` orchestrator that wires it all together against a temp repo.

Only the single external boundary (`subprocess.run` for `opencode models`) and
the interactive builtins are stubbed; everything else is the real code.
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
class DiscoverModelsTests(unittest.TestCase):
    def _completed(self, returncode, stdout):
        cp = mock.Mock()
        cp.returncode = returncode
        cp.stdout = stdout
        return cp

    def test_invokes_opencode_models_with_expected_args(self):
        cp = self._completed(0, "x/y\n")
        with mock.patch.object(init.subprocess, "run", return_value=cp) as run:
            out = init.discover_models()
        self.assertEqual(out, ["x/y"])
        args, kwargs = run.call_args
        self.assertEqual(args[0], ["opencode", "models"])
        self.assertTrue(kwargs.get("capture_output"))
        self.assertTrue(kwargs.get("text"))
        self.assertFalse(kwargs.get("check", False))

    def test_parses_successful_output(self):
        cp = self._completed(0, "Available:\nanthropic/claude\nopenai/gpt-4o\n")
        with mock.patch.object(init.subprocess, "run", return_value=cp):
            out = init.discover_models()
        self.assertEqual(out, ["anthropic/claude", "openai/gpt-4o"])

    def test_nonzero_returncode_yields_empty(self):
        cp = self._completed(2, "anthropic/claude\n")
        with mock.patch.object(init.subprocess, "run", return_value=cp):
            self.assertEqual(init.discover_models(), [])

    def test_missing_binary_filenotfound_yields_empty(self):
        with mock.patch.object(init.subprocess, "run", side_effect=FileNotFoundError):
            self.assertEqual(init.discover_models(), [])


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
        # a warning about unavailable/empty discovery should be printed.
        self.assertTrue(
            "opencode models" in out or "free-text" in out.lower(),
            f"expected a discovery warning, got: {out!r}",
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
