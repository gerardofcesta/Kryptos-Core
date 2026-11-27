"""Protected stateless MCP endpoint (`POST /mcp`, JSON-RPC 2.0) with OWASP MCP03/SSRF & PII guards."""

from __future__ import annotations

import asyncio
import collections
import datetime
import ipaddress
import json
import pathlib
import re
import threading
import time
import urllib.parse
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..engine.backends import build_loopback_async_client
from ..engine.coordinator import GatewayEngineError, get_gateway_coordinator
from ..prompt_firewall import is_prompt_firewall_enabled, redact_structure
from ..rag.store import get_rag_manager
from ..services.model_manager import get_airgapped_inventory

router = APIRouter(tags=["mcp"])

MCP_PROTOCOL_VERSION = "2025-03-26"
_URL_SCHEME_RE = re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]{1,15})://([^\s/\"'`<>?#]+)")
_IPV4_RE = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b")
_OBFUSCATED_IP_HOST_RE = re.compile(
    r"^(?:0x[0-9a-f]+|\d{7,12}|(?:0[0-7]+\.){1,3}0?[0-7]+)$",
    re.IGNORECASE,
)

_SSRF_URL_KEYS = frozenset(
    {
        "url",
        "uri",
        "endpoint",
        "webhook",
        "callback",
        "callback_url",
        "target_url",
        "fetch_url",
        "remote_url",
        "base_url",
        "href",
        "link",
    }
)

_BLOCKED_METADATA_HOSTS = frozenset(
    {
        "169.254.169.254",
        "100.100.100.200",
        "metadata.google.internal",
        "metadata.goog",
        "instance-data",
    }
)

_SENSITIVE_EXACT_NAMES = frozenset(
    {
        ".env",
        ".ssh",
        ".aws",
        ".gnupg",
        ".kube",
        ".azure",
        ".docker",
        "id_rsa",
        "id_ed25519",
        "id_dsa",
        "id_ecdsa",
        "authorized_keys",
        "known_hosts",
        "gateway.token",
        "passwd",
        "shadow",
    }
)

_SENSITIVE_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".jks", ".token")
_PATH_ARG_KEYS = frozenset(
    {
        "target_path",
        "path",
        "file",
        "filepath",
        "file_path",
        "filename",
        "rel_path",
        "dir",
        "directory",
        "folder",
        "root",
    }
)


class McpAuditLogger:
    """Thread-safe RAM-only ring buffer storing structured JSONL MCP audit events (Ghost zero-disk)."""

    def __init__(self, maxlen: int = 500) -> None:
        self._lock = threading.Lock()
        self._ring: collections.deque[str] = collections.deque(maxlen=maxlen)

    def record(
        self,
        *,
        method: str,
        decision: str,
        http_status: int,
        reason: str = "ok",
        tool: str | None = None,
        jsonrpc_id: Any = None,
        client_app: str | None = None,
        latency_ms: float = 0.0,
    ) -> str:
        entry = {
            "ts": datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat(),
            "method": method,
            "tool": tool,
            "jsonrpc_id": jsonrpc_id,
            "decision": decision,
            "http_status": http_status,
            "reason": reason,
            "client_app": client_app or "unknown",
            "latency_ms": round(latency_ms, 2),
        }
        line = json.dumps(entry, ensure_ascii=False)
        with self._lock:
            self._ring.append(line)
        return line

    def list_entries(self) -> list[dict[str, Any]]:
        with self._lock:
            return [json.loads(line) for line in self._ring]

    def get_jsonl(self) -> str:
        with self._lock:
            return "\n".join(self._ring)

    def clear(self) -> None:
        with self._lock:
            self._ring.clear()


_AUDIT_LOGGER = McpAuditLogger()


def get_mcp_audit_logger() -> McpAuditLogger:
    """Return the process-wide Ghost RAM-only MCP JSONL audit logger."""
    return _AUDIT_LOGGER


def _jsonrpc_ok(req_id: Any, result: dict[str, Any], status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"jsonrpc": "2.0", "id": req_id, "result": result},
    )


