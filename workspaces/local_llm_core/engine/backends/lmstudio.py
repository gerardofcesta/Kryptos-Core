"""LM Studio loopback backend (`127.0.0.1:1234`)."""

from __future__ import annotations

from ...config import SETTINGS
from ._base import OpenAICompatibleBackend


class LmStudioBackend(OpenAICompatibleBackend):
    """Async loopback client for local LM Studio server."""

    def __init__(self, base_url: str | None = None) -> None:
        super().__init__(
            name="lmstudio",
            tier="fast",
            base_url=base_url or SETTINGS.lms_base,
            health_path="/v1/models",
            models_path="/v1/models",
            chat_path="/v1/chat/completions",
            embeddings_path="/v1/embeddings",
            timeout=180.0,
        )
