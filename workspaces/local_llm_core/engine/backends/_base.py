"""Base OpenAI-compatible loopback backend implementation using httpx.AsyncClient."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from . import assert_loopback_url, build_loopback_async_client


class OpenAICompatibleBackend:
    """Shared async loopback backend for OpenAI-compatible servers."""

    def __init__(
        self,
        name: str,
        tier: str,
        base_url: str,
        health_path: str = "/health",
        models_path: str = "/v1/models",
        chat_path: str = "/v1/chat/completions",
        embeddings_path: str = "/v1/embeddings",
        timeout: float = 180.0,
    ) -> None:
        self.name = name
        self.tier = tier
        self.base_url = assert_loopback_url(base_url.rstrip("/"))
        self.health_path = health_path
        self.models_path = models_path
        self.chat_path = chat_path
        self.embeddings_path = embeddings_path
        self.timeout = timeout

    def _client(self, timeout: float | None = None) -> httpx.AsyncClient:
        return build_loopback_async_client(self.base_url, timeout=timeout or self.timeout)

    async def health(self) -> bool:
        try:
            async with self._client(timeout=1.5) as client:
                resp = await client.get(self.health_path, headers={"Accept": "application/json"})
                return 200 <= resp.status_code < 300
        except Exception:
            return False

    async def list_models(self) -> list[str]:
        try:
            async with self._client(timeout=2.0) as client:
                resp = await client.get(self.models_path, headers={"Accept": "application/json"})
                if not (200 <= resp.status_code < 300):
                    return []
                data = resp.json()
                if isinstance(data, dict):
                    if isinstance(data.get("data"), list):
                        return [str(item["id"]) for item in data["data"] if isinstance(item, dict) and item.get("id")]
                    if isinstance(data.get("models"), list):
                        return [str(m["name"]) for m in data["models"] if isinstance(m, dict) and m.get("name")]
                return []
        except Exception:
            return []

    async def chat_completions(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        body = dict(payload)
        body["stream"] = False
        async with self._client() as client:
            resp = await client.post(
                self.chat_path,
                json=body,
                headers={"Content-Type": "application/json", "Accept": "application/json"},
            )
            try:
                parsed = resp.json()
                if not isinstance(parsed, dict):
                    parsed = {"error": {"message": str(parsed), "type": "backend_error"}}
            except ValueError:
                parsed = {"error": {"message": resp.text or f"HTTP {resp.status_code}", "type": "backend_error"}}
            return resp.status_code, parsed

    async def chat_completions_stream(self, payload: dict[str, Any]) -> AsyncIterator[bytes]:
        body = dict(payload)
        body["stream"] = True
        async with self._client(timeout=600.0) as client:
            async with client.stream(
                "POST",
                self.chat_path,
                json=body,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "text/event-stream, application/json",
                },
            ) as resp:
                if resp.status_code >= 400:
                    err_raw = await resp.aread()
                    err_txt = err_raw.decode("utf-8", errors="replace")
                    try:
                        err_obj = json.loads(err_txt)
                    except ValueError:
                        err_obj = {"error": {"message": err_txt or f"HTTP {resp.status_code}", "type": "backend_error"}}
                    yield f"data: {json.dumps(err_obj, ensure_ascii=False)}\n\ndata: [DONE]\n\n".encode()
                    return

                async for chunk in resp.aiter_raw():
                    if chunk:
                        yield chunk

    async def embeddings(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        async with self._client(timeout=120.0) as client:
            resp = await client.post(
                self.embeddings_path,
                json=payload,
                headers={"Content-Type": "application/json", "Accept": "application/json"},
            )
            try:
                parsed = resp.json()
                if not isinstance(parsed, dict):
                    parsed = {"error": {"message": str(parsed), "type": "backend_error"}}
            except ValueError:
                parsed = {"error": {"message": resp.text or f"HTTP {resp.status_code}", "type": "backend_error"}}
            return resp.status_code, parsed
