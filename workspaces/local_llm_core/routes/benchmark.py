"""Dedicated Benchmark endpoint (`POST /api/benchmark/run`) with cold/warm & thinking-token metrics."""

from __future__ import annotations

import asyncio
import datetime
import json
import re
import time
from typing import Any

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..engine.coordinator import GatewayEngineError, get_gateway_coordinator
from ..services.ledger import get_cost_ledger
from ..services.telemetry import get_gpu_telemetry, get_ram_telemetry

router = APIRouter(tags=["benchmark"])

_DEFAULT_BENCHMARK_PROMPT = (
    "Explain in 5 concise technical bullet points how loopback-only isolation "
    "(127.0.0.1) and ephemeral RAM-only storage reduce the attack surface of a local LLM gateway."
)
_THINK_TAG_RE = re.compile(r"<think>([\s\S]*?)</think>", re.IGNORECASE)


class BenchmarkRunRequest(BaseModel):
    """Request payload for `POST /api/benchmark/run`."""

    model: str = Field(default="local-fast", min_length=1, max_length=160)
    prompt: str | None = Field(default=None, max_length=4000)
    max_tokens: int = Field(default=128, ge=8, le=2048)
    warmup: bool = False
    allow_tier_switch: bool = True


def _split_think_tags(raw_content: str) -> tuple[str, str]:
    """Split inline `<think>...</think>` (including unclosed `<think>`) from visible text."""
    think_parts: list[str] = []

    def _collect(match: re.Match[str]) -> str:
        inner = match.group(1).strip()
        if inner:
            think_parts.append(inner)
        return ""

    visible = _THINK_TAG_RE.sub(_collect, raw_content or "")
    open_idx = visible.lower().find("<think>")
    if open_idx != -1:
        partial = visible[open_idx + 7 :].strip()
        if partial:
            think_parts.append(partial)
        visible = visible[:open_idx]
    return "\n\n".join(think_parts).strip(), visible.strip()


def _model_matches_installed(concrete_model: str, installed: list[str]) -> bool:
    target = concrete_model.strip().lower()
    norm_installed = {m.strip().lower() for m in installed if m and m.strip()}
    if target in norm_installed:
        return True
    if ":" not in target and f"{target}:latest" in norm_installed:
        return True
    return bool(target.endswith(":latest") and target[:-7] in norm_installed)


async def _is_ollama_model_loaded(backend: Any, concrete_model: str) -> bool | None:
    """Best-effort check of Ollama `/api/ps` to see whether model weights are already in VRAM."""
    client_fn = getattr(backend, "_client", None)
    if not callable(client_fn):
        return None
    try:
        async with client_fn(timeout=0.8) as client:
            resp = await client.get("/api/ps", headers={"Accept": "application/json"})
            if resp.status_code != 200:
                return None
            data = resp.json()
            if not isinstance(data, dict):
                return None
            loaded = [
                str(item.get("name"))
                for item in data.get("models", [])
                if isinstance(item, dict) and item.get("name")
            ]
            return _model_matches_installed(concrete_model, loaded)
    except Exception:
        return None


def _collect_hardware_summary() -> dict[str, Any]:
    gpu = get_gpu_telemetry()
    ram = get_ram_telemetry()
    return {
        "gpu_name": gpu.get("name"),
        "vram_total_gb": gpu.get("vramTotalGb") if gpu.get("available") else None,
        "vram_free_gb": gpu.get("vramFreeGb") if gpu.get("available") else None,
        "ram_total_gb": ram.get("totalGb"),
        "ram_free_gb": ram.get("freeGb"),
    }


def _estimate_token_split(
    *,
    explicit_thinking_chars: int,
    explicit_thinking_chunks: int,
    raw_content: str,
    content_chunks: int,
    observed_completion_tokens: int | None,
) -> tuple[int, int, int]:
    """Compute `(thinking_tokens, visible_tokens, total_tokens)` including reasoning/thinking tokens."""
    inline_thinking, visible_text = _split_think_tags(raw_content)
    inline_think_chars = len(inline_thinking)
    visible_chars = len(visible_text)

    inline_think_chunks = 0
    visible_chunks = 0
    if content_chunks > 0:
        if inline_think_chars > 0 and visible_chars == 0:
            inline_think_chunks = content_chunks
        elif inline_think_chars == 0 and visible_chars > 0:
            visible_chunks = content_chunks
        elif (inline_think_chars + visible_chars) > 0:
            ratio = inline_think_chars / (inline_think_chars + visible_chars)
            inline_think_chunks = round(content_chunks * ratio)
            visible_chunks = max(0, content_chunks - inline_think_chunks)

    total_think_chars = explicit_thinking_chars + inline_think_chars
    total_think_chunks = explicit_thinking_chunks + inline_think_chunks

    est_thinking = max(total_think_chunks, round(total_think_chars / 4)) if total_think_chars > 0 else 0
    est_visible = max(visible_chunks, round(visible_chars / 4)) if visible_chars > 0 else 0

    if observed_completion_tokens is not None and observed_completion_tokens > 0:
        if est_thinking > 0 and est_visible == 0:
            return observed_completion_tokens, 0, observed_completion_tokens
        if est_visible > 0 and est_thinking == 0:
            return 0, observed_completion_tokens, observed_completion_tokens
        if (est_thinking + est_visible) > 0:
            t_tok = round(observed_completion_tokens * (est_thinking / (est_thinking + est_visible)))
            v_tok = max(0, observed_completion_tokens - t_tok)
            return t_tok, v_tok, observed_completion_tokens
        return 0, observed_completion_tokens, observed_completion_tokens

    total = est_thinking + est_visible
    return est_thinking, est_visible, total


