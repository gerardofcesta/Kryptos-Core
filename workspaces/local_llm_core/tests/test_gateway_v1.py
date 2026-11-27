"""Comprehensive tests for FastAPI Core, Unified OpenAI Gateway (/v1/*), Cascade Router, Cost Ledger & Strict CSP."""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import re
import tempfile
import unittest
from collections.abc import AsyncIterator
from typing import Any
from unittest import mock

import httpx
from fastapi.testclient import TestClient
from workspaces.local_llm_core.app import create_app
from workspaces.local_llm_core.config import ENDPOINTS_JSON_FILE
from workspaces.local_llm_core.engine.backends import assert_loopback_url
from workspaces.local_llm_core.engine.coordinator import get_gateway_coordinator
from workspaces.local_llm_core.security import rotate_gateway_token
from workspaces.local_llm_core.services.ledger import get_cost_ledger
from workspaces.local_llm_core.services.telemetry import invalidate_telemetry_cache


class TestUnifiedGatewayV1(unittest.TestCase):
    """Integration tests against FastAPI TestClient & httpx.ASGITransport with mocked backends."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._token_file = pathlib.Path(self._tmpdir.name) / "LocalLLM" / "gateway.token"
        self._env_patch = mock.patch.dict(
            os.environ,
            {"LOCAL_LLM_GATEWAY_TOKEN_FILE": str(self._token_file)},
            clear=False,
        )
        self._env_patch.start()
        self.app = create_app()
        self.token = rotate_gateway_token()
        get_cost_ledger().reset()
        invalidate_telemetry_cache()
        self.client = TestClient(self.app, base_url="http://127.0.0.1:8787")

    def tearDown(self) -> None:
        self.client.close()
        get_cost_ledger().reset()
        invalidate_telemetry_cache()
        self._env_patch.stop()
        self._tmpdir.cleanup()

    def _auth_headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        hdrs = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        if extra:
            hdrs.update(extra)
        return hdrs

    def test_security_controls_on_v1_and_api_routes(self) -> None:
        # 1. Host allowlist blocks DNS rebinding on both /v1/* and /api/*
        for bad_host in ("evil.attacker.com", "evil.attacker.com:8787", "127.0.0.1:9999"):
            r_v1 = self.client.get(
                "/v1/models",
                headers={"Host": bad_host, "Authorization": f"Bearer {self.token}"},
            )
            self.assertEqual(r_v1.status_code, 403, f"Expected 403 for Host={bad_host!r}")

        # 2. Cross-site Origin / Sec-Fetch-Site blocked with 403
        r_csrf = self.client.post(
            "/v1/chat/completions",
            headers={
                "Origin": "https://evil.attacker.com",
                "Sec-Fetch-Site": "cross-site",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.token}",
            },
            content=b'{"model":"local-fast","messages":[{"role":"user","content":"hi"}]}',
        )
        self.assertEqual(r_csrf.status_code, 403)

        # 3. 415 enforced on non-JSON POST to /v1/* and /api/*
        for path in ("/v1/chat/completions", "/v1/embeddings", "/api/control/engine"):
            r_415 = self.client.post(
                path,
                headers={"Authorization": f"Bearer {self.token}", "Content-Type": "text/plain"},
                content=b"{}",
            )
            self.assertEqual(r_415.status_code, 415, f"Expected 415 on {path}")

        # 4. Missing or invalid token returns 401 + WWW-Authenticate: Bearer
        r_no_tok = self.client.get("/v1/health")
        self.assertEqual(r_no_tok.status_code, 401)
        self.assertEqual(r_no_tok.headers.get("www-authenticate"), "Bearer")

        # 5. GET /api/auth/session-token requires Sec-Fetch-Site: same-origin
        self.assertEqual(self.client.get("/api/auth/session-token").status_code, 403)
        r_sess = self.client.get(
            "/api/auth/session-token",
            headers={"Origin": "http://127.0.0.1:8787", "Sec-Fetch-Site": "same-origin"},
        )
        self.assertEqual(r_sess.status_code, 200)
        self.assertIn("no-store", r_sess.headers.get("cache-control", ""))
        self.assertEqual(r_sess.json()["token"], self.token)

        # 6. Non-loopback backend URL blocked
        with self.assertRaises(ValueError):
            assert_loopback_url("https://api.openai.com/v1")

    def test_static_es_modules_strict_csp_and_endpoints_json(self) -> None:
        # endpoints.json registers unifiedGateway
        ep_data = json.loads(ENDPOINTS_JSON_FILE.read_text(encoding="utf-8"))
        ug = ep_data["endpoints"]["unifiedGateway"]
        self.assertTrue(ug["enabled"])
        self.assertEqual(ug["openaiCompatibleUrl"], "http://127.0.0.1:8787/v1")

        # Root HTML has strict CSP without 'unsafe-inline' in script-src and zero inline scripts/styles
        r_idx = self.client.get("/")
        self.assertEqual(r_idx.status_code, 200)
        csp = r_idx.headers.get("content-security-policy", "")
        self.assertIn("script-src 'self'", csp)
        script_src_part = [p.strip() for p in csp.split(";") if "script-src" in p][0]
        self.assertNotIn("'unsafe-inline'", script_src_part)
        self.assertIn("object-src 'none'", csp)
        self.assertIn("frame-ancestors 'none'", csp)

        html = r_idx.text
        self.assertIn('/static/app.js', html)
        self.assertIn('/static/styles.css', html)
        self.assertIsNone(re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html, re.IGNORECASE))
        self.assertIsNone(re.search(r"<style\b", html, re.IGNORECASE))

        # Static assets served cleanly with no-store
        r_js = self.client.get("/static/app.js")
        self.assertEqual(r_js.status_code, 200)
        self.assertIn("javascript", r_js.headers.get("content-type", ""))
        self.assertIn("/api/system/telemetry/stream", r_js.text)

        r_css = self.client.get("/static/styles.css")
        self.assertEqual(r_css.status_code, 200)
        self.assertIn("text/css", r_css.headers.get("content-type", ""))

        r_cfg = self.client.get("/api/system/config", headers={"X-Local-LLM-Token": self.token})
        self.assertEqual(r_cfg.status_code, 200)
        self.assertEqual(r_cfg.json()["unifiedOpenAIUrl"], "http://127.0.0.1:8787/v1")

    def test_v1_health_and_models(self) -> None:
        coord = get_gateway_coordinator()
        with (
            mock.patch.object(coord, "get_active_tier", new=mock.AsyncMock(return_value="fast")),
            mock.patch.object(
                coord.ollama, "list_models", new=mock.AsyncMock(return_value=["local-worker:latest"])
            ),
            mock.patch.object(coord.colibri, "list_models", new=mock.AsyncMock(return_value=[])),
            mock.patch.object(coord.lmstudio, "list_models", new=mock.AsyncMock(return_value=[])),
            mock.patch(
                "workspaces.local_llm_core.routes.openai_v1.async_get_full_telemetry",
                new=mock.AsyncMock(return_value={"backends": {"ollama": {"online": True}}}),
            ),
        ):
            r_health = self.client.get("/v1/health", headers={"Authorization": f"Bearer {self.token}"})
            self.assertEqual(r_health.status_code, 200)
            h = r_health.json()
            self.assertTrue(h["ok"])
            self.assertEqual(h["active_tier"], "fast")
            self.assertEqual(h["retention_mode"], "ephemeral")
            self.assertIn("auto", h["aliases"])

            r_models = self.client.get("/v1/models", headers={"X-Local-LLM-Token": self.token})
            self.assertEqual(r_models.status_code, 200)
            ids = [m["id"] for m in r_models.json()["data"]]
            for alias in ("auto", "local-fast", "local-balanced", "local-heavy", "local-worker:latest"):
                self.assertIn(alias, ids)

    def test_v1_chat_sync_json_schema_tools_and_ephemeral_header(self) -> None:
        coord = get_gateway_coordinator()
        captured_payloads: list[dict[str, Any]] = []

        async def fake_ollama_chat(payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
            captured_payloads.append(payload)
            return 200, {
                "id": "chatcmpl-1",
                "object": "chat.completion",
                "model": payload["model"],
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": '{"topic":"AI","score":9}'},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 25, "completion_tokens": 12, "total_tokens": 37},
            }

        with (
            mock.patch.object(coord, "get_active_tier", new=mock.AsyncMock(return_value="fast")),
            mock.patch.object(coord.ollama, "chat_completions", side_effect=fake_ollama_chat),
        ):
            resp = self.client.post(
                "/v1/chat/completions",
                headers=self._auth_headers({"X-App-Id": "rag-indexer", "X-Workflow-Id": "wf-schema"}),
                json={
                    "model": "local-fast",
                    "messages": [{"role": "user", "content": "Classify"}],
                    "response_format": {
                        "type": "json_schema",
                        "json_schema": {
                            "name": "Classification",
                            "strict": True,
                            "schema": {
                                "type": "object",
                                "required": ["topic", "score"],
                                "properties": {
                                    "topic": {"type": "string"},
                                    "score": {"type": "integer", "minimum": 1},
                                },
                            },
                        },
                    },
                    "tools": [
                        {
                            "type": "function",
                            "function": {"name": "lookup", "parameters": {"type": "object"}},
                        }
                    ],
                },
            )
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.headers.get("x-retention-mode"), "ephemeral")
            self.assertEqual(resp.headers.get("x-resolved-model"), "local-fast")
            self.assertEqual(resp.headers.get("x-schema-valid"), "true")
            self.assertEqual(captured_payloads[0]["model"], "local-worker:latest")
            self.assertIn("tools", captured_payloads[0])

    def test_tier_switch_policy_409_and_orchestrator_503(self) -> None:
        coord = get_gateway_coordinator()

        # 1. Fast active -> local-heavy without X-Allow-Tier-Switch returns 409
        with mock.patch.object(coord, "get_active_tier", new=mock.AsyncMock(return_value="fast")):
            r_409 = self.client.post(
                "/v1/chat/completions",
                headers=self._auth_headers(),
                json={"model": "local-heavy", "messages": [{"role": "user", "content": "hello"}]},
            )
            self.assertEqual(r_409.status_code, 409)
            self.assertEqual(r_409.json()["error"]["code"], "tier_switch_not_allowed")

        # 2. Orchestrator active -> returns 503 with Retry-After
        with (
            mock.patch.object(coord, "get_active_tier", new=mock.AsyncMock(return_value="fast")),
            mock.patch(
                "workspaces.local_llm_core.engine.coordinator.get_orchestrator_telemetry_summary",
                return_value={"active": True},
            ),
        ):
            r_503 = self.client.post(
                "/v1/chat/completions",
                headers=self._auth_headers({"X-Allow-Tier-Switch": "true"}),
                json={"model": "local-heavy", "messages": [{"role": "user", "content": "hello"}]},
            )
            self.assertEqual(r_503.status_code, 503)
            self.assertEqual(r_503.headers.get("retry-after"), "30")

    def test_sse_streaming_and_keepalive_during_heavy_tier_switch(self) -> None:
        coord = get_gateway_coordinator()
        state = {"tier": "fast"}

        async def fake_active_tier() -> str:
            return state["tier"]

        def fake_switch_sync(target_tier: str) -> tuple[bool, str]:
            state["tier"] = target_tier
            return True, "switched"

        async def fake_colibri_stream(payload: dict[str, Any]) -> AsyncIterator[bytes]:
            yield b'data: {"choices":[{"delta":{"content":"Heavy "}}]}\n\n'
            yield b'data: {"choices":[{"delta":{"content":"answer"}}],"usage":{"prompt_tokens":10,"completion_tokens":5}}\n\n'
            yield b"data: [DONE]\n\n"

        with (
            mock.patch.object(coord, "get_active_tier", side_effect=fake_active_tier),
            mock.patch.object(coord, "_switch_tier_sync", side_effect=fake_switch_sync),
            mock.patch.object(coord.colibri, "chat_completions_stream", side_effect=fake_colibri_stream),
        ):
            r_sse = self.client.post(
                "/v1/chat/completions",
                headers=self._auth_headers(
                    {
                        "X-Allow-Tier-Switch": "true",
                        "X-App-Id": "doc-writer",
                        "X-Workflow-Id": "sse-heavy",
                    }
                ),
                json={
                    "model": "local-heavy",
                    "stream": True,
                    "messages": [{"role": "user", "content": "Deep analysis"}],
                },
            )
            self.assertEqual(r_sse.status_code, 200)
            self.assertIn("text/event-stream", r_sse.headers.get("content-type", ""))
            body = r_sse.text
            self.assertIn(": switching-tier heavy", body)
            self.assertIn("Heavy ", body)
            self.assertIn("data: [DONE]", body)
            self.assertEqual(state["tier"], "heavy")

        snap = get_cost_ledger().get_snapshot(app_id="doc-writer", workflow_id="sse-heavy")
        self.assertEqual(snap["totals"]["total_tokens"], 15)

    def test_auto_cascade_router_escalates_on_schema_failure(self) -> None:
        coord = get_gateway_coordinator()
        calls: list[str] = []

        async def fake_fast_tier_chat(payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
            model = payload["model"]
            calls.append(model)
            if "worker" in model:
                # Invalid JSON triggers cascade escalation to local-balanced
                return 200, {
                    "choices": [{"message": {"role": "assistant", "content": "not valid json"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 12, "completion_tokens": 4},
                }
            # local-balanced succeeds with valid JSON matching schema
            return 200, {
                "choices": [{"message": {"role": "assistant", "content": '{"ok":true}'}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 14, "completion_tokens": 6},
            }

        with (
            mock.patch.object(coord, "get_active_tier", new=mock.AsyncMock(return_value="fast")),
            mock.patch.object(coord.ollama, "chat_completions", side_effect=fake_fast_tier_chat),
        ):
            r = self.client.post(
                "/v1/chat/completions",
                headers=self._auth_headers({"X-App-Id": "ledger-app", "X-Workflow-Id": "txn-classify"}),
                json={
                    "model": "auto",
                    "messages": [{"role": "user", "content": "Return JSON"}],
                    "response_format": {
                        "type": "json_schema",
                        "json_schema": {
                            "name": "Result",
                            "schema": {"type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}}},
                        },
                    },
                },
            )
            self.assertEqual(r.status_code, 200)
            self.assertEqual(calls, ["local-worker:latest", "local-uncensored:latest"])
            self.assertEqual(r.headers.get("x-resolved-model"), "local-balanced")
            self.assertEqual(r.headers.get("x-cascade-escalated-from"), "local-fast")
            self.assertEqual(r.headers.get("x-cascade-attempts"), "2")

        snap = get_cost_ledger().get_snapshot(app_id="ledger-app", workflow_id="txn-classify")
        self.assertEqual(snap["totals"]["total_tokens"], 36)
        self.assertEqual(snap["totals"]["retries"], 1)
        self.assertEqual(snap["workflows"][0]["escalations"], 1)

    def test_v1_embeddings_and_ledger_budget_cap_429(self) -> None:
        coord = get_gateway_coordinator()
        get_cost_ledger().set_budget("rag-indexer", "ingest-1", max_tokens=20, max_cost_usd=5.0)

        async def fake_embed(payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
            return 200, {
                "object": "list",
                "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2, 0.3]}],
                "model": payload["model"],
                "usage": {"prompt_tokens": 25, "total_tokens": 25},
            }

        with (
            mock.patch.object(coord, "get_active_tier", new=mock.AsyncMock(return_value="fast")),
            mock.patch.object(coord.ollama, "embeddings", side_effect=fake_embed),
        ):
            r1 = self.client.post(
                "/v1/embeddings",
                headers=self._auth_headers({"X-App-Id": "rag-indexer", "X-Workflow-Id": "ingest-1"}),
                json={"model": "local-fast", "input": "Hello local RAG"},
            )
            self.assertEqual(r1.status_code, 200)
            self.assertEqual(r1.json()["data"][0]["embedding"], [0.1, 0.2, 0.3])

            # Second request exceeds max_tokens=20 -> 429 budget_exceeded
            r2 = self.client.post(
                "/v1/embeddings",
                headers=self._auth_headers({"X-App-Id": "rag-indexer", "X-Workflow-Id": "ingest-1"}),
                json={"model": "local-fast", "input": "Blocked request"},
            )
            self.assertEqual(r2.status_code, 429)
            self.assertEqual(r2.json()["error"]["code"], "budget_exceeded")

        # GET /api/ledger reports RAM-only totals
        r_led = self.client.get(
            "/api/ledger?app_id=rag-indexer&workflow_id=ingest-1",
            headers={"Authorization": f"Bearer {self.token}"},
        )
        self.assertEqual(r_led.status_code, 200)
        led = r_led.json()
        self.assertEqual(led["storage"], "ram-only")
        self.assertTrue(led["workflows"][0]["budget"]["exceeded"])

    def test_asgi_transport_async_chat_completion(self) -> None:
        coord = get_gateway_coordinator()

        async def run_async_check() -> None:
            transport = httpx.ASGITransport(app=self.app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://127.0.0.1:8787"
            ) as ac:
                resp = await ac.post(
                    "/v1/chat/completions",
                    headers=self._auth_headers(),
                    json={
                        "model": "local-fast",
                        "messages": [{"role": "user", "content": "Ping ASGI"}],
                    },
                )
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(resp.json()["choices"][0]["message"]["content"], "ASGI ok")

        with (
            mock.patch.object(coord, "get_active_tier", new=mock.AsyncMock(return_value="fast")),
            mock.patch.object(
                coord.ollama,
                "chat_completions",
                new=mock.AsyncMock(
                    return_value=(
                        200,
                        {
                            "choices": [{"message": {"role": "assistant", "content": "ASGI ok"}}],
                            "usage": {"prompt_tokens": 4, "completion_tokens": 2},
                        },
                    )
                ),
            ),
        ):
            asyncio.run(run_async_check())


if __name__ == "__main__":
    unittest.main()
