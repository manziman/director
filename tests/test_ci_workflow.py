"""Acceptance tests for the ci-windows-matrix node.

Pins the contract of `.github/workflows/ci.yml` after the `test` job is taught to
run on both Linux and Windows:

  - The `test` job gains an OS axis: ``matrix.os: [ubuntu-latest, windows-latest]``.
  - ``strategy.fail-fast: false`` is kept.
  - The existing ``python-version`` matrix is unchanged.
  - The job's ``runs-on`` becomes ``${{ matrix.os }}``.
  - A comment near the matrix documents that the suite stubs the external provider
    boundary and drives real temp git repos, and that ``windows-latest`` ships
    git + Git Bash ``sh`` (satisfying the shell-preference path).
  - Every ``run:`` step in the `test` job is cross-platform: it either avoids
    POSIX-only shell syntax or pins ``shell: bash``.
  - The `lint` and `build` jobs stay ``runs-on: ubuntu-latest`` (Windows is NOT
    added to them).
  - The workflow remains valid YAML and introduces no new third-party action.

No network, time, or randomness is used: the test only parses the static
workflow file.

Run: python -m unittest tests.test_ci_workflow -v
"""

import pathlib
import re
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402

CI_PATH = pathlib.Path(__file__).resolve().parent.parent / ".github" / "workflows" / "ci.yml"

# Shell metacharacters / constructs that are NOT portable to the default Windows
# shell. A run step using any of these must pin ``shell: bash`` to stay
# cross-platform on windows-latest.
_NON_PORTABLE = (";", "|", "&", ">", "<", "`", "$(", "&&", "||")


def _load():
    return yaml.safe_load(CI_PATH.read_text())


def _raw():
    return CI_PATH.read_text()


def _jobs(data):
    # PyYAML parses the bare ``on:`` key as the boolean True; jobs is unaffected.
    return data["jobs"]


def _steps(job):
    return job.get("steps", []) or []


class TestWorkflowIsValid(unittest.TestCase):
    def test_file_exists(self):
        self.assertTrue(CI_PATH.exists(), msg=f"missing workflow file: {CI_PATH}")

    def test_valid_yaml_with_test_job(self):
        data = _load()
        self.assertIsInstance(data, dict, msg="ci.yml did not parse to a mapping")
        self.assertIn("test", _jobs(data), msg="no `test` job in ci.yml")


class TestOSMatrix(unittest.TestCase):
    def test_matrix_has_os_axis_with_both_platforms(self):
        test_job = _jobs(_load())["test"]
        matrix = test_job["strategy"]["matrix"]
        self.assertIn("os", matrix, msg="`test` job matrix has no `os` axis")
        self.assertEqual(
            list(matrix["os"]),
            ["ubuntu-latest", "windows-latest"],
            msg=f"unexpected os axis: {matrix.get('os')!r}",
        )

    def test_runs_on_uses_matrix_os(self):
        test_job = _jobs(_load())["test"]
        self.assertEqual(
            str(test_job["runs-on"]).strip(),
            "${{ matrix.os }}",
            msg=f"`test` job runs-on is not the matrix os: {test_job.get('runs-on')!r}",
        )

    def test_fail_fast_is_false(self):
        strategy = _jobs(_load())["test"]["strategy"]
        self.assertIn("fail-fast", strategy, msg="strategy.fail-fast missing")
        self.assertIs(strategy["fail-fast"], False, msg="strategy.fail-fast must be false")

    def test_python_version_matrix_unchanged(self):
        matrix = _jobs(_load())["test"]["strategy"]["matrix"]
        self.assertIn("python-version", matrix, msg="python-version axis removed")
        self.assertEqual(
            [str(v) for v in matrix["python-version"]],
            ["3.11", "3.12", "3.13", "3.14"],
            msg=f"python-version matrix changed: {matrix.get('python-version')!r}",
        )


class TestMatrixComment(unittest.TestCase):
    """The matrix must carry a comment documenting the Windows rationale."""

    def test_comment_mentions_windows_and_shell_preference(self):
        comments = "\n".join(
            line for line in _raw().splitlines() if line.lstrip().startswith("#")
        ).lower()
        self.assertIn(
            "windows-latest",
            comments,
            msg="no comment documents the windows-latest rationale",
        )
        self.assertTrue(
            ("git bash" in comments) or re.search(r"\bsh\b", comments),
            msg="comment does not mention Git Bash `sh` (shell-preference path)",
        )


class TestOtherJobsStayLinux(unittest.TestCase):
    def test_lint_runs_on_ubuntu(self):
        self.assertEqual(_jobs(_load())["lint"]["runs-on"], "ubuntu-latest")

    def test_build_runs_on_ubuntu(self):
        self.assertEqual(_jobs(_load())["build"]["runs-on"], "ubuntu-latest")

    def test_only_test_job_uses_os_matrix(self):
        jobs = _jobs(_load())
        for name in ("lint", "build"):
            matrix = jobs[name].get("strategy", {}).get("matrix", {}) or {}
            self.assertNotIn(
                "os",
                matrix,
                msg=f"`{name}` job must not gain an os matrix axis",
            )


class TestCrossPlatformRunSteps(unittest.TestCase):
    def test_every_run_step_is_portable_or_pins_bash(self):
        test_job = _jobs(_load())["test"]
        offending = []
        for i, step in enumerate(_steps(test_job)):
            if not isinstance(step, dict) or "run" not in step:
                continue
            cmd = step["run"]
            if str(step.get("shell", "")).strip() == "bash":
                continue
            for token in _NON_PORTABLE:
                if token in cmd:
                    offending.append((i, token, cmd))
                    break
        self.assertEqual(
            offending,
            [],
            msg=(
                "test-job run steps use non-portable shell syntax without "
                f"`shell: bash`: {offending}"
            ),
        )


class TestNoNewThirdPartyAction(unittest.TestCase):
    """Windows support must not pull in a new third-party action."""

    def test_test_job_uses_only_known_actions(self):
        allowed = {
            "actions/checkout",
            "astral-sh/setup-uv",
            "actions/upload-artifact",
        }
        test_job = _jobs(_load())["test"]
        for step in _steps(test_job):
            if not isinstance(step, dict) or "uses" not in step:
                continue
            ref = str(step["uses"]).split("@", 1)[0]
            self.assertIn(
                ref,
                allowed,
                msg=f"test job introduces a new action dependency: {ref!r}",
            )


if __name__ == "__main__":
    unittest.main()
