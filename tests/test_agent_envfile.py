"""Strict agent.env parsing (director/agent/envfile.py): data, never shell."""

import pathlib
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from director.agent.envfile import EnvFileError, load_env_file, parse_env_text  # noqa: E402


class ParseTests(unittest.TestCase):
    def test_basic_pairs_comments_and_blanks(self):
        text = "# comment\n\nPATH=/usr/bin:/bin\nLMSTUDIO_API_KEY=sk-123\n"
        self.assertEqual(
            parse_env_text(text),
            {"PATH": "/usr/bin:/bin", "LMSTUDIO_API_KEY": "sk-123"},
        )

    def test_surrounding_quotes_stripped_but_never_evaluated(self):
        env = parse_env_text("A=\"hello world\"\nB='$(dangerous) `cmd` $HOME'\n")
        self.assertEqual(env["A"], "hello world")
        # shell metacharacters survive verbatim — nothing is expanded or run
        self.assertEqual(env["B"], "$(dangerous) `cmd` $HOME")

    def test_value_may_contain_equals(self):
        self.assertEqual(parse_env_text("X=a=b=c"), {"X": "a=b=c"})

    def test_missing_equals_is_an_error_with_line_number(self):
        with self.assertRaises(EnvFileError) as ctx:
            parse_env_text("GOOD=1\njust some words\n")
        self.assertIn(":2:", str(ctx.exception))

    def test_invalid_key_is_an_error(self):
        with self.assertRaises(EnvFileError):
            parse_env_text("1BAD=x")
        with self.assertRaises(EnvFileError):
            parse_env_text("SPACED KEY=x")

    def test_export_prefix_is_rejected_not_interpreted(self):
        # `export KEY=V` is shell syntax; the file is strict KEY=VALUE only.
        with self.assertRaises(EnvFileError):
            parse_env_text("export KEY=V")


class LoadTests(unittest.TestCase):
    def test_missing_file_is_empty_env(self):
        self.assertEqual(load_env_file(Path(tempfile.mkdtemp()) / "agent.env"), {})

    def test_load_reads_and_names_the_file_in_errors(self):
        p = Path(tempfile.mkdtemp()) / "agent.env"
        p.write_text("nope")
        with self.assertRaises(EnvFileError) as ctx:
            load_env_file(p)
        self.assertIn(str(p), str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
