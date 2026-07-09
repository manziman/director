"""Pi provider registry and dispatch coverage."""

from __future__ import annotations

import pathlib
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import director.config as config  # noqa: E402
import director.opencode as dispatch  # noqa: E402
import director.pi as pi  # noqa: E402
import director.provider as provider  # noqa: E402


class TestPiRegistration(unittest.TestCase):
    def test_dispatch_module_registers_pi(self):
        self.assertIsInstance(provider.resolve("pi"), pi.PiProvider)

    def test_config_builtin_registration_includes_pi(self):
        self.assertIn("pi", [item.name for item in config._ensure_builtin_providers_registered().providers()])

    def test_init_side_effect_import_is_present(self):
        source = (ROOT / "director" / "init.py").read_text()
        self.assertIn("import director.pi", source)

    def test_run_agent_routes_pi_model(self):
        with mock.patch.object(pi, "run_pi", return_value=provider.RunResult(text="ok")) as run:
            result = dispatch.run_agent(
                agent="executor",
                model="pi/groq/llama-3.3",
                message="m",
                cwd=".",
                log_path="run.jsonl",
                timeout=5,
            )
        self.assertEqual(result.text, "ok")
        self.assertEqual(run.call_args.kwargs["model"], "groq/llama-3.3")

    def test_unknown_provider_lists_pi(self):
        result = dispatch.run_agent(
            agent="executor",
            model="missing/model",
            message="m",
            cwd=".",
            log_path="run.jsonl",
            timeout=5,
        )
        self.assertIn("pi", result.error or "")


if __name__ == "__main__":
    unittest.main()
