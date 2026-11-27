"""InferenceBackend protocol and loopback-enforced httpx client helpers."""

from __future__ import annotations

import urllib.parse
from collections.abc import AsyncIterator
from typing import Any, Protocol, runtime_checkable

import httpx

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def assert_loopback_url(url: str) -> str:
    """Validate that a backend URL targets loopback (`127.0.0.1`, `localhost`, `::1`)."""
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https") or host not in _LOOPBACK_HOSTS:
        raise ValueError(f"Non-loopback inference URL blocked: {url!r}")
    return url


def build_loopback_async_client(base_url: str, timeout: float = 120.0) -> httpx.AsyncClient:
    """Construct an `httpx.AsyncClient` pinned to a verified loopback base URL."""
    safe_base = assert_loopback_url(base_url)
    return httpx.AsyncClient(
        base_url=safe_base,
        timeout=httpx.Timeout(timeout, connect=5.0),
        trust_env=False,
    )


@runtime_checkable
class InferenceBackend(Protocol):
    """Unified async inference backend contract."""

    name: str
    tier: str
    base_url: str

    async def health(self) -> bool:
        """Return True if the backend is reachable and healthy."""
        ...

    async def list_models(self) -> list[str]:
        """Return model identifiers served by this backend."""
        ...

    async def chat_completions(
        self, payload: dict[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        """Execute a synchronous OpenAI-compatible chat completion."""
        ...

    def chat_completions_stream(
        self, payload: dict[str, Any]
    ) -> AsyncIterator[bytes]:
        """Stream SSE chunks from an OpenAI-compatible chat completion endpoint."""
        ...

    async def embeddings(
        self, payload: dict[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        """Execute an OpenAI-compatible embeddings request."""
        ...
