"""
Asynchronous 4-Role Phased State Machine (Blackboard & Context Reset) & OrchestratorManager.

Roles & Execution Flow:
1. SCOUT (Deterministic, No-LLM)      -> 01_SURFACE_MAP
2. STRATEGIST (Heavy 177B / Fast-only) -> 02_HYPOTHESES (or immediate IMPOSSIBLE_PROVEN)
3. Swap -> Fast Tier (if dual_tier)
4. INVESTIGATOR (Fast Tier loop, Context Reset per hypothesis, Rolling Purge in ephemeral,
   atomic checkpoint per step in persistent, NoProgressCircuitBreaker -> ESCALATE)
5. VALIDATOR (Deterministic, No-LLM)   -> schema & sandbox-evidence verification
6. Swap -> Heavy 177B (if dual_tier)
7. ADJUDICATOR (Heavy 177B / Fast-only)-> resolves escalations, emits formal Verdict + FINAL_REPORT.md
8. Ephemeral Finish Purge              -> immediately destroys all working state, leaving ONLY
   FINAL_REPORT in RAM under Dead-Man's Switch + explicit purge_report().

Strict Dependency Injection:
- When retention_mode == "ephemeral", ONLY MemoryEphemeralStore is imported and instantiated;
  DiskVaultStore is NEVER instantiated.
- When retention_mode == "persistent", DiskVaultStore is instantiated.
"""

from __future__ import annotations

import datetime
import inspect
import json
import os
import pathlib
import re
import secrets
import threading
import time
from typing import Any, Callable

from ..storage.base import RunStore
from .circuit_breaker import NoProgressCircuitBreaker
from .schemas import (
    HYPOTHESIS_STATUSES,
    IMPOSSIBLE_CATEGORIES,
    MAX_LEDGER_CHARS,
    TERMINAL_HYPOTHESIS_STATUSES,
    Hypothesis,
    RunRequest,
    SchemaValidationError,
    Verdict,
    compact_ledger_text,
    validate_verdict,
)
from .tier_switcher import TierInferenceError, TierSwitcher
from .tool_protocol import (
    ProtocolParseError,
    ToolCall,
    call_with_self_correction,
    parse_json_object,
    parse_tool_calls,
)

DEFAULT_TOOL_NAMES: tuple[str, ...] = (
    "list_files",
    "read_file_slice",
    "search_code",
    "run_python_sandbox",
    "update_hypothesis",
)

ACTIVE_FSM_STATES = frozenset(
    {
        "STARTING",
        "SCOUT",
        "STRATEGIST",
        "INVESTIGATOR",
        "VALIDATOR",
        "ADJUDICATOR",
        "PAUSED",
    }
)

_SAFE_ARTIFACT_ID_RE = re.compile(r"[^a-zA-Z0-9_-]+")


def _utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()


DEFAULT_HOST_READONLY_TOOL_NAMES: tuple[str, ...] = (
    "list_files",
    "read_file_slice",
    "search_code",
    "update_hypothesis",
    "code_audit",
)


def _lazy_sandbox_imports() -> tuple[Any, Any, Any, tuple[str, ...]]:
    """Import sandbox components gracefully so headless/unit tests can inject mocks."""
    try:
        from ..sandbox import (
            TOOL_NAMES,
            DockerSandboxRunner,
            SandboxToolkit,
            run_deterministic_scout,
        )

        return DockerSandboxRunner, run_deterministic_scout, SandboxToolkit, tuple(TOOL_NAMES)
    except Exception:
        return None, None, None, DEFAULT_TOOL_NAMES


def _lazy_host_readonly_toolkit() -> tuple[Any, tuple[str, ...]]:
    try:
        from ..sandbox import HOST_READONLY_TOOL_NAMES, HostReadOnlyToolkit

        return HostReadOnlyToolkit, tuple(HOST_READONLY_TOOL_NAMES)
    except Exception:
        return None, DEFAULT_HOST_READONLY_TOOL_NAMES


def _default_tools_prompt() -> str:
    return (
        "## Sandbox Tools (Offline Docker `--network none`, `/target` read-only)\n"
        'Invoke a tool with exact syntax: <tool_call name="TOOL_NAME">{"arg": "value"}</tool_call>\n'
        "Tools:\n"
        '1. list_files: {"subdir": "", "glob": "*", "max_entries": 100}\n'
        '2. read_file_slice: {"rel_path": "app.py", "start_line": 1, "end_line": 120}\n'
        '3. search_code: {"pattern": "regex", "glob": "*.py", "max_matches": 40}\n'
        '4. run_python_sandbox: {"code": "python script", "timeout_sec": 60}\n'
        '5. update_hypothesis: {"id": "H1", "status": "CONFIRMED|REFUTED|BLOCKED|ESCALATE|TESTING", '
        '"evidence": "...", "poc_snippet": "..."}\n'
    )


def _build_strategist_messages(mission: str, surface_map: dict[str, Any]) -> list[dict[str, str]]:
    compact_map = str(surface_map.get("compactSummary") or "")
    if not compact_map:
        compact_map = json.dumps(surface_map, ensure_ascii=False)[:4800]
    compact_map = compact_map[:4800]

    sys_prompt = (
        "You are the STRATEGIST in an offline autonomous code/data audit orchestrator.\n"
        "The execution sandbox has ZERO network access (--network none) and `/target` is read-only.\n"
        "Respond with a SINGLE JSON object ONLY (no markdown outside JSON).\n"
        "Option A — If the mission CANNOT be completed offline because it requires missing external "
        "assets/network/remote APIs or contains a logical contradiction, return:\n"
        '{"status":"IMPOSSIBLE_PROVEN","impossible_category":"MISSING_EXTERNAL_DEPENDENCY|LOGICAL_CONTRADICTION",'
        '"impossible_proof":"concrete structured proof >= 20 chars","summary":"concise summary"}\n'
        "Option B — Otherwise generate 3 to 6 concrete, testable hypotheses based on the surface map:\n"
        '{"status":"HYPOTHESES_READY","hypotheses":[{"id":"H1","title":"...","target_files":["file.py"],"risk_area":"..."}]}'
    )
    user_prompt = (
        f"## Mission\n{mission[:3000]}\n\n"
        f"## Deterministic Surface Map\n{compact_map}\n\n"
        "Return ONLY the JSON object."
    )
    return [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": user_prompt},
    ]


def _build_investigator_messages(
    mission: str,
    hyp: Hypothesis,
    ledger: str,
    surface_map: dict[str, Any],
    tools_desc: str,
    host_readonly: bool = False,
) -> list[dict[str, str]]:
    # Strictly bounded context (< 8,000 tokens; reset cleanly per hypothesis)
    relevant_ast: list[dict[str, Any]] = []
    target_set = set(hyp.target_files)
    for item in (surface_map.get("pythonAst") or []):
        if isinstance(item, dict) and (
            not target_set or str(item.get("path") or "") in target_set
        ):
            relevant_ast.append(item)
            if len(relevant_ast) >= 5:
                break

    ast_excerpt = json.dumps(relevant_ast, ensure_ascii=False)[:2000] if relevant_ast else "[]"
    compact_ledger = compact_ledger_text(ledger, max_chars=4000)

    verify_rule = (
        "Before marking a hypothesis CONFIRMED, you MUST verify it directly with "
        "`read_file_slice`, `search_code`, or `code_audit` and cite concrete file/line evidence."
        if host_readonly
        else "Before marking a hypothesis CONFIRMED, you MUST execute a reproducible PoC via "
        "`run_python_sandbox` and observe real verification evidence."
    )
    sys_prompt = (
        "You are the INVESTIGATOR in an offline autonomous audit orchestrator.\n"
        "Investigate ONLY the assigned hypothesis step by step using the sandbox tools.\n"
        f"{verify_rule}\n"
        "Once you have conclusive evidence, call `update_hypothesis` with status CONFIRMED, "
        "REFUTED, BLOCKED, or ESCALATE.\n\n"
        f"{tools_desc}"
    )
    user_prompt = (
        f"## Mission\n{mission[:1200]}\n\n"
        f"## Active Hypothesis\n{json.dumps(hyp.to_dict(), ensure_ascii=False)}\n\n"
        f"## Relevant AST / Surface Excerpt\n{ast_excerpt}\n\n"
        f"## Compact Evidence Ledger\n{compact_ledger or '(empty)'}\n\n"
        "Invoke the next tool call using <tool_call name=\"...\">...</tool_call>."
    )
    return [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": user_prompt},
    ]


