"""Abstract RunStore interface enforcing strict Retention Policy Dependency Injection."""

from __future__ import annotations

import pathlib
from abc import ABC, abstractmethod
from typing import Any


class RunStore(ABC):
    """
    Contract implemented identically by MemoryEphemeralStore (RAM-only) and
    DiskVaultStore (Persistent Audit Vault).

    Dependency Injection invariant:
    - When retention_mode == "ephemeral", ONLY MemoryEphemeralStore is instantiated.
    - When retention_mode == "persistent", DiskVaultStore is instantiated.
    """

    @property
    @abstractmethod
    def run_id(self) -> str:
        """Unique identifier for this orchestrator run."""

    @property
    @abstractmethod
    def retention_mode(self) -> str:
        """Immutable retention policy for this run: 'ephemeral' or 'persistent'."""

    @abstractmethod
    def save_mission(self, mission: dict[str, Any]) -> None:
        """Persist or hold mission specification and policy metadata."""

    @abstractmethod
    def get_mission(self) -> dict[str, Any] | None:
        """Return stored mission dict, or None if absent/purged."""

    @abstractmethod
    def save_surface_map(self, surface_map: dict[str, Any]) -> None:
        """Store deterministic Scout surface map (01_SURFACE_MAP)."""

    @abstractmethod
    def get_surface_map(self) -> dict[str, Any] | None:
        """Return surface map dict, or None if absent/purged."""

    @abstractmethod
    def save_hypotheses(self, hypotheses: list[dict[str, Any]]) -> None:
        """Store current hypothesis registry (02_HYPOTHESES)."""

    @abstractmethod
    def get_hypotheses(self) -> list[dict[str, Any]]:
        """Return current hypotheses list (empty if absent/purged)."""

    @abstractmethod
    def save_ledger(self, ledger_md: str) -> None:
        """Store compact evidence ledger markdown (03_EVIDENCE_LEDGER)."""

    @abstractmethod
    def get_ledger(self) -> str:
        """Return compact evidence ledger markdown (empty string if absent/purged)."""

    @abstractmethod
    def record_step_trace(self, entry: dict[str, Any]) -> None:
        """Record a single execution step event (disk JSONL in persistent, volatile in ephemeral)."""

    @abstractmethod
    def save_checkpoint(self, step_num: int, state: dict[str, Any]) -> None:
        """Persist step checkpoint for pause/resume (no-op in ephemeral)."""

    @abstractmethod
    def load_checkpoint(self) -> dict[str, Any] | None:
        """Load the latest saved checkpoint, or None if unavailable/ephemeral."""

    @abstractmethod
    def save_final_report(
        self, report_md: str, verdict: dict[str, Any] | None = None
    ) -> None:
        """Store FINAL_REPORT.md and formal verdict metadata."""

    @abstractmethod
    def get_final_report(self) -> str | None:
        """Retrieve final report markdown, or None if absent/purged."""

    @abstractmethod
    def rolling_purge_step(
        self, step_num: int, raw_buffers: list[Any] | None = None
    ) -> None:
        """Purge transient raw buffers produced during micro-step `step_num`."""

    @abstractmethod
    def purge_working_state_on_finish(self) -> None:
        """
        Immediately destroy all intermediate working state (mission, surface_map,
        hypotheses, ledger, transient traces) once final_report is produced.
        In ephemeral mode, leaves ONLY the final report buffer + minimal metadata in RAM.
        In persistent mode, this is a no-op so the audit archive remains intact.
        """

    @abstractmethod
    def purge_report(self) -> None:
        """Wipe the final report buffer (and cancel Dead-Man's Switch timer in ephemeral)."""

    @abstractmethod
    def get_artifacts_dir(self) -> pathlib.Path | None:
        """Return writable artifacts directory in persistent mode, or None in ephemeral."""

    @abstractmethod
    def get_kv_cache_dir(self) -> pathlib.Path | None:
        """Return isolated Colibri KV cache directory in persistent mode, or None in ephemeral."""

    @abstractmethod
    def auto_purge_remaining_sec(self) -> int | None:
        """Return seconds remaining until Dead-Man's Switch purge, or None if N/A."""

    def get_verdict(self) -> dict[str, Any] | None:
        """Return stored verdict metadata if available."""
        return None

    def has_report(self) -> bool:
        """Return True if a final report is currently readable."""
        return self.get_final_report() is not None
