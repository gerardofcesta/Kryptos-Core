"""Telemetry, system config, registry, SSE stream, and Cost/Token Ledger routes."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from ..config import HEAVY_BASE, LMS_BASE, OLLAMA_BASE, SETTINGS, read_hardware_profile
from ..paths import resolve_app_paths
from ..runtime_state import (
    DEVELOPER_DEFAULT_PORT,
    DEVELOPER_PORT_RANGE,
    get_active_runtime_info,
    load_desktop_settings,
    save_desktop_settings,
)
from ..services.ledger import get_cost_ledger
from ..services.telemetry import async_get_full_telemetry, get_local_registry

router = APIRouter(tags=["telemetry"])


class DeveloperGatewayToggleRequest(BaseModel):
    """Toggle fixed-port local gateway exposure for external IDEs and MCP clients."""

    enabled: bool


@router.get("/api/system/telemetry")
async def system_telemetry() -> JSONResponse:
    """Return cached/parallel hardware and backend telemetry."""
    data = await async_get_full_telemetry()
    return JSONResponse(status_code=200, content=data)


@router.get("/api/system/telemetry/stream")
async def system_telemetry_stream(
    request: Request,
    interval: float = Query(default=3.0, ge=0.5, le=30.0),
) -> StreamingResponse:
    """Stream live system telemetry via Server-Sent Events (SSE)."""

    async def event_generator() -> AsyncIterator[bytes]:
        while True:
            if await request.is_disconnected():
                break
            snapshot = await async_get_full_telemetry()
            yield f"event: telemetry\ndata: {json.dumps(snapshot, ensure_ascii=False)}\n\n".encode()
            await asyncio.sleep(interval)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream; charset=utf-8",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/api/system/config")
async def system_config() -> dict[str, object]:
    """Return data-driven runtime endpoints and hardware capabilities for the UI."""
    hw = read_hardware_profile()
    return {
        "ok": True,
        "gatewayUrl": f"http://{SETTINGS.host}:{SETTINGS.port}",
        "unifiedOpenAIUrl": f"http://{SETTINGS.host}:{SETTINGS.port}/v1",
        "backends": {
            "ollama": OLLAMA_BASE,
            "heavyMoe": HEAVY_BASE,
            "lmstudio": LMS_BASE,
        },
        "models": {
            "fast": SETTINGS.fast_model,
            "balanced": SETTINGS.balanced_model,
            "heavy": SETTINGS.heavy_model,
            "embedding": SETTINGS.embedding_model,
        },
        "hardware": hw,
    }


@router.get("/api/registry/local")
async def local_registry() -> dict[str, object]:
    """Return local Ollama, Heavy MoE GGUF, and benchmark inventory."""
    return await asyncio.to_thread(get_local_registry)


@router.get("/api/ledger")
async def cost_token_ledger(
    app_id: str | None = Query(default=None),
    workflow_id: str | None = Query(default=None),
) -> dict[str, object]:
    """Return in-memory Cost/Token Ledger snapshot per `X-App-Id` / `X-Workflow-Id`."""
    return get_cost_ledger().get_snapshot(app_id=app_id, workflow_id=workflow_id)


@router.get("/api/system/developer-gateway")
async def get_developer_gateway_status() -> dict[str, object]:
    """Return Developer Drawer opt-in fixed-port (:8787) settings and runtime status."""
    paths = resolve_app_paths("KryptosCore", env_prefix="KRYPTOS", ephemeral_cache=True)
    settings_data = load_desktop_settings(paths.data_dir)
    rt = get_active_runtime_info()
    enabled = bool(settings_data.get("developer_gateway_enabled", False))
    active_port = rt.get("port") or (DEVELOPER_DEFAULT_PORT if enabled else None)
    return {
        "ok": True,
        "enabled": enabled,
        "pinned_port": DEVELOPER_DEFAULT_PORT,
        "fallback_range": f"{DEVELOPER_PORT_RANGE[0]}-{DEVELOPER_PORT_RANGE[1]}",
        "active_port": active_port,
        "ephemeral_default": not enabled,
        "restart_required": bool(rt and bool(rt.get("developer_gateway_pinned")) != enabled),
        "runtime_discovery_file": str(paths.data_dir / "runtime.json"),
    }


@router.post("/api/system/developer-gateway")
async def set_developer_gateway_status(req: DeveloperGatewayToggleRequest) -> dict[str, object]:
    """Persist Developer Drawer opt-in fixed-port (:8787) preference."""
    paths = resolve_app_paths("KryptosCore", env_prefix="KRYPTOS", ephemeral_cache=True)
    save_desktop_settings(paths.data_dir, {"developer_gateway_enabled": bool(req.enabled)})
    rt = get_active_runtime_info()
    active_port = rt.get("port") or (DEVELOPER_DEFAULT_PORT if req.enabled else None)
    return {
        "ok": True,
        "enabled": bool(req.enabled),
        "pinned_port": DEVELOPER_DEFAULT_PORT,
        "fallback_range": f"{DEVELOPER_PORT_RANGE[0]}-{DEVELOPER_PORT_RANGE[1]}",
        "active_port": active_port,
        "ephemeral_default": not req.enabled,
        "restart_required": bool(rt and bool(rt.get("developer_gateway_pinned")) != bool(req.enabled)),
        "runtime_discovery_file": str(paths.data_dir / "runtime.json"),
    }


@router.get("/api/system/ide-configs")
async def system_ide_configs() -> dict[str, object]:
    """Generate copy-ready local configuration snippets for IDEs and SDKs (token masked)."""
    rt = get_active_runtime_info()
    active_port = int(rt.get("port") or SETTINGS.port)
    gateway_base = f"http://{SETTINGS.host}:{active_port}"
    openai_base = f"{gateway_base}/v1"
    mcp_url = f"{gateway_base}/mcp"
    token_placeholder = "<YOUR_GATEWAY_TOKEN>"

    continue_snippet = json.dumps(
        {
            "models": [
                {
                    "title": "Kryptos Core — Fast Worker",
                    "provider": "openai",
                    "model": "local-fast",
                    "apiBase": openai_base,
                    "apiKey": token_placeholder,
                },
                {
                    "title": "Kryptos Core — Cascade Auto",
                    "provider": "openai",
                    "model": "auto",
                    "apiBase": openai_base,
                    "apiKey": token_placeholder,
                },
            ]
        },
        indent=2,
    )

    cline_snippet = json.dumps(
        {
            "apiProvider": "openai",
            "openAiBaseUrl": openai_base,
            "openAiApiKey": token_placeholder,
            "openAiModelId": "local-fast",
        },
        indent=2,
    )

    cursor_snippet = json.dumps(
        {
            "overrideOpenAIBaseUrl": openai_base,
            "openAIApiKey": token_placeholder,
            "model": "auto",
        },
        indent=2,
    )

    openai_sdk_snippet = "\n".join(
        [
            "from openai import OpenAI",
            "",
            "client = OpenAI(",
            f'    base_url="{openai_base}",',
            f'    api_key="{token_placeholder}",',
            ")",
            "",
            "resp = client.chat.completions.create(",
            '    model="auto",',
            '    messages=[{"role": "user", "content": "Summarize this module."}],',
            ")",
            "print(resp.choices[0].message.content)",
        ]
    )

    mcp_snippet = json.dumps(
        {
            "mcpServers": {
                "kryptos-core": {
                    "type": "http",
                    "url": mcp_url,
                    "headers": {
                        "Authorization": f"Bearer {token_placeholder}",
                    },
                }
            }
        },
        indent=2,
    )

    return {
        "ok": True,
        "gatewayUrl": gateway_base,
        "openaiBaseUrl": openai_base,
        "mcpUrl": mcp_url,
        "tokenPlaceholder": token_placeholder,
        "tokenStorageHint": "%LOCALAPPDATA%\\KryptosCore\\gateway.token",
        "configs": [
            {
                "id": "continue",
                "name": "Continue (VS Code / JetBrains)",
                "subtitle": "OpenAI-compatible local gateway models",
                "filename": ".continue/config.json",
                "language": "json",
                "snippet": continue_snippet,
            },
            {
                "id": "cline",
                "name": "Cline",
                "subtitle": "VS Code autonomous coding extension",
                "filename": "Cline API Settings",
                "language": "json",
                "snippet": cline_snippet,
            },
            {
                "id": "cursor",
                "name": "Cursor",
                "subtitle": "Custom OpenAI Base URL override",
                "filename": "Cursor Models Settings",
                "language": "json",
                "snippet": cursor_snippet,
            },
            {
                "id": "openai_sdk",
                "name": "OpenAI Python SDK",
                "subtitle": "Drop-in local client via /v1/chat/completions",
                "filename": "local_client.py",
                "language": "python",
                "snippet": openai_sdk_snippet,
            },
            {
                "id": "mcp",
                "name": "MCP Client (HTTP JSON-RPC)",
                "subtitle": "Exposes rag_query, code_audit, local_chat, model_inventory",
                "filename": "mcp_servers.json",
                "language": "json",
                "snippet": mcp_snippet,
            },
        ],
    }
