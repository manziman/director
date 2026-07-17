"""Regression tests for repository-local agent guidance context."""

from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import types
import unittest
from pathlib import Path

os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from director.guidance import RepositoryGuidance  # noqa: E402
from director.models import Node  # noqa: E402
from director.plan import _explorer_prompt, _planner_prompt, _testauthor_prompt  # noqa: E402
from director.review import _reviewer_message  # noqa: E402
from director.run import _executor_message  # noqa: E402


class RepositoryGuidanceTests(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp(prefix="director-guidance-"))
        (self.repo / "src" / "feature").mkdir(parents=True)
        (self.repo / "e2e").mkdir()
        (self.repo / "node_modules" / "package").mkdir(parents=True)
        (self.repo / "AGENTS.md").write_text("ROOT AGENTS\n")
        (self.repo / "CLAUDE.md").write_text("ROOT CLAUDE\n")
        (self.repo / "src" / "CLAUDE.md").write_text("SRC CONVENTION\n")
        (self.repo / "e2e" / "AGENTS.md").write_text("E2E CONVENTION\n")
        (self.repo / "node_modules" / "package" / "AGENTS.md").write_text("DEPENDENCY GUIDE\n")
        self.guidance = RepositoryGuidance.discover(self.repo)

    def test_discovery_ignores_dependency_guidance_and_keeps_repo_paths(self):
        self.assertEqual(
            [item.path for item in self.guidance.files],
            ["AGENTS.md", "CLAUDE.md", "e2e/AGENTS.md", "src/CLAUDE.md"],
        )

    def test_node_context_includes_root_and_ancestor_guidance_only(self):
        context = self.guidance.for_files(["src/feature/widget.ts"])

        self.assertIn("ROOT AGENTS", context)
        self.assertIn("ROOT CLAUDE", context)
        self.assertIn("SRC CONVENTION", context)
        self.assertNotIn("E2E CONVENTION", context)

    def test_test_paths_can_contribute_nested_guidance(self):
        context = self.guidance.for_files(["src/feature/widget.ts", "e2e/widget.spec.ts"])

        self.assertIn("SRC CONVENTION", context)
        self.assertIn("E2E CONVENTION", context)

    def test_planning_context_contains_all_discovered_guidance(self):
        context = self.guidance.for_planning()

        self.assertIn("ROOT AGENTS", context)
        self.assertIn("SRC CONVENTION", context)
        self.assertIn("E2E CONVENTION", context)

    def test_role_messages_include_their_relevant_guidance(self):
        node = Node(
            "widget",
            "Widget",
            "implement it",
            ["src/feature/widget.ts"],
            test_cmd="test widget",
            tests=["e2e/widget.spec.ts"],
        )
        planning = self.guidance.for_planning()
        node_guidance = self.guidance.for_files([*node.files, *node.tests])

        self.assertIn("SRC CONVENTION", _explorer_prompt("task", planning))
        self.assertIn(
            "E2E CONVENTION",
            _planner_prompt("spec", "recon", types.SimpleNamespace(gates={}), planning),
        )
        self.assertIn("E2E CONVENTION", _testauthor_prompt(node, node_guidance))
        self.assertIn("E2E CONVENTION", _executor_message(node, self.repo, "red", node_guidance))
        self.assertIn("E2E CONVENTION", _reviewer_message(node, "diff", "review", node_guidance))


if __name__ == "__main__":
    unittest.main()
