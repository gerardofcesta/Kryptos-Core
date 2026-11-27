"""WU-22 verification tests: API-UI contract parity, UNVERIFIED_NO_MANIFEST, SSE stage heartbeats, /api/benchmark/run, and SPA parity."""

from __future__ import annotations

import asyncio
import hashlib
import os
import pathlib
import tempfile
import unittest
from collections.abc import AsyncIterator
from typing import Any
from unittest import mock

import httpx
from fastapi.testclient import TestClient
from workspaces.local_llm_core.app import create_app
from workspaces.local_llm_core.config import WEB_DIST_DIR
from workspaces.local_llm_core.engine.coordinator import get_gateway_coordinator
from workspaces.local_llm_core.security import rotate_gateway_token
from workspaces.local_llm_core.services.ledger import get_cost_ledger
from workspaces.local_llm_core.services.telemetry import (
    get_backends_status,
    get_full_telemetry,
    get_gpu_telemetry,
    get_local_registry,
    get_ram_telemetry,
    invalidate_telemetry_cache,
)


class TestWu22ContractsAndBenchmark(unittest.TestCase):
    """Comprehensive tests for WU-22 schema parity, SSE stages, and benchmark endpoint."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_root = pathlib.Path(self._tmpdir.name)
        self._token_file = self.tmp_root / "LocalLLM" / "gateway.token"
        self._models_dir = self.tmp_root / "models"
        self._models_dir.mkdir(parents=True, exist_ok=True)
        self._env_patch = mock.patch.dict(
            os.environ,
            {
                "LOCAL_LLM_GATEWAY_TOKEN_FILE": str(self._token_file),
                "LOCAL_LLM_MODELS_DIR": str(self._models_dir),
            },
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

    # ------------------------------------------------------------------
    # Micro-Step 22.1: Schema Drift & Contract Parity + UNVERIFIED_NO_MANIFEST
    # ------------------------------------------------------------------
    def test_telemetry_gpu_ram_backends_and_registry_contract_parity(self) -> None:
        # 1. GPU telemetry: mocked nvidia-smi exposes both legacy & canonical frontend fields
        fake_proc = mock.Mock(
            returncode=0,
            stdout="Test GPU 16GB, 4096, 12288, 16384, 52, 84.5, 37\n",
        )
        with (
            mock.patch("workspaces.local_llm_core.services.telemetry.shutil.which", return_value="nvidia-smi"),
            mock.patch("workspaces.local_llm_core.services.telemetry.subprocess.run", return_value=fake_proc),
        ):
            gpu = get_gpu_telemetry()

        self.assertTrue(gpu["available"])
        self.assertEqual(gpu["memoryUsedGb"], gpu["vramUsedGb"])
        self.assertEqual(gpu["memoryFreeGb"], gpu["vramFreeGb"])
        self.assertEqual(gpu["memoryTotalGb"], gpu["vramTotalGb"])
        self.assertEqual(gpu["memoryUsedPct"], gpu["vramUsedPct"])
        self.assertEqual(gpu["temperatureC"], gpu["tempC"])
        self.assertEqual(gpu["powerDrawW"], gpu["powerW"])
        self.assertEqual(gpu["utilizationPct"], gpu["utilPct"])
        self.assertEqual(gpu["vramUsedGb"], 4.0)
        self.assertEqual(gpu["vramTotalGb"], 16.0)
        self.assertEqual(gpu["tempC"], 52.0)

        # Fallback GPU still defines all canonical keys
        with mock.patch("workspaces.local_llm_core.services.telemetry.shutil.which", return_value=None):
            gpu_fb = get_gpu_telemetry()
        for key in ("vramUsedGb", "vramFreeGb", "vramTotalGb", "vramUsedPct", "tempC", "powerW", "utilPct"):
            self.assertIn(key, gpu_fb)

        # 2. RAM telemetry: heavyMoeSafe aliases heavyReady + heavyMoeMinFreeGb numeric
        ram = get_ram_telemetry()
        self.assertIn("heavyReady", ram)
        self.assertIn("heavyMoeSafe", ram)
        self.assertEqual(ram["heavyMoeSafe"], ram["heavyReady"])
        self.assertIsInstance(ram["heavyMoeMinFreeGb"], (int, float))

        # 3. Backends telemetry: backends.heavy aliases backends.heavyMoe
        def fake_http(host: str, port: int, method: str, path: str, timeout: float = 0.9) -> tuple[bool, int, Any, str | None]:
            if port == 11434 and path == "/api/tags":
                return True, 200, {"models": [{"name": "local-worker:latest", "size": 9 * (1024**3)}]}, None
            if port == 11434 and path == "/api/ps":
                return True, 200, {"models": []}, None
            if port == 11435 and path == "/health":
                return True, 200, {"status": "ok"}, None
            if port == 11435 and path == "/v1/models":
                return True, 200, {"data": [{"id": "heavy-moe-uncensored"}]}, None
            return False, 0, None, "offline"

        backends = get_backends_status(http_json_fn=fake_http)
        self.assertEqual(backends["heavy"], backends["heavyMoe"])
        self.assertTrue(backends["heavy"]["online"])
        self.assertEqual(backends["heavy"]["activeModel"], "heavy-moe-uncensored")

        # Full telemetry endpoint exposes aligned schema
        full = get_full_telemetry(
            force_refresh=True,
            http_json_fn=fake_http,
            gpu_fn=lambda: gpu,
            orch_summary_fn=lambda: {"active": False, "state": "IDLE"},
        )
        self.assertEqual(full["backends"]["heavy"]["online"], True)
        self.assertEqual(full["gpu"]["vramTotalGb"], 16.0)
        self.assertIn("heavyMoeSafe", full["ram"])

        # 4. Registry listing exposes size_gb and heavy aliasing sizeGb and heavyMoe
        heavy_dir = self.tmp_root / "heavy_moe"
        heavy_dir.mkdir()
        (heavy_dir / "moe-q4.gguf").write_bytes(b"GGUF" * 64)
        with mock.patch("workspaces.local_llm_core.services.telemetry.HEAVY_MOE_DIR", heavy_dir):
            reg = get_local_registry(http_json_fn=fake_http)
        self.assertEqual(reg["ollama"]["models"][0]["size_gb"], reg["ollama"]["models"][0]["sizeGb"])
        self.assertEqual(reg["heavy"], reg["heavyMoe"])
        self.assertEqual(reg["heavy"]["files"][0]["size_gb"], reg["heavy"]["files"][0]["sizeGb"])
        self.assertEqual(reg["heavy"]["files"][0]["rel_path"], "moe-q4.gguf")

    def test_ledger_entries_alias_and_totals_escalations(self) -> None:
        ledger = get_cost_ledger()
        ledger.record_usage(
            "ui-test",
            "wf-1",
            model_alias="local-fast",
            prompt_tokens=100,
            completion_tokens=40,
            retries=1,
            escalated=True,
        )
        ledger.record_usage(
            "ui-test",
            "wf-2",
            model_alias="local-heavy",
            prompt_tokens=200,
            completion_tokens=80,
            retries=0,
            escalated=True,
        )
        r_led = self.client.get("/api/ledger", headers={"Authorization": f"Bearer {self.token}"})
        self.assertEqual(r_led.status_code, 200)
        data = r_led.json()
        self.assertEqual(data["totals"]["escalations"], 2)
        self.assertEqual(data["entries"], data["workflows"])
        self.assertEqual(len(data["entries"]), 2)

    def test_model_manager_unverified_no_manifest_and_contract_fields(self) -> None:
        sub = self._models_dir / "qwen"
        sub.mkdir()
        weight_path = sub / "qwen3-14b.gguf"
        raw = b"GGUF_NO_MANIFEST_SAMPLE_" * 64
        weight_path.write_bytes(raw)
        expected_digest = hashlib.sha256(raw).hexdigest()

        r_inv = self.client.get("/api/models/inventory", headers={"Authorization": f"Bearer {self.token}"})
        self.assertEqual(r_inv.status_code, 200)
        inv_item = r_inv.json()["models"][0]
        self.assertEqual(inv_item["rel_path"], "qwen/qwen3-14b.gguf")
        self.assertEqual(inv_item["relative_path"], "qwen/qwen3-14b.gguf")
        self.assertEqual(inv_item["ext"], "gguf")
        self.assertIn("size_gb", inv_item)

        # Missing expected_sha256 + no manifest returns 200 UNVERIFIED_NO_MANIFEST with computed digest
        r_ver = self.client.post(
            "/api/models/verify",
            headers=self._auth_headers(),
            json={"path": "qwen/qwen3-14b.gguf", "expected_sha256": None},
        )
        self.assertEqual(r_ver.status_code, 200, r_ver.text)
        ver = r_ver.json()
        self.assertTrue(ver["ok"])
        self.assertFalse(ver["verified"])
        self.assertEqual(ver["status"], "UNVERIFIED_NO_MANIFEST")
        self.assertEqual(ver["computed_sha256"], expected_digest)
        self.assertEqual(ver["actual_sha256"], expected_digest)
        self.assertEqual(ver["rel_path"], "qwen/qwen3-14b.gguf")

        # Malformed non-empty expected_sha256 still returns 400
        r_bad = self.client.post(
            "/api/models/verify",
            headers=self._auth_headers(),
            json={"path": "qwen/qwen3-14b.gguf", "expected_sha256": "invalid-short"},
        )
        self.assertEqual(r_bad.status_code, 400)

    # ------------------------------------------------------------------
    # Micro-Step 22.2: SSE Stage Heartbeats (even when needs_switch is False)
    # ------------------------------------------------------------------
    def test_sse_streaming_emits_stage_heartbeats_without_tier_switch(self) -> None:
        coord = get_gateway_coordinator()

        async def slow_first_chunk_stream(payload: dict[str, Any]) -> AsyncIterator[bytes]:
            await asyncio.sleep(1.15)
            yield b'data: {"choices":[{"delta":{"reasoning_content":"Step 1"}}]}\n\n'
            yield b'data: {"choices":[{"delta":{"content":"Final answer"}}],"usage":{"prompt_tokens":10,"completion_tokens":8}}\n\n'
            yield b"data: [DONE]\n\n"

        with (
            mock.patch.object(coord, "get_active_tier", new=mock.AsyncMock(return_value="fast")),
            mock.patch.object(coord.ollama, "chat_completions_stream", side_effect=slow_first_chunk_stream),
        ):
            r_sse = self.client.post(
                "/v1/chat/completions",
                headers=self._auth_headers(),
                json={
                    "model": "local-fast",
                    "stream": True,
                    "messages": [{"role": "user", "content": "Hello"}],
                },
            )
        self.assertEqual(r_sse.status_code, 200)
        body = r_sse.text
        self.assertIn(": stage=checking-backend", body)
        self.assertIn(": stage=loading-model", body)
        self.assertIn(": stage=prefilling", body)
        self.assertIn(": stage=streaming", body)
        self.assertIn(": elapsed_ms=", body)
        self.assertIn("Final answer", body)
        # Ensure at least one 1s heartbeat was emitted while awaiting the delayed first chunk
        self.assertGreaterEqual(body.count(": stage=loading-model"), 2)

    # ------------------------------------------------------------------
    # Micro-Step 22.3: POST /api/benchmark/run
    # ------------------------------------------------------------------
    def test_benchmark_run_thinking_tokens_cold_warm_and_offline_errors(self) -> None:
        coord = get_gateway_coordinator()

        # 1. Backend offline -> explicit 503 OFFLINE error (never 0.0 tok/s)
        with (
            mock.patch.object(coord, "get_active_tier", new=mock.AsyncMock(return_value="fast")),
            mock.patch.object(coord.ollama, "health", new=mock.AsyncMock(return_value=False)),
            mock.patch.object(coord.ollama, "list_models", new=mock.AsyncMock(return_value=[])),
        ):
            r_off = self.client.post(
                "/api/benchmark/run",
                headers=self._auth_headers(),
                json={"model": "local-fast"},
            )
        self.assertEqual(r_off.status_code, 503)
        off_json = r_off.json()
        self.assertFalse(off_json["ok"])
        self.assertEqual(off_json["status"], "OFFLINE")
        self.assertIn("offline", off_json["error"].lower())

        # 2. Missing model -> explicit 404 MODEL_NOT_FOUND
        with (
            mock.patch.object(coord, "get_active_tier", new=mock.AsyncMock(return_value="fast")),
            mock.patch.object(coord.ollama, "health", new=mock.AsyncMock(return_value=True)),
            mock.patch.object(coord.ollama, "list_models", new=mock.AsyncMock(return_value=["other:7b"])),
        ):
            r_missing = self.client.post(
                "/api/benchmark/run",
                headers=self._auth_headers(),
                json={"model": "local-fast"},
            )
        self.assertEqual(r_missing.status_code, 404)
        self.assertEqual(r_missing.json()["status"], "MODEL_NOT_FOUND")

        # 3. Thinking model (reasoning_content + inline <think> + visible) with cold tier switch
        async def fake_thinking_stream(payload: dict[str, Any]) -> AsyncIterator[bytes]:
            yield b'data: {"choices":[{"delta":{"reasoning_content":"Analyzing loopback isolation carefully. "}}]}\n\n'
            yield b'data: {"choices":[{"delta":{"content":"<think>Double check</think>Loopback binds 127.0.0.1 only."}}],"usage":{"prompt_tokens":20,"completion_tokens":30}}\n\n'
            yield b"data: [DONE]\n\n"

        with (
            mock.patch.object(coord, "ensure_tier", new=mock.AsyncMock(return_value=True)),
            mock.patch.object(coord.ollama, "health", new=mock.AsyncMock(return_value=True)),
            mock.patch.object(coord.ollama, "list_models", new=mock.AsyncMock(return_value=["local-worker:latest"])),
            mock.patch.object(coord.ollama, "chat_completions_stream", side_effect=fake_thinking_stream),
        ):
            r_cold = self.client.post(
                "/api/benchmark/run",
                headers=self._auth_headers(),
                json={"model": "local-fast", "max_tokens": 128, "warmup": False},
            )
        self.assertEqual(r_cold.status_code, 200, r_cold.text)
        cold_json = r_cold.json()
        self.assertTrue(cold_json["ok"])
        self.assertEqual(cold_json["status"], "OK")
        self.assertTrue(cold_json["cold_start"])
        self.assertGreater(cold_json["thinking_tokens"], 0)
        self.assertGreater(cold_json["visible_tokens"], 0)
        self.assertEqual(cold_json["total_tokens"], 30)
        self.assertGreater(cold_json["tok_per_sec"], 0.0)
        self.assertGreater(cold_json["thinking_tok_s"], 0.0)
        self.assertGreater(cold_json["visible_tok_s"], 0.0)
        self.assertIsNotNone(cold_json["ttft_ms"])
        self.assertIn("hardware_summary", cold_json)
        self.assertIn("timestamp", cold_json)

        # 4. Warm run (warmup=True) with reasoning-only stream still reports >0 tok/s
        async def fake_reasoning_only_stream(payload: dict[str, Any]) -> AsyncIterator[bytes]:
            if payload.get("max_tokens") == 4:
                yield b'data: {"choices":[{"delta":{"content":"OK"}}]}\n\n'
                yield b"data: [DONE]\n\n"
                return
            yield b'data: {"choices":[{"delta":{"thinking":"Deep internal reasoning without visible output yet."}}]}\n\n'
            yield b"data: [DONE]\n\n"

        with (
            mock.patch.object(coord, "ensure_tier", new=mock.AsyncMock(return_value=False)),
            mock.patch.object(coord.ollama, "health", new=mock.AsyncMock(return_value=True)),
            mock.patch.object(coord.ollama, "list_models", new=mock.AsyncMock(return_value=["local-worker:latest"])),
            mock.patch.object(coord.ollama, "chat_completions_stream", side_effect=fake_reasoning_only_stream),
        ):
            r_warm = self.client.post(
                "/api/benchmark/run",
                headers=self._auth_headers(),
                json={"model": "local-fast", "max_tokens": 128, "warmup": True},
            )
        self.assertEqual(r_warm.status_code, 200, r_warm.text)
        warm_json = r_warm.json()
        self.assertFalse(warm_json["cold_start"])
        self.assertGreater(warm_json["thinking_tokens"], 0)
        self.assertEqual(warm_json["visible_tokens"], 0)
        self.assertGreater(warm_json["tok_per_sec"], 0.0)

        # 5. Connection error mid-stream -> explicit 503 error
        async def broken_stream(payload: dict[str, Any]) -> AsyncIterator[bytes]:
            raise httpx.ConnectError("Connection refused")
            yield b""  # pragma: no cover

        with (
            mock.patch.object(coord, "ensure_tier", new=mock.AsyncMock(return_value=False)),
            mock.patch.object(coord.ollama, "health", new=mock.AsyncMock(return_value=True)),
            mock.patch.object(coord.ollama, "list_models", new=mock.AsyncMock(return_value=["local-worker:latest"])),
            mock.patch.object(coord.ollama, "chat_completions_stream", side_effect=broken_stream),
        ):
            r_err = self.client.post(
                "/api/benchmark/run",
                headers=self._auth_headers(),
                json={"model": "local-fast"},
            )
        self.assertEqual(r_err.status_code, 503)
        self.assertEqual(r_err.json()["status"], "OFFLINE")

    # ------------------------------------------------------------------
    # SPA Bundle Parity
    # ------------------------------------------------------------------
    def test_spa_dist_bundle_contains_wu22_features(self) -> None:
        app_js = (WEB_DIST_DIR / "static" / "app.js").read_text(encoding="utf-8", errors="replace")
        for expected_marker in (
            "/api/benchmark/run",
            "UNVERIFIED_NO_MANIFEST",
            "vramUsedGb",
            "heavyMoeSafe",
            "Loading into VRAM",
            "Download JSON",
            "Download Markdown",
        ):
            self.assertIn(expected_marker, app_js)
        self.assertNotIn("unsafe-inline", app_js)


if __name__ == "__main__":
    unittest.main()