def _build_adjudicator_messages(
    mission: str,
    hypotheses: list[Hypothesis],
    ledger: str,
    escalated_notes: list[str],
    embed_poc: bool,
) -> list[dict[str, str]]:
    # Strictly bounded input (< 4,000 tokens / ~12k chars for fast Heavy MoE prefill)
    hyp_summary = [h.to_dict() for h in hypotheses]
    hyp_json = json.dumps(hyp_summary, ensure_ascii=False)[:4500]
    compact_ledger = compact_ledger_text(ledger, max_chars=4500)
    esc_text = "\n".join(escalated_notes[-10:])[:1500]

    sys_prompt = (
        "You are the ADJUDICATOR / CRITIC in an offline autonomous audit orchestrator.\n"
        "Review the evidence ledger and hypotheses, resolve any ESCALATE items if evidence permits, "
        "and issue a formal mandatory 3-outcome Verdict plus a concise Markdown report.\n"
        "Verdict rules:\n"
        "- OBJECTIVE_ACHIEVED: requires >= 1 CONFIRMED hypothesis OR all hypotheses resolved.\n"
        "- IMPOSSIBLE_PROVEN: requires impossible_category in "
        "(MISSING_EXTERNAL_DEPENDENCY, LOGICAL_CONTRADICTION, SEARCH_SPACE_EXHAUSTED) "
        "and a non-empty structured impossible_proof.\n"
        "- HALTED_CIRCUIT_BREAKER: use if unresolved escalations remain after stalling.\n"
        "Respond with a SINGLE JSON object ONLY:\n"
        '{"resolved_hypotheses":[{"id":"H3","status":"REFUTED|CONFIRMED|BLOCKED","evidence":"...","poc_snippet":""}],'
        '"verdict":{"code":"OBJECTIVE_ACHIEVED|IMPOSSIBLE_PROVEN|HALTED_CIRCUIT_BREAKER",'
        '"summary":"...","impossible_category":null,"impossible_proof":null,'
        '"confirmed_ids":["H1"],"refuted_ids":["H2"]},'
        '"report_markdown":"# Final Audit Report\\n..."}'
    )
    user_prompt = (
        f"## Mission\n{mission[:1500]}\n\n"
        f"## Hypotheses Registry\n{hyp_json}\n\n"
        f"## Compact Evidence Ledger\n{compact_ledger or '(empty)'}\n\n"
        f"## Escalation / Validator Notes\n{esc_text or '(none)'}\n\n"
        f"## PoC Embedding Policy\nembed_verified_poc_in_report={embed_poc}\n\n"
        "Return ONLY the JSON object."
    )
    return [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": user_prompt},
    ]


def _render_report_with_pocs(
    base_report: str,
    verdict: Verdict,
    hypotheses: list[Hypothesis],
    embed_poc: bool,
    retention_mode: str,
) -> str:
    report = (base_report or "").strip()
    if not report:
        lines = [
            f"# Autonomous Audit Report — {verdict.code}",
            "",
            f"- **Retention Policy:** `{retention_mode}`",
            f"- **Verdict:** `{verdict.code}`",
        ]
        if verdict.impossible_category:
            lines.append(f"- **Impossibility Category:** `{verdict.impossible_category}`")
        lines.extend(["", "## Summary", verdict.summary])
        if verdict.impossible_proof:
            lines.extend(["", "## Structured Impossibility Proof", verdict.impossible_proof])
        if hypotheses:
            lines.extend(["", "## Hypotheses Findings"])
            for h in hypotheses:
                lines.append(f"- **{h.id} [{h.status}]** — {h.title}: {h.evidence or '(no evidence)'}")
        report = "\n".join(lines)

    if embed_poc:
        confirmed_with_poc = [
            h for h in hypotheses if h.status == "CONFIRMED" and h.poc_snippet.strip()
        ]
        missing_pocs = [
            h for h in confirmed_with_poc if h.poc_snippet.strip() not in report
        ]
        if missing_pocs:
            poc_sections = ["", "## Verified PoC Snippets"]
            for h in missing_pocs:
                poc_sections.append(f"\n### {h.id}: {h.title}")
                poc_sections.append("```python")
                poc_sections.append(h.poc_snippet.strip())
                poc_sections.append("```")
            report = report + "\n" + "\n".join(poc_sections)

    return report.strip() + "\n"


