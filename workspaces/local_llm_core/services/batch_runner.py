"""Overnight Batch Job Runner with max_safe_ctx chunking, checkpoints, exports & Thermal/RAM Watchdog."""

from __future__ import annotations

import asyncio
import csv
import datetime
import io
import json
import pathlib
import secrets
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from ..config import PROJECT_ROOT, SETTINGS
from ..engine.coordinator import get_gateway_coordinator
from .telemetry import get_gpu_telemetry, get_ram_telemetry
from .tuning_bounds import compute_tuning_bounds

BATCH_TASK_TYPES: tuple[str, ...] = (
    "translate_paragraphs",
    "bulk_summarize",
    "code_docstring_audit",
    "jsonl_extract",
)

ACTIVE_JOB_STATUSES = frozenset({"queued", "running", "paused", "paused_watchdog"})
TERMINAL_JOB_STATUSES = frozenset({"completed", "cancelled", "failed"})


def _utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()


def compute_safe_chunk_chars(model_alias: str = "local-fast", requested_chars: int | None = None) -> dict[str, int]:
    """Compute safe character budget per chunk strictly bounded by Layer 1 `max_safe_ctx`."""
    bounds = compute_tuning_bounds()
    is_heavy = (model_alias or "").strip().lower() in ("local-heavy", "heavy", "heavy-moe")
    tier_bounds = bounds["heavy_ctx"] if is_heavy else bounds["fast_ctx"]
    max_safe_ctx = int(tier_bounds.get("max_safe") or 8192)
    # Reserve ~60% of context for system instructions + model output; ~3.5 chars/token -> 1.4 * ctx chars
    max_chunk_chars = max(600, int(max_safe_ctx * 1.4))
    recommended_chars = max(600, min(max_chunk_chars, int(int(tier_bounds.get("recommended") or max_safe_ctx) * 1.0)))
    effective = recommended_chars
    if requested_chars is not None and requested_chars > 0:
        effective = max(200, min(max_chunk_chars, int(requested_chars)))
    return {
        "max_safe_ctx": max_safe_ctx,
        "max_chunk_chars": max_chunk_chars,
        "effective_chunk_chars": effective,
    }


def split_text_into_chunks(text: str, max_chunk_chars: int) -> list[str]:
    """Split text along paragraph/line boundaries so every chunk is <= max_chunk_chars."""
    cleaned = (text or "").strip()
    if not cleaned:
        return []
    cap = max(200, int(max_chunk_chars))
    paragraphs = [p.strip() for p in cleaned.split("\n\n") if p.strip()]
    if len(paragraphs) <= 1 and "\n" in cleaned:
        paragraphs = [ln.strip() for ln in cleaned.splitlines() if ln.strip()]

    chunks: list[str] = []
    current_parts: list[str] = []
    current_len = 0

    def _flush() -> None:
        nonlocal current_len
        if current_parts:
            chunks.append("\n\n".join(current_parts))
            current_parts.clear()
            current_len = 0

    for para in paragraphs:
        if len(para) > cap:
            _flush()
            # Split oversized paragraph cleanly by line or hard slice
            lines = para.splitlines() if "\n" in para else [para]
            buf = ""
            for line in lines:
                if len(line) > cap:
                    if buf:
                        chunks.append(buf)
                        buf = ""
                    for i in range(0, len(line), cap):
                        chunks.append(line[i : i + cap])
                elif not buf:
                    buf = line
                elif len(buf) + 1 + len(line) <= cap:
                    buf = f"{buf}\n{line}"
                else:
                    chunks.append(buf)
                    buf = line
            if buf:
                chunks.append(buf)
            continue

        sep_len = 2 if current_parts else 0
        if current_len + sep_len + len(para) <= cap:
            current_parts.append(para)
            current_len += sep_len + len(para)
        else:
            _flush()
            current_parts.append(para)
            current_len = len(para)

    _flush()
    return chunks


