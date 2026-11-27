"""Ollama loopback backend (`127.0.0.1:11434`)."""

from __future__ import annotations

from typing import Any

from ...config import SETTINGS
from ._base import OpenAICompatibleBackend


class OllamaBackend(OpenAICompatibleBackend):
    """Async loopback client for local Ollama OpenAI-compatible & native APIs."""

    def __init__(self, base_url: str | None = None) -> None:
        super().__init__(
            name="ollama",
            tier="fast",
            base_url=base_url or SETTINGS.ollama_base,
            health_path="/api/version",
            models_path="/api/tags",
            chat_path="/v1/chat/completions",
            embeddings_path="/v1/embeddings",
            timeout=180.0,
        )

    async def embeddings(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        status, data = await super().embeddings(payload)
        if status == 404:
            # Fallback for older Ollama builds exposing `/api/embed` instead of `/v1/embeddings`.
            async with self._client(timeout=120.0) as client:
                resp = await client.post(
                    "/api/embed",
                    json={"model": payload.get("model"), "input": payload.get("input")},
                    headers={"Content-Type": "application/json", "Accept": "application/json"},
                )
                if 200 <= resp.status_code < 300:
                    raw = resp.json()
                    vecs = raw.get("embeddings") or []
                    return 200, {
                        "object": "list",
                        "data": [
                            {"object": "embedding", "index": idx, "embedding": vec}
                            for idx, vec in enumerate(vecs)
                        ],
                        "model": raw.get("model") or payload.get("model"),
                        "usage": {
                            "prompt_tokens": int(raw.get("prompt_eval_count") or 0),
                            "total_tokens": int(raw.get("prompt_eval_count") or 0),
                        },
                    }
                return resp.status_code, {"error": {"message": resp.text, "type": "backend_error"}}
        return status, data
