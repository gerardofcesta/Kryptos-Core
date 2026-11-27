"""
Delimited Tool Protocol Parser, Structured JSON Extractor, and 3-Retry Self-Correction Loop.

Designed for high reliability with quantized/abliterated local models (e.g. Qwen3.8 UD-IQ4_XS):
- Strips `<think>...</think>` reasoning blocks before parsing.
- Parses `<tool_call name="...">{...}</tool_call>` blocks (single or multiple).
- Supports raw fenced ```python``` fallback inside `<tool_call name="run_python_sandbox">`.
- Extracts structured JSON objects from raw responses or fenced ```json``` blocks.
- Self-correction retries up to `max_retries` (default 3) appending ONLY a concise syntax
  error message without polluting the caller's message list or global state.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Iterable

_THINK_BLOCK_RE = re.compile(r"<think\b[^>]*>.*?</think>", re.IGNORECASE | re.DOTALL)
_ORPHAN_THINK_CLOSE_RE = re.compile(r"^.*?</think>", re.IGNORECASE | re.DOTALL)
_TOOL_CALL_RE = re.compile(
    r"<tool_call\s+name\s*=\s*(?:[\"']([^\"']+)[\"']|([a-zA-Z0-9_.-]+))\s*>(.*?)</tool_call>",
    re.IGNORECASE | re.DOTALL,
)
_TOOL_CALL_OPEN_RE = re.compile(r"<tool_call\b", re.IGNORECASE)
_FENCED_JSON_RE = re.compile(r"```(?:json)?\s*\n(.*?)```", re.IGNORECASE | re.DOTALL)
_FENCED_PYTHON_RE = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.IGNORECASE | re.DOTALL)


class ProtocolParseError(ValueError):
    """Raised when LLM output violates the delimited tool or JSON schema protocol."""


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any]
    raw_body: str = ""


def strip_think_blocks(text: str) -> str:
    """Remove `<think>...</think>` reasoning blocks (and orphan leading `</think>` if present)."""
    if not text:
        return ""
    cleaned = _THINK_BLOCK_RE.sub("", text)
    if "</think>" in cleaned.lower() and "<think" not in cleaned.lower():
        cleaned = _ORPHAN_THINK_CLOSE_RE.sub("", cleaned, count=1)
    # Handle truncated unclosed <think> block at end only if no structured tags follow
    open_idx = cleaned.lower().find("<think")
    if open_idx != -1 and "</think>" not in cleaned.lower()[open_idx:]:
        prefix = cleaned[:open_idx].strip()
        if prefix:
            cleaned = prefix
    return cleaned.strip()


def _parse_tool_body(tool_name: str, body: str) -> dict[str, Any]:
    raw = body.strip()
    if not raw:
        return {}

    # 1. Direct JSON object
    if raw.startswith("{"):
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ProtocolParseError(
                f"Invalid JSON inside <tool_call name=\"{tool_name}\">: {exc.msg} "
                f"(line {exc.lineno}, col {exc.colno}). Return valid JSON object arguments."
            ) from exc
        if not isinstance(obj, dict):
            raise ProtocolParseError(
                f"<tool_call name=\"{tool_name}\"> arguments must be a JSON object, got {type(obj).__name__}."
            )
        return obj

    # 2. Special fenced Python fallback for run_python_sandbox
    if tool_name == "run_python_sandbox":
        py_match = _FENCED_PYTHON_RE.search(raw)
        if py_match:
            code = py_match.group(1)
            # If the fenced block is actually valid JSON with a "code" key, unwrap JSON instead
            stripped_inner = code.strip()
            if stripped_inner.startswith("{"):
                try:
                    maybe_json = json.loads(stripped_inner)
                    if isinstance(maybe_json, dict) and "code" in maybe_json:
                        return maybe_json
                except ValueError:
                    pass
            return {"code": code}

    # 3. Fenced JSON block inside <tool_call>
    json_fence = _FENCED_JSON_RE.search(raw)
    if json_fence:
        inner = json_fence.group(1).strip()
        try:
            obj = json.loads(inner)
        except json.JSONDecodeError as exc:
            raise ProtocolParseError(
                f"Invalid fenced JSON inside <tool_call name=\"{tool_name}\">: {exc.msg}."
            ) from exc
        if not isinstance(obj, dict):
            raise ProtocolParseError(
                f"<tool_call name=\"{tool_name}\"> fenced JSON must be an object."
            )
        return obj

    # 4. Fallback JSON parse attempt (reject non-object / malformed payloads cleanly)
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProtocolParseError(
            f"Malformed arguments inside <tool_call name=\"{tool_name}\">: expected a JSON object "
            f"(or ```python``` block for run_python_sandbox). Parse error: {exc.msg}."
        ) from exc
    if not isinstance(obj, dict):
        raise ProtocolParseError(
            f"<tool_call name=\"{tool_name}\"> arguments must be a JSON object, got {type(obj).__name__}."
        )
    return obj


def parse_tool_calls(
    text: str,
    allowed_tools: Iterable[str] | None = None,
    require_at_least_one: bool = False,
) -> list[ToolCall]:
    """
    Parse one or more `<tool_call name="...">...</tool_call>` blocks after stripping `<think>`.
    Raises `ProtocolParseError` on malformed/unclosed tags, unknown tool names, or bad JSON.
    """
    cleaned = strip_think_blocks(text)
    allowed_set = {str(t).strip() for t in allowed_tools} if allowed_tools is not None else None

    matches = list(_TOOL_CALL_RE.finditer(cleaned))
    open_tags = _TOOL_CALL_OPEN_RE.findall(cleaned)
    if len(open_tags) > len(matches):
        raise ProtocolParseError(
            "Malformed or unclosed <tool_call> block detected. "
            "Use exact syntax: <tool_call name=\"tool_name\">{\"arg\": \"value\"}</tool_call>."
        )

    if not matches:
        if require_at_least_one:
            raise ProtocolParseError(
                "No <tool_call name=\"...\">...</tool_call> block found in response."
            )
        return []

    calls: list[ToolCall] = []
    for match in matches:
        name = (match.group(1) or match.group(2) or "").strip()
        body = match.group(3)
        if not name:
            raise ProtocolParseError("Empty tool name in <tool_call>.")
        if allowed_set is not None and name not in allowed_set:
            raise ProtocolParseError(
                f"Unknown tool '{name}'. Allowed tools: {sorted(allowed_set)}."
            )
        args = _parse_tool_body(name, body)
        calls.append(ToolCall(name=name, arguments=args, raw_body=body.strip()))

    return calls


def parse_json_object(text: str) -> dict[str, Any]:
    """
    Extract a structured JSON object (`dict`) from raw LLM output or a fenced ```json``` block,
    stripping `<think>...</think>` first. Raises `ProtocolParseError` if invalid.
    """
    cleaned = strip_think_blocks(text)
    if not cleaned:
        raise ProtocolParseError("Empty response; expected a valid JSON object.")

    # 1. Direct JSON object parse
    if cleaned.startswith("{"):
        try:
            obj = json.loads(cleaned)
            if isinstance(obj, dict):
                return obj
            raise ProtocolParseError(f"Expected JSON object, got {type(obj).__name__}.")
        except json.JSONDecodeError as exc:
            # Fall through to check if trailing prose follows a valid JSON object
            decoder = json.JSONDecoder()
            try:
                obj, _ = decoder.raw_decode(cleaned)
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                pass
            raise ProtocolParseError(
                f"Malformed JSON object: {exc.msg} (line {exc.lineno}, col {exc.colno})."
            ) from exc

    # 2. Fenced ```json ... ``` block
    for m in _FENCED_JSON_RE.finditer(cleaned):
        candidate = m.group(1).strip()
        if not candidate:
            continue
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError as exc:
            raise ProtocolParseError(
                f"Malformed JSON inside fenced block: {exc.msg} (line {exc.lineno}, col {exc.colno})."
            ) from exc

    # 3. Scan for first valid embedded JSON object `{...}`
    decoder = json.JSONDecoder()
    idx = 0
    last_err: json.JSONDecodeError | None = None
    while True:
        brace_pos = cleaned.find("{", idx)
        if brace_pos == -1:
            break
        try:
            obj, _ = decoder.raw_decode(cleaned, brace_pos)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError as exc:
            last_err = exc
        idx = brace_pos + 1

    if last_err is not None:
        raise ProtocolParseError(
            f"Could not parse valid JSON object from response: {last_err.msg}."
        ) from last_err
    raise ProtocolParseError("No JSON object found in response. Respond with a valid JSON object.")


def _extract_response_text(raw_response: Any) -> str:
    if isinstance(raw_response, str):
        return raw_response
    if isinstance(raw_response, dict):
        if "content" in raw_response and isinstance(raw_response["content"], str):
            return raw_response["content"]
        msg = raw_response.get("message")
        if isinstance(msg, dict) and isinstance(msg.get("content"), str):
            return msg["content"]
        choices = raw_response.get("choices")
        if isinstance(choices, list) and choices:
            first = choices[0]
            if isinstance(first, dict):
                c_msg = first.get("message")
                if isinstance(c_msg, dict) and isinstance(c_msg.get("content"), str):
                    return c_msg["content"]
    return str(raw_response or "")


def call_with_self_correction(
    llm_fn: Callable[[list[dict[str, str]]], Any],
    messages: list[dict[str, str]],
    parser_fn: Callable[[str], Any],
    max_retries: int = 3,
) -> tuple[Any, str, int]:
    """
    Invoke `llm_fn(attempt_messages)` and parse with `parser_fn(raw_text)`.
    If `parser_fn` raises `ProtocolParseError`, retry up to `max_retries` total attempts,
    appending ONLY a concise error-correction feedback message on a local copy of `messages`
    so neither caller messages nor global state are polluted.

    Returns: `(parsed_value, raw_text, attempts_used)`.
    """
    total_attempts = max(1, int(max_retries))
    base_messages = [dict(m) for m in messages]
    attempt_messages = list(base_messages)
    last_error: ProtocolParseError | None = None

    for attempt in range(1, total_attempts + 1):
        raw_resp = llm_fn(attempt_messages)
        raw_text = _extract_response_text(raw_resp)
        try:
            parsed = parser_fn(raw_text)
            return parsed, raw_text, attempt
        except ProtocolParseError as exc:
            last_error = exc
            if attempt >= total_attempts:
                break
            # Build bounded retry prompt without polluting base_messages or stacking old failures
            snippet = strip_think_blocks(raw_text)[:300]
            correction = (
                f"FORMAT ERROR (attempt {attempt}/{total_attempts}): {exc}\n"
                "Return ONLY a syntactically valid response following the required format. "
                "Do not add extra commentary."
            )
            attempt_messages = [
                *base_messages,
                {"role": "assistant", "content": snippet},
                {"role": "user", "content": correction},
            ]

    raise ProtocolParseError(
        f"Self-correction exhausted after {total_attempts} attempt(s): {last_error}"
    ) from last_error
