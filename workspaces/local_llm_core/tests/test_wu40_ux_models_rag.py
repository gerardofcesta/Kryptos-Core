"""WU-40 verification tests: Dual-Binding runtime.json, port-less UI, Hardware-Fit, Range resume, Zero-Copy, and Drop-to-Chat Ghost RAG."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import pathlib
import re
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from workspaces.local_llm_core import desktop, runtime_state
from workspaces.local_llm_core.app import create_app
from workspaces.local_llm_core.config import PROJECT_ROOT
from workspaces.local_llm_core.security import get_gateway_token
from workspaces.local_llm_core.services import model_manager


def test_dual_binding_runtime_json_and_developer_gateway_toggle(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    local_app = tmp_path / "LocalApp"
    token_file = local_app / "LocalLLM" / "gateway.token"
    monkeypatch.setenv("LOCALAPPDATA", str(local_app))
    monkeypatch.setenv("LOCAL_LLM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LOCAL_LLM_GATEWAY_TOKEN_FILE", str(token_file))

    paths = desktop.get_app_paths(ensure=True)
    # Default must be ephemeral port 0 and title "Kryptos Core" without IP/port
    cfg_default = desktop.build_desktop_config(paths=paths)
    assert cfg_default.preferred_port == 0
    assert cfg_default.title == "Kryptos Core"

    observed: dict[str, Any] = {}

    def fake_win(**kwargs: Any) -> dict[str, Any]:
        observed["url"] = kwargs["url"]
        rt = runtime_state.read_active_runtime(paths.data_dir)
        observed["runtime_during_run"] = rt
        return {"ok": True}

    def fake_start(win: Any, **kwargs: Any) -> None:
        observed["started"] = True

    rc = desktop.launch(
        config=cfg_default,
        window_factory=fake_win,
        start_hook=fake_start,
        use_mutex=False,
    )
    assert rc == 0
    rt = observed["runtime_during_run"]
    assert rt is not None
    assert rt["schema"] == "kryptos.runtime.v1"
    assert rt["pid"] == os.getpid()
    assert rt["host"] == "127.0.0.1"
    assert isinstance(rt["port"], int) and rt["port"] > 0
    assert rt["developer_gateway_pinned"] is False
    assert rt["base_url"] == f"http://127.0.0.1:{rt['port']}"
    # Cleanup on shutdown
    assert runtime_state.read_active_runtime(paths.data_dir) is None

    # Toggle developer gateway via API -> persists and pins :8787
    app = create_app()
    with TestClient(app, base_url="http://127.0.0.1:8787") as client:
        tok = get_gateway_token()
        hdrs = {"Authorization": f"Bearer {tok}"}
        r_get = client.get("/api/system/developer-gateway", headers=hdrs)
        assert r_get.status_code == 200
        assert r_get.json()["enabled"] is False

        r_post = client.post(
            "/api/system/developer-gateway",
            headers={**hdrs, "Content-Type": "application/json"},
            json={"enabled": True},
        )
        assert r_post.status_code == 200
        assert r_post.json()["enabled"] is True
        assert r_post.json()["pinned_port"] == 8787

    cfg_pinned = desktop.build_desktop_config(paths=paths)
    assert cfg_pinned.preferred_port == 8787


def test_default_ui_contains_zero_hardcoded_ips_or_ports() -> None:
    web_src = PROJECT_ROOT / "workspaces" / "offline-chat" / "web" / "src"
    rx = re.compile(r"(127\.0\.0\.1|:8787|:11434|:11435|:1234|:8080)")
    for rel in ("App.tsx", "components/ChatPanel.tsx", "components/EnginesPanel.tsx", "components/IdeConfigsCard.tsx"):
        text = (web_src / rel).read_text(encoding="utf-8")
        hits = rx.findall(text)
        assert hits == [], f"Unexpected IP/port literal in {rel}: {hits}"


def test_evaluate_model_fit_green_yellow_red() -> None:
    green = model_manager.evaluate_model_fit(
        4.5,
        hardware={"vram_total_gb": 16.0, "ram_free_gb": 24.0, "mem_bandwidth_gbps": 448.0},
    )
    assert green["emoji"] == "🟢"
    assert green["level"] == "green"
    assert green["est_tok_s"] > 10.0

    yellow = model_manager.evaluate_model_fit(
        10.0,
        hardware={"vram_total_gb": 8.0, "ram_free_gb": 24.0},
    )
    assert yellow["emoji"] == "🟡"
    assert yellow["level"] == "yellow"
    assert 6.0 <= yellow["est_tok_s"] <= 18.0

    red = model_manager.evaluate_model_fit(
        40.0,
        hardware={"vram_total_gb": 8.0, "ram_free_gb": 16.0},
    )
    assert red["emoji"] == "🔴"
    assert red["level"] == "red"
    assert red["est_tok_s"] <= 4.0


def test_smart_downloader_consent_range_resume_and_sha256_abort(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    models_dir = tmp_path / "models"
    models_dir.mkdir()
    monkeypatch.setenv("LOCAL_LLM_MODELS_DIR", str(models_dir))

    full_bytes = b"GGUF_HEADER_" + (b"0123456789ABCDEF" * 64)
    expected_sha = hashlib.sha256(full_bytes).hexdigest()
    split_at = 200
    part_dir = models_dir / ".downloads"
    part_dir.mkdir()
    part_file = part_dir / "resume-test.gguf.part"
    part_file.write_bytes(full_bytes[:split_at])

    seen_headers: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "HEAD":
            return httpx.Response(
                200,
                headers={
                    "Content-Length": str(len(full_bytes)),
                    "ETag": '"etag-v1"',
                    "X-Linked-Etag": expected_sha,
                },
            )
        seen_headers.append(dict(request.headers))
        rng = request.headers.get("range", "")
        assert rng == f"bytes={split_at}-"
        return httpx.Response(
            206,
            headers={
                "Content-Length": str(len(full_bytes) - split_at),
                "Content-Range": f"bytes {split_at}-{len(full_bytes) - 1}/{len(full_bytes)}",
            },
            content=full_bytes[split_at:],
        )

    async def _run_resume() -> None:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            prep = await model_manager.prepare_model_download(
                custom_url="https://huggingface.co/example/repo/resolve/main/resume-test.gguf",
                models_dir=models_dir,
                head_client=client,
            )
            assert prep["exact_bytes"] == len(full_bytes)
            assert prep["resume_offset_bytes"] == split_at
            assert prep["expected_sha256"] == expected_sha
            assert prep["hardware_fit"]["emoji"] in ("🟢", "🟡", "🔴")

            events: list[dict[str, Any]] = []
            async for raw_evt in model_manager.stream_resumable_model_download(
                prep["download_token"],
                confirm_authorize=True,
                models_dir=models_dir,
                http_client=client,
            ):
                line = raw_evt.decode("utf-8").strip()
                if line.startswith("data: "):
                    events.append(json.loads(line[6:]))

            assert events[-1]["stage"] == "completed"
            assert events[-1]["sha256"] == expected_sha
            final_file = models_dir / "resume-test.gguf"
            assert final_file.is_file()
            assert final_file.read_bytes() == full_bytes
            assert not part_file.exists()

    asyncio.run(_run_resume())

    # SHA-256 mismatch must delete corrupted .part
    bad_bytes = b"CORRUPTED_DATA" * 20
    wrong_sha = "a" * 64

    def bad_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "HEAD":
            return httpx.Response(
                200,
                headers={"Content-Length": str(len(bad_bytes)), "X-Linked-Etag": wrong_sha},
            )
        return httpx.Response(200, headers={"Content-Length": str(len(bad_bytes))}, content=bad_bytes)

    async def _run_bad_sha() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(bad_handler)) as bad_client:
            prep_bad = await model_manager.prepare_model_download(
                custom_url="https://huggingface.co/example/repo/resolve/main/corrupt.gguf",
                models_dir=models_dir,
                head_client=bad_client,
            )
            bad_events: list[dict[str, Any]] = []
            async for raw_evt in model_manager.stream_resumable_model_download(
                prep_bad["download_token"],
                confirm_authorize=True,
                models_dir=models_dir,
                http_client=bad_client,
            ):
                line = raw_evt.decode("utf-8").strip()
                if line.startswith("data: "):
                    bad_events.append(json.loads(line[6:]))
            assert bad_events[-1]["stage"] == "error"
            assert bad_events[-1]["code"] == "sha256_mismatch"
            assert not (part_dir / "corrupt.gguf.part").exists()
            assert not (models_dir / "corrupt.gguf").exists()

    asyncio.run(_run_bad_sha())


def test_download_endpoints_block_unconsented_and_discover_local_zero_copy(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token_file = tmp_path / "LocalLLM" / "gateway.token"
    monkeypatch.setenv("LOCAL_LLM_GATEWAY_TOKEN_FILE", str(token_file))

    app = create_app()
    with TestClient(app, base_url="http://127.0.0.1:8787") as client:
        tok = get_gateway_token()
        hdrs = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}

        # Fail-closed without confirm_authorize=true
        r_deny = client.post(
            "/api/models/download/start",
            headers=hdrs,
            json={"download_token": "dummy-token-123456", "confirm_authorize": False},
        )
        assert r_deny.status_code == 403

        # Non-allowlisted host blocked at prepare
        r_bad_host = client.post(
            "/api/models/download/prepare",
            headers=hdrs,
            json={"custom_url": "http://127.0.0.1:9999/evil.gguf"},
        )
        assert r_bad_host.status_code == 403

    # Zero-Copy local model discovery across synthetic Ollama, HF, LM Studio dirs
    ol_dir = tmp_path / "ollama"
    mf = ol_dir / "manifests" / "registry.ollama.ai" / "library" / "qwen2.5" / "7b"
    mf.parent.mkdir(parents=True)
    mf.write_text(json.dumps({"layers": [{"size": 4_500_000_000}]}), encoding="utf-8")

    hf_dir = tmp_path / "hf_hub"
    hf_gguf = hf_dir / "models--org--demo" / "snapshots" / "abc" / "demo-q4.gguf"
    hf_gguf.parent.mkdir(parents=True)
    hf_gguf.write_bytes(b"GGUF" * 1024)

    lms_dir = tmp_path / "lmstudio"
    lms_gguf = lms_dir / "vendor" / "model.gguf"
    lms_gguf.parent.mkdir(parents=True)
    lms_gguf.write_bytes(b"GGUF" * 512)

    disc = model_manager.discover_local_models(
        ollama_dir=ol_dir,
        hf_cache_dir=hf_dir,
        lmstudio_dir=lms_dir,
    )
    assert disc["ok"] is True
    assert disc["extra_disk_gb"] == 0.0
    sources = {m["source"] for m in disc["models"]}
    assert sources == {"ollama", "huggingface_cache", "lmstudio"}
    assert all(m["zero_copy"] is True and m["extra_disk_gb"] == 0.0 for m in disc["models"])


def test_drop_to_chat_ghost_rag_ephemeral_docs_and_ram_wipe(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token_file = tmp_path / "LocalLLM" / "gateway.token"
    monkeypatch.setenv("LOCAL_LLM_GATEWAY_TOKEN_FILE", str(token_file))

    app = create_app()
    with TestClient(app, base_url="http://127.0.0.1:8787") as client:
        tok = get_gateway_token()
        hdrs = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}

        r_up = client.post(
            "/api/chat/ephemeral-docs",
            headers=hdrs,
            json={
                "files": [
                    {
                        "filename": "contract_draft.md",
                        "content": "# Clause 1\nConfidentiality term is 36 months.\n# Clause 2\nPenalty cap is 5000 EUR.\n",
                    }
                ]
            },
        )
        assert r_up.status_code == 200
        up_json = r_up.json()
        assert up_json["ok"] is True
        assert up_json["disk_writes"] == 0
        assert up_json["documents"][0]["source_file"] == "contract_draft.md"

        r_q = client.post(
            "/api/chat/ephemeral-docs/query",
            headers=hdrs,
            json={"query": "Confidentiality term months", "top_k": 3},
        )
        assert r_q.status_code == 200
        q_json = r_q.json()
        assert q_json["count"] >= 1
        assert "[contract_draft.md, L." in q_json["results"][0]["citation"]
        assert "[DOC: contract_draft.md |" in q_json["context_block"]

        r_del = client.delete("/api/chat/ephemeral-docs", headers={"Authorization": f"Bearer {tok}"})
        assert r_del.status_code == 200
        assert r_del.json()["wiped_chunks"] >= 1

        r_list = client.get("/api/chat/ephemeral-docs", headers={"Authorization": f"Bearer {tok}"})
        assert r_list.status_code == 200
        assert r_list.json()["count"] == 0