def _build_chunk_messages(
    task_type: str,
    chunk_text: str,
    *,
    target_language: str = "Italian",
    instruction: str = "",
    chunk_index: int = 0,
    total_chunks: int = 1,
) -> list[dict[str, str]]:
    extra = f" Extra instructions: {instruction.strip()}" if instruction.strip() else ""
    if task_type == "translate_paragraphs":
        sys_msg = (
            f"You are an accurate offline translator. Translate the provided text into {target_language}. "
            "Preserve structure, markdown, and technical terms. Output only the translation." + extra
        )
    elif task_type == "bulk_summarize":
        sys_msg = (
            "You are a concise analyst. Summarize the key facts, decisions, and entities in this text chunk "
            "using clear bullet points." + extra
        )
    elif task_type == "code_docstring_audit":
        sys_msg = (
            "You are a senior code reviewer. Audit this code snippet for missing docstrings, edge-case bugs, "
            "and security issues, and propose concise docstring/fix recommendations." + extra
        )
    else:  # jsonl_extract
        sys_msg = (
            "Extract structured entities, topics, and action items from this chunk as a single valid JSON object "
            "(or JSONL lines) with keys: entities, summary, tags." + extra
        )

    return [
        {"role": "system", "content": sys_msg},
        {
            "role": "user",
            "content": f"[Chunk {chunk_index + 1}/{total_chunks}]\n\n{chunk_text}",
        },
    ]


@dataclass
class WatchdogConfig:
    """Thermal & anti-swap RAM Watchdog thresholds."""

    enabled: bool = True
    max_gpu_temp_c: float = 82.0
    cooldown_gpu_temp_c: float = 74.0
    min_free_ram_gb: float = 6.0
    resume_free_ram_gb: float = 7.5
    poll_interval_sec: float = 1.5

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "max_gpu_temp_c": self.max_gpu_temp_c,
            "cooldown_gpu_temp_c": self.cooldown_gpu_temp_c,
            "min_free_ram_gb": self.min_free_ram_gb,
            "resume_free_ram_gb": self.resume_free_ram_gb,
            "poll_interval_sec": self.poll_interval_sec,
        }


class ThermalRamWatchdog:
    """Monitors GPU temperature & physical free RAM to auto-pause and auto-resume batch jobs."""

    def __init__(
        self,
        config: WatchdogConfig | None = None,
        telemetry_fn: Callable[[], tuple[dict[str, Any], dict[str, Any]]] | None = None,
    ) -> None:
        self.config = config or WatchdogConfig()
        self._telemetry_fn = telemetry_fn or (lambda: (get_gpu_telemetry(), get_ram_telemetry()))
        self.last_status: dict[str, Any] = {
            "triggered": False,
            "reason": None,
            "gpu_temp_c": None,
            "free_ram_gb": None,
            "checked_at": None,
        }

    def evaluate(self) -> tuple[bool, bool, str | None, dict[str, Any]]:
        """
        Evaluate live GPU/RAM telemetry against watchdog thresholds.

        Returns `(should_pause, can_resume, reason, snapshot)`.
        """
        gpu, ram = self._telemetry_fn()
        raw_temp = gpu.get("tempC") if "tempC" in gpu else gpu.get("temperatureC")
        gpu_temp = float(raw_temp) if raw_temp is not None else None
        raw_ram = ram.get("freeGb")
        free_ram = float(raw_ram) if raw_ram is not None else None

        should_pause = False
        reasons: list[str] = []
        if self.config.enabled:
            if gpu_temp is not None and gpu_temp >= self.config.max_gpu_temp_c:
                should_pause = True
                reasons.append(
                    f"GPU temperature {gpu_temp:.1f}°C >= threshold {self.config.max_gpu_temp_c:.1f}°C"
                )
            if free_ram is not None and free_ram < self.config.min_free_ram_gb:
                should_pause = True
                reasons.append(
                    f"Free RAM {free_ram:.2f} GiB < floor {self.config.min_free_ram_gb:.2f} GiB"
                )

        can_resume = True
        if self.config.enabled:
            temp_ok = gpu_temp is None or gpu_temp <= self.config.cooldown_gpu_temp_c
            ram_ok = free_ram is None or free_ram >= self.config.resume_free_ram_gb
            can_resume = temp_ok and ram_ok

        reason_str = "; ".join(reasons) if reasons else None
        snap = {
            "triggered": should_pause,
            "can_resume": can_resume,
            "reason": reason_str,
            "gpu_temp_c": gpu_temp,
            "free_ram_gb": free_ram,
            "checked_at": _utc_now_iso(),
            "thresholds": self.config.to_dict(),
        }
        self.last_status = snap
        return should_pause, can_resume, reason_str, snap


