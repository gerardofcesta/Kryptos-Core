"""
Local LLM Autonomous Orchestrator Package (Pure Python 3.14 stdlib).

Exposes the singleton `OrchestratorManager` and `get_orchestrator_manager()` adhering to the
shared API contract with `api_router.py` and the offline console server (:8787).
"""

from __future__ import annotations

import threading

from .engine.state_machine import OrchestratorManager

_MANAGER_LOCK = threading.Lock()
_MANAGER_SINGLETON: OrchestratorManager | None = None


def get_orchestrator_manager() -> OrchestratorManager:
    """Return the process-wide singleton OrchestratorManager instance."""
    global _MANAGER_SINGLETON
    if _MANAGER_SINGLETON is None:
        with _MANAGER_LOCK:
            if _MANAGER_SINGLETON is None:
                _MANAGER_SINGLETON = OrchestratorManager()
    return _MANAGER_SINGLETON


__all__ = [
    "OrchestratorManager",
    "get_orchestrator_manager",
]
