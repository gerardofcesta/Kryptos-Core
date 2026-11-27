"""Local Ghost (RAM-only) and Vault (disk-isolated) RAG engine powered by `doc-ingest-core`."""

from __future__ import annotations

from .store import (
    DEFAULT_VAULT_RAG_ROOT,
    GhostRagStore,
    RagChunkRecord,
    RagQueryHit,
    RagStoreManager,
    VaultRagStore,
    get_rag_manager,
)

__all__ = [
    "DEFAULT_VAULT_RAG_ROOT",
    "GhostRagStore",
    "RagChunkRecord",
    "RagQueryHit",
    "RagStoreManager",
    "VaultRagStore",
    "get_rag_manager",
]
