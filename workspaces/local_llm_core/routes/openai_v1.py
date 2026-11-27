"""Unified OpenAI-Compatible Gateway routes (`/v1/health`, `/v1/models`, `/v1/chat/completions`, `/v1/embeddings`)."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from ..config import SETTINGS
from ..engine.coordinator import (
    GatewayEngineError,
    ResolvedModelSpec,
    get_gateway_coordinator,
)
from ..prompt_firewall import is_prompt_firewall_enabled, redact_messages
from ..services.ledger import BudgetExceededError, get_cost_ledger
from ..services.telemetry import (
    async_get_full_telemetry,
    get_orchestrator_telemetry_summary,
)

router = APIRouter(prefix="/v1", tags=["openai-v1"])

_CANONICAL_ALIASES: tuple[dict[str, Any], ...] = (
    {
        "id": "auto",
        "object": "model",
        "owned_by": "local-llm-gateway",
        "tier": "cascade",
        "description": "Cascade Router: starts on local-fast and escalates to local-balanced/local-heavy on schema/confidence failure.",
    },
    {
        "id": "local-fast",
        "object": "model",
        "owned_by": "local-llm-gateway",
        "tier": "fast",
        "context_window": 32768,
    },
    {
        "id": "local-balanced",
        "object": "model",
        "owned_by": "local-llm-gateway",
        "tier": "fast",
        "context_window": 16384,
    },
    {
        "id": "local-heavy",
        "object": "model",
        "owned_by": "local-llm-gateway",
        "tier": "heavy",
        "context_window": 16384,
    },
)


class ChatCompletionRequest(BaseModel):
    """OpenAI-compatible `/v1/chat/completions` request schema."""

    model_config = ConfigDict(extra="allow")

    model: str = Field(min_length=1)
    messages: list[dict[str, Any]]
    stream: bool = False
    temperature: float | None = None
    max_tokens: int | None = None
    response_format: dict[str, Any] | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: Any = None
    escalate: bool = False
    confidence_threshold: float | None = None
    metadata: dict[str, Any] | None = None


class EmbeddingsRequest(BaseModel):
    """OpenAI-compatible `/v1/embeddings` request schema."""

    model_config = ConfigDict(extra="allow")

    model: str = "nomic-embed-text"
    input: str | list[str] | list[int] | list[list[int]]
    encoding_format: str | None = None


def _openai_error(
    status_code: int,
    message: str,
    *,
    error_type: str = "invalid_request_error",
    code: str = "error",
    retry_after: int | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    hdrs = {"X-Retention-Mode": "ephemeral"}
    if retry_after is not None:
        hdrs["Retry-After"] = str(retry_after)
    if headers:
        hdrs.update(headers)
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "message": message,
                "type": error_type,
                "code": code,
            }
        },
        headers=hdrs,
    )


def _parse_bool_header(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def _parse_int_header(value: str | None) -> int | None:
    if not value:
        return None
    try:
        parsed = int(value.strip())
        return parsed if parsed > 0 else None
    except ValueError:
        return None


def _resolve_retention_header(raw_val: str | None) -> str:
    # Zero-disk invariant: gateway always defaults to ephemeral and never persists prompts/replies.
    _ = raw_val
    return "ephemeral"


def _validate_json_against_schema(data: Any, schema: dict[str, Any]) -> tuple[bool, str]:
    """Deterministic lightweight JSON Schema validator for `response_format.json_schema`."""
    expected_type = schema.get("type")
    if expected_type is not None:
        allowed = expected_type if isinstance(expected_type, list) else [expected_type]
        type_ok = False
        for t in allowed:
            if t == "object" and isinstance(data, dict) or t == "array" and isinstance(data, list) or t == "string" and isinstance(data, str) or t == "integer" and isinstance(data, int) and not isinstance(data, bool) or t == "number" and isinstance(data, (int, float)) and not isinstance(data, bool) or t == "boolean" and isinstance(data, bool) or t == "null" and data is None:
                type_ok = True
        if not type_ok:
            return False, f"expected type {expected_type!r}, got {type(data).__name__}"

    if "enum" in schema and isinstance(schema["enum"], list):
        if data not in schema["enum"]:
            return False, f"value {data!r} not in enum"

    if isinstance(data, dict):
        for req_key in schema.get("required", []) or []:
            if req_key not in data:
                return False, f"missing required property {req_key!r}"
        props = schema.get("properties")
        if isinstance(props, dict):
            for key, sub_schema in props.items():
                if key in data and isinstance(sub_schema, dict):
                    ok, msg = _validate_json_against_schema(data[key], sub_schema)
                    if not ok:
                        return False, f"{key}: {msg}"
            if schema.get("additionalProperties") is False:
                extra_keys = set(data.keys()) - set(props.keys())
                if extra_keys:
                    return False, f"unexpected properties: {sorted(extra_keys)}"

    if isinstance(data, list):
        items_schema = schema.get("items")
        if isinstance(items_schema, dict):
            for idx, item in enumerate(data):
                ok, msg = _validate_json_against_schema(item, items_schema)
                if not ok:
                    return False, f"[{idx}]: {msg}"

    if isinstance(data, (int, float)) and not isinstance(data, bool):
        if "minimum" in schema and data < schema["minimum"]:
            return False, f"value {data} < minimum {schema['minimum']}"
        if "maximum" in schema and data > schema["maximum"]:
            return False, f"value {data} > maximum {schema['maximum']}"

    if isinstance(data, str):
        if "minLength" in schema and len(data) < int(schema["minLength"]):
            return False, "string shorter than minLength"
        if "maxLength" in schema and len(data) > int(schema["maxLength"]):
            return False, "string longer than maxLength"

    return True, "ok"


def _validate_response_format_content(
    content: str, response_format: dict[str, Any] | None
) -> tuple[bool, str]:
    if not response_format:
        return True, "ok"
    rf_type = str(response_format.get("type") or "text").lower()
    if rf_type == "text":
        return True, "ok"
    if rf_type in ("json_object", "json_schema"):
        cleaned = content.strip()
        try:
            parsed = json.loads(cleaned)
        except ValueError as exc:
            return False, f"invalid JSON: {exc}"
        if rf_type == "json_schema":
            js_wrap = response_format.get("json_schema") or {}
            schema = js_wrap.get("schema") if isinstance(js_wrap, dict) else None
            if isinstance(schema, dict):
                return _validate_json_against_schema(parsed, schema)
        return True, "ok"
    return True, "ok"


def _estimate_tokens_from_messages(messages: list[dict[str, Any]]) -> int:
    chars = sum(len(str(m.get("content") or "")) for m in messages)
    return max(1, chars // 4)


def _extract_usage(
    resp_json: dict[str, Any], messages: list[dict[str, Any]], content_text: str
) -> tuple[int, int]:
    usage = resp_json.get("usage")
    if isinstance(usage, dict):
        p = usage.get("prompt_tokens")
        c = usage.get("completion_tokens")
        if isinstance(p, int) and isinstance(c, int) and (p > 0 or c > 0):
            return p, c
    return _estimate_tokens_from_messages(messages), max(1, len(content_text) // 4)


def _extract_choice_info(resp_json: dict[str, Any]) -> tuple[str, str | None, bool]:
    choices = resp_json.get("choices")
    if not isinstance(choices, list) or not choices:
        return "", None, False
    first = choices[0] if isinstance(choices[0], dict) else {}
    msg = first.get("message") if isinstance(first.get("message"), dict) else {}
    content = str(msg.get("content") or "")
    has_tools = bool(msg.get("tool_calls"))
    finish_reason = first.get("finish_reason")
    return content, str(finish_reason) if finish_reason is not None else None, has_tools


def _needs_cascade_escalation(
    req: ChatCompletionRequest,
    request: Request,
    status_code: int,
    resp_json: dict[str, Any],
) -> tuple[bool, str]:
    if status_code >= 500 or "error" in resp_json:
        return True, "backend_error"
    if req.escalate or (req.metadata or {}).get("escalate") or _parse_bool_header(
        request.headers.get("x-escalate")
    ):
        return True, "explicit_escalation"

    content, finish_reason, has_tools = _extract_choice_info(resp_json)
    if not content.strip() and not has_tools:
        return True, "empty_response"

    if req.response_format:
        if finish_reason == "length":
            return True, "truncated_structured_output"
        ok_schema, reason = _validate_response_format_content(content, req.response_format)
        if not ok_schema:
            return True, f"schema_failure:{reason}"

    threshold = req.confidence_threshold if req.confidence_threshold is not None else 0.5
    raw_conf = resp_json.get("confidence")
    if isinstance(raw_conf, (int, float)) and float(raw_conf) < threshold:
        return True, "low_confidence"
    if content.strip().startswith("{"):
        try:
            obj = json.loads(content)
            if isinstance(obj, dict) and isinstance(obj.get("confidence"), (int, float)):
                if float(obj["confidence"]) < threshold:
                    return True, "low_confidence_payload"
        except ValueError:
            pass
    return False, "ok"


def _build_upstream_payload(
    req: ChatCompletionRequest,
    concrete_model: str,
    *,
    backend_name: str = "",
    fast_num_ctx: int | None = None,
) -> dict[str, Any]:
    data = req.model_dump(exclude_none=True)
    data["model"] = concrete_model
    # Strip gateway-only routing fields before forwarding upstream
    data.pop("escalate", None)
    data.pop("confidence_threshold", None)
    data.pop("metadata", None)

    if backend_name == "ollama" and fast_num_ctx and fast_num_ctx > 0:
        existing_opts = data.get("options")
        caller_has_ctx = (
            data.get("num_ctx") is not None
            or (isinstance(existing_opts, dict) and existing_opts.get("num_ctx") is not None)
        )
        if not caller_has_ctx:
            opts = dict(existing_opts) if isinstance(existing_opts, dict) else {}
            opts["num_ctx"] = int(fast_num_ctx)
            data["options"] = opts
    return data


@router.get("/health")
async def v1_health() -> dict[str, Any]:
    """Return Unified Gateway health, active tier, and backend status."""
    coord = get_gateway_coordinator()
    active_tier, telemetry = await asyncio.gather(
        coord.get_active_tier(),
        async_get_full_telemetry(),
    )
    orch = get_orchestrator_telemetry_summary()
    return {
        "ok": True,
        "status": "ok",
        "gateway": "local-llm-unified-v1",
        "retention_mode": "ephemeral",
        "active_tier": active_tier,
        "orchestrator_active": bool(orch.get("active")),
        "aliases": [item["id"] for item in _CANONICAL_ALIASES],
        "backends": telemetry.get("backends", {}),
    }


@router.get("/models")
async def v1_models() -> dict[str, Any]:
    """List canonical virtual aliases and live discovered backend models."""
    coord = get_gateway_coordinator()
    now_ts = int(time.time())
    items: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    for alias_meta in _CANONICAL_ALIASES:
        entry = dict(alias_meta)
        entry["created"] = now_ts
        entry["virtual"] = True
        if entry["id"] == "local-fast":
            entry["target_model"] = SETTINGS.fast_model
            entry["context_window"] = coord.get_fast_num_ctx()
        elif entry["id"] == "local-balanced":
            entry["target_model"] = SETTINGS.balanced_model
        elif entry["id"] == "local-heavy":
            entry["target_model"] = coord.get_heavy_model_id()
        items.append(entry)
        seen_ids.add(entry["id"])

    ol_models, hv_models, lms_models = await asyncio.gather(
        coord.ollama.list_models(),
        coord.colibri.list_models(),
        coord.lmstudio.list_models(),
    )
    for owner, model_list in (("ollama", ol_models), ("colibri", hv_models), ("lmstudio", lms_models)):
        for mid in model_list:
            if mid not in seen_ids:
                seen_ids.add(mid)
                items.append(
                    {
                        "id": mid,
                        "object": "model",
                        "created": now_ts,
                        "owned_by": owner,
                        "virtual": False,
                    }
                )

    return {"object": "list", "data": items}


@router.post("/chat/completions")
async def v1_chat_completions(req: ChatCompletionRequest, request: Request) -> Response:
    """OpenAI-compatible `/v1/chat/completions` with Cascade Router, SSE keep-alive, and Cost Ledger."""
    if req.response_format is not None:
        rf_type = str(req.response_format.get("type") or "text").lower()
        if rf_type not in ("text", "json_object", "json_schema"):
            return _openai_error(
                400,
                f"Unsupported response_format.type: {rf_type!r}",
                code="invalid_response_format",
            )
        if rf_type == "json_schema":
            js_cfg = req.response_format.get("json_schema")
            if not isinstance(js_cfg, dict) or not isinstance(js_cfg.get("schema"), dict):
                return _openai_error(
                    400,
                    "response_format.json_schema.schema must be a JSON Schema object.",
                    code="invalid_json_schema",
                )

    app_id = request.headers.get("x-app-id", "default")
    workflow_id = request.headers.get("x-workflow-id", "default")
    req_budget_cap = _parse_int_header(request.headers.get("x-budget-max-tokens"))
    allow_tier_switch = _parse_bool_header(request.headers.get("x-allow-tier-switch"))
    retention_mode = _resolve_retention_header(request.headers.get("x-retention-mode"))
    escalate_tier_hdr = (request.headers.get("x-escalate-tier") or "").strip().lower()
    firewall_active = is_prompt_firewall_enabled(request.headers.get("x-prompt-firewall"))
    firewall_redactions = 0
    if firewall_active:
        sanitized_messages, fw_counts = redact_messages(req.messages)
        firewall_redactions = sum(fw_counts.values())
        req = req.model_copy(update={"messages": sanitized_messages})

    ledger = get_cost_ledger()
    try:
        ledger.check_budget(app_id, workflow_id, req_budget_cap)
    except BudgetExceededError as exc:
        return _openai_error(
            429,
            exc.detail,
            error_type="rate_limit_exceeded",
            code="budget_exceeded",
            retry_after=60,
        )

    coord = get_gateway_coordinator()
    try:
        resolved = coord.resolve_model(req.model)
    except GatewayEngineError as exc:
        return _openai_error(
            exc.status_code,
            exc.message,
            error_type=exc.error_type,
            code=exc.code,
            retry_after=exc.retry_after,
        )

    if req.stream:
        return await _handle_streaming_chat(
            req=req,
            request=request,
            resolved=resolved,
            allow_tier_switch=allow_tier_switch,
            escalate_tier_hdr=escalate_tier_hdr,
            app_id=app_id,
            workflow_id=workflow_id,
            retention_mode=retention_mode,
            firewall_active=firewall_active,
            firewall_redactions=firewall_redactions,
        )

    return await _handle_sync_chat(
        req=req,
        request=request,
        resolved=resolved,
        allow_tier_switch=allow_tier_switch,
        escalate_tier_hdr=escalate_tier_hdr,
        app_id=app_id,
        workflow_id=workflow_id,
        retention_mode=retention_mode,
        req_budget_cap=req_budget_cap,
        firewall_active=firewall_active,
        firewall_redactions=firewall_redactions,
    )


async def _handle_streaming_chat(
    *,
    req: ChatCompletionRequest,
    request: Request,
    resolved: ResolvedModelSpec,
    allow_tier_switch: bool,
    escalate_tier_hdr: str,
    app_id: str,
    workflow_id: str,
    retention_mode: str,
    firewall_active: bool = False,
    firewall_redactions: int = 0,
) -> Response:
    coord = get_gateway_coordinator()
    ledger = get_cost_ledger()

    target_spec = resolved
    if resolved.alias == "auto":
        if escalate_tier_hdr in ("heavy", "local-heavy"):
            target_spec = coord.resolve_model("local-heavy")
        elif escalate_tier_hdr in ("balanced", "local-balanced"):
            target_spec = coord.resolve_model("local-balanced")
        else:
            target_spec = coord.resolve_model("local-fast")

    active_tier = await coord.get_active_tier()
    needs_switch = active_tier != target_spec.tier

    if needs_switch:
        orch = get_orchestrator_telemetry_summary()
        if orch.get("active"):
            return _openai_error(
                503,
                "Orchestrator run is active; cannot switch tier for streaming request.",
                error_type="server_error",
                code="orchestrator_active",
                retry_after=30,
            )
        if not allow_tier_switch:
            if target_spec.tier == "heavy" or active_tier in ("fast", "heavy"):
                return _openai_error(
                    409,
                    (
                        f"Automatic tier switch from '{active_tier}' to '{target_spec.tier}' is disabled. "
                        "Pass header 'X-Allow-Tier-Switch: true' to authorize cold-swap."
                    ),
                    error_type="conflict_error",
                    code="tier_switch_not_allowed",
                )
            return _openai_error(
                503,
                f"Target tier '{target_spec.tier}' is offline.",
                error_type="service_unavailable",
                code="backend_offline",
                retry_after=5,
            )

    backend = coord.get_backend(target_spec.backend_name)
    upstream_payload = _build_upstream_payload(
        req,
        target_spec.concrete_model,
        backend_name=target_spec.backend_name,
        fast_num_ctx=coord.get_fast_num_ctx(),
    )
    prompt_tokens_est = _estimate_tokens_from_messages(req.messages)

    async def sse_generator() -> AsyncIterator[bytes]:
        t_start = time.perf_counter()
        collected_text: list[str] = []
        collected_thinking: list[str] = []
        observed_prompt_tokens: int | None = None
        observed_completion_tokens: int | None = None
        stream_aiter: AsyncIterator[bytes] | None = None
        first_task: asyncio.Task[bytes] | None = None

        def _elapsed_ms() -> int:
            return max(0, int((time.perf_counter() - t_start) * 1000))

        def _inspect_chunk(raw_chunk: bytes) -> None:
            nonlocal observed_prompt_tokens, observed_completion_tokens
            text_chunk = raw_chunk.decode("utf-8", errors="replace")
            for line in text_chunk.splitlines():
                if not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if not data_str or data_str == "[DONE]":
                    continue
                try:
                    evt = json.loads(data_str)
                    if isinstance(evt, dict):
                        u = evt.get("usage")
                        if isinstance(u, dict):
                            if isinstance(u.get("prompt_tokens"), int):
                                observed_prompt_tokens = u["prompt_tokens"]
                            if isinstance(u.get("completion_tokens"), int):
                                observed_completion_tokens = u["completion_tokens"]
                        for ch in evt.get("choices") or []:
                            if isinstance(ch, dict):
                                delta = ch.get("delta")
                                if isinstance(delta, dict):
                                    if delta.get("content"):
                                        collected_text.append(str(delta["content"]))
                                    if delta.get("reasoning_content"):
                                        collected_thinking.append(str(delta["reasoning_content"]))
                                    if delta.get("thinking"):
                                        collected_thinking.append(str(delta["thinking"]))
                except ValueError:
                    pass

        try:
            yield f": stage=checking-backend\n: elapsed_ms={_elapsed_ms()}\n\n".encode()

            if needs_switch:
                yield f": switching-tier {target_spec.tier}\n: stage=loading-model\n: elapsed_ms={_elapsed_ms()}\n\n".encode()
                switch_task = asyncio.create_task(
                    coord.ensure_tier(target_spec.tier, allow_tier_switch=True)
                )
                while not switch_task.done():
                    try:
                        await asyncio.wait_for(asyncio.shield(switch_task), timeout=1.0)
                    except TimeoutError:
                        yield (
                            f": switching-tier {target_spec.tier}\n"
                            f": stage=loading-model\n"
                            f": elapsed_ms={_elapsed_ms()}\n\n"
                        ).encode()
                await switch_task
            else:
                yield f": stage=loading-model\n: elapsed_ms={_elapsed_ms()}\n\n".encode()

            yield f": stage=prefilling\n: elapsed_ms={_elapsed_ms()}\n\n".encode()

            stream_aiter = backend.chat_completions_stream(upstream_payload).__aiter__()
            first_task = asyncio.create_task(stream_aiter.__anext__())
            wait_ticks = 0
            while not first_task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(first_task), timeout=1.0)
                except TimeoutError:
                    wait_ticks += 1
                    hb_stage = "loading-model" if (not needs_switch and wait_ticks <= 2) else "prefilling"
                    yield f": stage={hb_stage}\n: elapsed_ms={_elapsed_ms()}\n\n".encode()

            try:
                first_chunk = await first_task
            except StopAsyncIteration:
                first_chunk = None
            finally:
                first_task = None

            yield f": stage=streaming\n: elapsed_ms={_elapsed_ms()}\n\n".encode()

            if first_chunk is not None:
                _inspect_chunk(first_chunk)
                yield first_chunk

            async for raw_chunk in stream_aiter:
                _inspect_chunk(raw_chunk)
                yield raw_chunk
        except GatewayEngineError as exc:
            err_payload = {"error": {"message": exc.message, "type": exc.error_type, "code": exc.code}}
            yield f"data: {json.dumps(err_payload, ensure_ascii=False)}\n\ndata: [DONE]\n\n".encode()
        except Exception as exc:
            err_payload = {"error": {"message": str(exc), "type": "backend_error", "code": "stream_error"}}
            yield f"data: {json.dumps(err_payload, ensure_ascii=False)}\n\ndata: [DONE]\n\n".encode()
        finally:
            if first_task is not None and not first_task.done():
                first_task.cancel()
            if stream_aiter is not None:
                aclose_fn = getattr(stream_aiter, "aclose", None)
                if callable(aclose_fn):
                    try:
                        await aclose_fn()
                    except Exception:
                        pass
            full_generated = "".join(collected_text) + "".join(collected_thinking)
            p_tok = observed_prompt_tokens if observed_prompt_tokens is not None else prompt_tokens_est
            c_tok = (
                observed_completion_tokens
                if observed_completion_tokens is not None
                else (max(1, len(full_generated) // 4) if full_generated else 0)
            )
            ledger.record_usage(
                app_id,
                workflow_id,
                model_alias=target_spec.alias,
                prompt_tokens=p_tok,
                completion_tokens=c_tok,
                retries=0,
                escalated=False,
            )

    stream_headers = {
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "X-Accel-Buffering": "no",
        "X-Retention-Mode": retention_mode,
        "X-Resolved-Model": target_spec.alias,
    }
    if firewall_active:
        stream_headers["X-Prompt-Firewall"] = "active"
        stream_headers["X-Prompt-Firewall-Redactions"] = str(firewall_redactions)

    return StreamingResponse(
        sse_generator(),
        media_type="text/event-stream; charset=utf-8",
        headers=stream_headers,
    )


async def _handle_sync_chat(
    *,
    req: ChatCompletionRequest,
    request: Request,
    resolved: ResolvedModelSpec,
    allow_tier_switch: bool,
    escalate_tier_hdr: str,
    app_id: str,
    workflow_id: str,
    retention_mode: str,
    req_budget_cap: int | None,
    firewall_active: bool = False,
    firewall_redactions: int = 0,
) -> Response:
    coord = get_gateway_coordinator()
    ledger = get_cost_ledger()

    if resolved.alias != "auto":
        try:
            switched = await coord.ensure_tier(resolved.tier, allow_tier_switch=allow_tier_switch)
        except GatewayEngineError as exc:
            return _openai_error(
                exc.status_code,
                exc.message,
                error_type=exc.error_type,
                code=exc.code,
                retry_after=exc.retry_after,
            )

        backend = coord.get_backend(resolved.backend_name)
        upstream_payload = _build_upstream_payload(
            req,
            resolved.concrete_model,
            backend_name=resolved.backend_name,
            fast_num_ctx=coord.get_fast_num_ctx(),
        )
        try:
            status_code, resp_json = await backend.chat_completions(upstream_payload)
        except Exception as exc:
            return _openai_error(
                503,
                f"Backend '{resolved.backend_name}' unreachable: {exc}",
                error_type="service_unavailable",
                code="backend_unreachable",
            )

        content, _, _ = _extract_choice_info(resp_json)
        p_tok, c_tok = _extract_usage(resp_json, req.messages, content)
        ledger.record_usage(
            app_id,
            workflow_id,
            model_alias=resolved.alias,
            prompt_tokens=p_tok,
            completion_tokens=c_tok,
        )

        schema_ok, _ = _validate_response_format_content(content, req.response_format)
        resp_headers = {
            "X-Retention-Mode": retention_mode,
            "X-Resolved-Model": resolved.alias,
            "X-Active-Tier": resolved.tier,
            "X-Tier-Switched": "true" if switched else "false",
            "X-Schema-Valid": "true" if schema_ok else "false",
        }
        if firewall_active:
            resp_headers["X-Prompt-Firewall"] = "active"
            resp_headers["X-Prompt-Firewall-Redactions"] = str(firewall_redactions)
        return JSONResponse(
            status_code=status_code,
            content=resp_json,
            headers=resp_headers,
        )

    # Cascade Router (`model == "auto"`)
    if escalate_tier_hdr in ("heavy", "local-heavy"):
        chain_aliases = ["local-heavy"]
    elif escalate_tier_hdr in ("balanced", "local-balanced"):
        chain_aliases = ["local-balanced", "local-heavy"]
    else:
        chain_aliases = ["local-fast", "local-balanced", "local-heavy"]

    last_status = 503
    last_json: dict[str, Any] = {"error": {"message": "No cascade step executed", "type": "server_error"}}
    first_alias = chain_aliases[0]
    chosen_alias = first_alias
    last_reason = "ok"

    for idx, candidate_alias in enumerate(chain_aliases):
        try:
            ledger.check_budget(app_id, workflow_id, req_budget_cap)
        except BudgetExceededError as exc:
            return _openai_error(
                429,
                exc.detail,
                error_type="rate_limit_exceeded",
                code="budget_exceeded",
                retry_after=60,
            )

        spec = coord.resolve_model(candidate_alias)
        try:
            await coord.ensure_tier(spec.tier, allow_tier_switch=allow_tier_switch)
        except GatewayEngineError as exc:
            if idx == 0 or candidate_alias == "local-heavy" and escalate_tier_hdr in ("heavy", "local-heavy"):
                return _openai_error(
                    exc.status_code,
                    exc.message,
                    error_type=exc.error_type,
                    code=exc.code,
                    retry_after=exc.retry_after,
                )
            # If escalation to heavy is blocked by X-Allow-Tier-Switch policy, return best prior result
            # with explicit diagnostic header rather than losing completed work, unless prior failed fatally.
            if last_status < 500:
                return JSONResponse(
                    status_code=last_status,
                    content=last_json,
                    headers={
                        "X-Retention-Mode": retention_mode,
                        "X-Resolved-Model": chosen_alias,
                        "X-Cascade-Attempts": str(idx),
                        "X-Cascade-Escalated-From": first_alias,
                        "X-Cascade-Reason": last_reason,
                        "X-Cascade-Heavy-Blocked": exc.code,
                    },
                )
            return _openai_error(
                exc.status_code,
                exc.message,
                error_type=exc.error_type,
                code=exc.code,
                retry_after=exc.retry_after,
            )

        backend = coord.get_backend(spec.backend_name)
        upstream_payload = _build_upstream_payload(
            req,
            spec.concrete_model,
            backend_name=spec.backend_name,
            fast_num_ctx=coord.get_fast_num_ctx(),
        )
        try:
            status_code, resp_json = await backend.chat_completions(upstream_payload)
        except Exception as exc:
            status_code = 503
            resp_json = {
                "error": {
                    "message": f"Backend '{spec.backend_name}' unreachable: {exc}",
                    "type": "service_unavailable",
                }
            }

        content, _, _ = _extract_choice_info(resp_json)
        p_tok, c_tok = _extract_usage(resp_json, req.messages, content)
        ledger.record_usage(
            app_id,
            workflow_id,
            model_alias=spec.alias,
            prompt_tokens=p_tok,
            completion_tokens=c_tok,
            retries=1 if idx > 0 else 0,
            escalated=idx > 0,
            count_request=(idx == 0),
        )

        last_status = status_code
        last_json = resp_json
        chosen_alias = spec.alias

        escalate_needed, reason = _needs_cascade_escalation(req, request, status_code, resp_json)
        last_reason = reason
        if not escalate_needed:
            hdrs = {
                "X-Retention-Mode": retention_mode,
                "X-Resolved-Model": chosen_alias,
                "X-Cascade-Attempts": str(idx + 1),
                "X-Cascade-Reason": reason,
            }
            if idx > 0:
                hdrs["X-Cascade-Escalated-From"] = first_alias
            if firewall_active:
                hdrs["X-Prompt-Firewall"] = "active"
                hdrs["X-Prompt-Firewall-Redactions"] = str(firewall_redactions)
            return JSONResponse(status_code=status_code, content=resp_json, headers=hdrs)

    hdrs = {
        "X-Retention-Mode": retention_mode,
        "X-Resolved-Model": chosen_alias,
        "X-Cascade-Attempts": str(len(chain_aliases)),
        "X-Cascade-Reason": last_reason,
    }
    if len(chain_aliases) > 1:
        hdrs["X-Cascade-Escalated-From"] = first_alias
    if firewall_active:
        hdrs["X-Prompt-Firewall"] = "active"
        hdrs["X-Prompt-Firewall-Redactions"] = str(firewall_redactions)
    return JSONResponse(status_code=last_status, content=last_json, headers=hdrs)


@router.post("/embeddings")
async def v1_embeddings(req: EmbeddingsRequest, request: Request) -> Response:
    """OpenAI-compatible `/v1/embeddings` endpoint with Cost/Token Ledger enforcement."""
    app_id = request.headers.get("x-app-id", "default")
    workflow_id = request.headers.get("x-workflow-id", "default")
    req_budget_cap = _parse_int_header(request.headers.get("x-budget-max-tokens"))
    allow_tier_switch = _parse_bool_header(request.headers.get("x-allow-tier-switch"))
    retention_mode = _resolve_retention_header(request.headers.get("x-retention-mode"))

    ledger = get_cost_ledger()
    try:
        ledger.check_budget(app_id, workflow_id, req_budget_cap)
    except BudgetExceededError as exc:
        return _openai_error(
            429,
            exc.detail,
            error_type="rate_limit_exceeded",
            code="budget_exceeded",
            retry_after=60,
        )

    coord = get_gateway_coordinator()
    raw_model = (req.model or "").strip()
    if not raw_model or raw_model.lower() in ("auto", "local-fast", "local-balanced"):
        target_model = SETTINGS.embedding_model
        target_tier = "fast"
    elif raw_model.lower() == "local-heavy":
        target_model = coord.get_heavy_model_id()
        target_tier = "heavy"
    else:
        target_model = raw_model
        target_tier = "fast"

    try:
        await coord.ensure_tier(target_tier, allow_tier_switch=allow_tier_switch)
    except GatewayEngineError as exc:
        return _openai_error(
            exc.status_code,
            exc.message,
            error_type=exc.error_type,
            code=exc.code,
            retry_after=exc.retry_after,
        )

    backend = coord.colibri if target_tier == "heavy" else coord.ollama
    payload = req.model_dump(exclude_none=True)
    payload["model"] = target_model

    try:
        status_code, resp_json = await backend.embeddings(payload)
    except Exception as exc:
        return _openai_error(
            503,
            f"Embeddings backend unreachable: {exc}",
            error_type="service_unavailable",
            code="backend_unreachable",
        )

    usage = resp_json.get("usage") if isinstance(resp_json.get("usage"), dict) else {}
    raw_in = req.input
    in_chars = (
        len(raw_in)
        if isinstance(raw_in, str)
        else sum(len(str(item)) for item in raw_in)
    )
    prompt_tokens = int(usage.get("prompt_tokens") or max(1, in_chars // 4))
    ledger.record_usage(
        app_id,
        workflow_id,
        model_alias=raw_model or "embeddings",
        prompt_tokens=prompt_tokens,
        completion_tokens=0,
    )

    return JSONResponse(
        status_code=status_code,
        content=resp_json,
        headers={
            "X-Retention-Mode": retention_mode,
            "X-Resolved-Model": target_model,
        },
    )
