"""Packaging regression tests for bundled package data.

hatchling honors .gitignore patterns when selecting wheel/sdist contents, so an
overly broad ignore pattern (e.g. a bare ``*.json``) silently drops git-tracked
package data from the built distribution. That shipped 0.8.1 without
``director/agent_templates/opencode.json`` and broke ``director plan`` /
``sync-agents`` at first use.

These tests pin two invariants:

  - every git-tracked file under ``director/agent_templates/`` is NOT matched by
    the repo's ignore rules (``git check-ignore`` finds nothing), and
  - ``pyproject.toml`` force-includes the templates via hatch ``artifacts`` as a
    second line of defense.

No network, time, or randomness is used: the tests shell out to the local git
checkout only. They skip when not running from a git checkout (e.g. an sdist).

Run: python -m unittest tests.test_packaging -v
"""

import pathlib
import subprocess
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

REPO = pathlib.Path(__file__).resolve().parent.parent
TEMPLATES_DIR = REPO / "director" / "agent_templates"


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=REPO,
        capture_output=True,
        text=True,
    )


def _in_git_checkout() -> bool:
    proc = _git("rev-parse", "--is-inside-work-tree")
    return proc.returncode == 0 and proc.stdout.strip() == "true"


class TestAgentTemplatesNotIgnored(unittest.TestCase):
    """Ignore rules must never match the packaged agent templates."""

    @classmethod
    def setUpClass(cls):
        if not _in_git_checkout():
            raise unittest.SkipTest("not a git checkout (sdist install?)")
        proc = _git("ls-files", "--", "director/agent_templates")
        cls.tracked = [line for line in proc.stdout.splitlines() if line.strip()]

    def test_templates_are_tracked(self):
        self.assertTrue(self.tracked, "no tracked files under director/agent_templates/")

    def test_opencode_json_is_tracked(self):
        self.assertIn("director/agent_templates/opencode.json", self.tracked)

    def test_no_tracked_template_is_ignore_matched(self):
        """`git check-ignore` must match none of the tracked templates.

        Tracked files are unaffected by .gitignore for git itself, but hatchling
        applies the same patterns when building — a match here means the file
        would be dropped from the wheel.
        """
        proc = _git("check-ignore", "--no-index", "--", *self.tracked)
        self.assertEqual(
            proc.stdout.strip(),
            "",
            f"ignore rules match packaged files (would be dropped from the wheel):\n{proc.stdout}",
        )


class TestHatchArtifactsForceInclude(unittest.TestCase):
    """pyproject must force-include the templates regardless of ignore rules."""

    def test_artifacts_covers_agent_templates(self):
        import tomllib

        data = tomllib.loads((REPO / "pyproject.toml").read_text())
        artifacts = data.get("tool", {}).get("hatch", {}).get("build", {}).get("artifacts", [])
        self.assertTrue(
            any(pattern.startswith("director/agent_templates/") for pattern in artifacts),
            f"[tool.hatch.build] artifacts does not cover director/agent_templates/: {artifacts}",
        )


if __name__ == "__main__":
    unittest.main()
