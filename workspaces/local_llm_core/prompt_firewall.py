"""Deterministic local PII & Secret Prompt Firewall (opt-in on `/v1/*` and `/mcp`)."""

from __future__ import annotations

import os
import re
from typing import Any

_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.MULTILINE,
)

_API_TOKEN_RE = re.compile(
    r"\b(?:"
    r"sk-(?:proj-|svcacct-|test_|live_)?[A-Za-z0-9_-]{16,}"
    r"|gh[pousr]_[A-Za-z0-9]{20,}"
    r"|github_pat_[A-Za-z0-9_]{20,}"
    r"|AKIA[0-9A-Z]{16}"
    r"|xox[baprs]-[A-Za-z0-9-]{16,}"
    r")\b"
)

_IBAN_RE = re.compile(
    r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]){11,30}\b"
)

_CODICE_FISCALE_RE = re.compile(
    r"\b[A-Z]{6}\d{2}[ABCDEHLMPRST]\d{2}[A-Z]\d{3}[A-Z]\b",
    re.IGNORECASE,
)

_EMAIL_RE = re.compile(
    r"\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b"
)


def is_prompt_firewall_enabled(header_val: str | None = None) -> bool:
    """
    Determine whether deterministic PII/Secret prompt redaction is active.

    Header `X-Prompt-Firewall` explicitly enables/disables per-request when supplied;
    otherwise falls back to env `LOCAL_LLM_PROMPT_FIREWALL` (default disabled).
    """
    if header_val is not None and header_val.strip() != "":
        low = header_val.strip().lower()
        return low in ("1", "true", "yes", "on", "active")
    env_val = os.environ.get("LOCAL_LLM_PROMPT_FIREWALL", "").strip().lower()
    return env_val in ("1", "true", "yes", "on", "active")


def _replace_iban(match: re.Match[str]) -> str:
    compact = match.group(0).replace(" ", "")
    if 15 <= len(compact) <= 34 and any(ch.isdigit() for ch in compact[4:]):
        return "[REDACTED:IBAN]"
    return match.group(0)


def redact_text(text: str) -> tuple[str, dict[str, int]]:
    """Deterministically mask private keys, API tokens, IBANs, Codice Fiscale, and emails."""
    counts: dict[str, int] = {
        "private_key": 0,
        "api_token": 0,
        "iban": 0,
        "codice_fiscale": 0,
        "email": 0,
    }
    if not text:
        return text, counts

    out = text

    def _sub_count(pattern: re.Pattern[str], repl: str, key: str, src: str) -> str:
        new_src, n = pattern.subn(repl, src)
        counts[key] += n
        return new_src

    out = _sub_count(_PRIVATE_KEY_RE, "[REDACTED:PRIVATE_KEY]", "private_key", out)
    out = _sub_count(_API_TOKEN_RE, "[REDACTED:API_TOKEN]", "api_token", out)

    def _iban_counter(m: re.Match[str]) -> str:
        res = _replace_iban(m)
        if res == "[REDACTED:IBAN]":
            counts["iban"] += 1
        return res

    out = _IBAN_RE.sub(_iban_counter, out)
    out = _sub_count(_CODICE_FISCALE_RE, "[REDACTED:CODICE_FISCALE]", "codice_fiscale", out)
    out = _sub_count(_EMAIL_RE, "[REDACTED:EMAIL]", "email", out)
    return out, counts


def _merge_counts(acc: dict[str, int], delta: dict[str, int]) -> None:
    for k, v in delta.items():
        acc[k] = acc.get(k, 0) + v


def redact_structure(obj: Any) -> tuple[Any, dict[str, int]]:
    """Recursively redact strings inside dicts/lists without mutating input in place."""
    total: dict[str, int] = {
        "private_key": 0,
        "api_token": 0,
        "iban": 0,
        "codice_fiscale": 0,
        "email": 0,
    }
    if isinstance(obj, str):
        return redact_text(obj)
    if isinstance(obj, list):
        new_list: list[Any] = []
        for item in obj:
            red_item, c = redact_structure(item)
            _merge_counts(total, c)
            new_list.append(red_item)
        return new_list, total
    if isinstance(obj, dict):
        new_dict: dict[Any, Any] = {}
        for k, v in obj.items():
            red_v, c = redact_structure(v)
            _merge_counts(total, c)
            new_dict[k] = red_v
        return new_dict, total
    return obj, total


def redact_messages(messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Redact chat completion messages (string or multimodal list contents)."""
    red_msgs, counts = redact_structure(messages)
    return list(red_msgs), counts