def _jsonrpc_error(
    req_id: Any,
    code: int,
    message: str,
    status_code: int = 400,
    data: dict[str, Any] | None = None,
) -> JSONResponse:
    err: dict[str, Any] = {"code": code, "message": message}
    if data:
        err["data"] = data
    return JSONResponse(
        status_code=status_code,
        content={"jsonrpc": "2.0", "id": req_id, "error": err},
    )


def _is_forbidden_ip(host_str: str) -> bool:
    clean = host_str.strip().strip("[]").split("%", 1)[0].rstrip(".")
    low = clean.lower()
    if low in _BLOCKED_METADATA_HOSTS:
        return True
    if _OBFUSCATED_IP_HOST_RE.fullmatch(low):
        return True
    try:
        ip = ipaddress.ip_address(clean)
    except ValueError:
        return False
    # Block all non-loopback IPs AND IPv6-mapped IPv4 addresses even if mapped to loopback
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return True
    return not ip.is_loopback


def _check_ssrf_value(key_hint: str, val: str) -> str | None:
    """Return denial reason if `val` violates OWASP MCP03 / Anti-SSRF rules, else None."""
    low = val.lower()
    for meta_host in _BLOCKED_METADATA_HOSTS:
        if meta_host in low:
            return f"OWASP MCP03 SSRF blocked: cloud metadata host '{meta_host}' is forbidden."

    if "::ffff:" in low or "[::1]" in low:
        return "OWASP MCP03 SSRF blocked: raw IPv6/IPv4-mapped target is forbidden."

    for m in _IPV4_RE.finditer(val):
        ip_cand = m.group(1)
        try:
            ip_obj = ipaddress.ip_address(ip_cand)
            if ip_obj.is_link_local or ip_obj.is_unspecified or (
                key_hint.lower() in _SSRF_URL_KEYS and not ip_obj.is_loopback
            ):
                return f"OWASP MCP03 SSRF blocked: forbidden IP target '{ip_cand}'."
        except ValueError:
            pass

    for match in _URL_SCHEME_RE.finditer(val):
        scheme = match.group(1).lower()
        authority = match.group(2)
        parsed = urllib.parse.urlsplit(f"{scheme}://{authority}")
        host = (parsed.hostname or authority.split(":", 1)[0]).strip("[]").lower()
        if scheme in ("file", "gopher", "ftp", "dict", "ldap", "ldaps", "jar", "data"):
            return f"OWASP MCP03 SSRF blocked: URL scheme '{scheme}://' is forbidden."
        if scheme in ("http", "https", "ws", "wss"):
            if _OBFUSCATED_IP_HOST_RE.fullmatch(host) or _is_forbidden_ip(host):
                return f"OWASP MCP03 SSRF blocked: forbidden or obfuscated IP host '{host}'."
            if host not in ("127.0.0.1", "localhost"):
                return f"OWASP MCP03 SSRF blocked: external/non-loopback URL host '{host}' is forbidden."
            if key_hint.lower() in _SSRF_URL_KEYS or parsed.port not in (None, 8787):
                return "OWASP MCP03 SSRF blocked: arbitrary URL or port targets are disabled in MCP tools."

    if key_hint.lower() in _SSRF_URL_KEYS and val.strip():
        return f"OWASP MCP03 SSRF blocked: URL parameter '{key_hint}' is not permitted."
    return None


def _decode_unquoted_passes(raw: str, max_passes: int = 3) -> str:
    cur = raw
    for _ in range(max_passes):
        nxt = urllib.parse.unquote(cur)
        if nxt == cur:
            break
        cur = nxt
    return cur


