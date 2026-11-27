"""Security primitives & ASGI middleware for Local LLM Gateway (:8787)."""

from __future__ import annotations

import getpass
import json
import os
import pathlib
import re
import secrets
import shutil
import subprocess
import sys
import threading
import urllib.parse
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .config import PORT

_TOKEN_LOCK = threading.Lock()
_GATEWAY_TOKEN: str | None = None

_HOST_HEADER_RE = re.compile(r"^(127\.0\.0\.1|localhost|\[::1\])(?::(\d{1,5}))?$")
_ALLOWED_ORIGIN_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

# Strict CSP: zero 'unsafe-inline' in script-src or style-src; blocks all external origins.
STRICT_CSP_HEADER = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "connect-src 'self'; "
    "img-src 'self' data:; "
    "object-src 'none'; "
    "frame-ancestors 'none';"
)

NO_STORE_HEADER = "no-store, no-cache, must-revalidate, max-age=0"


def _subprocess_kwargs() -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
    }
    if sys.platform == "win32" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return kwargs


def get_gateway_token_path() -> pathlib.Path:
    """Return the per-user `gateway.token` path."""
    override = (
        os.environ.get("KRYPTOS_GATEWAY_TOKEN_FILE", "").strip()
        or os.environ.get("LOCAL_LLM_GATEWAY_TOKEN_FILE", "").strip()
    )
    if override:
        return pathlib.Path(override).expanduser()
    if sys.platform == "win32":
        local_app = os.environ.get("LOCALAPPDATA", "").strip()
        if local_app:
            return pathlib.Path(local_app) / "KryptosCore" / "gateway.token"
    return pathlib.Path.home() / ".local" / "share" / "KryptosCore" / "gateway.token"


def restrict_token_file_acl(token_path: pathlib.Path) -> None:
    """Restrict token file ACL to current user only (`icacls` on Windows, `0600` on POSIX)."""
    if sys.platform == "win32":
        icacls_exe = shutil.which("icacls")
        user_name = (os.environ.get("USERNAME") or getpass.getuser()).strip()
        if icacls_exe and user_name:
            try:
                subprocess.run(
                    [icacls_exe, str(token_path), "/inheritance:r", "/grant:r", f"{user_name}:(R,W)"],
                    timeout=10,
                    shell=False,
                    **_subprocess_kwargs(),
                )
            except Exception:
                pass
    else:
        try:
            os.chmod(token_path, 0o600)
        except OSError:
            pass


def rotate_gateway_token() -> str:
    """Generate/rotate the local gateway token and persist it with strict user-only ACL."""
    global _GATEWAY_TOKEN
    with _TOKEN_LOCK:
        token = secrets.token_urlsafe(32)
        token_path = get_gateway_token_path()
        try:
            token_path.parent.mkdir(parents=True, exist_ok=True)
            token_path.write_text(token, encoding="utf-8")
            restrict_token_file_acl(token_path)
        except OSError:
            pass
        _GATEWAY_TOKEN = token
        return token


def get_gateway_token() -> str:
    """Return the active gateway token, rotating if uninitialized."""
    global _GATEWAY_TOKEN
    with _TOKEN_LOCK:
        if _GATEWAY_TOKEN is not None:
            return _GATEWAY_TOKEN
    return rotate_gateway_token()


def is_allowed_host(raw_host: str | None, allowed_ports: set[int]) -> bool:
    """Validate HTTP `Host` header against loopback allowlist and bound ports."""
    clean = (raw_host or "").strip().lower()
    match = _HOST_HEADER_RE.fullmatch(clean)
    if not match:
        return False
    port_str = match.group(2)
    if port_str is not None:
        try:
            port_num = int(port_str)
        except ValueError:
            return False
        if port_num not in allowed_ports:
            return False
    return True


def is_allowed_origin_and_fetch_site(
    origin: str | None,
    sec_fetch_site: str | None,
    allowed_ports: set[int],
    require_same_origin: bool = False,
) -> bool:
    """Validate `Origin` and `Sec-Fetch-Site` headers to prevent CSRF."""
    if origin is not None:
        origin_clean = origin.strip()
        if not origin_clean or origin_clean.lower() == "null":
            return False
        parsed = urllib.parse.urlsplit(origin_clean)
        host = (parsed.hostname or "").lower()
        if parsed.scheme.lower() != "http" or host not in _ALLOWED_ORIGIN_HOSTS:
            return False
        if parsed.port is not None and parsed.port not in allowed_ports:
            return False

    if require_same_origin:
        if not sec_fetch_site or sec_fetch_site.strip().lower() != "same-origin":
            return False
    elif sec_fetch_site is not None:
        sfs_val = sec_fetch_site.strip().lower()
        if sfs_val not in ("same-origin", "none"):
            return False

    return True


def is_json_content_type(raw_ctype: str | None) -> bool:
    """Return True iff the media type is `application/json`."""
    media_type = (raw_ctype or "").split(";", 1)[0].strip().lower()
    return media_type == "application/json"


def is_authorized_token(x_local_token: str | None, authorization: str | None) -> bool:
    """Verify either `X-Local-LLM-Token` or `Authorization: Bearer <token>`."""
    expected = get_gateway_token()
    candidate = (x_local_token or "").strip()
    if not candidate:
        auth_hdr = (authorization or "").strip()
        if auth_hdr.lower().startswith("bearer "):
            candidate = auth_hdr[7:].strip()
    if not candidate:
        return False
    return secrets.compare_digest(candidate, expected)