class _RunController:
    """Internal execution state and 4-role async worker for a single Orchestrator run."""

    def __init__(
        self,
        req: RunRequest,
        store: RunStore,
        tier_switcher: TierSwitcher,
        sandbox_runner: Any,
        scout_fn: Callable[..., dict[str, Any]] | None,
        toolkit_factory: Callable[..., Any] | None,
        tool_names: tuple[str, ...],
        resume_checkpoint: dict[str, Any] | None = None,
        custom_toolkit_injected: bool = False,
    ) -> None:
        self.req = req
        self.store = store
        self.tier_switcher = tier_switcher
        self.sandbox_runner = sandbox_runner
        self.scout_fn = scout_fn
        self.toolkit_factory = toolkit_factory
        self.tool_names = tool_names
        self._active_tool_names: tuple[str, ...] = tool_names
        self._custom_toolkit_injected = custom_toolkit_injected
        self.resolved_sandbox_mode: str = (
            "host_readonly" if req.sandbox_mode == "host_readonly" else "docker"
        )
        self._resume_checkpoint = resume_checkpoint

        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._pause_requested = threading.Event()
        self._resume_event = threading.Event()
        self._resume_event.set()
        self._done_event = threading.Event()

        self.phase: str = "STARTING"
        self.active_engine: str = "none"
        self.step: int = 0
        self.escalations_used: int = 0
        self.started_monotonic: float = time.monotonic()
        self.started_at: str = _utc_now_iso()
        self.finished_at: str | None = None
        self.error: str | None = None
        self.verdict: Verdict | None = None

        self._surface_map: dict[str, Any] | None = None
        self._hypotheses: list[Hypothesis] = []
        self._ledger: str = ""
        self._events: list[dict[str, Any]] = []
        self._escalated_notes: list[str] = []
        self._circuit_breaker = NoProgressCircuitBreaker(limit=req.budget.no_progress_limit)
        self._worker: threading.Thread | None = None

    def start(self) -> None:
        self._worker = threading.Thread(
            target=self._run_safe,
            name=f"orchestrator-fsm-{self.store.run_id[:20]}",
            daemon=True,
        )
        self._worker.start()

    def _emit_event(self, phase: str, message: str, extra: dict[str, Any] | None = None) -> None:
        evt: dict[str, Any] = {
            "ts": _utc_now_iso(),
            "phase": phase,
            "step": self.step,
            "message": message,
        }
        if extra:
            evt.update(extra)
        with self._lock:
            self._events.append(evt)
            if len(self._events) > 80:
                self._events = self._events[-80:]

    def _set_phase(self, phase: str) -> None:
        with self._lock:
            self.phase = phase

    def request_pause(self) -> tuple[int, dict[str, Any]]:
        if self.req.retention_mode == "ephemeral":
            return 409, {
                "ok": False,
                "error": "Pause/Resume is rejected in 'ephemeral' mode (zero-disk amnesia policy).",
            }
        with self._lock:
            if self.phase not in ACTIVE_FSM_STATES:
                return 409, {"ok": False, "error": f"Cannot pause run in state '{self.phase}'."}
            if self.phase == "PAUSED":
                return 200, {
                    "ok": True,
                    "run_id": self.store.run_id,
                    "state": "PAUSED",
                    "step": self.step,
                }
            self._pause_requested.set()
            self._resume_event.clear()
            self._emit_event("PAUSED", "Pause requested; saving checkpoint at step boundary.")
        return 200, {
            "ok": True,
            "run_id": self.store.run_id,
            "state": "PAUSED",
            "step": self.step,
            "pause_requested": True,
        }

    def request_resume(self) -> tuple[int, dict[str, Any]]:
        if self.req.retention_mode == "ephemeral":
            return 409, {
                "ok": False,
                "error": "Resume is rejected in 'ephemeral' mode.",
            }
        with self._lock:
            if self.phase != "PAUSED" and not self._pause_requested.is_set():
                return 409, {"ok": False, "error": f"Run is not paused (current state: '{self.phase}')."}
            self._pause_requested.clear()
            self._resume_event.set()
            self._emit_event("RESUMED", f"Resuming persistent run from step {self.step}.")
        return 200, {
            "ok": True,
            "run_id": self.store.run_id,
            "resumed": True,
            "step": self.step,
        }

    def request_stop(self) -> None:
        self._stop_event.set()
        self._pause_requested.clear()
        self._resume_event.set()
        self._cleanup_containers_best_effort()

    def _cleanup_containers_best_effort(self) -> None:
        if self.sandbox_runner is not None:
            fn = getattr(self.sandbox_runner, "cleanup_run_containers", None)
            if callable(fn):
                try:
                    fn(self.store.run_id)
                except Exception:
                    pass

    def _checkpoint_if_persistent(self, next_stage: str) -> None:
        if self.req.retention_mode != "persistent":
            return
        state_payload = {
            "phase": self.phase,
            "next_stage": next_stage,
            "step": self.step,
            "escalations_used": self.escalations_used,
            "escalated_notes": list(self._escalated_notes),
            "circuit_breaker": self._circuit_breaker.to_dict(),
        }
        self.store.save_hypotheses([h.to_dict() for h in self._hypotheses])
        self.store.save_ledger(self._ledger)
        self.store.save_checkpoint(self.step, state_payload)

    def _wait_if_paused(self, resume_stage: str) -> bool:
        """Pause cleanly at step boundaries (persistent only). Returns False if stopped."""
        if self._stop_event.is_set():
            return False
        if self._pause_requested.is_set() and self.req.retention_mode == "persistent":
            prev_phase = self.phase
            self._set_phase("PAUSED")
            self._checkpoint_if_persistent(resume_stage)
            self._emit_event("PAUSED", f"Paused cleanly at step {self.step} (checkpoint saved).")
            while self._pause_requested.is_set() and not self._stop_event.is_set():
                self._resume_event.wait(timeout=0.2)
            if self._stop_event.is_set():
                return False
            self._set_phase(prev_phase)
        return not self._stop_event.is_set()

    def _ensure_tier_for_role(self, role_tier: str) -> None:
        desired = "fast" if self.req.execution_mode == "fast_only" else role_tier
        allow_kv = (
            self.req.retention_mode == "persistent"
            and self.req.ephemeral_rules.allow_kv_save
        )
        kv_dir = self.store.get_kv_cache_dir() if allow_kv else None
        ok, msg = self.tier_switcher.ensure_tier(
            target_tier=desired,
            retention_mode=self.req.retention_mode,
            allow_kv_save=allow_kv,
            kv_cache_dir=kv_dir,
        )
        if not ok:
            raise TierInferenceError(f"Failed switching to '{desired}' tier: {msg}")
        with self._lock:
            self.active_engine = desired
        self._emit_event(self.phase, f"Active engine: {desired} ({msg})")

    @staticmethod
    def _resolve_fast_num_ctx(model_name: str) -> int:
        raw_env = os.environ.get("ORCHESTRATOR_FAST_NUM_CTX", "").strip()
        if raw_env:
            try:
                return max(2048, min(131072, int(raw_env)))
            except ValueError:
                pass
        is_worker = str(model_name or "").strip().lower().startswith("local-worker")
        hw_profile = pathlib.Path(__file__).resolve().parents[3] / "config" / "hardware-profile.json"
        if hw_profile.is_file():
            try:
                data = json.loads(hw_profile.read_text(encoding="utf-8", errors="replace"))
                tiers = data.get("tiers", {}) if isinstance(data, dict) else {}
                tier_cfg = tiers.get("fastWorker" if is_worker else "fast") or {}
                if isinstance(tier_cfg, dict) and tier_cfg.get("ctxDefault"):
                    return max(2048, min(131072, int(tier_cfg["ctxDefault"])))
            except (OSError, ValueError, TypeError):
                pass
        return 32768 if is_worker else 16384

    def _call_tier_llm(
        self,
        role_tier: str,
        messages: list[dict[str, str]],
        json_schema: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        desired = "fast" if self.req.execution_mode == "fast_only" else role_tier
        if desired == "heavy":
            return self.tier_switcher.chat_heavy(
                messages=messages,
                model=self.req.heavy_model,
                temperature=0.2,
                max_tokens=2048,
            )
        return self.tier_switcher.chat_ollama(
            messages=messages,
            model=self.req.fast_model,
            temperature=0.2,
            num_ctx=self._resolve_fast_num_ctx(self.req.fast_model),
            json_schema=json_schema,
        )

    def _append_ledger(self, line: str) -> None:
        entry = (line or "").strip()
        if not entry:
            return
        with self._lock:
            combined = f"{self._ledger}\n{entry}".strip() if self._ledger else entry
            self._ledger = compact_ledger_text(combined, MAX_LEDGER_CHARS)
        self.store.save_ledger(self._ledger)

    def _update_hypothesis_callback(self, payload: dict[str, Any]) -> dict[str, Any]:
        hid = str(payload.get("id") or "").strip()
        status = str(payload.get("status") or "").strip().upper()
        evidence = str(payload.get("evidence") or "").strip()
        poc_snippet = str(payload.get("poc_snippet") or "").strip()

        if status not in HYPOTHESIS_STATUSES:
            return {"updated": False, "error": f"Invalid status '{status}'"}

        with self._lock:
            target = next((h for h in self._hypotheses if h.id == hid), None)
            if target is None:
                # Allow Investigator to register a newly discovered hypothesis cleanly
                target = Hypothesis(
                    id=hid,
                    title=evidence[:120] or f"Discovered hypothesis {hid}",
                    status=status,
                    evidence=evidence,
                    poc_snippet=poc_snippet,
                )
                self._hypotheses.append(target)
            else:
                target.status = status
                if evidence:
                    target.evidence = evidence
                if poc_snippet:
                    target.poc_snippet = poc_snippet
            snapshot = [h.to_dict() for h in self._hypotheses]

        self.store.save_hypotheses(snapshot)
        return {"updated": True, "id": hid, "status": status}

    def _build_toolkit(self) -> Any:
        artifacts_dir = (
            self.store.get_artifacts_dir() if self.req.retention_mode == "persistent" else None
        )
        if not self._custom_toolkit_injected and self.resolved_sandbox_mode == "host_readonly":
            host_cls, host_tools = _lazy_host_readonly_toolkit()
            if host_cls is not None:
                self._active_tool_names = host_tools
                return host_cls(
                    target_path=self.req.target_path,
                    retention_mode=self.req.retention_mode,
                    run_id=self.store.run_id,
                    artifacts_path=artifacts_dir,
                )
        if self.toolkit_factory is None:
            raise RuntimeError("SandboxToolkit is unavailable.")
        self._active_tool_names = self.tool_names
        return self.toolkit_factory(
            runner=self.sandbox_runner,
            target_path=self.req.target_path,
            retention_mode=self.req.retention_mode,
            run_id=self.store.run_id,
            artifacts_path=artifacts_dir,
        )

    def _finalize_with_verdict(self, verdict: Verdict, report_md: str) -> None:
        rendered = _render_report_with_pocs(
            base_report=report_md,
            verdict=verdict,
            hypotheses=self._hypotheses,
            embed_poc=self.req.ephemeral_rules.embed_verified_poc_in_report,
            retention_mode=self.req.retention_mode,
        )
        verdict_dict = verdict.to_dict()
        self.store.save_final_report(rendered, verdict=verdict_dict)

        if self.req.retention_mode == "persistent":
            self._save_persistent_poc_artifacts()
            self._checkpoint_if_persistent("FINISHED")

        terminal_phase = {
            "OBJECTIVE_ACHIEVED": "DONE",
            "IMPOSSIBLE_PROVEN": "IMPOSSIBLE",
            "HALTED_CIRCUIT_BREAKER": "HALTED",
        }.get(verdict.code, "HALTED")

        with self._lock:
            self.verdict = verdict
            self.finished_at = _utc_now_iso()
            self.phase = terminal_phase

        # CRITICAL EPHEMERAL PRIVACY INVARIANT:
        # Immediately destroy all working state once final_report is saved, leaving ONLY
        # `_report_buffer` + minimal metadata (`verdict`, `retention_mode`) in RAM!
        if self.req.retention_mode == "ephemeral":
            self.store.purge_working_state_on_finish()
            with self._lock:
                self._surface_map = None
                self._hypotheses = []
                self._ledger = ""
                self._escalated_notes = []
                self._events = [
                    {
                        "ts": _utc_now_iso(),
                        "phase": terminal_phase,
                        "step": self.step,
                        "message": f"Run completed ({verdict.code}); ephemeral working state purged.",
                    }
                ]

    def _save_persistent_poc_artifacts(self) -> None:
        artifacts_dir = self.store.get_artifacts_dir()
        if artifacts_dir is None:
            return
        for h in self._hypotheses:
            if h.status == "CONFIRMED" and h.poc_snippet.strip():
                safe_hid = _SAFE_ARTIFACT_ID_RE.sub("_", h.id).strip("_") or "poc"
                poc_path = artifacts_dir / f"poc_{safe_hid.lower()}.py"
                tmp_path = artifacts_dir / f".poc_{safe_hid.lower()}.{secrets.token_hex(4)}.tmp"
                try:
                    tmp_path.write_text(h.poc_snippet.strip() + "\n", encoding="utf-8")
                    tmp_path.replace(poc_path)
                except OSError:
                    try:
                        if tmp_path.exists():
                            tmp_path.unlink()
                    except OSError:
                        pass

    def _restore_from_checkpoint_if_needed(self) -> str:
        """Restore persistent state from checkpoint and return next stage to execute."""
        cp = self._resume_checkpoint
        if not isinstance(cp, dict):
            self.store.save_mission(self.req.to_dict())
            return "SCOUT"

        cp_state = cp.get("state") if isinstance(cp.get("state"), dict) else {}
        self.step = int(cp.get("step") or cp_state.get("step") or 0)
        self.escalations_used = int(cp_state.get("escalations_used") or 0)
        self._escalated_notes = list(cp_state.get("escalated_notes") or [])
        if isinstance(cp_state.get("circuit_breaker"), dict):
            self._circuit_breaker = NoProgressCircuitBreaker.from_dict(cp_state["circuit_breaker"])

        self._surface_map = self.store.get_surface_map()
        raw_hyps = self.store.get_hypotheses()
        self._hypotheses = [
            Hypothesis.from_dict(item, default_id=f"H{idx + 1}")
            for idx, item in enumerate(raw_hyps)
            if isinstance(item, dict)
        ]
        self._ledger = self.store.get_ledger()
        next_stage = str(cp_state.get("next_stage") or "").upper()
        if next_stage in ("SCOUT", "STRATEGIST", "INVESTIGATOR", "VALIDATOR", "ADJUDICATOR"):
            self._emit_event("RESUMED", f"Loaded checkpoint at step {self.step}; resuming at {next_stage}.")
            return next_stage
        if self._surface_map is None:
            return "SCOUT"
        if not self._hypotheses:
            return "STRATEGIST"
        if any(h.status in ("OPEN", "TESTING") for h in self._hypotheses):
            return "INVESTIGATOR"
        return "ADJUDICATOR"

    def _run_safe(self) -> None:
        try:
            self._execute_fsm()
        except Exception as exc:
            self._cleanup_containers_best_effort()
            with self._lock:
                self.error = str(exc)
                self.phase = "ERROR"
                self.finished_at = _utc_now_iso()
            self._emit_event("ERROR", f"Orchestrator failed: {exc}")
            if self.req.retention_mode == "ephemeral":
                self.store.purge_working_state_on_finish()
        finally:
            self._cleanup_containers_best_effort()
            self._done_event.set()

    def _execute_fsm(self) -> None:
        stage = self._restore_from_checkpoint_if_needed()

        # ---------------- PHASE 1: SCOUT (Deterministic, No-LLM) ----------------
        if stage == "SCOUT":
            if not self._wait_if_paused("SCOUT"):
                return self._halt_user_stopped()
            self._set_phase("SCOUT")
            self._emit_event(
                "SCOUT",
                f"Running deterministic pre-LLM Scout (mode={self.req.sandbox_mode}).",
            )
            if self.scout_fn is None:
                raise RuntimeError("Deterministic scout function is unavailable.")
            artifacts_dir = (
                self.store.get_artifacts_dir() if self.req.retention_mode == "persistent" else None
            )
            scout_kwargs: dict[str, Any] = {
                "runner": self.sandbox_runner,
                "target_path": self.req.target_path,
                "retention_mode": self.req.retention_mode,
                "run_id": self.store.run_id,
                "artifacts_path": artifacts_dir,
            }
            try:
                sig = inspect.signature(self.scout_fn)
                if "execution_mode" in sig.parameters or any(
                    p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
                ):
                    scout_kwargs["execution_mode"] = self.req.sandbox_mode
            except (TypeError, ValueError):
                pass
            surface_map = self.scout_fn(**scout_kwargs)
            scout_mode = str(surface_map.get("mode") or "")
            if (
                scout_mode in ("host_readonly", "host_readonly_fallback")
                or surface_map.get("executionMode") == "host_readonly"
                or self.req.sandbox_mode == "host_readonly"
            ):
                self.resolved_sandbox_mode = "host_readonly"
                reason = str(surface_map.get("fallbackReason") or "host_readonly active")
                self._emit_event(
                    "SCOUT",
                    f"Host Read-Only Safe Mode active ({reason}). Arbitrary code execution disabled.",
                )
            else:
                self.resolved_sandbox_mode = "docker"
            if self.req.mission_profile == "code_audit":
                try:
                    from .code_audit import run_code_audit

                    audit_res = run_code_audit(self.req.target_path)
                    surface_map = dict(surface_map)
                    surface_map["codeAudit"] = audit_res
                    surface_map["code_audit"] = audit_res
                    self._append_ledger(f"## Deterministic Code Audit\n- {audit_res['summary']}")
                except Exception as exc:
                    self._append_ledger(f"## Deterministic Code Audit\n- Warning: {exc}")
            with self._lock:
                self._surface_map = surface_map
            self.store.save_surface_map(surface_map)
            self.store.record_step_trace(
                {
                    "step": self.step,
                    "phase": "SCOUT",
                    "file_count": surface_map.get("fileCount"),
                    "mode": surface_map.get("mode"),
                }
            )
            self._append_ledger(
                f"## Scout Surface Map\n- Target: `{surface_map.get('targetName', 'target')}`, "
                f"Files: {surface_map.get('fileCount', 0)}, Entrypoints: {surface_map.get('entrypoints', [])[:5]}"
            )
            if self.req.retention_mode == "ephemeral":
                self.store.rolling_purge_step(self.step)
            self._checkpoint_if_persistent("STRATEGIST")
            stage = "STRATEGIST"

        # ---------------- PHASE 2: STRATEGIST (Heavy MoE 177B / Fast-only) ----------------
        if stage == "STRATEGIST":
            if not self._wait_if_paused("STRATEGIST"):
                return self._halt_user_stopped()
            self._set_phase("STRATEGIST")
            self._ensure_tier_for_role("heavy")
            self._emit_event("STRATEGIST", "Generating testable hypotheses from compact surface map.")

            strat_msgs = _build_strategist_messages(self.req.mission, self._surface_map or {})

            def _parse_strategist(raw: str) -> dict[str, Any]:
                obj = parse_json_object(raw)
                st = str(obj.get("status") or "").strip().upper()
                if st == "IMPOSSIBLE_PROVEN":
                    cat = str(obj.get("impossible_category") or "").strip().upper()
                    proof = str(obj.get("impossible_proof") or "").strip()
                    summary = str(obj.get("summary") or proof).strip()
                    if cat not in IMPOSSIBLE_CATEGORIES:
                        raise ProtocolParseError(
                            f"IMPOSSIBLE_PROVEN requires impossible_category in {IMPOSSIBLE_CATEGORIES}."
                        )
                    if len(proof) < 10:
                        raise ProtocolParseError(
                            "IMPOSSIBLE_PROVEN requires impossible_proof (>= 10 chars)."
                        )
                    return {
                        "status": "IMPOSSIBLE_PROVEN",
                        "impossible_category": cat,
                        "impossible_proof": proof,
                        "summary": summary,
                        "report_markdown": str(obj.get("report_markdown") or ""),
                    }
                raw_hyps = obj.get("hypotheses")
                if not isinstance(raw_hyps, list) or not raw_hyps:
                    raise ProtocolParseError(
                        "Strategist JSON must contain a non-empty 'hypotheses' list or status='IMPOSSIBLE_PROVEN'."
                    )
                parsed_hyps: list[Hypothesis] = []
                for idx, item in enumerate(raw_hyps[:8]):
                    try:
                        parsed_hyps.append(Hypothesis.from_dict(item, default_id=f"H{idx + 1}"))
                    except SchemaValidationError as exc:
                        raise ProtocolParseError(str(exc)) from exc
                return {"status": "HYPOTHESES_READY", "hypotheses": parsed_hyps}

            strat_result, raw_strat_text, attempts = call_with_self_correction(
                llm_fn=lambda msgs: self._call_tier_llm("heavy", msgs),
                messages=strat_msgs,
                parser_fn=_parse_strategist,
                max_retries=3,
            )
            self.step += 1
            self.store.record_step_trace(
                {
                    "step": self.step,
                    "phase": "STRATEGIST",
                    "status": strat_result["status"],
                    "attempts": attempts,
                }
            )
            if self.req.retention_mode == "ephemeral":
                self.store.rolling_purge_step(
                    self.step, [bytearray(raw_strat_text.encode("utf-8", errors="replace"))]
                )

            if strat_result["status"] == "IMPOSSIBLE_PROVEN":
                verdict = Verdict(
                    code="IMPOSSIBLE_PROVEN",
                    summary=strat_result["summary"],
                    impossible_category=strat_result["impossible_category"],
                    impossible_proof=strat_result["impossible_proof"],
                    confirmed_ids=[],
                    refuted_ids=[],
                )
                valid, errs = validate_verdict(verdict, [], self._surface_map)
                if not valid:
                    raise RuntimeError(f"Strategist IMPOSSIBLE_PROVEN failed validation: {errs}")
                self._append_ledger(
                    f"- [STRATEGIST] Immediate IMPOSSIBLE_PROVEN ({verdict.impossible_category}): {verdict.summary}"
                )
                self._finalize_with_verdict(verdict, strat_result.get("report_markdown") or "")
                return

            with self._lock:
                self._hypotheses = strat_result["hypotheses"]
            self.store.save_hypotheses([h.to_dict() for h in self._hypotheses])
            self._append_ledger(
                "## Initial Hypotheses\n"
                + "\n".join(f"- {h.id}: {h.title} ({h.risk_area})" for h in self._hypotheses)
            )
            self._checkpoint_if_persistent("INVESTIGATOR")
            stage = "INVESTIGATOR"

        # ---------------- PHASE 3: INVESTIGATOR (Fast Tier loop + Context Reset) ----------------
        if stage == "INVESTIGATOR":
            if not self._wait_if_paused("INVESTIGATOR"):
                return self._halt_user_stopped()
            self._set_phase("INVESTIGATOR")
            self._ensure_tier_for_role("fast")
            toolkit = self._build_toolkit()
            tools_desc = (
                toolkit.describe_tools_for_prompt()
                if hasattr(toolkit, "describe_tools_for_prompt")
                else _default_tools_prompt()
            )

            while not self._stop_event.is_set():
                if not self._wait_if_paused("INVESTIGATOR"):
                    return self._halt_user_stopped()
                if self.step >= self.req.budget.max_steps:
                    self._escalated_notes.append(
                        f"Reached global step budget max_steps={self.req.budget.max_steps}."
                    )
                    break

                with self._lock:
                    current_hyp = next(
                        (h for h in self._hypotheses if h.status in ("OPEN", "TESTING")),
                        None,
                    )
                if current_hyp is None:
                    break

                # Context Reset for this hypothesis!
                current_hyp.status = "TESTING"
                self.store.save_hypotheses([h.to_dict() for h in self._hypotheses])
                self._circuit_breaker.reset(preserve_seen_files=True)
                self._emit_event(
                    "INVESTIGATOR",
                    f"Investigating {current_hyp.id}: {current_hyp.title}",
                    {"hypothesis_id": current_hyp.id},
                )

                local_messages = _build_investigator_messages(
                    mission=self.req.mission,
                    hyp=current_hyp,
                    ledger=self._ledger,
                    surface_map=self._surface_map or {},
                    tools_desc=tools_desc,
                    host_readonly=(self.resolved_sandbox_mode == "host_readonly"),
                )

                for turn_idx in range(self.req.budget.max_turns_per_hypothesis):
                    if not self._wait_if_paused("INVESTIGATOR"):
                        return self._halt_user_stopped()
                    if self.step >= self.req.budget.max_steps:
                        break
                    if current_hyp.status not in ("OPEN", "TESTING"):
                        break

                    raw_step_buffers: list[bytearray] = []
                    try:
                        tool_calls, raw_llm_text, parse_attempts = call_with_self_correction(
                            llm_fn=lambda msgs: self._call_tier_llm("fast", msgs),
                            messages=local_messages,
                            parser_fn=lambda txt: parse_tool_calls(
                                txt,
                                allowed_tools=self._active_tool_names,
                                require_at_least_one=True,
                            ),
                            max_retries=3,
                        )
                    except ProtocolParseError as exc:
                        self.step += 1
                        self.escalations_used += 1
                        current_hyp.attempts += 1
                        current_hyp.status = "ESCALATE"
                        current_hyp.evidence = (
                            f"{current_hyp.evidence}\n[Protocol error after 3 retries: {exc}]"
                        ).strip()
                        self._escalated_notes.append(f"{current_hyp.id}: {exc}")
                        self.store.save_hypotheses([h.to_dict() for h in self._hypotheses])
                        self._checkpoint_if_persistent("INVESTIGATOR")
                        break

                    raw_step_buffers.append(
                        bytearray(raw_llm_text.encode("utf-8", errors="replace"))
                    )
                    # Keep micro-steps atomic (max 2 tool calls per turn)
                    turn_outputs: list[str] = []
                    tripped_this_turn = False

                    for tc in tool_calls[:2]:
                        self.step += 1
                        current_hyp.attempts += 1
                        result = toolkit.execute(
                            tc.name,
                            tc.arguments,
                            hypothesis_callback=self._update_hypothesis_callback,
                        )
                        res_json = json.dumps(result, ensure_ascii=False)
                        raw_step_buffers.append(
                            bytearray(res_json.encode("utf-8", errors="replace"))
                        )

                        files_read: list[str] = []
                        if tc.name == "read_file_slice" and result.get("ok"):
                            slice_tag = (
                                f"{result.get('rel_path', tc.arguments.get('rel_path', ''))}:"
                                f"{result.get('start_line', '')}-{result.get('end_line', '')}"
                            )
                            files_read.append(slice_tag)
                            if slice_tag not in current_hyp.inspected_slices:
                                current_hyp.inspected_slices.append(slice_tag)

                        if tc.name == "run_python_sandbox":
                            # Any real sandbox execution (not Docker transport failure exit_code=-1)
                            # counts as verified sandbox execution evidence.
                            if result.get("exit_code", -1) != -1 or result.get("ok") is True:
                                current_hyp.sandbox_verified = True
                        elif tc.name == "code_audit" and result.get("ok"):
                            current_hyp.sandbox_verified = True
                            if "code_audit" not in current_hyp.inspected_slices:
                                current_hyp.inspected_slices.append("code_audit")

                        out_text = (
                            str(result.get("stdout") or "")
                            + str(result.get("stderr") or "")
                            + str(result.get("content") or "")
                            + str(result.get("error") or "")
                            + str(result.get("matches") or "")
                            + str(result.get("entries") or "")
                            + str(result.get("hypothesis") or "")
                        )
                        out_hash = NoProgressCircuitBreaker.fingerprint_output(
                            out_text,
                            exit_code=result.get("exit_code")
                            if isinstance(result.get("exit_code"), int)
                            else (0 if result.get("ok") else 1),
                        )

                        hyps_snapshot = [h.to_dict() for h in self._hypotheses]
                        self.store.save_hypotheses(hyps_snapshot)
                        tripped, cb_reason = self._circuit_breaker.record_step(
                            hypotheses_snapshot=hyps_snapshot,
                            files_read=files_read,
                            tool_name=tc.name,
                            output_hash=out_hash,
                        )

                        self.store.record_step_trace(
                            {
                                "step": self.step,
                                "phase": "INVESTIGATOR",
                                "hypothesis_id": current_hyp.id,
                                "tool": tc.name,
                                "ok": bool(result.get("ok")),
                                "parse_attempts": parse_attempts,
                                "circuit_breaker": cb_reason,
                            }
                        )

                        summary_line = (
                            f"- Step {self.step} [{current_hyp.id}] `{tc.name}` "
                            f"ok={result.get('ok')} -> {current_hyp.status}"
                        )
                        if tc.name == "run_python_sandbox":
                            summary_line += (
                                f" (rc={result.get('exit_code')}, "
                                f"out={str(result.get('stdout') or '').strip()[:140]})"
                            )
                        elif tc.name == "update_hypothesis":
                            summary_line += f" ({current_hyp.evidence[:160]})"
                        self._append_ledger(summary_line)

                        turn_outputs.append(f"Tool `{tc.name}` result:\n{res_json[:3000]}")

                        if tripped:
                            tripped_this_turn = True
                            self.escalations_used += 1
                            current_hyp.status = "ESCALATE"
                            note = f"{current_hyp.id}: {cb_reason}"
                            self._escalated_notes.append(note)
                            self._emit_event("INVESTIGATOR", note, {"escalated": True})
                            self.store.save_hypotheses([h.to_dict() for h in self._hypotheses])
                            break

                        if current_hyp.status not in ("OPEN", "TESTING"):
                            break

                    # Rolling purge in ephemeral mode / atomic checkpoint in persistent mode
                    if self.req.retention_mode == "ephemeral":
                        self.store.rolling_purge_step(self.step, raw_step_buffers)
                    else:
                        self._checkpoint_if_persistent("INVESTIGATOR")

                    if tripped_this_turn or current_hyp.status not in ("OPEN", "TESTING"):
                        break

                    # Bounded intra-hypothesis dialogue (keep total context well under 8k tokens)
                    local_messages.append({"role": "assistant", "content": raw_llm_text[:1800]})
                    local_messages.append(
                        {"role": "user", "content": "\n\n".join(turn_outputs)[:4000]}
                    )
                    if len(local_messages) > 8:
                        local_messages = [local_messages[0], local_messages[1], *local_messages[-4:]]

                # If hypothesis exhausted turns without resolution, escalate cleanly
                if current_hyp.status in ("OPEN", "TESTING"):
                    current_hyp.status = "ESCALATE"
                    self.escalations_used += 1
                    self._escalated_notes.append(
                        f"{current_hyp.id}: exhausted {self.req.budget.max_turns_per_hypothesis} turns without resolution."
                    )
                    self.store.save_hypotheses([h.to_dict() for h in self._hypotheses])
                    self._checkpoint_if_persistent("INVESTIGATOR")

                # Explicit Context Reset before next hypothesis!
                del local_messages

            stage = "VALIDATOR"

        # ---------------- PHASE 3b: VALIDATOR (Deterministic, No-LLM) ----------------
        if stage == "VALIDATOR":
            if not self._wait_if_paused("VALIDATOR"):
                return self._halt_user_stopped()
            self._set_phase("VALIDATOR")
            self._emit_event("VALIDATOR", "Running deterministic schema & sandbox-evidence validation.")

            validated_hyps: list[Hypothesis] = []
            for idx, h in enumerate(self._hypotheses):
                rechecked = Hypothesis.from_dict(h.to_dict(), default_id=f"H{idx + 1}")
                if rechecked.status == "CONFIRMED":
                    verified = (
                        (rechecked.sandbox_verified or bool(rechecked.inspected_slices))
                        if self.resolved_sandbox_mode == "host_readonly"
                        else rechecked.sandbox_verified
                    )
                    if not verified or not rechecked.evidence.strip():
                        rechecked.status = "ESCALATE"
                        note = (
                            f"[VALIDATOR] Downgraded {rechecked.id} CONFIRMED -> ESCALATE: "
                            "confirmed finding requires executed sandbox verification evidence."
                        )
                        rechecked.evidence = f"{rechecked.evidence}\n{note}".strip()
                        self._escalated_notes.append(note)
                validated_hyps.append(rechecked)

            with self._lock:
                self._hypotheses = validated_hyps
            code_audit_blockers: list[str] = []
            if self.req.mission_profile == "code_audit" and isinstance(self._surface_map, dict):
                ca = self._surface_map.get("codeAudit")
                if isinstance(ca, dict) and ca.get("blocked"):
                    code_audit_blockers = list(ca.get("blockers") or [])[:10]
                    for blk in code_audit_blockers:
                        self._escalated_notes.append(f"[CODE_AUDIT_BLOCK] {blk}")
            self.store.save_hypotheses([h.to_dict() for h in self._hypotheses])
            self.store.record_step_trace(
                {
                    "step": self.step,
                    "phase": "VALIDATOR",
                    "confirmed": [h.id for h in self._hypotheses if h.status == "CONFIRMED"],
                    "refuted": [h.id for h in self._hypotheses if h.status == "REFUTED"],
                    "escalated": [h.id for h in self._hypotheses if h.status == "ESCALATE"],
                    "code_audit_blocked": bool(code_audit_blockers),
                }
            )
            self._checkpoint_if_persistent("ADJUDICATOR")
            stage = "ADJUDICATOR"

        # ---------------- PHASE 4: ADJUDICATOR (Heavy MoE 177B / Fast-only) ----------------
        if stage == "ADJUDICATOR":
            if not self._wait_if_paused("ADJUDICATOR"):
                return self._halt_user_stopped()
            self._set_phase("ADJUDICATOR")

            # If Heavy escalation budget is strictly 0 and circuit breaker escalated items exist,
            # emit clean HALTED_CIRCUIT_BREAKER directly without calling Heavy escalation.
            escalated_hyps = [h for h in self._hypotheses if h.status == "ESCALATE"]
            if (
                escalated_hyps
                and self.escalations_used > self.req.budget.max_heavy_escalations
                and not any(h.status == "CONFIRMED" for h in self._hypotheses)
            ):
                verdict = Verdict(
                    code="HALTED_CIRCUIT_BREAKER",
                    summary=(
                        f"Circuit breaker halted run after {self.escalations_used} escalation(s) "
                        f"exceeded budget max_heavy_escalations={self.req.budget.max_heavy_escalations}. "
                        f"Unresolved: {[h.id for h in escalated_hyps]}."
                    ),
                    confirmed_ids=[],
                    refuted_ids=[h.id for h in self._hypotheses if h.status == "REFUTED"],
                )
                self._finalize_with_verdict(verdict, "")
                return

            self._cleanup_containers_best_effort()
            self._ensure_tier_for_role("heavy")
            self._emit_event("ADJUDICATOR", "Synthesizing formal verdict and final report.")

            adj_msgs = _build_adjudicator_messages(
                mission=self.req.mission,
                hypotheses=self._hypotheses,
                ledger=self._ledger,
                escalated_notes=self._escalated_notes,
                embed_poc=self.req.ephemeral_rules.embed_verified_poc_in_report,
            )

            def _parse_adjudicator(raw: str) -> tuple[list[Hypothesis], Verdict, str]:
                obj = parse_json_object(raw)
                preview_hyps = [Hypothesis.from_dict(h.to_dict()) for h in self._hypotheses]
                by_id = {h.id: h for h in preview_hyps}

                resolved_updates = obj.get("resolved_hypotheses") or []
                if isinstance(resolved_updates, list):
                    for upd in resolved_updates:
                        if not isinstance(upd, dict):
                            continue
                        uid = str(upd.get("id") or "").strip()
                        if uid not in by_id:
                            continue
                        new_status = str(upd.get("status") or "").strip().upper()
                        if new_status in HYPOTHESIS_STATUSES:
                            # Do not allow Adjudicator to blindly confirm without evidence
                            if new_status == "CONFIRMED" and not (
                                by_id[uid].sandbox_verified or str(upd.get("evidence") or "").strip()
                            ):
                                raise ProtocolParseError(
                                    f"Cannot mark {uid} CONFIRMED without concrete evidence."
                                )
                            by_id[uid].status = new_status
                        if upd.get("evidence"):
                            by_id[uid].evidence = str(upd["evidence"]).strip()
                        if upd.get("poc_snippet"):
                            by_id[uid].poc_snippet = str(upd["poc_snippet"]).strip()

                raw_verdict = obj.get("verdict")
                if not isinstance(raw_verdict, dict):
                    raise ProtocolParseError("Adjudicator JSON must include a 'verdict' object.")

                # Auto-fill confirmed_ids/refuted_ids consistently if omitted
                if not raw_verdict.get("confirmed_ids"):
                    raw_verdict = dict(raw_verdict)
                    raw_verdict["confirmed_ids"] = [
                        h.id for h in preview_hyps if h.status == "CONFIRMED"
                    ]
                if not raw_verdict.get("refuted_ids"):
                    raw_verdict = dict(raw_verdict)
                    raw_verdict["refuted_ids"] = [
                        h.id for h in preview_hyps if h.status == "REFUTED"
                    ]

                verdict = Verdict.from_dict(raw_verdict)
                is_valid, errs = validate_verdict(verdict, preview_hyps, self._surface_map)
                if not is_valid:
                    raise ProtocolParseError("Verdict validation failed: " + "; ".join(errs))

                report_md = str(obj.get("report_markdown") or obj.get("report") or "")
                return preview_hyps, verdict, report_md

            try:
                final_hyps, verdict, report_md = None, None, ""
                parsed_adj, raw_adj_text, adj_attempts = call_with_self_correction(
                    llm_fn=lambda msgs: self._call_tier_llm("heavy", msgs),
                    messages=adj_msgs,
                    parser_fn=_parse_adjudicator,
                    max_retries=3,
                )
                final_hyps, verdict, report_md = parsed_adj
                with self._lock:
                    self._hypotheses = final_hyps
                self.store.save_hypotheses([h.to_dict() for h in self._hypotheses])
                self.step += 1
                self.store.record_step_trace(
                    {
                        "step": self.step,
                        "phase": "ADJUDICATOR",
                        "verdict": verdict.code,
                        "attempts": adj_attempts,
                    }
                )
                if self.req.retention_mode == "ephemeral":
                    self.store.rolling_purge_step(
                        self.step, [bytearray(raw_adj_text.encode("utf-8", errors="replace"))]
                    )
            except ProtocolParseError as exc:
                # Deterministic fallback verdict when Adjudicator cannot emit a schema-valid verdict
                confirmed_ids = [h.id for h in self._hypotheses if h.status == "CONFIRMED"]
                refuted_ids = [h.id for h in self._hypotheses if h.status == "REFUTED"]
                verdict = Verdict(
                    code="HALTED_CIRCUIT_BREAKER",
                    summary=f"Adjudicator could not produce a schema-valid verdict: {exc}",
                    confirmed_ids=confirmed_ids,
                    refuted_ids=refuted_ids,
                )
                report_md = ""

            self._finalize_with_verdict(verdict, report_md)

    def _halt_user_stopped(self) -> None:
        self._cleanup_containers_best_effort()
        verdict = Verdict(
            code="HALTED_CIRCUIT_BREAKER",
            summary="Run stopped cleanly by user request.",
            confirmed_ids=[h.id for h in self._hypotheses if h.status == "CONFIRMED"],
            refuted_ids=[h.id for h in self._hypotheses if h.status == "REFUTED"],
        )
        self._finalize_with_verdict(verdict, "")

    def get_status_snapshot(self) -> dict[str, Any]:
        with self._lock:
            is_active = self.phase in ACTIVE_FSM_STATES
            elapsed = round(time.monotonic() - self.started_monotonic, 1)
            verdict_dict = self.verdict.to_dict() if self.verdict is not None else self.store.get_verdict()
            has_report = self.store.has_report()
            remaining = self.store.auto_purge_remaining_sec()
            hyps_out = [h.to_dict() for h in self._hypotheses]
            ledger_out = self._ledger
            events_out = list(self._events[-20:])
            surface_out: dict[str, Any] | None = None
            code_audit_out: dict[str, Any] | None = None
            if isinstance(self._surface_map, dict):
                raw_ca = self._surface_map.get("code_audit") or self._surface_map.get("codeAudit")
                if isinstance(raw_ca, dict):
                    code_audit_out = raw_ca
                surface_out = {
                    "mode": self._surface_map.get("mode"),
                    "executionMode": self._surface_map.get("executionMode"),
                    "fallbackReason": self._surface_map.get("fallbackReason"),
                    "targetName": self._surface_map.get("targetName"),
                    "fileCount": self._surface_map.get("fileCount"),
                    "languages": self._surface_map.get("languages"),
                    "entrypoints": self._surface_map.get("entrypoints"),
                    "compactSummary": self._surface_map.get("compactSummary"),
                }
                if code_audit_out is not None:
                    surface_out["codeAudit"] = code_audit_out
                    surface_out["code_audit"] = code_audit_out

            return {
                "ok": True,
                "available": True,
                "operator": self.req.operator,
                "run_id": self.store.run_id,
                "runId": self.store.run_id,
                "retention_mode": self.req.retention_mode,
                "retentionMode": self.req.retention_mode,
                "execution_mode": self.req.execution_mode,
                "sandbox_mode": self.req.sandbox_mode,
                "resolved_sandbox_mode": self.resolved_sandbox_mode,
                "mission_profile": self.req.mission_profile,
                "state": self.phase,
                "phase": self.phase,
                "active": is_active,
                "active_engine": self.active_engine,
                "activeEngine": self.active_engine,
                "step": self.step,
                "max_steps": self.req.budget.max_steps,
                "maxSteps": self.req.budget.max_steps,
                "escalations_used": self.escalations_used,
                "max_heavy_escalations": self.req.budget.max_heavy_escalations,
                "elapsed_sec": elapsed,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "target_path": self.req.target_path,
                "target_access": self.req.target_access,
                "mission": self.req.mission if self.req.retention_mode == "persistent" or is_active else "",
                "budget": self.req.budget.to_dict(),
                "ephemeral_rules": self.req.ephemeral_rules.to_dict(),
                "has_report": has_report,
                "hasReport": has_report,
                "auto_purge_remaining_sec": remaining,
                "autoPurgeRemainingSec": remaining,
                "verdict": verdict_dict,
                "hypotheses": hyps_out,
                "ledger": ledger_out,
                "events": events_out,
                "error": self.error,
                "surface_map": surface_out,
                "code_audit": code_audit_out,
            }


class OrchestratorManager:
    """
    Singleton facade managing Orchestrator lifecycle, strict Retention Policy DI,
    and Persistent Vault inspection/deletion.
    """

    def __init__(
        self,
        vault_root: pathlib.Path | str | None = None,
        tier_switcher: TierSwitcher | None = None,
        sandbox_runner: Any | None = None,
        scout_fn: Callable[..., dict[str, Any]] | None = None,
        toolkit_factory: Callable[..., Any] | None = None,
        ephemeral_auto_purge_seconds_override: float | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self._vault_root = pathlib.Path(vault_root).resolve() if vault_root is not None else None
        self._tier_switcher = tier_switcher if tier_switcher is not None else TierSwitcher()
        self._injected_runner = sandbox_runner
        self._injected_scout_fn = scout_fn
        self._injected_toolkit_factory = toolkit_factory
        self._ephemeral_auto_purge_seconds_override = ephemeral_auto_purge_seconds_override

        self._current: _RunController | None = None

    def _resolve_sandbox_deps(self) -> tuple[Any, Callable[..., dict[str, Any]] | None, Callable[..., Any] | None, tuple[str, ...]]:
        runner_cls, default_scout, default_toolkit, tool_names = _lazy_sandbox_imports()
        runner = self._injected_runner
        if runner is None and runner_cls is not None:
            runner = runner_cls()
        scout_fn = self._injected_scout_fn if self._injected_scout_fn is not None else default_scout
        toolkit_factory = (
            self._injected_toolkit_factory
            if self._injected_toolkit_factory is not None
            else default_toolkit
        )
        return runner, scout_fn, toolkit_factory, tool_names

    def _on_ephemeral_report_auto_purged(self) -> None:
        with self._lock:
            if (
                self._current is not None
                and self._current.req.retention_mode == "ephemeral"
                and self._current.phase not in ACTIVE_FSM_STATES
            ):
                self._current = None

    def start_run(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        try:
            req = RunRequest.from_dict(payload)
        except SchemaValidationError as exc:
            return exc.http_status, {"ok": False, "error": exc.message, "field": exc.field_name}

        with self._lock:
            if self._current is not None and self._current.phase in ACTIVE_FSM_STATES:
                return 409, {
                    "ok": False,
                    "error": f"An orchestrator run is already active ('{self._current.store.run_id}', state={self._current.phase}).",
                }
            # Purge any leftover ephemeral report before starting a brand-new run
            if self._current is not None and self._current.req.retention_mode == "ephemeral":
                self._current.store.purge_report()
                self._current = None

            # STRICT DEPENDENCY INJECTION BY RETENTION POLICY:
            # In ephemeral mode, DiskVaultStore is NEVER imported or instantiated!
            if req.retention_mode == "ephemeral":
                from ..storage.memory_store import MemoryEphemeralStore

                ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d_%H%M%S")
                run_id = f"eph_{ts}_{secrets.token_hex(4)}"
                store: RunStore = MemoryEphemeralStore(
                    run_id=run_id,
                    report_auto_purge_minutes=req.ephemeral_rules.report_auto_purge_minutes,
                    on_report_purged=self._on_ephemeral_report_auto_purged,
                    auto_purge_seconds=self._ephemeral_auto_purge_seconds_override,
                )
            else:
                from ..storage.disk_vault import DiskVaultStore

                slug = pathlib.Path(req.target_path).name or "audit"
                store = DiskVaultStore(slug=slug, vault_root=self._vault_root)

            runner, scout_fn, toolkit_factory, tool_names = self._resolve_sandbox_deps()
            ctrl = _RunController(
                req=req,
                store=store,
                tier_switcher=self._tier_switcher,
                sandbox_runner=runner,
                scout_fn=scout_fn,
                toolkit_factory=toolkit_factory,
                tool_names=tool_names,
                custom_toolkit_injected=(self._injected_toolkit_factory is not None),
            )
            self._current = ctrl
            ctrl.start()

            return 200, {
                "ok": True,
                "run_id": store.run_id,
                "runId": store.run_id,
                "retention_mode": req.retention_mode,
                "retentionMode": req.retention_mode,
                "execution_mode": req.execution_mode,
                "sandbox_mode": req.sandbox_mode,
                "state": ctrl.phase,
            }

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            if self._current is None:
                return {
                    "ok": True,
                    "available": True,
                    "operator": "orchestrator",
                    "run_id": None,
                    "runId": None,
                    "retention_mode": None,
                    "retentionMode": None,
                    "execution_mode": None,
                    "sandbox_mode": "auto",
                    "resolved_sandbox_mode": "auto",
                    "state": "IDLE",
                    "phase": "IDLE",
                    "active": False,
                    "active_engine": "none",
                    "activeEngine": "none",
                    "step": 0,
                    "max_steps": 0,
                    "maxSteps": 0,
                    "elapsed_sec": 0.0,
                    "has_report": False,
                    "hasReport": False,
                    "auto_purge_remaining_sec": None,
                    "autoPurgeRemainingSec": None,
                    "verdict": None,
                    "hypotheses": [],
                    "ledger": "",
                    "events": [],
                    "error": None,
                    "surface_map": None,
                    "code_audit": None,
                }
            return self._current.get_status_snapshot()

    def pause_run(self) -> tuple[int, dict[str, Any]]:
        with self._lock:
            if self._current is None:
                return 409, {"ok": False, "error": "No orchestrator run to pause."}
            return self._current.request_pause()

    def resume_run(self, run_id: str | None = None) -> tuple[int, dict[str, Any]]:
        with self._lock:
            if self._current is not None:
                if self._current.req.retention_mode == "ephemeral":
                    return 409, {
                        "ok": False,
                        "error": "Pause/Resume is not permitted in 'ephemeral' mode.",
                    }
                if self._current.phase == "PAUSED" or self._current._pause_requested.is_set():
                    if run_id is None or run_id == self._current.store.run_id:
                        return self._current.request_resume()
                if self._current.phase in ACTIVE_FSM_STATES:
                    return 409, {
                        "ok": False,
                        "error": f"Run '{self._current.store.run_id}' is already running (state={self._current.phase}).",
                    }

            if not run_id:
                return 400, {
                    "ok": False,
                    "error": "No paused run in memory; provide a persistent 'run_id' to resume from disk vault.",
                }

            from ..storage.disk_vault import DiskVaultStore

            try:
                store = DiskVaultStore.open_existing(self._vault_root, run_id)
            except ValueError as exc:
                return 400, {"ok": False, "error": str(exc)}
            except FileNotFoundError as exc:
                return 404, {"ok": False, "error": str(exc)}

            manifest = store.get_mission()
            if not isinstance(manifest, dict):
                return 400, {"ok": False, "error": f"Vault '{run_id}' is missing 00_MANIFEST.json."}

            try:
                req = RunRequest.from_dict(manifest)
            except SchemaValidationError as exc:
                return 400, {"ok": False, "error": f"Invalid saved manifest: {exc.message}"}

            cp = store.load_checkpoint()
            runner, scout_fn, toolkit_factory, tool_names = self._resolve_sandbox_deps()
            ctrl = _RunController(
                req=req,
                store=store,
                tier_switcher=self._tier_switcher,
                sandbox_runner=runner,
                scout_fn=scout_fn,
                toolkit_factory=toolkit_factory,
                tool_names=tool_names,
                resume_checkpoint=cp,
                custom_toolkit_injected=(self._injected_toolkit_factory is not None),
            )
            self._current = ctrl
            ctrl.start()
            from_step = int(cp.get("step") or 0) if isinstance(cp, dict) else 0
            return 200, {
                "ok": True,
                "run_id": store.run_id,
                "resumed": True,
                "from_step": from_step,
                "state": ctrl.phase,
            }

    def stop_run(self) -> tuple[int, dict[str, Any]]:
        with self._lock:
            ctrl = self._current
        if ctrl is None:
            return 200, {"ok": True, "stopped": False, "state": "IDLE"}
        ctrl.request_stop()
        ctrl._done_event.wait(timeout=5.0)
        return 200, {
            "ok": True,
            "stopped": True,
            "run_id": ctrl.store.run_id,
            "state": ctrl.phase,
        }

    def get_report(self) -> tuple[int, dict[str, Any]]:
        with self._lock:
            ctrl = self._current
        if ctrl is None:
            return 404, {"ok": False, "error": "No orchestrator report available."}
        report = ctrl.store.get_final_report()
        if report is None:
            return 404, {"ok": False, "error": "Report not generated yet or already purged."}
        verdict_dict = (
            ctrl.verdict.to_dict() if ctrl.verdict is not None else ctrl.store.get_verdict()
        )
        return 200, {
            "ok": True,
            "run_id": ctrl.store.run_id,
            "runId": ctrl.store.run_id,
            "retention_mode": ctrl.req.retention_mode,
            "retentionMode": ctrl.req.retention_mode,
            "verdict": verdict_dict,
            "report": report,
            "auto_purge_remaining_sec": ctrl.store.auto_purge_remaining_sec(),
            "autoPurgeRemainingSec": ctrl.store.auto_purge_remaining_sec(),
        }

    def purge_report(self) -> tuple[int, dict[str, Any]]:
        with self._lock:
            ctrl = self._current
            if ctrl is None:
                return 200, {"ok": True, "purged": True, "state": "IDLE"}
            if ctrl.phase in ACTIVE_FSM_STATES:
                ctrl.request_stop()
            run_id = ctrl.store.run_id
            mode = ctrl.req.retention_mode
            ctrl.store.purge_report()
            if mode == "ephemeral":
                self._current = None
        return 200, {
            "ok": True,
            "purged": True,
            "run_id": run_id,
            "retention_mode": mode,
            "state": "IDLE" if mode == "ephemeral" else ctrl.phase,
        }

    def list_vaults(self) -> tuple[int, dict[str, Any]]:
        from ..storage.disk_vault import DiskVaultStore

        vaults = DiskVaultStore.list_vaults(self._vault_root)
        return 200, {"ok": True, "vaults": vaults}

    def get_vault_detail(self, run_id: str) -> tuple[int, dict[str, Any]]:
        from ..storage.disk_vault import DiskVaultStore

        try:
            detail = DiskVaultStore.load_vault_detail(self._vault_root, run_id)
        except ValueError as exc:
            return 400, {"ok": False, "error": str(exc)}
        if detail is None:
            return 404, {"ok": False, "error": f"Vault '{run_id}' not found."}
        return 200, {"ok": True, "vault": detail}

    def delete_vault(self, run_id: str) -> tuple[int, dict[str, Any]]:
        from ..storage.disk_vault import DiskVaultStore

        with self._lock:
            if (
                self._current is not None
                and self._current.store.run_id == run_id
                and self._current.phase in ACTIVE_FSM_STATES
            ):
                return 409, {
                    "ok": False,
                    "error": f"Cannot delete active vault '{run_id}'; stop the run first.",
                }
        try:
            deleted = DiskVaultStore.delete_vault(self._vault_root, run_id)
        except ValueError as exc:
            return 400, {"ok": False, "error": str(exc)}
        if not deleted:
            return 404, {"ok": False, "error": f"Vault '{run_id}' not found."}
        with self._lock:
            if self._current is not None and self._current.store.run_id == run_id:
                self._current = None
        return 200, {"ok": True, "deleted": True, "run_id": run_id}

    def wait_for_completion(self, timeout: float | None = None) -> bool:
        """Wait for the active run worker thread to reach a terminal state (helper for tests/CLI)."""
        with self._lock:
            ctrl = self._current
        if ctrl is None:
            return True
        return ctrl._done_event.wait(timeout=timeout)

    def shutdown_cleanup(self) -> None:
        """Safely stop any active run, destroy sandbox containers, and purge ephemeral RAM buffers."""
        with self._lock:
            ctrl = self._current
        if ctrl is not None:
            try:
                ctrl.request_stop()
                ctrl._done_event.wait(timeout=3.0)
            except Exception:
                pass
            if ctrl.req.retention_mode == "ephemeral":
                try:
                    ctrl.store.purge_report()
                except Exception:
                    pass
        try:
            self._tier_switcher.cleanup_orchestrator_containers()
        except Exception:
            pass
        with self._lock:
            if self._current is not None and self._current.req.retention_mode == "ephemeral":
                self._current = None