def _check_sensitive_path_value(key_hint: str, val: str) -> str | None:
    """Return denial reason if `val` references sensitive files, keys, PII configs, or path traversal."""
    for candidate in (val.strip(), _decode_unquoted_passes(val).strip()):
        if not candidate:
            continue

        if "\x00" in candidate:
            return "Access denied: null byte in path argument."

        if candidate.startswith("\\\\") or candidate.startswith("//"):
            return "Access denied: UNC/network paths are forbidden."

        # Check path segments across / and \
        tokens = [seg for seg in re.split(r"[\s\"'`<>|;,]+", candidate) if seg]
        for tok in tokens:
            parts = [p for p in re.split(r"[\\/]+", tok) if p]
            if ".." in parts:
                return "Access denied: path traversal ('..') is forbidden."
            for part in parts:
                low_part = part.lower()
                if low_part in _SENSITIVE_EXACT_NAMES or low_part.startswith(".env."):
                    return f"Access denied: sensitive file or directory '{part}' is blocked."
                if any(low_part.endswith(suf) for suf in _SENSITIVE_SUFFIXES):
                    return f"Access denied: sensitive key/token artifact '{part}' is blocked."

        if key_hint.lower() in _PATH_ARG_KEYS:
            p_obj = pathlib.Path(candidate)
            if p_obj.name.lower() in _SENSITIVE_EXACT_NAMES or p_obj.name.lower().startswith(".env."):
                return f"Access denied: sensitive target '{p_obj.name}' is blocked."
    return None


def _inspect_tool_arguments(args: Any, key_hint: str = "") -> str | None:
    """Recursively enforce Anti-SSRF & Sensitive File / PII blocklist on MCP `tools/call` args."""
    if isinstance(args, dict):
        for k, v in args.items():
            k_str = str(k)
            if k_str.lower() in _SSRF_URL_KEYS:
                reason = _check_ssrf_value(k_str, str(v or ""))
                if reason:
                    return reason
            err = _inspect_tool_arguments(v, key_hint=k_str)
            if err:
                return err
    elif isinstance(args, list):
        for item in args:
            err = _inspect_tool_arguments(item, key_hint=key_hint)
            if err:
                return err
    elif isinstance(args, str):
        ssrf_err = _check_ssrf_value(key_hint, args)
        if ssrf_err:
            return ssrf_err
        path_err = _check_sensitive_path_value(key_hint, args)
        if path_err:
            return path_err
    return None


_MCP_TOOLS: tuple[dict[str, Any], ...] = (
    {
        "name": "rag_query",
        "description": "Query local Ghost (RAM-only) or Vault RAG index with page/file citations.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1},
                "mode": {"type": "string", "enum": ["ghost", "vault"], "default": "ghost"},
                "collection": {"type": "string", "default": "default"},
                "top_k": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "code_audit",
        "description": "Run deterministic Orchestrator Anti-Slopsquatting & Secret Scan on an allowed directory.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "target_path": {"type": "string", "minLength": 1},
                "check_secrets": {"type": "boolean", "default": True},
                "check_slopsquatting": {"type": "boolean", "default": True},
            },
            "required": ["target_path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "local_chat",
        "description": "Run local loopback chat completion via Unified Gateway coordinator (ephemeral).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "minLength": 1},
                "model": {"type": "string", "default": "local-fast"},
                "system_prompt": {"type": "string"},
            },
            "required": ["prompt"],
            "additionalProperties": False,
        },
    },
    {
        "name": "model_inventory",
        "description": "List air-gapped GGUF weights and Modelfiles from LOCAL_LLM_MODELS_DIR.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
)


def _execute_rag_query_tool(args: dict[str, Any]) -> dict[str, Any]:
    query = str(args.get("query") or "").strip()
    if not query:
        raise ValueError("'query' is required for rag_query.")
    mode = str(args.get("mode") or "ghost").strip().lower()
    collection = str(args.get("collection") or "default").strip()
    top_k = max(1, min(20, int(args.get("top_k") or 5)))
    hits = get_rag_manager().query(query, mode=mode, collection=collection, top_k=top_k)
    return {
        "mode": mode,
        "collection": collection,
        "count": len(hits),
        "results": [h.to_dict() for h in hits],
    }


def _execute_code_audit_tool(args: dict[str, Any]) -> dict[str, Any]:
    from workspaces.orchestrator.engine.code_audit import run_code_audit
    from workspaces.orchestrator.engine.schemas import SchemaValidationError

    raw_target = args.get("target_path")
    try:
        return run_code_audit(
            raw_target,
            check_secrets=bool(args.get("check_secrets", True)),
            check_slopsquatting=bool(args.get("check_slopsquatting", True)),
        )
    except SchemaValidationError as exc:
        if exc.http_status == 403:
            raise PermissionError(exc.message) from exc
        raise ValueError(exc.message) from exc


