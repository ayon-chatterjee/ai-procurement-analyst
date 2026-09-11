from __future__ import annotations

import json
import subprocess
import unittest

from rfq_copilot.ai_schemas import PING_SCHEMA, TURN_OUTPUT_SCHEMA
from rfq_copilot.ai_service import (
    AIInvalidOutput, AINotAuthenticated, AITimeout, AITransient, AIUnavailable, AIUsageLimit, ClaudeCLIProvider,
    get_ai_service, validate_against,
)
from rfq_copilot.config import Settings
from tests.helpers import load_fixture


def fake_runner(stdout: str, returncode: int = 0, stderr: str = ""):
    calls = []

    def run(argv, **kwargs):
        calls.append({"argv": argv, "kwargs": kwargs})
        return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr=stderr)
    run.calls = calls  # type: ignore[attr-defined]
    return run


class ClaudeCLIProviderTest(unittest.TestCase):
    def setUp(self):
        self.settings = Settings()
        self.settings.claude_bin = "claude"
        self.ok_env = json.dumps(load_fixture("cli_envelope_ok.json"))

    def test_recorded_envelope_parses_and_validates(self):
        run = fake_runner(self.ok_env)
        p = ClaudeCLIProvider(self.settings, runner=run)
        res = p.complete_json("prompt", TURN_OUTPUT_SCHEMA, "system")
        self.assertIsNone(validate_against(TURN_OUTPUT_SCHEMA, res.data))
        self.assertEqual(res.data["classification"]["product"], "Corrugated Carton Boxes")
        self.assertEqual(res.provider, "claude_cli")
        self.assertIn("sonnet", res.model)
        self.assertTrue(res.raw.startswith("{"))

    def test_argv_and_environment_hygiene(self):
        run = fake_runner(self.ok_env)
        p = ClaudeCLIProvider(self.settings, runner=run)
        p.complete_json("the prompt", TURN_OUTPUT_SCHEMA, "the system")
        argv = run.calls[0]["argv"]
        kw = run.calls[0]["kwargs"]
        self.assertEqual(argv[:3], ["claude", "-p", "the prompt"])
        for flag in ("--system-prompt", "--output-format", "--json-schema", "--model", "--max-turns", "--tools", "--no-session-persistence"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--output-format") + 1], "json")
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        self.assertEqual(argv[argv.index("--model") + 1], "sonnet")
        self.assertNotIn("--bare", argv)
        self.assertNotIn("CLAUDECODE", kw["env"])
        self.assertNotIn("CLAUDE_CODE_ENTRYPOINT", kw["env"])
        self.assertEqual(kw["timeout"], self.settings.ai_timeout_s)
        self.assertEqual(kw["stdin"], subprocess.DEVNULL)
        self.assertEqual(json.loads(argv[argv.index("--json-schema") + 1]), TURN_OUTPUT_SCHEMA)

    def test_fast_tier_uses_fast_model(self):
        run = fake_runner(json.dumps({"is_error": False, "structured_output": {"ok": True, "model_note": "hi there friend"}, "result": "", "modelUsage": {"claude-haiku-4-5": {}}}))
        p = ClaudeCLIProvider(self.settings, runner=run)
        res = p.complete_json("ping", PING_SCHEMA, "sys", tier="fast")
        self.assertEqual(run.calls[0]["argv"][run.calls[0]["argv"].index("--model") + 1], "haiku")
        self.assertTrue(res.data["ok"])

    def test_not_logged_in_maps_to_auth_error(self):
        run = fake_runner(json.dumps(load_fixture("cli_envelope_auth_error.json")))
        with self.assertRaises(AINotAuthenticated):
            ClaudeCLIProvider(self.settings, runner=run).complete_json("p", PING_SCHEMA, "s")

    def test_max_turns_and_dropped_connection_map_to_transient(self):
        run = fake_runner(json.dumps(load_fixture("cli_envelope_max_turns.json")))
        with self.assertRaises(AITransient) as ctx:
            ClaudeCLIProvider(self.settings, runner=run).complete_json("p", PING_SCHEMA, "s")
        self.assertIn("maximum number of turns", str(ctx.exception))
        dropped = {"is_error": True, "result": "API Error: Connection lost mid-response. The response above may be incomplete.",
                   "modelUsage": {}, "terminal_reason": "api_error"}
        with self.assertRaises(AITransient):
            ClaudeCLIProvider(self.settings, runner=fake_runner(json.dumps(dropped))).complete_json("p", PING_SCHEMA, "s")

    def test_session_limit_maps_to_a_usage_limit_with_its_reset_time(self):
        env = {"is_error": True, "result": "you've hit your session limit · resets 9pm (asia/calcutta)",
               "modelUsage": {}, "terminal_reason": "api_error"}
        with self.assertRaises(AIUsageLimit) as ctx:
            ClaudeCLIProvider(self.settings, runner=fake_runner(json.dumps(env))).complete_json("p", PING_SCHEMA, "s")
        msg = ctx.exception.user_message
        self.assertIn("usage limit", msg.lower())
        self.assertIn("resets 9pm", msg, "the reset time is the actionable part")
        self.assertNotIsInstance(ctx.exception, AIInvalidOutput)

    def test_overloaded_is_unavailable_not_a_usage_limit(self):
        env = {"is_error": True, "result": "API Error: service overloaded", "modelUsage": {}}
        with self.assertRaises(AIUnavailable):
            ClaudeCLIProvider(self.settings, runner=fake_runner(json.dumps(env))).complete_json("p", PING_SCHEMA, "s")

    def test_non_json_stdout(self):
        run = fake_runner("Something went wrong\n", returncode=0)
        with self.assertRaises(AIInvalidOutput):
            ClaudeCLIProvider(self.settings, runner=run).complete_json("p", PING_SCHEMA, "s")

    def test_result_text_fallback_when_no_structured_output(self):
        run = fake_runner(json.dumps({"is_error": False, "result": json.dumps({"ok": True, "model_note": "from result text"}), "modelUsage": {}}))
        res = ClaudeCLIProvider(self.settings, runner=run).complete_json("p", PING_SCHEMA, "s")
        self.assertEqual(res.data["model_note"], "from result text")

    def test_schema_violation_is_invalid_output(self):
        run = fake_runner(json.dumps({"is_error": False, "structured_output": {"ok": "yes"}, "result": "", "modelUsage": {}}))
        with self.assertRaises(AIInvalidOutput):
            ClaudeCLIProvider(self.settings, runner=run).complete_json("p", PING_SCHEMA, "s")

    def test_missing_binary_and_timeout(self):
        def missing(argv, **kw):
            raise FileNotFoundError(argv[0])
        with self.assertRaises(AIUnavailable):
            ClaudeCLIProvider(self.settings, runner=missing).complete_json("p", PING_SCHEMA, "s")

        def slow(argv, **kw):
            raise subprocess.TimeoutExpired(argv, kw.get("timeout", 1))
        with self.assertRaises(AITimeout):
            ClaudeCLIProvider(self.settings, runner=slow).complete_json("p", PING_SCHEMA, "s")

    def test_health_reads_auth_status(self):
        run = fake_runner(json.dumps({"loggedIn": True, "email": "buyer@example.com", "subscriptionType": "max"}))
        import shutil
        p = ClaudeCLIProvider(self.settings, runner=run)
        if shutil.which("claude"):
            h = p.health()
            self.assertTrue(h["authenticated"])
            self.assertEqual(h["detail"], "max", "detail shows the plan, never the account email")
            self.assertNotIn("@", h["detail"])
        self.settings.claude_bin = "/definitely/not/here/claude"
        h = ClaudeCLIProvider(self.settings, runner=run).health()
        self.assertFalse(h["available"])

    def test_factory_returns_cli_provider(self):
        self.assertIsInstance(get_ai_service(self.settings), ClaudeCLIProvider)


if __name__ == "__main__":
    unittest.main()
