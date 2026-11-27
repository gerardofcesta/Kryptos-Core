"""
Deterministic No-Progress Circuit Breaker.

Detects stagnant execution loops (repeated tool outputs, no new file slices inspected,
no change in hypothesis statuses or evidence) and trips after `limit` consecutive
stagnant steps, triggering Heavy MoE 177B Escalation or HALTED_CIRCUIT_BREAKER.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Iterable

_VOLATILE_PATTERNS = (
    re.compile(r"\b[0-9a-f]{12,64}\b", re.IGNORECASE),
    re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?\b"),
    re.compile(r"0x[0-9a-f]+", re.IGNORECASE),
)


class NoProgressCircuitBreaker:
    """Tracks deterministic progress across micro-steps and trips on stagnation."""

    def __init__(self, limit: int = 3) -> None:
        self.limit: int = max(1, int(limit))
        self.stagnant_streak: int = 0
        self._last_hypotheses_sig: str | None = None
        self._seen_files: set[str] = set()
        self._seen_outputs: set[tuple[str, str]] = set()
        self._total_steps: int = 0

    @staticmethod
    def fingerprint_output(output_text: str, exit_code: int | None = None) -> str:
        """Compute a stable SHA-256 fingerprint of tool output, normalizing volatile tokens."""
        normalized = (output_text or "").replace("\r\n", "\n").strip()
        for pat in _VOLATILE_PATTERNS:
            normalized = pat.sub("<VOL>", normalized)
        payload = f"rc={exit_code}|{normalized[:4096]}"
        return hashlib.sha256(payload.encode("utf-8", errors="replace")).hexdigest()

    @staticmethod
    def _hypotheses_signature(hypotheses_snapshot: list[dict[str, Any]]) -> str:
        canonical: list[dict[str, Any]] = []
        for item in hypotheses_snapshot or []:
            if not isinstance(item, dict):
                continue
            canonical.append(
                {
                    "id": str(item.get("id") or ""),
                    "status": str(item.get("status") or ""),
                    "evidence_hash": hashlib.sha256(
                        str(item.get("evidence") or "").strip().encode("utf-8")
                    ).hexdigest()[:16],
                    "poc_hash": hashlib.sha256(
                        str(item.get("poc_snippet") or "").strip().encode("utf-8")
                    ).hexdigest()[:16],
                    "slices": sorted(str(s) for s in (item.get("inspected_slices") or [])),
                }
            )
        canonical.sort(key=lambda x: x["id"])
        blob = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def record_step(
        self,
        hypotheses_snapshot: list[dict[str, Any]],
        files_read: Iterable[str],
        tool_name: str,
        output_hash: str,
    ) -> tuple[bool, str]:
        """
        Evaluate whether this step made measurable progress.

        Progress is defined deterministically as ANY of:
        1. Hypothesis state/evidence/PoC/inspected slices changed from the previous step.
        2. At least one previously unread file slice was read in `files_read`.
        3. A new distinct `(tool_name, output_hash)` result was observed (non-empty `output_hash`).

        Returns:
            `(tripped: bool, reason: str)` — `tripped` is True when `stagnant_streak >= limit`.
        """
        self._total_steps += 1
        hyp_sig = self._hypotheses_signature(hypotheses_snapshot)
        norm_tool = str(tool_name or "").strip().lower()
        norm_out = str(output_hash or "").strip()
        norm_files = [str(f).strip() for f in (files_read or []) if str(f).strip()]

        reasons: list[str] = []

        if self._last_hypotheses_sig is not None and hyp_sig != self._last_hypotheses_sig:
            reasons.append("hypotheses_changed")

        new_files = [f for f in norm_files if f not in self._seen_files]
        if new_files:
            reasons.append(f"new_files({len(new_files)})")

        out_key = (norm_tool, norm_out)
        if norm_out and out_key not in self._seen_outputs:
            reasons.append("new_output")

        # Update cumulative tracking
        self._last_hypotheses_sig = hyp_sig
        self._seen_files.update(norm_files)
        if norm_out:
            self._seen_outputs.add(out_key)

        if reasons:
            self.stagnant_streak = 0
            return False, "progress: " + ", ".join(reasons)

        self.stagnant_streak += 1
        if self.stagnant_streak >= self.limit:
            return (
                True,
                f"Circuit breaker tripped: no measurable progress for {self.stagnant_streak} "
                f"consecutive steps (limit={self.limit}, last_tool='{norm_tool}').",
            )
        return (
            False,
            f"stagnant step {self.stagnant_streak}/{self.limit} (tool='{norm_tool}')",
        )

    def reset(self, preserve_seen_files: bool = False) -> None:
        """Reset stagnant streak (e.g., after Heavy escalation or moving to a new hypothesis)."""
        self.stagnant_streak = 0
        self._last_hypotheses_sig = None
        self._seen_outputs.clear()
        if not preserve_seen_files:
            self._seen_files.clear()

    def to_dict(self) -> dict[str, Any]:
        return {
            "limit": self.limit,
            "stagnant_streak": self.stagnant_streak,
            "last_hypotheses_sig": self._last_hypotheses_sig,
            "seen_files": sorted(self._seen_files),
            "seen_outputs": [list(x) for x in sorted(self._seen_outputs)],
            "total_steps": self._total_steps,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "NoProgressCircuitBreaker":
        cb = cls(limit=int(data.get("limit") or 3))
        cb.stagnant_streak = int(data.get("stagnant_streak") or 0)
        cb._last_hypotheses_sig = data.get("last_hypotheses_sig")
        cb._seen_files = set(str(x) for x in (data.get("seen_files") or []))
        cb._seen_outputs = {
            (str(pair[0]), str(pair[1]))
            for pair in (data.get("seen_outputs") or [])
            if isinstance(pair, (list, tuple)) and len(pair) == 2
        }
        cb._total_steps = int(data.get("total_steps") or 0)
        return cb