@router.post("/api/benchmark/run")
async def run_benchmark(req: BenchmarkRunRequest, request: Request) -> JSONResponse:
    """Run a verified local LLM streaming benchmark measuring TTFT, thinking tokens, and visible tok/s."""
    coord = get_gateway_coordinator()
    try:
        resolved = coord.resolve_model(req.model)
    except GatewayEngineError as exc:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "ok": False,
                "status": "ERROR",
                "code": exc.code,
                "error": exc.message,
                "model": req.model,
            },
        )

    hdr_switch = (request.headers.get("x-allow-tier-switch") or "").strip().lower()
    if hdr_switch in ("0", "false", "no", "off"):
        allow_switch = False
    elif hdr_switch in ("1", "true", "yes", "on"):
        allow_switch = True
    else:
        allow_switch = req.allow_tier_switch

    try:
        switched = await coord.ensure_tier(resolved.tier, allow_tier_switch=allow_switch)
    except GatewayEngineError as exc:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "ok": False,
                "status": "OFFLINE" if exc.code == "backend_offline" else "ERROR",
                "code": exc.code,
                "error": exc.message,
                "model": resolved.concrete_model,
            },
        )

    backend = coord.get_backend(resolved.backend_name)
    is_healthy, installed_models = await asyncio.gather(backend.health(), backend.list_models())
    if not is_healthy:
        return JSONResponse(
            status_code=503,
            content={
                "ok": False,
                "status": "OFFLINE",
                "code": "backend_offline",
                "error": (
                    f"Backend '{resolved.backend_name}' offline o non raggiungibile "
                    f"({backend.base_url}). Avvia il motore prima di eseguire il benchmark."
                ),
                "model": resolved.concrete_model,
            },
        )

    if installed_models and not _model_matches_installed(resolved.concrete_model, installed_models):
        return JSONResponse(
            status_code=404,
            content={
                "ok": False,
                "status": "MODEL_NOT_FOUND",
                "code": "model_not_found",
                "error": (
                    f"Modello '{resolved.concrete_model}' non trovato su '{resolved.backend_name}'. "
                    f"Disponibili: {', '.join(installed_models[:8])}"
                ),
                "model": resolved.concrete_model,
                "available_models": installed_models,
            },
        )

    loaded_before: bool | None = None
    if not switched and resolved.backend_name == "ollama":
        loaded_before = await _is_ollama_model_loaded(backend, resolved.concrete_model)

    prompt_text = (req.prompt or "").strip() or _DEFAULT_BENCHMARK_PROMPT

    try:
        if req.warmup:
            warmup_payload = {
                "model": resolved.concrete_model,
                "messages": [{"role": "user", "content": "Reply with OK."}],
                "temperature": 0.0,
                "max_tokens": 4,
            }
            async for _ in backend.chat_completions_stream(warmup_payload):
                pass
            cold_start = False
        else:
            cold_start = bool(switched or (loaded_before is False))

        upstream_payload = {
            "model": resolved.concrete_model,
            "messages": [{"role": "user", "content": prompt_text}],
            "temperature": 0.2,
            "max_tokens": req.max_tokens,
        }

        t0 = time.perf_counter()
        first_token_ms: float | None = None
        explicit_thinking_chars = 0
        explicit_thinking_chunks = 0
        content_parts: list[str] = []
        content_chunks = 0
        observed_prompt_tokens: int | None = None
        observed_completion_tokens: int | None = None
        stream_error: str | None = None

        async for raw_chunk in backend.chat_completions_stream(upstream_payload):
            text_chunk = raw_chunk.decode("utf-8", errors="replace")
            for line in text_chunk.splitlines():
                if not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if not data_str or data_str == "[DONE]":
                    continue
                try:
                    evt = json.loads(data_str)
                except ValueError:
                    continue
                if not isinstance(evt, dict):
                    continue
                if evt.get("error"):
                    err_obj = evt["error"]
                    stream_error = (
                        err_obj.get("message") if isinstance(err_obj, dict) else str(err_obj)
                    ) or "Upstream stream error"
                    break
                u = evt.get("usage")
                if isinstance(u, dict):
                    if isinstance(u.get("prompt_tokens"), int):
                        observed_prompt_tokens = u["prompt_tokens"]
                    if isinstance(u.get("completion_tokens"), int):
                        observed_completion_tokens = u["completion_tokens"]
                for ch in evt.get("choices") or []:
                    if not isinstance(ch, dict):
                        continue
                    delta = ch.get("delta")
                    if not isinstance(delta, dict):
                        continue
                    r_txt = delta.get("reasoning_content") or delta.get("thinking")
                    c_txt = delta.get("content")
                    if r_txt:
                        if first_token_ms is None:
                            first_token_ms = (time.perf_counter() - t0) * 1000.0
                        r_str = str(r_txt)
                        explicit_thinking_chars += len(r_str)
                        explicit_thinking_chunks += 1
                    if c_txt:
                        if first_token_ms is None:
                            first_token_ms = (time.perf_counter() - t0) * 1000.0
                        c_str = str(c_txt)
                        content_parts.append(c_str)
                        content_chunks += 1
            if stream_error:
                break

        if stream_error:
            return JSONResponse(
                status_code=502,
                content={
                    "ok": False,
                    "status": "ERROR",
                    "code": "upstream_stream_error",
                    "error": stream_error,
                    "model": resolved.concrete_model,
                },
            )

        duration_s = round(max(0.001, time.perf_counter() - t0), 3)
        raw_content = "".join(content_parts)
        thinking_tokens, visible_tokens, total_tokens = _estimate_token_split(
            explicit_thinking_chars=explicit_thinking_chars,
            explicit_thinking_chunks=explicit_thinking_chunks,
            raw_content=raw_content,
            content_chunks=content_chunks,
            observed_completion_tokens=observed_completion_tokens,
        )

        if total_tokens <= 0:
            return JSONResponse(
                status_code=502,
                content={
                    "ok": False,
                    "status": "ERROR",
                    "code": "empty_benchmark_output",
                    "error": (
                        f"Il modello '{resolved.concrete_model}' non ha prodotto alcun token "
                        "(verifica che il modello sia valido e non in crash)."
                    ),
                    "model": resolved.concrete_model,
                },
            )

        tok_per_sec = round(total_tokens / duration_s, 2)
        thinking_tok_s = round(thinking_tokens / duration_s, 2)
        visible_tok_s = round(visible_tokens / duration_s, 2)
        ttft_ms = round(first_token_ms, 1) if first_token_ms is not None else None

        prompt_tok_est = observed_prompt_tokens if observed_prompt_tokens is not None else max(1, len(prompt_text) // 4)
        get_cost_ledger().record_usage(
            "local-llm-ui",
            "benchmark",
            model_alias=resolved.alias,
            prompt_tokens=prompt_tok_est,
            completion_tokens=total_tokens,
        )

        hw_summary = await asyncio.to_thread(_collect_hardware_summary)
        timestamp_iso = datetime.datetime.now(datetime.UTC).isoformat()

        return JSONResponse(
            status_code=200,
            content={
                "ok": True,
                "status": "OK",
                "model": resolved.concrete_model,
                "requested_model": req.model,
                "resolved_alias": resolved.alias,
                "tier": resolved.tier,
                "backend": resolved.backend_name,
                "cold_start": cold_start,
                "ttft_ms": ttft_ms,
                "thinking_tokens": thinking_tokens,
                "visible_tokens": visible_tokens,
                "total_tokens": total_tokens,
                "duration_s": duration_s,
                "thinking_tok_s": thinking_tok_s,
                "visible_tok_s": visible_tok_s,
                "tok_per_sec": tok_per_sec,
                "hardware_summary": hw_summary,
                "timestamp": timestamp_iso,
            },
        )
    except httpx.TimeoutException as exc:
        return JSONResponse(
            status_code=504,
            content={
                "ok": False,
                "status": "ERROR",
                "code": "benchmark_timeout",
                "error": f"Timeout comunicazione con '{resolved.backend_name}': {exc}",
                "model": resolved.concrete_model,
            },
        )
    except (httpx.ConnectError, OSError) as exc:
        return JSONResponse(
            status_code=503,
            content={
                "ok": False,
                "status": "OFFLINE",
                "code": "benchmark_connection_error",
                "error": f"Connessione rifiutata o interrotta verso '{resolved.backend_name}': {exc}",
                "model": resolved.concrete_model,
            },
        )
    except Exception as exc:
        return JSONResponse(
            status_code=502,
            content={
                "ok": False,
                "status": "ERROR",
                "code": "benchmark_failed",
                "error": f"Errore benchmark su '{resolved.concrete_model}': {exc}",
                "model": resolved.concrete_model,
            },
        )
