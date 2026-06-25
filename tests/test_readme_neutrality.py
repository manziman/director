"""Acceptance tests for README neutrality — OpenCode and Claude Code as peer runtimes.

Both README.md and director/README.md must frame OpenCode and Claude Code as
interchangeable peers; neither may appear as the sole unconditional prerequisite.
Tests mirror the style of tests/test_config_example_runtime.py.
"""

import pathlib
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

_ROOT = Path(__file__).resolve().parent.parent
_README = _ROOT / "README.md"
_DIRECTOR_README = _ROOT / "director" / "README.md"


def _read(path):
    return path.read_text(encoding="utf-8")


def _strip_md_links(text):
    """Replace [label](url) with just label so substring checks hit the rendered text."""
    return re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)


class ReadmeFilesExistTest(unittest.TestCase):
    def test_readme_exists(self):
        self.assertTrue(_README.exists(), f"missing {_README}")

    def test_director_readme_exists(self):
        self.assertTrue(_DIRECTOR_README.exists(), f"missing {_DIRECTOR_README}")


class ReadmeNeutralityTest(unittest.TestCase):
    """README.md must frame OpenCode and Claude Code as peer runtimes."""

    def setUp(self):
        self.text = _read(_README)
        self.text_plain = _strip_md_links(self.text)

    # ------------------------------------------------------------------ #
    # Framing / removed strings
    # ------------------------------------------------------------------ #

    def test_claude_code_named_as_peer(self):
        """'Claude Code' must appear in README.md as a peer runtime."""
        self.assertIn(
            "Claude Code",
            self.text,
            "README.md must name 'Claude Code' as a peer runtime alongside OpenCode",
        )

    def test_no_thin_orchestrator_over_opencode(self):
        """The absolute framing 'a thin orchestrator over OpenCode' must be removed.

        Checks link-stripped text so [OpenCode](url) and bare OpenCode both match.
        """
        self.assertNotIn(
            "a thin orchestrator over OpenCode",
            self.text_plain,
            "README.md must not contain 'a thin orchestrator over OpenCode' "
            "(checked after stripping markdown links); "
            "reframe generically around agent runtimes",
        )

    def test_no_absolute_parenthetical_agent_runtime(self):
        """The absolute parenthetical '(the agent runtime director drives)' must be removed."""
        self.assertNotIn(
            "(the agent runtime director drives)",
            self.text,
            "README.md must not contain '(the agent runtime director drives)' — "
            "it frames OpenCode as the sole unconditional runtime",
        )

    # ------------------------------------------------------------------ #
    # Prerequisites section — conditional phrasing
    # ------------------------------------------------------------------ #

    def _prereq_section(self):
        """Extract the Prerequisites section text (up to the next heading or divider).

        Returns link-stripped text so checks match rendered words not raw markdown.
        """
        lines = self.text_plain.splitlines()
        start = None
        for i, line in enumerate(lines):
            if "Prerequisites" in line and line.startswith("#"):
                start = i
                break
        if start is None:
            return ""
        end = len(lines)
        for i in range(start + 1, len(lines)):
            line = lines[i]
            if line.startswith("---") or (line.startswith("##") and i > start):
                end = i
                break
        return "\n".join(lines[start:end])

    def test_prerequisites_mentions_opencode(self):
        """Prerequisites section must still mention OpenCode."""
        prereq = self._prereq_section()
        self.assertIn(
            "OpenCode",
            prereq,
            "Prerequisites section must still mention OpenCode",
        )

    def test_prerequisites_mentions_claude_code(self):
        """Prerequisites section must name 'Claude Code' as a runtime option."""
        prereq = self._prereq_section()
        self.assertIn(
            "Claude Code",
            prereq,
            "Prerequisites section must mention 'Claude Code' as a runtime option",
        )

    def test_prerequisites_has_conditional_if(self):
        """The word 'if' must appear as a standalone qualifier in the prerequisites section."""
        prereq = self._prereq_section()
        match = re.search(r"\bif\b", prereq, re.IGNORECASE)
        self.assertIsNotNone(
            match,
            "Prerequisites section must contain a conditional 'if' qualifier "
            "alongside the runtime prerequisites "
            "(e.g. 'IF you use an OpenCode-owned provider')",
        )

    # ------------------------------------------------------------------ #
    # Quickstart ordering
    # ------------------------------------------------------------------ #

    def test_director_init_before_sync_agents(self):
        """In README.md, the first 'director init' must precede 'director sync-agents'."""
        init_idx = self.text.find("director init")
        sync_idx = self.text.find("director sync-agents")
        self.assertNotEqual(init_idx, -1, "README.md must contain 'director init'")
        self.assertNotEqual(sync_idx, -1, "README.md must contain 'director sync-agents'")
        self.assertLess(
            init_idx,
            sync_idx,
            f"'director init' (pos {init_idx}) must appear before "
            f"'director sync-agents' (pos {sync_idx}) in README.md quickstart",
        )

    # ------------------------------------------------------------------ #
    # sync-agents command table row
    # ------------------------------------------------------------------ #

    def _sync_agents_table_row(self):
        """Return the markdown table row for sync-agents in README.md."""
        for line in self.text.splitlines():
            if "sync-agents" in line and "|" in line:
                return line
        return None

    def test_sync_agents_table_row_exists(self):
        """README.md must have a sync-agents row in the command table."""
        self.assertIsNotNone(
            self._sync_agents_table_row(),
            "README.md must contain a sync-agents markdown table row",
        )

    def test_sync_agents_table_row_conditional_wording(self):
        """The sync-agents command table row must use conditional wording."""
        row = self._sync_agents_table_row()
        if row is None:
            self.fail("sync-agents table row not found in README.md")
        row_lower = row.lower()
        has_conditional = (
            "only" in row_lower
            or re.search(r"\bwhen\b", row_lower) is not None
            or re.search(r"\bif\b", row_lower) is not None
        )
        self.assertTrue(
            has_conditional,
            "sync-agents command table row must use conditional wording "
            "('only', 'when', or 'if') to indicate .opencode/ is written only for "
            "OpenCode-provider tiers; the current row states this unconditionally",
        )