async def _execute_local_chat_tool(args: dict[str, Any]) -> dict[str, Any]:
    prompt = str(args.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("'prompt' is required for local_chat.")
    model = str(args.get("model") or "local-fast").strip()
    sys_prompt = str(args.get("system_prompt") or "").strip()
    messages: list[dict[str, str]] = []
    if sys_prompt:
        messages.append({"role": "system", "content": sys_prompt})
    messages.append({"role": "user", "content": prompt})

    coord = get_gateway_coordinator()
    spec = coord.resolve_model(model)
    payload = {"model": spec.upstream_model, "messages": messages, "stream": False}
    try:
        async with coord.acquire_for_request(spec, allow_tier_switch=False):
            async with build_loopback_async_client(spec.base_url, timeout=60.0) as client:
                resp = await client.post("/v1/chat/completions", json=payload)
                data = resp.json()
                return {
                    "model": spec.public_model,
                    "backend": spec.backend,
                    "response": data,
                }
    except GatewayEngineError as exc:
        raise RuntimeError(exc.message) from exc


@router.post("/mcp")
async def mcp_endpoint(request: Request) -> JSONResponse:
    """Protected stateless Model Context Protocol (MCP) JSON-RPC 2.0 endpoint."""
    t0 = time.perf_counter()
    audit = get_mcp_audit_logger()
    client_app = request.headers.get("x-app-id", "mcp-client").strip() or "mcp-client"

    try:
        raw_body = await request.body()
        body = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        audit.record(
            method="parse_error",
            decision="deny",
            http_status=400,
            reason="Invalid JSON payload",
            client_app=client_app,
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )
        return _jsonrpc_error(None, -32700, "Parse error: invalid JSON.", status_code=400)

    if not isinstance(body, dict) or body.get("jsonrpc") != "2.0" or not isinstance(body.get("method"), str):
        req_id = body.get("id") if isinstance(body, dict) else None
        audit.record(
            method="invalid_request",
            decision="deny",
            http_status=400,
            reason="Invalid JSON-RPC 2.0 envelope",
            jsonrpc_id=req_id,
            client_app=client_app,
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )
        return _jsonrpc_error(req_id, -32600, "Invalid Request: expected JSON-RPC 2.0 object.", status_code=400)

    req_id = body.get("id")
    method = body["method"].strip()
    params = body.get("params") if isinstance(body.get("params"), dict) else {}

    if method == "initialize":
        audit.record(
            method=method,
            decision="allow",
            http_status=200,
            jsonrpc_id=req_id,
            client_app=client_app,
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )
        return _jsonrpc_ok(
            req_id,
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "kryptos-core-mcp-gateway", "version": "3.0.0"},
            },
        )

    if method in ("notifications/initialized", "ping"):
        audit.record(
            method=method,
            decision="allow",
            http_status=200,
            jsonrpc_id=req_id,
            client_app=client_app,
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )
        return _jsonrpc_ok(req_id, {})

    if method == "tools/list":
        audit.record(
            method=method,
            decision="allow",
            http_status=200,
            jsonrpc_id=req_id,
            client_app=client_app,
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )
        return _jsonrpc_ok(req_id, {"tools": list(_MCP_TOOLS)})

    if method == "tools/call":
        tool_name = str(params.get("name") or "").strip()
        tool_args = params.get("arguments")
        if tool_args is None:
            tool_args = {}
        if not isinstance(tool_args, dict):
            audit.record(
                method=method,
                tool=tool_name or None,
                decision="deny",
                http_status=400,
                reason="Tool arguments must be an object",
                jsonrpc_id=req_id,
                client_app=client_app,
                latency_ms=(time.perf_counter() - t0) * 1000.0,
            )
            return _jsonrpc_error(req_id, -32602, "Invalid params: 'arguments' must be an object.", status_code=400)

        # Enforce OWASP MCP03 / Anti-SSRF & Sensitive File / PII Blocklist BEFORE tool execution
        security_violation = _inspect_tool_arguments(tool_args)
        if security_violation:
            audit.record(
                method=method,
                tool=tool_name or None,
                decision="deny",
                http_status=403,
                reason=security_violation,
                jsonrpc_id=req_id,
                client_app=client_app,
                latency_ms=(time.perf_counter() - t0) * 1000.0,
            )
            return _jsonrpc_error(req_id, -32003, security_violation, status_code=403)

        firewall_redactions = 0
        if is_prompt_firewall_enabled(request.headers.get("x-prompt-firewall")):
            tool_args, fw_counts = redact_structure(tool_args)
            firewall_redactions = sum(fw_counts.values())

        try:
            if tool_name == "rag_query":
                result_obj = await asyncio.to_thread(_execute_rag_query_tool, tool_args)
            elif tool_name == "code_audit":
                result_obj = await asyncio.to_thread(_execute_code_audit_tool, tool_args)
            elif tool_name == "local_chat":
                result_obj = await _execute_local_chat_tool(tool_args)
            elif tool_name == "model_inventory":
                result_obj = await asyncio.to_thread(get_airgapped_inventory)
            else:
                audit.record(
                    method=method,
                    tool=tool_name or None,
                    decision="deny",
                    http_status=404,
                    reason=f"Unknown MCP tool '{tool_name}'",
                    jsonrpc_id=req_id,
                    client_app=client_app,
                    latency_ms=(time.perf_counter() - t0) * 1000.0,
                )
                return _jsonrpc_error(
                    req_id, -32601, f"Unknown tool: '{tool_name}'.", status_code=404
                )
        except PermissionError as exc:
            audit.record(
                method=method,
                tool=tool_name,
                decision="deny",
                http_status=403,
                reason=str(exc),
                jsonrpc_id=req_id,
                client_app=client_app,
                latency_ms=(time.perf_counter() - t0) * 1000.0,
            )
            return _jsonrpc_error(req_id, -32003, str(exc), status_code=403)
        except ValueError as exc:
            audit.record(
                method=method,
                tool=tool_name,
                decision="deny",
                http_status=400,
                reason=str(exc),
                jsonrpc_id=req_id,
                client_app=client_app,
                latency_ms=(time.perf_counter() - t0) * 1000.0,
            )
            return _jsonrpc_error(req_id, -32602, str(exc), status_code=400)
        except Exception as exc:
            audit.record(
                method=method,
                tool=tool_name,
                decision="deny",
                http_status=503,
                reason=str(exc),
                jsonrpc_id=req_id,
                client_app=client_app,
                latency_ms=(time.perf_counter() - t0) * 1000.0,
            )
            return _jsonrpc_error(req_id, -32000, str(exc), status_code=503)

        audit.record(
            method=method,
            tool=tool_name,
            decision="allow",
            http_status=200,
            reason=f"ok;firewall_redactions={firewall_redactions}" if firewall_redactions else "ok",
            jsonrpc_id=req_id,
            client_app=client_app,
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )
        return _jsonrpc_ok(
            req_id,
            {
                "content": [
                    {"type": "text", "text": json.dumps(result_obj, ensure_ascii=False)}
                ],
                "structuredContent": result_obj,
                "isError": False,
            },
        )

    audit.record(
        method=method,
        decision="deny",
        http_status=404,
        reason=f"Method not found: {method}",
        jsonrpc_id=req_id,
        client_app=client_app,
        latency_ms=(time.perf_counter() - t0) * 1000.0,
    )
    return _jsonrpc_error(req_id, -32601, f"Method not found: '{method}'.", status_code=404)


@router.get("/api/mcp/audit")
async def mcp_audit_log() -> JSONResponse:
    """Return RAM-only Ghost MCP JSONL audit ring buffer (metadata only, zero disk writes)."""
    logger = get_mcp_audit_logger()
    entries = logger.list_entries()
    return JSONResponse(
        status_code=200,
        content={
            "ok": True,
            "retention_mode": "ghost",
            "count": len(entries),
            "entries": entries,
            "jsonl": logger.get_jsonl(),
        },
    )
