"""Acceptance tests: watch_it_fail _skip list is stack-neutral.

Covers:
  - watch_it_fail returns observed-failure for non-Python commands (go test, npm test)
  - python3 -m unittest discover still recognized after _skip change (regression)
  - "python" and "python3" are NOT in the _skip noise-token set
    (behavioral: token-matching fires for python3/python commands when no
    _TEST_SIGNALS keyword is present in the blob)
  - Generic noise tokens that must remain (-m, discover, &&) still suppress matches

Run: python3 -m unittest tests.test_watch_it_fail_agnostic -v
"""

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from director.opencode import _looks_like_test, watch_it_fail


def _shell_event(blob: str, name: str = "bash") -> dict:
    return {"name": name, "status": "completed", "blob": blob}


# --------------------------------------------------------------------------- #
# 1. Non-Python commands — acceptance criteria
# --------------------------------------------------------------------------- #


class TestWatchItFailNonPythonCommand(unittest.TestCase):
    """watch_it_fail handles non-Python test commands correctly."""

    def test_go_test_verdict_observed(self):
        blob = "go test ./... --- fail: TestFoo (0.001s) exit code 1"
        result = watch_it_fail([_shell_event(blob)], "go test ./...")
        self.assertEqual(result.verdict, "observed")

    def test_go_test_ran_before_edit(self):
        blob = "go test ./... --- fail: TestFoo (0.001s) exit code 1"
        result = watch_it_fail([_shell_event(blob)], "go test ./...")
        self.assertTrue(result.ran_before_edit)

    def test_go_test_observed_failure_true(self):
        blob = "go test ./... --- fail: TestFoo (0.001s) exit code 1"
        result = watch_it_fail([_shell_event(blob)], "go test ./...")
        self.assertTrue(result.observed_failure)

    def test_go_test_passing_run_not_observed_failure(self):
        # Tests ran (observed) but no failure signals present.
        blob = "go test ./... ok  (0.001s)"
        result = watch_it_fail([_shell_event(blob)], "go test ./...")
        self.assertEqual(result.verdict, "observed")
        self.assertFalse(result.observed_failure)

    def test_npm_test_verdict_observed(self):
        blob = "npm test: 3 failed, 0 passed exit code 1"
        result = watch_it_fail([_shell_event(blob)], "npm test")
        self.assertEqual(result.verdict, "observed")

    def test_npm_test_observed_failure_true(self):
        blob = "npm test: 3 failed, 0 passed exit code 1"
        result = watch_it_fail([_shell_event(blob)], "npm test")
        self.assertTrue(result.observed_failure)

    def test_non_python_edit_before_tests_not_observed(self):
        edit_event = {"name": "edit", "status": "completed", "blob": "edited foo.go"}
        result = watch_it_fail([edit_event], "go test ./...")
        self.assertEqual(result.verdict, "not_observed")

    def test_empty_events_yields_unknown(self):
        result = watch_it_fail([], "go test ./...")
        self.assertEqual(result.verdict, "unknown")


# --------------------------------------------------------------------------- #
# 2. Python regression — python3 -m unittest discover must still work
# --------------------------------------------------------------------------- #


class TestWatchItFailPythonRegression(unittest.TestCase):
    """python3 -m unittest discover is still recognized after _skip neutralization."""

    def test_python3_unittest_discover_verdict_observed(self):
        blob = "python3 -m unittest discover: error: exit code 1"
        result = watch_it_fail([_shell_event(blob)], "python3 -m unittest discover")
        self.assertEqual(result.verdict, "observed")

    def test_python3_unittest_discover_failure_seen(self):
        blob = "python3 -m unittest discover failed (failures=1)"
        result = watch_it_fail([_shell_event(blob)], "python3 -m unittest discover")
        self.assertTrue(result.observed_failure)

    def test_python3_unittest_discover_ran_before_edit(self):
        blob = "python3 -m unittest discover: no tests ran"
        result = watch_it_fail([_shell_event(blob)], "python3 -m unittest discover")
        self.assertTrue(result.ran_before_edit)

    def test_python3_unittest_looks_like_test(self):
        blob = "python3 -m unittest discover: error: exit code 1"
        self.assertTrue(
            _looks_like_test(blob, "python3 -m unittest discover"),
            "python3 -m unittest discover must be recognized as a test command "
            "via the 'unittest' _TEST_SIGNALS match",
        )