class DirectorReadmeNeutralityTest(unittest.TestCase):
    """director/README.md must name Claude Code as a peer, not the sole runtime."""

    def setUp(self):
        self.text = _read(_DIRECTOR_README)
        self.lines = self.text.splitlines()

    def test_opening_description_no_drives_opencode_headlessly_sole(self):
        """Lines 1-6 must not use 'drives OpenCode headlessly' as the sole framing."""
        preamble = "\n".join(self.lines[:6])
        self.assertNotIn(
            "drives OpenCode headlessly",
            preamble,
            "director/README.md opening description must not solely say "
            "'drives OpenCode headlessly'; name Claude Code as a peer or use "
            "generic 'agent runtimes' language",
        )

    def _sync_agents_line(self):
        """Return the first line describing sync-agents in director/README.md."""
        for line in self.lines:
            if "sync-agents" in line:
                return line
        return None

    def test_sync_agents_line_exists(self):
        """director/README.md must contain a sync-agents description line."""
        self.assertIsNotNone(
            self._sync_agents_line(),
            "director/README.md must contain a sync-agents line",
        )

    def test_sync_agents_line_conditional_wording(self):
        """The sync-agents description in director/README.md must use conditional wording."""
        line = self._sync_agents_line()
        if line is None:
            self.fail("sync-agents line not found in director/README.md")
        line_lower = line.lower()
        has_conditional = (
            "only" in line_lower
            or re.search(r"\bwhen\b", line_lower) is not None
            or re.search(r"\bif\b", line_lower) is not None
        )
        self.assertTrue(
            has_conditional,
            "director/README.md sync-agents line must use conditional wording "
            "('only', 'when', or 'if') to indicate .opencode/ is written only for "
            "OpenCode-provider tiers",
        )


if __name__ == "__main__":
    unittest.main()
