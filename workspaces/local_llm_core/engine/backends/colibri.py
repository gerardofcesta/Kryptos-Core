"""Colibri Heavy MoE loopback backend (`127.0.0.1:11435`)."""

from __future__ import annotations

from ...config import SETTINGS
from ._base import OpenAICompatibleBackend


class ColibriBackend(OpenAICompatibleBackend):
    """Async loopback client for Colibri Heavy MoE OpenAI-compatible server."""

    def __init__(self, base_url: str | None = None) -> None:
        super().__init__(
            name="colibri",
            tier="heavy",
            base_url=base_url or SETTINGS.heavy_base,
            health_path="/health",
            models_path="/v1/models",
            chat_path="/v1/chat/completions",
            embeddings_path="/v1/embeddings",
            timeout=300.0,
        )
