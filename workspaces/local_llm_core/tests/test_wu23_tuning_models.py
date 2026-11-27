"""WU-23 verification tests: tuning bounds, -Restart/num_ctx propagation, Hardware Advisor clamping, catalog & opt-in pull/pin."""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import subprocess
import tempfile
import unittest
from collections.abc import AsyncIterator
from typing import Any, Self
from unittest import mock

import httpx
from fastapi.testclient import TestClient
from workspaces.local_llm_core.app import create_app
from workspaces.local_llm_core.config import PROJECT_ROOT
from workspaces.local_llm_core.engine.coordinator import (
    execute_engine_action,
    get_gateway_coordinator,
)
from workspaces.local_llm_core.security import rotate_gateway_token
from workspaces.local_llm_core.services.model_manager import (
    get_annotated_model_catalog,
    verify_model_sha256,
)
from workspaces.local_llm_core.services.telemetry import invalidate_telemetry_cache
from workspaces.local_llm_core.services.tuning_advisor import search_hardware_kb
from workspaces.local_llm_core.services.tuning_bounds import (
    FAST_CTX_STEPS,
    compute_tuning_bounds,
)


class _FakeStreamResponse:
    """Minimal async streaming response mock for httpx.AsyncClient.stream."""

    def __init__(
        self,
        *,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        lines: list[str] | None = None,
        chunks: list[bytes] | None = None,
        url: str = "https://huggingface.co/test/model.gguf",
    ) -> None:
        self.status_code = status_code
        self.headers = headers or {}
        self._lines = lines or []
        self._chunks = chunks or []
        self.url = httpx.URL(url)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def aread(self) -> bytes:
        return b""

    async def aiter_lines(self) -> AsyncIterator[str]:
        for line in self._lines:
            yield line

    async def aiter_bytes(self, chunk_size: int = 262144) -> AsyncIterator[bytes]:
        _ = chunk_size
        for ch in self._chunks:
            yield ch


