"""Legacy UI proxy routes (`/api/ollama/*`, `/api/heavy/*`, `/api/chat`, etc.) using httpx."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from ..config import HEAVY_BASE, OLLAMA_BASE
from ..engine.backends import build_loopback_async_client

router = APIRouter(tags=["legacy-proxy"])

_OLLAMA_GET_MAP: dict[str, str] = {
    "/api/ollama/tags": "/api/tags",
    "/api/ollama/ps": "/api/ps",
    "/api/ollama/version": "/api/version",
    "/api/tags": "/api/tags",
    "/api/ps": "/api/ps",
    "/api/version": "/api/version",
}

_HEAVY_GET_MAP: dict[str, str] = {
    "/api/heavy/health": "/health",
    "/api/heavy/models": "/v1/models",
}

_OLLAMA_POST_MAP: dict[str, str] = {
    "/api/ollama/chat": "/api/chat",
    "/api/ollama/generate": "/api/generate",
    "/api/ollama/show": "/api/show",
    "/api/chat": "/api/chat",
    "/api/generate": "/api/generate",
    "/api/show": "/api/show",
}


async def _proxy_get(base_url: str, upstream_path: str, label: str) -> Response:
    try:
        async with build_loopback_async_client(base_url, timeout=5.0) as client:
            resp = await client.get(upstream_path, headers={"Accept": "application/json"})
            if resp.status_code >= 400:
                try:
                    err_obj = resp.json()
                    if isinstance(err_obj, dict) and "error" in err_obj:
                        return JSONResponse(status_code=resp.status_code, content=err_obj)
                except ValueError:
                    pass
                return JSONResponse(
                    status_code=resp.status_code,
                    content={"error": resp.text or f"HTTP {resp.status_code}"},
                )
            ctype = resp.headers.get("content-type") or "application/json; charset=utf-8"
            return Response(content=resp.content, status_code=resp.status_code, media_type=ctype)
    except Exception as exc:
        return JSONResponse(
            status_code=503,
            content={"error": f"{label} non raggiungibile su {base_url}: {exc}"},
        )


async def _proxy_post_stream(
    request: Request,
    base_url: str,
    upstream_path: str,
    label: str,
    default_ctype: str,
) -> Response:
    body = await request.body()
    if not body.strip():
        body = b"{}"

    client = build_loopback_async_client(base_url, timeout=600.0)
    try:
        req = client.build_request(
            "POST",
            upstream_path,
            content=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "text/event-stream, application/x-ndjson, application/json",
            },
        )
        resp = await client.send(req, stream=True)
    except Exception as exc:
        await client.aclose()
        return JSONResponse(
            status_code=503,
            content={"error": f"Errore comunicazione {label} su {base_url}: {exc}"},
        )

    if resp.status_code >= 400:
        try:
            err_raw = await resp.aread()
            err_txt = err_raw.decode("utf-8", errors="replace")
        finally:
            await resp.aclose()
            await client.aclose()
        try:
            err_obj = json.loads(err_txt)
            if isinstance(err_obj, dict) and "error" in err_obj:
                err_val: Any = err_obj["error"]
                if isinstance(err_val, dict) and "message" in err_val:
                    return JSONResponse(
                        status_code=resp.status_code, content={"error": str(err_val["message"])}
                    )
                return JSONResponse(status_code=resp.status_code, content={"error": err_val})
        except ValueError:
            pass
        return JSONResponse(
            status_code=resp.status_code,
            content={"error": err_txt or f"{label} HTTP {resp.status_code}"},
        )

    upstream_ctype = resp.headers.get("content-type") or default_ctype

    async def stream_iter() -> AsyncIterator[bytes]:
        try:
            async for chunk in resp.aiter_raw():
                if chunk:
                    yield chunk
        except Exception as exc:
            err_msg = f"Errore comunicazione {label} su {base_url}: {exc}"
            if "text/event-stream" in upstream_ctype.lower():
                payload = (
                    f"data: {json.dumps({'error': err_msg}, ensure_ascii=False)}\n\n"
                    "data: [DONE]\n\n"
                )
            else:
                payload = json.dumps({"error": err_msg}, ensure_ascii=False) + "\n"
            yield payload.encode("utf-8")
        finally:
            await resp.aclose()
            await client.aclose()

    return StreamingResponse(
        stream_iter(),
        status_code=resp.status_code,
        media_type=upstream_ctype,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
        },
    )


for _route_path, _upstream in _OLLAMA_GET_MAP.items():

    def _make_ollama_get(up: str = _upstream):
        async def _handler() -> Response:
            return await _proxy_get(OLLAMA_BASE, up, "Ollama locale")

        return _handler

    router.add_api_route(_route_path, _make_ollama_get(), methods=["GET"])


for _route_path, _upstream in _HEAVY_GET_MAP.items():

    def _make_heavy_get(up: str = _upstream):
        async def _handler() -> Response:
            return await _proxy_get(HEAVY_BASE, up, "Heavy MoE Colibri")

        return _handler

    router.add_api_route(_route_path, _make_heavy_get(), methods=["GET"])


for _route_path, _upstream in _OLLAMA_POST_MAP.items():

    def _make_ollama_post(up: str = _upstream):
        async def _handler(request: Request) -> Response:
            return await _proxy_post_stream(
                request,
                OLLAMA_BASE,
                up,
                "Ollama locale",
                "application/x-ndjson; charset=utf-8",
            )

        return _handler

    router.add_api_route(_route_path, _make_ollama_post(), methods=["POST"])


@router.post("/api/heavy/chat/completions")
async def proxy_heavy_chat(request: Request) -> Response:
    """Proxy SSE chat completions to local Heavy MoE Colibri server."""
    return await _proxy_post_stream(
        request,
        HEAVY_BASE,
        "/v1/chat/completions",
        "Heavy MoE Colibri",
        "text/event-stream; charset=utf-8",
    )
