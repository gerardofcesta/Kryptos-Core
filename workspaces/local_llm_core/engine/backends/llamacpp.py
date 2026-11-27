"""llama.cpp (`llama-server`) loopback backend (`127.0.0.1:8080`)."""

from __future__ import annotations

from ...config import SETTINGS
from ._base import OpenAICompatibleBackend


class LlamaCppBackend(OpenAICompatibleBackend):
    """Async loopback client for optional local `llama-server`."""

    def __init__(self, base_url: str | None = None) -> None:
        super().__init__(
            name="llamacpp",
            tier="fast",
            base_url=base_url or SETTINGS.llamacpp_base,
            health_path="/health",
            models_path="/v1/models",
            chat_path="/v1/chat/completions",
            embeddings_path="/v1/embeddings",
            timeout=180.0,
        )
