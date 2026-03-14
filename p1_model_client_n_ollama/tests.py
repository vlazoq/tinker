"""
Tinker Model Client — Test Harness
===================================
Validates both Ollama connections, routing logic, JSON extraction,
context enforcement, and error handling.

Usage
-----
    # With real Ollama servers running:
    python -m tinker.model_client.tests

    # Override endpoints:
    TINKER_SERVER_URL=http://myserver:11434 \
    TINKER_SECONDARY_URL=http://mysecondary:11434 \
    python -m tinker.model_client.tests

    # Run unit tests only (no network):
    python -m tinker.model_client.tests --unit-only

    # Run integration tests (requires live Ollama):
    python -m tinker.model_client.tests --integration
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import textwrap
import traceback
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

# ── configure logging ────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("tinker.tests")

# ── local imports ─────────────────────────────────────────────────────────────
from tinker.model_client import (
    AgentRole, Machine, MachineConfig, Message,
    ModelRequest, ModelResponse, ModelRouter, RetryConfig,
    ROLE_MACHINE_MAP, extract_json, build_json_instruction,
)
from tinker.model_client.context import count_tokens, enforce_context_limit
from tinker.model_client.client import (
    OllamaClient, ConnectionError as MCConnectionError,
    ServerError, TimeoutError as MCTimeoutError,
)


# ═══════════════════════════════════════════════════════════════════════════════
# 1. UNIT TESTS  (no network required)
# ═══════════════════════════════════════════════════════════════════════════════

class TestRoleRouting(unittest.TestCase):
    def test_server_roles(self):
        for role in (AgentRole.ARCHITECT, AgentRole.RESEARCHER, AgentRole.SYNTHESIZER):
            self.assertEqual(ROLE_MACHINE_MAP[role], Machine.SERVER, f"{role} → SERVER")

    def test_critic_secondary(self):
        self.assertEqual(ROLE_MACHINE_MAP[AgentRole.CRITIC], Machine.SECONDARY)


class TestJsonExtraction(unittest.TestCase):
    def _extract(self, text):
        result, strategy = extract_json(text)
        return result, strategy

    def test_direct_object(self):
        obj, strat = self._extract('{"key": "value", "num": 42}')
        self.assertEqual(obj, {"key": "value", "num": 42})
        self.assertEqual(strat, "direct")

    def test_direct_array(self):
        obj, strat = self._extract('[1, 2, 3]')
        self.assertEqual(obj, [1, 2, 3])

    def test_fenced_json(self):
        text = '```json\n{"a": 1}\n```'
        obj, strat = self._extract(text)
        self.assertEqual(obj, {"a": 1})
        self.assertEqual(strat, "fenced")

    def test_fenced_no_lang(self):
        text = '```\n{"x": true}\n```'
        obj, strat = self._extract(text)
        self.assertEqual(obj, {"x": True})

    def test_preamble_with_json(self):
        text = "Here is the JSON you requested:\n\n{\"result\": \"ok\"}"
        obj, strat = self._extract(text)
        self.assertEqual(obj, {"result": "ok"})

    def test_json_with_trailing_text(self):
        text = '{"done": true}\n\nHope that helps!'
        obj, strat = self._extract(text)
        self.assertEqual(obj, {"done": True})

    def test_nested_json(self):
        data = {"a": {"b": {"c": [1, 2, 3]}}}
        obj, _ = self._extract(json.dumps(data))
        self.assertEqual(obj, data)

    def test_extraction_failure(self):
        obj, strat = self._extract("This has no JSON at all.")
        self.assertIsNone(obj)
        self.assertIsNone(strat)

    def test_json_instruction_no_hint(self):
        instr = build_json_instruction()
        self.assertIn("JSON", instr)

    def test_json_instruction_with_hint(self):
        instr = build_json_instruction('{"name": "str"}')
        self.assertIn("name", instr)


class TestContextEnforcement(unittest.TestCase):
    def _make(self, role, content):
        return Message(role=role, content=content)

    def test_no_truncation_needed(self):
        msgs = [self._make("system", "sys"), self._make("user", "hi")]
        result = enforce_context_limit(msgs, context_window=8192, max_output_tokens=512)
        self.assertEqual(len(result), 2)

    def test_truncates_old_history(self):
        system = self._make("system", "You are an assistant.")
        old_msgs = [
            self._make("user", "old " * 200),
            self._make("assistant", "old " * 200),
        ] * 10   # lots of old history
        last_msg = self._make("user", "newest question")
        all_msgs = [system] + old_msgs + [last_msg]

        result = enforce_context_limit(all_msgs, context_window=1024, max_output_tokens=256)
        # System and last message must survive
        self.assertEqual(result[0].role, "system")
        self.assertEqual(result[-1].content, "newest question")

    def test_preserves_system_and_last(self):
        msgs = [
            self._make("system", "Be concise."),
            self._make("user", "A " * 500),
            self._make("assistant", "B " * 500),
            self._make("user", "final question"),
        ]
        result = enforce_context_limit(msgs, context_window=512, max_output_tokens=128)
        contents = [m.content for m in result]
        self.assertIn("Be concise.", contents)
        self.assertIn("final question", contents)

    def test_token_count_positive(self):
        self.assertGreater(count_tokens("Hello world"), 0)


class TestMachineConfig(unittest.TestCase):
    def test_server_defaults(self):
        cfg = MachineConfig.server_defaults()
        self.assertIn("11434", cfg.base_url)
        self.assertGreater(cfg.context_window, 0)

    def test_secondary_defaults(self):
        cfg = MachineConfig.secondary_defaults()
        self.assertGreater(cfg.max_output_tokens, 0)


# ═══════════════════════════════════════════════════════════════════════════════
# 2. MOCK INTEGRATION TESTS (network mocked via unittest.mock)
# ═══════════════════════════════════════════════════════════════════════════════

def _fake_response(content: str, prompt_tokens=10, completion_tokens=20) -> dict:
    return {
        "choices": [{"message": {"content": content}}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


class TestRouterMocked(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.router = ModelRouter(
            server_config=MachineConfig(
                base_url="http://mock-server:11434",
                model="qwen3:7b",
            ),
            secondary_config=MachineConfig(
                base_url="http://mock-secondary:11434",
                model="phi3:mini",
                context_window=4096,
            ),
        )
        await self.router.start()

    async def asyncTearDown(self):
        await self.router.shutdown()

    def _patch_client(self, machine: Machine, response_content: str):
        client = self.router._clients[machine]
        client.chat = AsyncMock(return_value=_fake_response(response_content))
        return client

    async def test_architect_routes_to_server(self):
        self._patch_client(Machine.SERVER, "Design complete.")
        resp = await self.router.complete_text(
            AgentRole.ARCHITECT, "Design a system."
        )
        self.assertEqual(resp.machine, Machine.SERVER)
        self.assertEqual(resp.raw_text, "Design complete.")

    async def test_critic_routes_to_secondary(self):
        self._patch_client(Machine.SECONDARY, "Score: 7/10")
        resp = await self.router.complete_text(
            AgentRole.CRITIC, "Evaluate this design."
        )
        self.assertEqual(resp.machine, Machine.SECONDARY)
        self.assertEqual(resp.raw_text, "Score: 7/10")

    async def test_json_extraction_on_response(self):
        payload = json.dumps({"score": 8, "feedback": "Good design."})
        self._patch_client(Machine.SECONDARY, payload)
        resp = await self.router.complete_json(
            AgentRole.CRITIC, "Evaluate this."
        )
        self.assertIsNotNone(resp.structured)
        self.assertEqual(resp.structured["score"], 8)

    async def test_json_extraction_from_fenced_response(self):
        payload = f'```json\n{{"services": ["auth", "billing"]}}\n```'
        self._patch_client(Machine.SERVER, payload)
        resp = await self.router.complete_json(
            AgentRole.ARCHITECT, "List services."
        )
        self.assertIn("auth", resp.structured["services"])

    async def test_token_usage_captured(self):
        self._patch_client(Machine.SERVER, "ok")
        resp = await self.router.complete_text(AgentRole.RESEARCHER, "Search X")
        self.assertEqual(resp.total_tokens, 30)

    async def test_json_instruction_injected(self):
        """System message must contain the JSON instruction when expect_json=True."""
        server_client = self.router._clients[Machine.SERVER]
        captured_messages: list = []

        async def capture_chat(messages, **kwargs):
            captured_messages.extend(messages)
            return _fake_response('{"ok": true}')

        server_client.chat = capture_chat
        await self.router.complete_json(AgentRole.ARCHITECT, "Design something.")

        system_content = captured_messages[0].content
        self.assertIn("JSON", system_content)


# ═══════════════════════════════════════════════════════════════════════════════
# 3. LIVE INTEGRATION TESTS (require running Ollama)
# ═══════════════════════════════════════════════════════════════════════════════

async def run_integration_tests(
    server_url: str,
    secondary_url: str,
    server_model: str,
    secondary_model: str,
) -> None:
    PASS = "✅"
    FAIL = "❌"
    SKIP = "⚠️ "

    results: list[tuple[str, str, str]] = []  # (name, status, detail)

    def record(name, passed, detail=""):
        status = PASS if passed else FAIL
        results.append((name, status, detail))
        icon = status
        print(f"  {icon}  {name}" + (f"  — {detail}" if detail else ""))

    print(f"\n{'═'*60}")
    print(f"  LIVE INTEGRATION TESTS")
    print(f"  Server:    {server_url}  [{server_model}]")
    print(f"  Secondary: {secondary_url}  [{secondary_model}]")
    print(f"{'═'*60}\n")

    router = ModelRouter(
        server_config=MachineConfig(
            base_url=server_url,
            model=server_model,
            context_window=8192,
            max_output_tokens=512,
            request_timeout=60,
        ),
        secondary_config=MachineConfig(
            base_url=secondary_url,
            model=secondary_model,
            context_window=4096,
            max_output_tokens=256,
            request_timeout=45,
        ),
        retry_config=RetryConfig(max_attempts=2),
    )
    await router.start()

    # ── 1. Health checks ─────────────────────────────────────────────────
    print("── Health checks")
    health = await router.health()
    for machine, alive in health.items():
        record(f"Health: {machine.value}", alive,
               "reachable" if alive else "UNREACHABLE — subsequent tests may fail")

    # ── 2. Basic text completions ────────────────────────────────────────
    print("\n── Text completions")
    for role in AgentRole:
        machine = ROLE_MACHINE_MAP[role]
        if not health.get(machine, False):
            record(f"Text completion [{role.value}]", False, "skipped (machine unreachable)")
            continue
        try:
            resp = await router.complete_text(
                role,
                "Reply with exactly three words: 'connection test passed'",
                system="You are a test assistant.",
                temperature=0.0,
            )
            ok = bool(resp.raw_text)
            record(
                f"Text completion [{role.value}] → {machine.value}",
                ok,
                f"{resp.total_tokens} tokens  {resp.elapsed_seconds:.1f}s",
            )
        except Exception as exc:
            record(f"Text completion [{role.value}]", False, str(exc))

    # ── 3. JSON completions ──────────────────────────────────────────────
    print("\n── JSON completions")
    json_prompt = (
        "Return a JSON object with keys: "
        '"status" (string "ok"), "value" (integer 42).'
    )
    for role in (AgentRole.ARCHITECT, AgentRole.CRITIC):
        machine = ROLE_MACHINE_MAP[role]
        if not health.get(machine, False):
            record(f"JSON completion [{role.value}]", False, "skipped")
            continue
        try:
            resp = await router.complete_json(
                role,
                json_prompt,
                schema_hint='{"status": "ok", "value": 42}',
                temperature=0.0,
            )
            parsed = resp.structured
            ok = (
                isinstance(parsed, dict)
                and parsed.get("status") == "ok"
                and parsed.get("value") == 42
            )
            record(
                f"JSON completion [{role.value}]",
                ok,
                f"parsed={parsed}",
            )
        except Exception as exc:
            record(f"JSON completion [{role.value}]", False, str(exc))

    # ── 4. Context enforcement ───────────────────────────────────────────
    print("\n── Context enforcement")
    if health.get(Machine.SERVER, False):
        try:
            long_history = (
                [Message("system", "You are Tinker.")] +
                [
                    Message("user" if i % 2 == 0 else "assistant", f"message {i} " + "word " * 50)
                    for i in range(40)
                ] +
                [Message("user", "What is 2+2?")]
            )
            req = ModelRequest(
                agent_role=AgentRole.ARCHITECT,
                messages=long_history,
                temperature=0.0,
            )
            resp = await router.complete(req)
            record("Context truncation (long history)", resp.ok,
                   f"{resp.total_tokens} tokens, elapsed {resp.elapsed_seconds:.1f}s")
        except Exception as exc:
            record("Context truncation", False, str(exc))

    # ── 5. Multi-turn conversation ───────────────────────────────────────
    print("\n── Multi-turn conversation")
    if health.get(Machine.SERVER, False):
        try:
            msgs = [
                Message("system", "You are a concise assistant."),
                Message("user", "My favourite number is 7. Remember it."),
                Message("assistant", "Noted, your favourite number is 7."),
                Message("user", "What is my favourite number?"),
            ]
            resp = await router.complete(
                ModelRequest(AgentRole.ARCHITECT, msgs, temperature=0.0)
            )
            ok = "7" in resp.raw_text
            record("Multi-turn memory", ok, f"response: {resp.raw_text[:80]!r}")
        except Exception as exc:
            record("Multi-turn memory", False, str(exc))

    # ── Summary ──────────────────────────────────────────────────────────
    await router.shutdown()
    total  = len(results)
    passed = sum(1 for _, s, _ in results if s == PASS)
    failed = sum(1 for _, s, _ in results if s == FAIL)

    print(f"\n{'═'*60}")
    print(f"  Results: {passed}/{total} passed   {failed} failed")
    print(f"{'═'*60}\n")

    if failed:
        sys.exit(1)


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Entry point
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    import os

    parser = argparse.ArgumentParser(description="Tinker Model Client Test Harness")
    parser.add_argument("--unit-only",    action="store_true", help="Only run unit tests (no network)")
    parser.add_argument("--integration",  action="store_true", help="Run live integration tests")
    parser.add_argument("--server-url",   default=os.getenv("TINKER_SERVER_URL",    "http://localhost:11434"))
    parser.add_argument("--secondary-url",default=os.getenv("TINKER_SECONDARY_URL", "http://secondary:11434"))
    parser.add_argument("--server-model", default=os.getenv("TINKER_SERVER_MODEL",  "qwen3:7b"))
    parser.add_argument("--secondary-model", default=os.getenv("TINKER_SECONDARY_MODEL", "phi3:mini"))
    args = parser.parse_args()

    # ── Unit + mock tests (always run unless --integration-only) ──────────
    if not args.integration:
        print("\n══ Unit + Mock Tests ══════════════════════════════════════\n")
        loader = unittest.TestLoader()
        suite  = unittest.TestSuite()
        for cls in (TestRoleRouting, TestJsonExtraction, TestContextEnforcement,
                    TestMachineConfig, TestRouterMocked):
            suite.addTests(loader.loadTestsFromTestCase(cls))
        runner = unittest.TextTestRunner(verbosity=2)
        result = runner.run(suite)
        if not result.wasSuccessful() and not args.integration:
            sys.exit(1)
        if args.unit_only:
            return

    # ── Live integration tests ─────────────────────────────────────────────
    if args.integration or not args.unit_only:
        asyncio.run(run_integration_tests(
            server_url=args.server_url,
            secondary_url=args.secondary_url,
            server_model=args.server_model,
            secondary_model=args.secondary_model,
        ))


if __name__ == "__main__":
    main()