@dataclass
class BatchChunk:
    """Single checkpointed work unit inside a BatchJob."""

    index: int
    input_text: str
    status: str = "pending"  # "pending" | "running" | "done" | "error" | "cancelled"
    output_text: str = ""
    duration_ms: int = 0
    error: str | None = None
    updated_at: str | None = None

    def to_dict(self, include_text: bool = True) -> dict[str, Any]:
        out: dict[str, Any] = {
            "index": self.index,
            "status": self.status,
            "duration_ms": self.duration_ms,
            "error": self.error,
            "updated_at": self.updated_at,
            "input_chars": len(self.input_text),
            "output_chars": len(self.output_text),
        }
        if include_text:
            out["input_text"] = self.input_text
            out["output_text"] = self.output_text
        return out


@dataclass
class BatchJob:
    """Resumable chunked overnight batch job."""

    job_id: str
    task_type: str
    model: str
    max_safe_ctx: int
    chunk_chars: int
    target_language: str = "Italian"
    instruction: str = ""
    persist_checkpoint: bool = False
    status: str = "queued"  # "queued" | "running" | "paused" | "paused_watchdog" | "completed" | "cancelled" | "failed"
    created_at: str = field(default_factory=_utc_now_iso)
    started_at: str | None = None
    finished_at: str | None = None
    watchdog_pause_count: int = 0
    watchdog_reason: str | None = None
    error: str | None = None
    chunks: list[BatchChunk] = field(default_factory=list)

    def completed_count(self) -> int:
        return sum(1 for c in self.chunks if c.status == "done")

    def eta_seconds(self) -> float | None:
        done_chunks = [c for c in self.chunks if c.status == "done" and c.duration_ms > 0]
        remaining = sum(1 for c in self.chunks if c.status in ("pending", "running"))
        if not done_chunks or remaining == 0:
            return 0.0 if remaining == 0 else None
        avg_ms = sum(c.duration_ms for c in done_chunks) / len(done_chunks)
        return round((avg_ms * remaining) / 1000.0, 1)

    def to_dict(self, include_chunks: bool = True) -> dict[str, Any]:
        total = len(self.chunks)
        done = self.completed_count()
        pct = round((done / total) * 100.0, 1) if total > 0 else 0.0
        payload: dict[str, Any] = {
            "job_id": self.job_id,
            "task_type": self.task_type,
            "model": self.model,
            "max_safe_ctx": self.max_safe_ctx,
            "chunk_chars": self.chunk_chars,
            "target_language": self.target_language,
            "instruction": self.instruction,
            "persist_checkpoint": self.persist_checkpoint,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "total_chunks": total,
            "completed_chunks": done,
            "progress_pct": pct,
            "eta_seconds": self.eta_seconds(),
            "watchdog_pause_count": self.watchdog_pause_count,
            "watchdog_reason": self.watchdog_reason,
            "error": self.error,
        }
        if include_chunks:
            payload["chunks"] = [c.to_dict(include_text=True) for c in self.chunks]
        return payload


LlmChunkCallable = Callable[[str, list[dict[str, str]], int], Awaitable[str]]


async def _default_llm_chunk_call(model: str, messages: list[dict[str, str]], num_ctx: int) -> str:
    coord = get_gateway_coordinator()
    spec = coord.resolve_model(model or "local-fast")
    backend = coord.get_backend(spec.backend_name)
    payload: dict[str, Any] = {
        "model": spec.concrete_model,
        "messages": messages,
        "temperature": 0.2,
        "stream": False,
    }
    if spec.backend_name == "ollama":
        payload["options"] = {"num_ctx": int(num_ctx)}
    status, resp = await backend.chat_completions(payload, timeout=180.0)
    if status >= 400 or not isinstance(resp, dict):
        err = resp.get("error") if isinstance(resp, dict) else f"HTTP {status}"
        raise RuntimeError(f"Local LLM backend error ({status}): {err}")
    choices = resp.get("choices") or []
    if choices and isinstance(choices[0], dict):
        msg = choices[0].get("message") or {}
        if isinstance(msg, dict) and msg.get("content"):
            return str(msg["content"]).strip()
    if resp.get("message") and isinstance(resp["message"], dict):
        return str(resp["message"].get("content") or "").strip()
    return ""


