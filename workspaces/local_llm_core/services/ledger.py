"""In-memory Cost/Token Ledger & Budget Cap enforcement (zero disk writes)."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

from ..config import SETTINGS

# Reference API-equivalent USD rates per 1M tokens (for cost-savings & budget tracking).
_MODEL_TIER_RATES_PER_1M: dict[str, tuple[float, float]] = {
    "local-fast": (0.15, 0.60),
    "local-balanced": (0.50, 1.50),
    "local-heavy": (2.50, 10.00),
}


@dataclass
class WorkflowLedgerEntry:
    """In-memory token & cost accumulator for a single `(app_id, workflow_id)` pair."""

    app_id: str
    workflow_id: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    requests: int = 0
    retries: int = 0
    escalations: int = 0
    estimated_cost_usd: float = 0.0
    max_tokens_cap: int = SETTINGS.default_max_tokens_per_workflow
    max_cost_usd_cap: float = SETTINGS.default_max_cost_usd_per_workflow
    by_model: dict[str, dict[str, int | float]] = field(default_factory=dict)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "app_id": self.app_id,
            "workflow_id": self.workflow_id,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "requests": self.requests,
            "retries": self.retries,
            "escalations": self.escalations,
            "estimated_cost_usd": round(self.estimated_cost_usd, 6),
            "budget": {
                "max_tokens": self.max_tokens_cap,
                "max_cost_usd": self.max_cost_usd_cap,
                "remaining_tokens": max(0, self.max_tokens_cap - self.total_tokens),
                "remaining_cost_usd": round(max(0.0, self.max_cost_usd_cap - self.estimated_cost_usd), 6),
                "exceeded": self.is_exceeded(),
            },
            "by_model": self.by_model,
            "updated_at": self.updated_at,
        }

    def is_exceeded(self) -> bool:
        if self.max_tokens_cap > 0 and self.total_tokens >= self.max_tokens_cap:
            return True
        if self.max_cost_usd_cap > 0 and self.estimated_cost_usd >= self.max_cost_usd_cap:
            return True
        return False


class BudgetExceededError(RuntimeError):
    """Raised when a workflow has exhausted its token or cost budget cap."""

    def __init__(self, app_id: str, workflow_id: str, detail: str) -> None:
        super().__init__(detail)
        self.app_id = app_id
        self.workflow_id = workflow_id
        self.detail = detail


class CostTokenLedger:
    """Thread-safe RAM-only Cost/Token Ledger keyed by `(X-App-Id, X-Workflow-Id)`."""

    def __init__(
        self,
        default_max_tokens: int | None = None,
        default_max_cost_usd: float | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._entries: dict[tuple[str, str], WorkflowLedgerEntry] = {}
        self._default_max_tokens = (
            default_max_tokens
            if default_max_tokens is not None
            else SETTINGS.default_max_tokens_per_workflow
        )
        self._default_max_cost_usd = (
            default_max_cost_usd
            if default_max_cost_usd is not None
            else SETTINGS.default_max_cost_usd_per_workflow
        )

    @staticmethod
    def _norm_id(value: str | None, default: str = "default") -> str:
        clean = (value or "").strip()
        return clean[:96] if clean else default

    def _get_or_create_locked(self, app_id: str, workflow_id: str) -> WorkflowLedgerEntry:
        key = (app_id, workflow_id)
        entry = self._entries.get(key)
        if entry is None:
            entry = WorkflowLedgerEntry(
                app_id=app_id,
                workflow_id=workflow_id,
                max_tokens_cap=self._default_max_tokens,
                max_cost_usd_cap=self._default_max_cost_usd,
            )
            self._entries[key] = entry
        return entry

    def set_budget(
        self,
        app_id: str | None,
        workflow_id: str | None,
        *,
        max_tokens: int | None = None,
        max_cost_usd: float | None = None,
    ) -> dict[str, Any]:
        """Configure or update token/cost caps for a `(app_id, workflow_id)` workflow."""
        a_id = self._norm_id(app_id)
        w_id = self._norm_id(workflow_id)
        with self._lock:
            entry = self._get_or_create_locked(a_id, w_id)
            if max_tokens is not None:
                entry.max_tokens_cap = max(1, int(max_tokens))
            if max_cost_usd is not None:
                entry.max_cost_usd_cap = max(0.0, float(max_cost_usd))
            return entry.to_dict()

    def check_budget(
        self,
        app_id: str | None,
        workflow_id: str | None,
        request_max_tokens_cap: int | None = None,
    ) -> None:
        """Raise `BudgetExceededError` if workflow budget is already exhausted."""
        a_id = self._norm_id(app_id)
        w_id = self._norm_id(workflow_id)
        with self._lock:
            entry = self._get_or_create_locked(a_id, w_id)
            if request_max_tokens_cap is not None and request_max_tokens_cap > 0:
                entry.max_tokens_cap = min(entry.max_tokens_cap, int(request_max_tokens_cap))
            if entry.is_exceeded():
                raise BudgetExceededError(
                    a_id,
                    w_id,
                    (
                        f"Budget cap exceeded for app='{a_id}', workflow='{w_id}' "
                        f"(tokens={entry.total_tokens}/{entry.max_tokens_cap}, "
                        f"cost=${entry.estimated_cost_usd:.4f}/${entry.max_cost_usd_cap:.4f})."
                    ),
                )

    @staticmethod
    def estimate_cost_usd(
        tier_alias: str, prompt_tokens: int, completion_tokens: int
    ) -> float:
        in_rate, out_rate = _MODEL_TIER_RATES_PER_1M.get(tier_alias, (0.50, 1.50))
        return (max(0, prompt_tokens) * in_rate + max(0, completion_tokens) * out_rate) / 1_000_000.0

    def record_usage(
        self,
        app_id: str | None,
        workflow_id: str | None,
        *,
        model_alias: str,
        prompt_tokens: int,
        completion_tokens: int,
        retries: int = 0,
        escalated: bool = False,
        count_request: bool = True,
    ) -> dict[str, Any]:
        """Record token usage, retry count, and API-equivalent cost in RAM."""
        a_id = self._norm_id(app_id)
        w_id = self._norm_id(workflow_id)
        p_tok = max(0, int(prompt_tokens))
        c_tok = max(0, int(completion_tokens))
        t_tok = p_tok + c_tok
        cost = self.estimate_cost_usd(model_alias, p_tok, c_tok)

        with self._lock:
            entry = self._get_or_create_locked(a_id, w_id)
            entry.prompt_tokens += p_tok
            entry.completion_tokens += c_tok
            entry.total_tokens += t_tok
            if count_request:
                entry.requests += 1
            entry.retries += max(0, int(retries))
            if escalated:
                entry.escalations += 1
            entry.estimated_cost_usd += cost
            entry.updated_at = time.time()

            m_stats = entry.by_model.setdefault(
                model_alias,
                {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "requests": 0, "cost_usd": 0.0},
            )
            m_stats["prompt_tokens"] = int(m_stats["prompt_tokens"]) + p_tok
            m_stats["completion_tokens"] = int(m_stats["completion_tokens"]) + c_tok
            m_stats["total_tokens"] = int(m_stats["total_tokens"]) + t_tok
            m_stats["requests"] = int(m_stats["requests"]) + (1 if count_request else 0)
            m_stats["cost_usd"] = round(float(m_stats["cost_usd"]) + cost, 6)
            return entry.to_dict()

    def get_snapshot(
        self, app_id: str | None = None, workflow_id: str | None = None
    ) -> dict[str, Any]:
        """Return an in-memory snapshot of the ledger (zero disk writes)."""
        a_filter = (app_id or "").strip() or None
        w_filter = (workflow_id or "").strip() or None
        with self._lock:
            items: list[dict[str, Any]] = []
            tot_prompt = 0
            tot_comp = 0
            tot_req = 0
            tot_retries = 0
            tot_escalations = 0
            tot_cost = 0.0
            for entry in self._entries.values():
                if a_filter and entry.app_id != a_filter:
                    continue
                if w_filter and entry.workflow_id != w_filter:
                    continue
                items.append(entry.to_dict())
                tot_prompt += entry.prompt_tokens
                tot_comp += entry.completion_tokens
                tot_req += entry.requests
                tot_retries += entry.retries
                tot_escalations += entry.escalations
                tot_cost += entry.estimated_cost_usd

        return {
            "ok": True,
            "retention_mode": "ephemeral",
            "storage": "ram-only",
            "totals": {
                "prompt_tokens": tot_prompt,
                "completion_tokens": tot_comp,
                "total_tokens": tot_prompt + tot_comp,
                "requests": tot_req,
                "retries": tot_retries,
                "escalations": tot_escalations,
                "estimated_cost_usd": round(tot_cost, 6),
            },
            "workflows": items,
            "entries": items,
        }

    def reset(self) -> None:
        """Clear all in-memory ledger entries."""
        with self._lock:
            self._entries.clear()


_LEDGER_SINGLETON = CostTokenLedger()


def get_cost_ledger() -> CostTokenLedger:
    """Return the process-wide RAM-only CostTokenLedger singleton."""
    return _LEDGER_SINGLETON
