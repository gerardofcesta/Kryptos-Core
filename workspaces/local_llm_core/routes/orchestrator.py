"""Orchestrator API proxy routes (`/api/orchestrator/*`) + live SSE status stream."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

router = APIRouter(tags=["orchestrator"])


def _call_orchestrator_router(
    method: str, path: str, payload: dict[str, Any] | None = None
) -> tuple[int, dict[str, Any]]:
    try:
        from workspaces.orchestrator.api_router import handle_orchestrator_request

        res = handle_orchestrator_request(method, path, payload)
        if res is not None:
            return res
        return 404, {"ok": False, "error": "Endpoint orchestrator non trovato"}
    except Exception:
        return 503, {
            "ok": False,
            "available": False,
            "state": "UNAVAILABLE",
            "error": "Modulo Orchestrator non disponibile.",
        }


@router.get("/api/orchestrator/stream")
async def orchestrator_status_stream(request: Request) -> StreamingResponse:
    """Stream live Orchestrator FSM status via SSE."""

    async def event_generator() -> AsyncIterator[bytes]:
        while True:
            if await request.is_disconnected():
                break
            status_code, status_data = await asyncio.to_thread(
                _call_orchestrator_router, "GET", "/api/orchestrator/status", None
            )
            payload = {"statusCode": status_code, **status_data}
            yield f"event: orchestrator\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()
            delay = 1.0 if status_data.get("active") else 3.5
            await asyncio.sleep(delay)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream; charset=utf-8",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "X-Accel-Buffering": "no",
        },
    )


@router.api_route(
    "/api/orchestrator/{subpath:path}",
    methods=["GET", "POST", "DELETE"],
)
async def proxy_orchestrator(request: Request, subpath: str) -> JSONResponse:
    """Forward `/api/orchestrator/*` requests to `workspaces.orchestrator.api_router`."""
    payload: dict[str, Any] | None = None
    if request.method == "POST":
        raw = await request.body()
        if raw.strip():
            try:
                parsed = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                return JSONResponse(
                    status_code=400,
                    content={"ok": False, "error": "JSON non valido nel corpo della richiesta"},
                )
            if not isinstance(parsed, dict):
                return JSONResponse(
                    status_code=400,
                    content={"ok": False, "error": "Il corpo JSON deve essere un oggetto"},
                )
            payload = parsed
        else:
            payload = {}

    full_path = f"/api/orchestrator/{subpath}" if subpath else "/api/orchestrator"
    code, res = await asyncio.to_thread(_call_orchestrator_router, request.method, full_path, payload)
    return JSONResponse(status_code=code, content=res)