class TestWu23TuningAndModels(unittest.TestCase):
    """Tests covering all 4 WU-23 implementation blocks."""

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
        invalidate_telemetry_cache()
        get_gateway_coordinator().set_fast_num_ctx(16384)
        self.client = TestClient(self.app, base_url="http://127.0.0.1:8787")

    def tearDown(self) -> None:
        self.client.close()
        invalidate_telemetry_cache()
        get_gateway_coordinator().set_fast_num_ctx(16384)
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
    # Part 1: ramLimitGb / ram_limit_gb / -Restart & Fast num_ctx
    # ------------------------------------------------------------------
    def test_coordinator_accepts_both_ram_limit_keys_and_restart_flag(self) -> None:
        captured_cmds: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs: Any) -> mock.Mock:
            _ = kwargs
            captured_cmds.append(list(cmd))
            return mock.Mock(returncode=0, stdout="OK", stderr="")

        with mock.patch("workspaces.local_llm_core.engine.coordinator.subprocess.run", side_effect=fake_run):
            st1, res1 = execute_engine_action(
                {"action": "start_heavy", "ramLimitGb": 18, "ctx": 24576, "restart": True},
                orch_summary_fn=lambda: {"active": False},
            )
            st2, res2 = execute_engine_action(
                {"action": "start_heavy", "ram_limit_gb": 14, "ctx": 8192, "restart": False},
                orch_summary_fn=lambda: {"active": False},
            )
            st3, _res3 = execute_engine_action(
                {"action": "start_fast", "ctx": 32768, "restart": True},
                orch_summary_fn=lambda: {"active": False},
            )

        self.assertEqual(st1, 200)
        self.assertTrue(res1["ok"])
        self.assertTrue(res1["restarted"])
        self.assertEqual(res1["ramLimitGb"], 18)
        self.assertIn("-RamLimitGB", captured_cmds[0])
        self.assertEqual(captured_cmds[0][captured_cmds[0].index("-RamLimitGB") + 1], "18")
        self.assertIn("-Restart", captured_cmds[0])

        self.assertEqual(st2, 200)
        self.assertEqual(res2["ramLimitGb"], 14)
        self.assertNotIn("-Restart", captured_cmds[1])

        self.assertEqual(st3, 200)
        self.assertIn("-Restart", captured_cmds[2])
        self.assertIn("-Ctx", captured_cmds[2])
        self.assertEqual(captured_cmds[2][captured_cmds[2].index("-Ctx") + 1], "32768")
        self.assertEqual(get_gateway_coordinator().get_fast_num_ctx(), 32768)

    def test_fast_num_ctx_injected_into_ollama_payload_unless_overridden(self) -> None:
        coord = get_gateway_coordinator()
        coord.set_fast_num_ctx(24576)
        captured: list[dict[str, Any]] = []

        async def fake_chat(payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
            captured.append(dict(payload))
            return 200, {
                "id": "chatcmpl-1",
                "object": "chat.completion",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2},
            }

        with (
            mock.patch.object(coord, "get_active_tier", new=mock.AsyncMock(return_value="fast")),
            mock.patch.object(coord.ollama, "chat_completions", side_effect=fake_chat),
        ):
            r1 = self.client.post(
                "/v1/chat/completions",
                headers=self._auth_headers(),
                json={"model": "local-fast", "messages": [{"role": "user", "content": "hi"}]},
            )
            r2 = self.client.post(
                "/v1/chat/completions",
                headers=self._auth_headers(),
                json={
                    "model": "local-fast",
                    "messages": [{"role": "user", "content": "hi"}],
                    "options": {"num_ctx": 4096},
                },
            )

        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(captured[0]["options"]["num_ctx"], 24576)
        self.assertEqual(captured[1]["options"]["num_ctx"], 4096)

    def test_start_llm_ps1_defines_restart_and_parses_cleanly(self) -> None:
        ps1 = PROJECT_ROOT / "scripts" / "start-llm.ps1"
        raw = ps1.read_text(encoding="utf-8", errors="replace")
        self.assertIn("[switch]$Restart", raw)
        self.assertIn("OLLAMA_CONTEXT_LENGTH", raw)
        ps_cmd = (
            "$errors = $null; $tokens = $null; "
            f"[void][System.Management.Automation.Language.Parser]::ParseFile('{ps1}', [ref]$tokens, [ref]$errors); "
            "if ($errors.Count -gt 0) { $errors | ForEach-Object { Write-Error $_.Message }; exit 1 }"
        )
        proc = subprocess.run(["powershell", "-NoProfile", "-Command", ps_cmd], check=False, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    # ------------------------------------------------------------------
    # Part 2: Layer 1 Deterministic Math Safety Bounds
    # ------------------------------------------------------------------
    def test_layer1_tuning_bounds_os_reserves_kv_quant_and_oom_clamping(self) -> None:
        gpu_16g = {"available": True, "vramTotalGb": 16.0, "vramFreeGb": 14.5}
        ram_32g = {"totalGb": 32.0, "freeGb": 22.0}

        b_q8 = compute_tuning_bounds(
            model_params_b=14.0,
            model_weights_gb=9.0,
            kv_quant="q8_0",
            gpu=gpu_16g,
            ram=ram_32g,
            heavy_meta={},
            loaded_ollama_vram_gb=0.0,
        )
        b_f16 = compute_tuning_bounds(
            model_params_b=14.0,
            model_weights_gb=9.0,
            kv_quant="f16",
            gpu=gpu_16g,
            ram=ram_32g,
            heavy_meta={},
            loaded_ollama_vram_gb=0.0,
        )

        # OS reserves: max(1.5, 10% VRAM) and max(6.0, 15% RAM)
        self.assertEqual(b_q8["budget"]["os_vram_reserve_gb"], 1.6)
        self.assertEqual(b_q8["budget"]["os_ram_reserve_gb"], 6.0)
        self.assertAlmostEqual(b_f16["budget"]["kv_gb_per_1k"], b_q8["budget"]["kv_gb_per_1k"] * 2.0, places=3)
        self.assertIn(b_q8["fast_ctx"]["max_safe"], FAST_CTX_STEPS)
        self.assertIn(b_q8["fast_ctx"]["recommended"], FAST_CTX_STEPS)
        self.assertLessEqual(b_q8["fast_ctx"]["recommended"], b_q8["fast_ctx"]["max_safe"])
        self.assertGreaterEqual(b_q8["fast_ctx"]["max_safe"], b_f16["fast_ctx"]["max_safe"])

        # Tight low-RAM / low-VRAM clamping
        b_tight = compute_tuning_bounds(
            model_params_b=14.0,
            model_weights_gb=9.0,
            kv_quant="q8_0",
            gpu={"available": True, "vramTotalGb": 8.0, "vramFreeGb": 7.0},
            ram={"totalGb": 16.0, "freeGb": 6.5},
            heavy_meta={},
            loaded_ollama_vram_gb=0.0,
        )
        self.assertEqual(b_tight["fast_ctx"]["max_safe"], 2048)
        self.assertEqual(b_tight["heavy_ram_limit_gb"]["min"], 4)
        self.assertLessEqual(b_tight["heavy_ram_limit_gb"]["recommended"], b_tight["heavy_ram_limit_gb"]["max_safe"])

        # Live HTTP endpoint
        r_bounds = self.client.get(
            "/api/system/tuning-bounds?model_params_b=14&kv_quant=q8_0",
            headers={"Authorization": f"Bearer {self.token}"},
        )
        self.assertEqual(r_bounds.status_code, 200)
        data = r_bounds.json()
        self.assertTrue(data["ok"])
        self.assertIn("fast_ctx", data)
        self.assertIn("heavy_ram_limit_gb", data)
        self.assertIn("budget", data)

    # ------------------------------------------------------------------
    # Part 3: Layer 2 Hardware Advisor (KB RAG + Strict Clamping)
    # ------------------------------------------------------------------
    def test_layer2_kb_search_and_advisor_clamping_and_offline_fallback(self) -> None:
        kb_dir = PROJECT_ROOT / "docs" / "hardware-tuning-kb"
        self.assertTrue((kb_dir / "colibri-moe-offload.md").is_file())
        self.assertTrue((kb_dir / "kv-cache-and-context.md").is_file())
        self.assertTrue((kb_dir / "vram-ram-troubleshooting.md").is_file())

        hits = search_hardware_kb("colibri moe expert cache vulkan offload", top_k=2)
        self.assertTrue(len(hits) >= 1)
        self.assertEqual(hits[0]["source_file"], "colibri-moe-offload.md")

        coord = get_gateway_coordinator()
        fake_gpu = {"available": True, "name": "Mock 8GB GPU", "vramTotalGb": 8.0, "vramFreeGb": 7.0}
        fake_ram = {"totalGb": 16.0, "freeGb": 9.0}

        # 1. Offline local-fast -> deterministic KB fallback
        with (
            mock.patch("workspaces.local_llm_core.services.tuning_advisor.get_gpu_telemetry", return_value=fake_gpu),
            mock.patch("workspaces.local_llm_core.services.tuning_advisor.get_ram_telemetry", return_value=fake_ram),
            mock.patch.object(coord.ollama, "health", new=mock.AsyncMock(return_value=False)),
        ):
            r_fb = self.client.post(
                "/api/system/tuning-advisor",
                headers=self._auth_headers(),
                json={"question": "How much context is safe?", "model_params_b": 7.0},
            )
        self.assertEqual(r_fb.status_code, 200)
        fb = r_fb.json()
        self.assertEqual(fb["mode"], "kb_only_fallback")
        self.assertLessEqual(fb["recommended"]["fast_ctx"], fb["bounds"]["fast_ctx"]["max_safe"])
        self.assertLessEqual(
            fb["recommended"]["heavy_ram_limit_gb"],
            fb["bounds"]["heavy_ram_limit_gb"]["max_safe"],
        )

        # 2. Online local-fast returning reckless values -> strictly clamped by Layer 1
        reckless_reply = json.dumps(
            {
                "explanation": "Push everything to max!",
                "fast_ctx": 131072,
                "heavy_ram_limit_gb": 999,
                "heavy_ctx": 131072,
                "warnings": [],
            }
        )
        with (
            mock.patch("workspaces.local_llm_core.services.tuning_advisor.get_gpu_telemetry", return_value=fake_gpu),
            mock.patch("workspaces.local_llm_core.services.tuning_advisor.get_ram_telemetry", return_value=fake_ram),
            mock.patch.object(coord.ollama, "health", new=mock.AsyncMock(return_value=True)),
            mock.patch.object(
                coord.ollama,
                "chat_completions",
                new=mock.AsyncMock(
                    return_value=(200, {"choices": [{"message": {"content": reckless_reply}}]})
                ),
            ),
        ):
            r_clamped = self.client.post(
                "/api/system/tuning-advisor",
                headers=self._auth_headers(),
                json={"question": "Can I use 128k ctx?", "model_params_b": 7.0},
            )
        self.assertEqual(r_clamped.status_code, 200)
        clamped_data = r_clamped.json()
        self.assertEqual(clamped_data["mode"], "llm+kb")
        self.assertTrue(clamped_data["clamped"])
        self.assertLessEqual(clamped_data["recommended"]["fast_ctx"], clamped_data["bounds"]["fast_ctx"]["max_safe"])
        self.assertLessEqual(
            clamped_data["recommended"]["heavy_ram_limit_gb"],
            clamped_data["bounds"]["heavy_ram_limit_gb"]["max_safe"],
        )
        self.assertLessEqual(clamped_data["recommended"]["heavy_ctx"], clamped_data["bounds"]["heavy_ctx"]["max_safe"])

    # ------------------------------------------------------------------
    # Part 4: Curated Catalog, SHA-256 Pinning & Fail-Closed Opt-In Pull
    # ------------------------------------------------------------------
    def test_catalog_fits_hardware_and_strips_external_urls(self) -> None:
        cat_8g = get_annotated_model_catalog(
            gpu={"available": True, "name": "8GB GPU", "vramTotalGb": 8.0},
            ram={"totalGb": 16.0},
            disk={"freeGb": 200.0},
            ollama_tags=[],
        )
        by_id = {m["id"]: m for m in cat_8g["models"]}
        self.assertEqual(by_id["qwen2.5-3b-instruct-cpu"]["fits_hardware"], "optimal")
        self.assertEqual(by_id["qwen2.5-32b-instruct-24gb"]["fits_hardware"], "too_large")
        for m in cat_8g["models"]:
            self.assertNotIn("download_url", m)

        r_cat = self.client.get("/api/models/catalog", headers={"Authorization": f"Bearer {self.token}"})
        self.assertEqual(r_cat.status_code, 200)
        self.assertGreaterEqual(r_cat.json()["count"], 5)

    def test_pin_sha256_and_verify_manifest(self) -> None:
        model_file = self._models_dir / "sample-model.gguf"
        payload = b"GGUF_PIN_TEST_" * 128
        model_file.write_bytes(payload)
        expected = hashlib.sha256(payload).hexdigest()

        # Traversal rejected
        r_trav = self.client.post(
            "/api/models/pin-sha256",
            headers=self._auth_headers(),
            json={"path": "../outside.gguf"},
        )
        self.assertEqual(r_trav.status_code, 403)

        # Pin writes manifest.sha256.json and verifies cleanly
        r_pin = self.client.post(
            "/api/models/pin-sha256",
            headers=self._auth_headers(),
            json={"path": "sample-model.gguf"},
        )
        self.assertEqual(r_pin.status_code, 200, r_pin.text)
        pin_data = r_pin.json()
        self.assertTrue(pin_data["pinned"])
        self.assertEqual(pin_data["sha256"], expected)

        ver = verify_model_sha256("sample-model.gguf", models_dir=self._models_dir)
        self.assertTrue(ver["verified"])
        self.assertEqual(ver["status"], "VERIFIED")

    def test_models_pull_fail_closed_and_mocked_sse_streams(self) -> None:
        # 1. Fail-closed air-gapped default when confirm_online_pull is False or omitted
        r_denied1 = self.client.post(
            "/api/models/pull",
            headers=self._auth_headers(),
            json={"model_id": "qwen2.5-3b-instruct-cpu"},
        )
        self.assertEqual(r_denied1.status_code, 403)
        self.assertEqual(r_denied1.json()["code"], "air_gapped_confirmation_required")

        r_denied2 = self.client.post(
            "/api/models/pull",
            headers=self._auth_headers(),
            json={"model_id": "qwen2.5-3b-instruct-cpu", "confirm_online_pull": False},
        )
        self.assertEqual(r_denied2.status_code, 403)

        # 2. Unknown model_id returns 404
        r_missing = self.client.post(
            "/api/models/pull",
            headers=self._auth_headers(),
            json={"model_id": "does-not-exist", "confirm_online_pull": True},
        )
        self.assertEqual(r_missing.status_code, 404)

        # 3. Mocked Ollama pull SSE stream
        fake_ollama_resp = _FakeStreamResponse(
            lines=[
                json.dumps({"status": "pulling manifest"}),
                json.dumps({"status": "downloading", "completed": 50, "total": 100}),
                json.dumps({"status": "success", "completed": 100, "total": 100}),
            ]
        )
        fake_client = mock.MagicMock()
        fake_client.__aenter__ = mock.AsyncMock(return_value=fake_client)
        fake_client.__aexit__ = mock.AsyncMock(return_value=None)
        fake_client.stream = mock.MagicMock(return_value=fake_ollama_resp)

        with mock.patch("workspaces.local_llm_core.services.model_manager.httpx.AsyncClient", return_value=fake_client):
            r_ol = self.client.post(
                "/api/models/pull",
                headers=self._auth_headers(),
                json={"model_id": "qwen2.5-3b-instruct-cpu", "confirm_online_pull": True},
            )
        self.assertEqual(r_ol.status_code, 200)
        self.assertIn('"stage": "completed"', r_ol.text)

        # 4. Mocked GGUF download + expected SHA-256 verification + mismatch cleanup
        gguf_bytes = b"GGUF_STREAM_VERIFIED_CONTENT_" * 32
        good_sha = hashlib.sha256(gguf_bytes).hexdigest()
        bad_sha = "0" * 64

        custom_catalog = [
            {
                "id": "mock-gguf-verified",
                "backend": "gguf",
                "filename": "mock-verified.gguf",
                "download_url": "https://huggingface.co/mock/repo/resolve/main/mock-verified.gguf",
                "expected_sha256": good_sha,
            },
            {
                "id": "mock-gguf-bad-hash",
                "backend": "gguf",
                "filename": "mock-bad.gguf",
                "download_url": "https://huggingface.co/mock/repo/resolve/main/mock-bad.gguf",
                "expected_sha256": bad_sha,
            },
        ]

        fake_gguf_resp_ok = _FakeStreamResponse(
            headers={"content-length": str(len(gguf_bytes))},
            chunks=[gguf_bytes[:100], gguf_bytes[100:]],
            url="https://huggingface.co/mock/repo/resolve/main/mock-verified.gguf",
        )
        fake_gguf_client_ok = mock.MagicMock()
        fake_gguf_client_ok.__aenter__ = mock.AsyncMock(return_value=fake_gguf_client_ok)
        fake_gguf_client_ok.__aexit__ = mock.AsyncMock(return_value=None)
        fake_gguf_client_ok.stream = mock.MagicMock(return_value=fake_gguf_resp_ok)

        with (
            mock.patch("workspaces.local_llm_core.routes.models.load_model_catalog", return_value=custom_catalog),
            mock.patch(
                "workspaces.local_llm_core.services.model_manager.httpx.AsyncClient",
                return_value=fake_gguf_client_ok,
            ),
        ):
            r_gguf_ok = self.client.post(
                "/api/models/pull",
                headers=self._auth_headers(),
                json={"model_id": "mock-gguf-verified", "confirm_online_pull": True},
            )
        self.assertEqual(r_gguf_ok.status_code, 200)
        self.assertIn('"status": "VERIFIED"', r_gguf_ok.text)
        self.assertTrue((self._models_dir / "mock-verified.gguf").is_file())
        self.assertTrue((self._models_dir / "manifest.sha256.json").is_file())

        # Mismatch removes .part and refuses to write final file
        fake_gguf_resp_bad = _FakeStreamResponse(
            headers={"content-length": str(len(gguf_bytes))},
            chunks=[gguf_bytes],
            url="https://huggingface.co/mock/repo/resolve/main/mock-bad.gguf",
        )
        fake_gguf_client_bad = mock.MagicMock()
        fake_gguf_client_bad.__aenter__ = mock.AsyncMock(return_value=fake_gguf_client_bad)
        fake_gguf_client_bad.__aexit__ = mock.AsyncMock(return_value=None)
        fake_gguf_client_bad.stream = mock.MagicMock(return_value=fake_gguf_resp_bad)

        with (
            mock.patch("workspaces.local_llm_core.routes.models.load_model_catalog", return_value=custom_catalog),
            mock.patch(
                "workspaces.local_llm_core.services.model_manager.httpx.AsyncClient",
                return_value=fake_gguf_client_bad,
            ),
        ):
            r_gguf_bad = self.client.post(
                "/api/models/pull",
                headers=self._auth_headers(),
                json={"model_id": "mock-gguf-bad-hash", "confirm_online_pull": True},
            )
        self.assertEqual(r_gguf_bad.status_code, 200)
        self.assertIn('"code": "sha256_mismatch"', r_gguf_bad.text)
        self.assertFalse((self._models_dir / "mock-bad.gguf").exists())
        self.assertFalse((self._models_dir / "mock-bad.gguf.part").exists())


if __name__ == "__main__":
    unittest.main()
