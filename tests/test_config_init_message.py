"""Acceptance test for the `config-error-message` node.

`director.config.load(repo)` raises `FileNotFoundError` when
`<repo>/.director/config.toml` is missing. The message must now direct the
user to run `director init` (not `director sync-agents`), while keeping the
exception TYPE and the `{path}` reference intact.
"""

import os
import pathlib
import sys
import tempfile
import unittest
from pathlib import Path

os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from director import config


class LoadMissingConfigMessageTests(unittest.TestCase):
    def setUp(self):
        # A fresh temp repo with NO .director/config.toml.
        self.tmp = Path(tempfile.mkdtemp(prefix="fm-cfgmsg-"))
        # Isolate HOME (and USERPROFILE) to an empty temp dir so no user-level
        # ~/.director/config.toml exists either. With two-level loading, the
        # FileNotFoundError only fires when BOTH the user and repo configs are
        # absent — this keeps the "both absent" precondition true.
        self._saved_home = os.environ.get("HOME")
        self._saved_userprofile = os.environ.get("USERPROFILE")
        self._home = Path(tempfile.mkdtemp(prefix="fm-cfgmsg-home-"))
        os.environ["HOME"] = str(self._home)
        os.environ["USERPROFILE"] = str(self._home)

    def tearDown(self):
        for key, val in (
            ("HOME", self._saved_home),
            ("USERPROFILE", self._saved_userprofile),
        ):
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val

    def _raise(self):
        with self.assertRaises(FileNotFoundError) as ctx:
            config.load(self.tmp)
        return ctx.exception

    def test_type_is_filenotfounderror(self):
        # The exception type must stay FileNotFoundError.
        exc = self._raise()
        self.assertIsInstance(exc, FileNotFoundError)

    def test_message_mentions_director_init(self):
        msg = str(self._raise())
        self.assertIn("director init", msg)

    def test_message_does_not_mention_sync_agents(self):
        msg = str(self._raise())
        self.assertNotIn("director sync-agents", msg)
        self.assertNotIn("sync-agents", msg)

    def test_message_includes_the_path(self):
        # The f-string must still interpolate {path}.
        expected_path = str(self.tmp / ".director" / "config.toml")
        msg = str(self._raise())
        self.assertIn(expected_path, msg)


if __name__ == "__main__":
    unittest.main(verbosity=2)