# --------------------------------------------------------------------------- #
# 3. _skip stack-neutrality — "python" and "python3" must not suppress matches
# --------------------------------------------------------------------------- #


class TestSkipListStackNeutral(unittest.TestCase):
    """_skip must not contain language-binary entries "python" or "python3".

    The key behavioral invariant: if the blob contains "python3" (or "python")
    and no _TEST_SIGNALS keyword is present, the token-matching path must fire.

    BEFORE implementation: "python3" (and "python") are in _skip → the token is
    suppressed → _looks_like_test returns False → the tests below FAIL (RED).

    AFTER  implementation: they are absent from _skip → token matched → True → PASS.
    """

    def test_python3_token_not_suppressed_by_skip(self):
        """Token "python3" must match when it appears in both cmd and blob.

        Blob deliberately contains NO _TEST_SIGNALS words so only the token-
        matching path can fire.  "myrunner" is absent from the blob so only
        the "python3" token can trigger a match.
        """
        blob = "python3 subprocess exited with exit code 1"
        self.assertTrue(
            _looks_like_test(blob, "python3 myrunner"),
            '"python3" must not be in _skip — removing it lets token-matching fire '
            "when python3 appears in the blob",
        )

    def test_python_token_not_suppressed_by_skip(self):
        """Same as above for the bare "python" binary."""
        blob = "python subprocess exited with exit code 1"
        self.assertTrue(
            _looks_like_test(blob, "python myrunner"),
            '"python" must not be in _skip — removing it lets token-matching fire '
            "when python appears in the blob",
        )

    def test_watch_it_fail_python3_runner_observed(self):
        """watch_it_fail must return "observed" for a python3-based runner whose
        blob contains no _TEST_SIGNALS keyword.

        BEFORE: _looks_like_test returns False → verdict "unknown"  [RED]
        AFTER:  _looks_like_test returns True  → verdict "observed" [GREEN]
        """
        blob = "python3 subprocess exited with exit code 1"
        result = watch_it_fail([_shell_event(blob)], "python3 myrunner")
        self.assertEqual(
            result.verdict,
            "observed",
            f'Expected verdict "observed" but got {result.verdict!r} — '
            '"python3" may still be suppressed by _skip',
        )

    def test_discover_noise_token_still_suppresses_match(self):
        """'discover' must remain in _skip and continue to suppress spurious matches.

        The blob explicitly contains "discover" so we can confirm the _skip
        suppression fires.  If someone accidentally removes "discover" from
        _skip, this test would fail (it would match and return True).

        The blob must contain NO _TEST_SIGNALS substrings so only the token-
        matching path is exercised (not the first-branch short-circuit).
        """
        # "artifacts" and "directory" contain no _TEST_SIGNALS substrings.
        blob = "discover artifacts in directory exit code 1"
        self.assertFalse(
            _looks_like_test(blob, "discover mymod"),
            '"discover" must stay in _skip — removing it would cause false-positive matches',
        )

    def test_only_python_python3_removed_not_other_entries(self):
        """Removing python/python3 must not accidentally remove other _skip entries.

        Checks that "discover" and "-m" (implied by behavior) still suppress.
        The implementation must be a minimal diff — only "python"/"python3" go.
        """
        # "discover" is the only entry with len > 3 (other than python/python3)
        # so it's the only one that could cause a spurious match if removed.
        blob = "discover artifacts exit code 1"
        self.assertFalse(
            _looks_like_test(blob, "discover something"),
            '"discover" must remain in _skip after the change',
        )


if __name__ == "__main__":
    unittest.main()