class BatchJobRunner:
    """Async chunked batch queue manager with checkpoints, pause/resume/cancel & Thermal/RAM Watchdog."""

    def __init__(
        self,
        *,
        checkpoint_dir: pathlib.Path | None = None,
        watchdog: ThermalRamWatchdog | None = None,
        llm_fn: LlmChunkCallable | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self._jobs: dict[str, BatchJob] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._checkpoint_dir = checkpoint_dir or (PROJECT_ROOT / "workspaces" / "runs" / "batch")
        self.watchdog = watchdog or ThermalRamWatchdog()
        self._llm_fn = llm_fn or _default_llm_chunk_call

    def set_llm_fn(self, fn: LlmChunkCallable) -> None:
        """Override LLM chunk executor (useful for deterministic unit tests)."""
        self._llm_fn = fn

    def set_telemetry_fn(self, fn: Callable[[], tuple[dict[str, Any], dict[str, Any]]]) -> None:
        """Override GPU/RAM telemetry probe for watchdog testing."""
        self.watchdog._telemetry_fn = fn

    def _save_checkpoint(self, job: BatchJob) -> None:
        if not job.persist_checkpoint:
            return
        self._checkpoint_dir.mkdir(parents=True, exist_ok=True)
        dest = self._checkpoint_dir / f"{job.job_id}.json"
        tmp = self._checkpoint_dir / f".{job.job_id}.{secrets.token_hex(4)}.tmp"
        tmp.write_text(json.dumps(job.to_dict(include_chunks=True), indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(dest)

    def _collect_input_chunks(self, payload: dict[str, Any], chunk_chars: int) -> list[str]:
        raw_items = payload.get("items")
        if isinstance(raw_items, list) and raw_items:
            out: list[str] = []
            for item in raw_items:
                s = str(item or "").strip()
                if s:
                    out.extend(split_text_into_chunks(s, chunk_chars))
            return out
        raw_text = str(payload.get("input_text") or "").strip()
        if raw_text:
            return split_text_into_chunks(raw_text, chunk_chars)
        return []

    async def create_and_start_job(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        task_type = str(payload.get("task_type") or "bulk_summarize").strip()
        if task_type not in BATCH_TASK_TYPES:
            return 400, {"ok": False, "error": f"Invalid task_type '{task_type}'. Must be one of {BATCH_TASK_TYPES}."}

        model = str(payload.get("model") or SETTINGS.fast_model or "local-fast").strip()
        req_chunk_chars = payload.get("chunk_chars")
        try:
            req_chars_int = int(req_chunk_chars) if req_chunk_chars is not None else None
        except (TypeError, ValueError):
            req_chars_int = None

        sizing = compute_safe_chunk_chars(model, req_chars_int)
        chunk_texts = self._collect_input_chunks(payload, sizing["effective_chunk_chars"])
        if not chunk_texts:
            return 400, {"ok": False, "error": "Provide non-empty 'input_text' or 'items' to process."}

        job_id = f"batch_{secrets.token_hex(5)}"
        chunks = [BatchChunk(index=idx, input_text=txt) for idx, txt in enumerate(chunk_texts)]
        job = BatchJob(
            job_id=job_id,
            task_type=task_type,
            model=model,
            max_safe_ctx=sizing["max_safe_ctx"],
            chunk_chars=sizing["effective_chunk_chars"],
            target_language=str(payload.get("target_language") or "Italian").strip() or "Italian",
            instruction=str(payload.get("instruction") or "").strip(),
            persist_checkpoint=bool(payload.get("persist_checkpoint", False)),
            status="running",
            started_at=_utc_now_iso(),
            chunks=chunks,
        )

        with self._lock:
            self._jobs[job_id] = job
            self._save_checkpoint(job)

        if payload.get("auto_start", True):
            task = asyncio.create_task(self._run_job_loop(job_id))
            with self._lock:
                self._tasks[job_id] = task

        return 200, {"ok": True, "job": job.to_dict(include_chunks=True)}

    async def _run_job_loop(self, job_id: str) -> None:
        while True:
            with self._lock:
                job = self._jobs.get(job_id)
                if job is None or job.status in ("cancelled", "completed", "failed"):
                    return
                if job.status == "paused":
                    # Stay paused until explicit resume_job()
                    pass
                status_now = job.status

            if status_now == "paused":
                await asyncio.sleep(0.05)
                continue

            # Evaluate Thermal & RAM Watchdog before processing next chunk
            should_pause, can_resume, reason, _ = self.watchdog.evaluate()
            if status_now == "paused_watchdog":
                if can_resume and not should_pause:
                    with self._lock:
                        if job.status == "paused_watchdog":
                            job.status = "running"
                            job.watchdog_reason = None
                            self._save_checkpoint(job)
                else:
                    await asyncio.sleep(max(0.02, self.watchdog.config.poll_interval_sec))
                    continue
            elif should_pause:
                with self._lock:
                    if job.status == "running":
                        job.status = "paused_watchdog"
                        job.watchdog_pause_count += 1
                        job.watchdog_reason = reason
                        self._save_checkpoint(job)
                await asyncio.sleep(max(0.02, self.watchdog.config.poll_interval_sec))
                continue

            # Find next pending chunk
            next_chunk: BatchChunk | None = None
            with self._lock:
                for ch in job.chunks:
                    if ch.status == "pending":
                        ch.status = "running"
                        next_chunk = ch
                        break
                if next_chunk is None:
                    job.status = "completed"
                    job.finished_at = _utc_now_iso()
                    self._save_checkpoint(job)
                    return

            t0 = time.monotonic()
            try:
                msgs = _build_chunk_messages(
                    job.task_type,
                    next_chunk.input_text,
                    target_language=job.target_language,
                    instruction=job.instruction,
                    chunk_index=next_chunk.index,
                    total_chunks=len(job.chunks),
                )
                out_text = await self._llm_fn(job.model, msgs, job.max_safe_ctx)
                elapsed_ms = max(1, int(round((time.monotonic() - t0) * 1000.0)))
                with self._lock:
                    if job.status == "cancelled":
                        next_chunk.status = "cancelled"
                    else:
                        next_chunk.status = "done"
                        next_chunk.output_text = out_text
                        next_chunk.duration_ms = elapsed_ms
                        next_chunk.updated_at = _utc_now_iso()
                    self._save_checkpoint(job)
            except asyncio.CancelledError:
                with self._lock:
                    if next_chunk.status == "running":
                        next_chunk.status = "pending"
                    self._save_checkpoint(job)
                raise
            except Exception as exc:
                elapsed_ms = max(1, int(round((time.monotonic() - t0) * 1000.0)))
                with self._lock:
                    next_chunk.status = "error"
                    next_chunk.error = str(exc)
                    next_chunk.duration_ms = elapsed_ms
                    next_chunk.updated_at = _utc_now_iso()
                    job.status = "failed"
                    job.error = str(exc)
                    job.finished_at = _utc_now_iso()
                    self._save_checkpoint(job)
                return

    def list_jobs(self) -> dict[str, Any]:
        _, _, _, wd_snap = self.watchdog.evaluate()
        with self._lock:
            jobs = [j.to_dict(include_chunks=False) for j in reversed(list(self._jobs.values()))]
        return {
            "ok": True,
            "jobs": jobs,
            "watchdog": wd_snap,
        }

    def get_job(self, job_id: str) -> tuple[int, dict[str, Any]]:
        _, _, _, wd_snap = self.watchdog.evaluate()
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return 404, {"ok": False, "error": f"Batch job '{job_id}' not found."}
            return 200, {"ok": True, "job": job.to_dict(include_chunks=True), "watchdog": wd_snap}

    def pause_job(self, job_id: str) -> tuple[int, dict[str, Any]]:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return 404, {"ok": False, "error": f"Batch job '{job_id}' not found."}
            if job.status in TERMINAL_JOB_STATUSES:
                return 409, {"ok": False, "error": f"Cannot pause job in terminal state '{job.status}'."}
            job.status = "paused"
            self._save_checkpoint(job)
            return 200, {"ok": True, "job": job.to_dict(include_chunks=False)}

    async def resume_job(self, job_id: str) -> tuple[int, dict[str, Any]]:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return 404, {"ok": False, "error": f"Batch job '{job_id}' not found."}
            if job.status in ("completed", "cancelled"):
                return 409, {"ok": False, "error": f"Cannot resume job in state '{job.status}'."}
            for ch in job.chunks:
                if ch.status == "error":
                    ch.status = "pending"
                    ch.error = None
            job.error = None
            job.status = "running"
            job.watchdog_reason = None
            self._save_checkpoint(job)
            task = self._tasks.get(job_id)
            need_spawn = task is None or task.done()
            if need_spawn:
                self._tasks[job_id] = asyncio.create_task(self._run_job_loop(job_id))
            return 200, {"ok": True, "job": job.to_dict(include_chunks=False)}

    def cancel_job(self, job_id: str) -> tuple[int, dict[str, Any]]:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return 404, {"ok": False, "error": f"Batch job '{job_id}' not found."}
            job.status = "cancelled"
            job.finished_at = _utc_now_iso()
            for ch in job.chunks:
                if ch.status in ("pending", "running"):
                    ch.status = "cancelled"
            self._save_checkpoint(job)
            task = self._tasks.get(job_id)
            if task is not None and not task.done():
                task.cancel()
            return 200, {"ok": True, "job": job.to_dict(include_chunks=False)}

    def export_job(self, job_id: str, fmt: str = "md") -> tuple[int, str | dict[str, Any], str, str]:
        """Export job results as Markdown, JSON, or CSV."""
        norm_fmt = (fmt or "md").strip().lower()
        if norm_fmt not in ("md", "markdown", "json", "csv"):
            return 400, {"ok": False, "error": "Unsupported export format (use md, json, csv)."}, "application/json", ""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return 404, {"ok": False, "error": f"Batch job '{job_id}' not found."}, "application/json", ""
            job_dict = job.to_dict(include_chunks=True)

        if norm_fmt == "json":
            return (
                200,
                json.dumps(job_dict, indent=2, ensure_ascii=False),
                "application/json; charset=utf-8",
                f"{job_id}.json",
            )
        if norm_fmt == "csv":
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow(["index", "status", "duration_ms", "input_text", "output_text", "error"])
            for ch in job_dict["chunks"]:
                writer.writerow([
                    ch["index"],
                    ch["status"],
                    ch["duration_ms"],
                    ch["input_text"],
                    ch["output_text"],
                    ch.get("error") or "",
                ])
            return (
                200,
                buf.getvalue(),
                "text/csv; charset=utf-8",
                f"{job_id}.csv",
            )

        # Markdown
        lines = [
            f"# Local LLM Batch Export — {job_id}",
            "",
            f"- **Task:** `{job_dict['task_type']}`",
            f"- **Model:** `{job_dict['model']}` (`max_safe_ctx={job_dict['max_safe_ctx']}`)",
            f"- **Status:** `{job_dict['status']}` ({job_dict['completed_chunks']}/{job_dict['total_chunks']} chunks)",
            "",
        ]
        for ch in job_dict["chunks"]:
            lines.append(f"## Chunk {ch['index'] + 1} [{ch['status']}]")
            lines.append("")
            lines.append(ch["output_text"] or ch.get("error") or "_(no output)_")
            lines.append("")
        return (
            200,
            "\n".join(lines),
            "text/markdown; charset=utf-8",
            f"{job_id}.md",
        )

    def get_watchdog_status(self) -> dict[str, Any]:
        _, _, _, snap = self.watchdog.evaluate()
        return {"ok": True, "watchdog": snap}

    def update_watchdog_config(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        cfg = self.watchdog.config
        with self._lock:
            if "enabled" in payload:
                cfg.enabled = bool(payload["enabled"])
            if "max_gpu_temp_c" in payload:
                cfg.max_gpu_temp_c = max(50.0, min(98.0, float(payload["max_gpu_temp_c"])))
            if "cooldown_gpu_temp_c" in payload:
                cfg.cooldown_gpu_temp_c = max(40.0, min(cfg.max_gpu_temp_c - 1.0, float(payload["cooldown_gpu_temp_c"])))
            if "min_free_ram_gb" in payload:
                cfg.min_free_ram_gb = max(1.0, min(128.0, float(payload["min_free_ram_gb"])))
            if "resume_free_ram_gb" in payload:
                cfg.resume_free_ram_gb = max(cfg.min_free_ram_gb, min(128.0, float(payload["resume_free_ram_gb"])))
            if "poll_interval_sec" in payload:
                cfg.poll_interval_sec = max(0.02, min(30.0, float(payload["poll_interval_sec"])))
        return 200, self.get_watchdog_status()


_BATCH_RUNNER: BatchJobRunner | None = None
_BATCH_LOCK = threading.Lock()


def get_batch_runner() -> BatchJobRunner:
    """Return singleton BatchJobRunner instance."""
    global _BATCH_RUNNER
    with _BATCH_LOCK:
        if _BATCH_RUNNER is None:
            _BATCH_RUNNER = BatchJobRunner()
        return _BATCH_RUNNER