def _allowed_ports_from_scope(scope: Scope) -> set[int]:
    ports = {PORT}
    try:
        env_port = int(os.environ.get("OFFLINE_CHAT_PORT", str(PORT)))
        ports.add(env_port)
    except ValueError:
        pass
    try:
        from .runtime_state import get_active_runtime_info

        rt_port = get_active_runtime_info().get("port")
        if isinstance(rt_port, int) and rt_port > 0:
            ports.add(rt_port)
    except Exception:
        pass
    server = scope.get("server")
    if isinstance(server, (tuple, list)) and len(server) >= 2 and isinstance(server[1], int):
        # Ignore Starlette TestClient's default dummy port 80 when no custom port was bound
        if server[1] > 0 and not (server[0] == "testserver" and server[1] == 80):
            ports.add(server[1])
    return ports


async def _send_json_error(
    send: Send,
    status_code: int,
    payload: dict[str, Any],
    extra_headers: list[tuple[bytes, bytes]] | None = None,
) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers: list[tuple[bytes, bytes]] = [
        (b"content-type", b"application/json; charset=utf-8"),
        (b"content-length", str(len(body)).encode("ascii")),
        (b"cache-control", NO_STORE_HEADER.encode("ascii")),
        (b"pragma", b"no-cache"),
        (b"x-content-type-options", b"nosniff"),
    ]
    if extra_headers:
        headers.extend(extra_headers)
    await send({"type": "http.response.start", "status": status_code, "headers": headers})
    await send({"type": "http.response.body", "body": body})


class GatewaySecurityMiddleware:
    """
    Pure ASGI middleware enforcing:
    1. Strict loopback `Host` header allowlist (anti-DNS-rebinding -> 403).
    2. Strict `Origin` / `Sec-Fetch-Site` policy (anti-CSRF -> 403).
    3. `Content-Type: application/json` on mutating `/api/*` and `/v1/*` requests (-> 415).
    4. `gateway.token` auth (`Authorization: Bearer` or `X-Local-LLM-Token` -> 401).
    5. Strict `no-store` + security headers + strict CSP (no `'unsafe-inline'` in `script-src`).
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = str(scope.get("method", "GET")).upper()
        path = str(scope.get("path", "/"))
        headers_map: dict[str, str] = {}
        for k_bytes, v_bytes in scope.get("headers", []):
            k = k_bytes.decode("latin-1").lower()
            v = v_bytes.decode("latin-1")
            headers_map[k] = v

        allowed_ports = _allowed_ports_from_scope(scope)

        # 1. Host allowlist on EVERY endpoint
        if not is_allowed_host(headers_map.get("host"), allowed_ports):
            await _send_json_error(
                send, 403, {"ok": False, "error": "Forbidden: header Host non consentito"}
            )
            return

        # 2. Public static UI assets (GET /, /index.html, /favicon.ico, /static/*, /assets/*)
        is_public_ui = method == "GET" and (
            path in ("/", "/index.html", "/favicon.ico")
            or path.startswith("/static/")
            or path.startswith("/assets/")
        )

        # 3. Protected session-token endpoint
        if path == "/api/auth/session-token":
            if method != "GET":
                await _send_json_error(send, 405, {"ok": False, "error": "Metodo non consentito"})
                return
            if not is_allowed_origin_and_fetch_site(
                headers_map.get("origin"),
                headers_map.get("sec-fetch-site"),
                allowed_ports,
                require_same_origin=True,
            ):
                await _send_json_error(
                    send,
                    403,
                    {"ok": False, "error": "Forbidden: richiesto contesto browser same-origin"},
                )
                return
        elif not is_public_ui:
            is_protected = (
                path == "/api"
                or path.startswith("/api/")
                or path == "/v1"
                or path.startswith("/v1/")
                or path == "/mcp"
                or path.startswith("/mcp/")
                or method in ("POST", "PUT", "PATCH", "DELETE")
            )
            if is_protected:
                if not is_allowed_origin_and_fetch_site(
                    headers_map.get("origin"),
                    headers_map.get("sec-fetch-site"),
                    allowed_ports,
                    require_same_origin=False,
                ):
                    await _send_json_error(
                        send,
                        403,
                        {"ok": False, "error": "Forbidden: violazione policy Origin / Sec-Fetch-Site"},
                    )
                    return

                if method in ("POST", "PUT", "PATCH") and not is_json_content_type(
                    headers_map.get("content-type")
                ):
                    await _send_json_error(
                        send,
                        415,
                        {
                            "ok": False,
                            "error": "Unsupported Media Type: Content-Type deve essere application/json",
                        },
                    )
                    return

                if not is_authorized_token(
                    headers_map.get("x-kryptos-token") or headers_map.get("x-local-llm-token"),
                    headers_map.get("authorization"),
                ):
                    await _send_json_error(
                        send,
                        401,
                        {"ok": False, "error": "Unauthorized: token gateway mancante o non valido"},
                        extra_headers=[(b"www-authenticate", b"Bearer")],
                    )
                    return

        async def send_with_security_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                raw_headers: list[tuple[bytes, bytes]] = list(message.get("headers", []))
                present = {k.lower() for k, _ in raw_headers}

                def _ensure(name: bytes, value: bytes) -> None:
                    if name.lower() not in present:
                        raw_headers.append((name, value))
                        present.add(name.lower())

                _ensure(b"cache-control", NO_STORE_HEADER.encode("ascii"))
                _ensure(b"pragma", b"no-cache")
                _ensure(b"x-content-type-options", b"nosniff")
                _ensure(b"x-frame-options", b"DENY")
                _ensure(b"referrer-policy", b"no-referrer")
                if path in ("/", "/index.html") or path.startswith("/static/") or path.startswith("/assets/"):
                    _ensure(b"content-security-policy", STRICT_CSP_HEADER.encode("ascii"))

                message = {**message, "headers": raw_headers}
            await send(message)

        await self.app(scope, receive, send_with_security_headers)
