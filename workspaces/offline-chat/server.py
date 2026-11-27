"""
Local LLM Offline Console Server Shim — delegates to `workspaces.local_llm_core` (FastAPI + Uvicorn).

Preserves backwards-compatible module exports, `main()` Uvicorn launcher, and
`OfflineChatHandler` / `ThreadingHTTPServer` compatibility for existing scripts and tests.
"""

from __future__ import annotations

import http.client
import http.server
import json
import pathlib
import socketserver
import sys
import urllib.parse
from typing import Any

ROOT_DIR = pathlib.Path(__file__).resolve().parent
PROJECT_ROOT = ROOT_DIR.parent.parent
_PROJECT_ROOT_STR = str(PROJECT_ROOT)
if _PROJECT_ROOT_STR not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT_STR)

from workspaces.local_llm_core.app import app, create_app, run_server
from workspaces.local_llm_core.config import (
    ALLOWED_ENGINE_ACTIONS,
    BENCHMARKS_RESULTS_FILE,
    ENDPOINTS_JSON_FILE,
    HEAVY_BASE,
    HEAVY_HOST,
    HEAVY_MOE_DIR,
    HEAVY_PORT,
    HOST,
    LMS_BASE,
    LMS_HOST,
    LMS_PORT,
    MAX_REQUEST_BODY_BYTES,
    MODEL_NAME_RE,
    MODELS_DIR,
    OLLAMA_BASE,
    OLLAMA_HOST,
    OLLAMA_PORT,
    PORT,
    PROMPT_ID_RE,
    PROMPTS_DIR,
    PROTECTED_DELETE_IDS,
    PROTECTED_SAVE_IDS,
    REGISTRY_MD_FILE,
    SCRIPTS_DIR,
    STATIC_DIR,
    WEB_DIST_DIR,
    load_dotenv,
    resolve_web_dist_dir,
)
from workspaces.local_llm_core.engine.coordinator import (
    execute_engine_action as _core_execute_engine_action,
)
from workspaces.local_llm_core.security import (
    NO_STORE_HEADER as _CORE_NO_STORE_HEADER,
    STRICT_CSP_HEADER,
    get_gateway_token,
    get_gateway_token_path,
    is_allowed_host,
    is_allowed_origin_and_fetch_site,
    is_authorized_token,
    is_json_content_type,
    restrict_token_file_acl,
    rotate_gateway_token,
)

NO_STORE_HEADER = _CORE_NO_STORE_HEADER or "no-store, no-cache, must-revalidate, max-age=0"
from workspaces.local_llm_core.services.prompts import (
    build_prompt_markdown,
    delete_prompt_preset,
    load_prompt_presets,
    parse_frontmatter,
    save_prompt_preset,
)
from workspaces.local_llm_core.services.telemetry import (
    _http_json,
    get_backends_status,
    get_disk_telemetry,
    get_full_telemetry as _core_get_full_telemetry,
    get_gpu_telemetry,
    get_local_registry as _core_get_local_registry,
    get_ram_telemetry,
    invalidate_telemetry_cache,
    read_active_heavy_model,
)

try:
    from workspaces.orchestrator.api_router import (
        get_orchestrator_telemetry_summary,
        handle_orchestrator_request,
        shutdown_orchestrator,
    )
except Exception:

    def handle_orchestrator_request(
        method: str, path: str, payload: dict[str, Any] | None = None
    ) -> tuple[int, dict[str, Any]] | None:
        clean = urllib.parse.urlsplit(path).path
        if clean == "/api/orchestrator" or clean.startswith("/api/orchestrator/"):
            return 503, {
                "ok": False,
                "available": False,
                "state": "UNAVAILABLE",
                "error": "Modulo Orchestrator non disponibile.",
            }
        return None

    def get_orchestrator_telemetry_summary() -> dict[str, Any]:
        return {
            "available": False,
            "state": "IDLE",
            "active": False,
            "retentionMode": None,
            "hasReport": False,
            "step": 0,
            "maxSteps": 0,
            "runId": None,
            "verdict": None,
            "activeEngine": None,
            "autoPurgeRemainingSec": None,
        }

    def shutdown_orchestrator() -> None:
        return None


def get_full_telemetry(force_refresh: bool = False) -> dict[str, Any]:
    """Compatibility wrapper forwarding module-level telemetry hooks."""
    return _core_get_full_telemetry(
        force_refresh=force_refresh,
        http_json_fn=_http_json,
        gpu_fn=get_gpu_telemetry,
        orch_summary_fn=get_orchestrator_telemetry_summary,
    )


def get_local_registry() -> dict[str, Any]:
    """Compatibility wrapper forwarding `_http_json` hook."""
    return _core_get_local_registry(http_json_fn=_http_json)


def execute_engine_action(payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """Compatibility wrapper forwarding `get_orchestrator_telemetry_summary` hook."""
    return _core_execute_engine_action(
        payload, orch_summary_fn=get_orchestrator_telemetry_summary
    )


_STATIC_MIME_MAP = {
    ".js": "application/javascript; charset=utf-8",
    ".mjs": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}


class OfflineChatHandler(http.server.BaseHTTPRequestHandler):
    """Legacy stdlib HTTP handler shim delegating security & core logic to `local_llm_core`."""

    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def _send_json(
        self, code: int, payload: object, extra_headers: dict[str, str] | None = None
    ) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", NO_STORE_HEADER)
        self.send_header("Pragma", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        if extra_headers:
            for hdr_name, hdr_val in extra_headers.items():
                self.send_header(hdr_name, hdr_val)
        self.end_headers()
        self.wfile.write(data)

    def _effective_server_port(self) -> int:
        srv = getattr(self, "server", None)
        srv_port = getattr(srv, "server_port", None)
        if isinstance(srv_port, int) and srv_port > 0:
            return srv_port
        return PORT

    def _is_allowed_host(self) -> bool:
        return is_allowed_host(self.headers.get("Host"), {self._effective_server_port()})

    def _is_allowed_origin_and_fetch_site(self, require_same_origin: bool = False) -> bool:
        return is_allowed_origin_and_fetch_site(
            self.headers.get("Origin"),
            self.headers.get("Sec-Fetch-Site"),
            {self._effective_server_port()},
            require_same_origin=require_same_origin,
        )

    def _require_json_content_type(self) -> bool:
        if not is_json_content_type(self.headers.get("Content-Type")):
            self._send_json(
                415,
                {
                    "ok": False,
                    "error": "Unsupported Media Type: Content-Type deve essere application/json",
                },
            )
            return False
        return True

    def _is_authorized_token(self) -> bool:
        if not is_authorized_token(
            self.headers.get("X-Local-LLM-Token"), self.headers.get("Authorization")
        ):
            self._send_json(
                401,
                {"ok": False, "error": "Unauthorized: token gateway mancante o non valido"},
                extra_headers={"WWW-Authenticate": "Bearer"},
            )
            return False
        return True

    def _enforce_request_security(self, method: str, path: str) -> bool:
        if not self._is_allowed_host():
            self._send_json(403, {"ok": False, "error": "Forbidden: header Host non consentito"})
            return False

        if method == "GET" and (
            path in ("/", "/index.html", "/favicon.ico") or path.startswith("/static/")
        ):
            return True

        if path == "/api/auth/session-token":
            if method != "GET":
                self._send_json(405, {"ok": False, "error": "Metodo non consentito"})
                return False
            if not self._is_allowed_origin_and_fetch_site(require_same_origin=True):
                self._send_json(
                    403,
                    {"ok": False, "error": "Forbidden: richiesto contesto browser same-origin"},
                )
                return False
            return True

        is_protected_route = (
            path == "/api"
            or path.startswith("/api/")
            or path == "/v1"
            or path.startswith("/v1/")
            or method in ("POST", "PUT", "PATCH", "DELETE")
        )
        if is_protected_route:
            if not self._is_allowed_origin_and_fetch_site(require_same_origin=False):
                self._send_json(
                    403,
                    {"ok": False, "error": "Forbidden: violazione policy Origin / Sec-Fetch-Site"},
                )
                return False
            if method in ("POST", "PUT", "PATCH") and not self._require_json_content_type():
                return False
            if not self._is_authorized_token():
                return False

        return True

    def _send_file(self, filepath: pathlib.Path, content_type: str) -> None:
        if not filepath.is_file():
            self._send_json(404, {"error": "Risorsa UI non trovata"})
            return
        data = filepath.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", NO_STORE_HEADER)
        self.send_header("Pragma", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", STRICT_CSP_HEADER)
        self.end_headers()
        self.wfile.write(data)

    def _read_request_body_bytes(self) -> tuple[bytes | None, tuple[int, str] | None]:
        te = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in te:
            chunks: list[bytes] = []
            total = 0
            while True:
                size_line = self.rfile.readline(64)
                if not size_line:
                    return None, (400, "Corpo chunked incompleto")
                size_part = size_line.split(b";", 1)[0].strip()
                try:
                    chunk_len = int(size_part, 16)
                except ValueError:
                    return None, (400, "Dimensione chunk non valida")
                if chunk_len < 0 or total + chunk_len > MAX_REQUEST_BODY_BYTES:
                    return None, (413, "Payload troppo grande o non valido")
                if chunk_len == 0:
                    while True:
                        trailer = self.rfile.readline(256)
                        if not trailer or trailer in (b"\r\n", b"\n"):
                            break
                    break
                data = self.rfile.read(chunk_len)
                if len(data) != chunk_len:
                    return None, (400, "Chunk troncato")
                chunks.append(data)
                total += chunk_len
                crlf = self.rfile.read(2)
                if crlf not in (b"\r\n", b"\n"):
                    return None, (400, "Delimitatore chunk non valido")
            return (b"".join(chunks) if chunks else b"{}"), None

        raw_len = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_len)
        except ValueError:
            return None, (400, "Header Content-Length non valido")
        if length < 0 or length > MAX_REQUEST_BODY_BYTES:
            return None, (413, "Payload troppo grande o non valido")
        raw_bytes = self.rfile.read(length) if length > 0 else b"{}"
        return raw_bytes, None

    def _read_json_body(self) -> tuple[dict[str, Any] | None, bytes | None, tuple[int, str] | None]:
        raw_bytes, err = self._read_request_body_bytes()
        if err is not None:
            return None, None, err
        assert raw_bytes is not None
        if not raw_bytes.strip():
            raw_bytes = b"{}"
        try:
            parsed = json.loads(raw_bytes.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None, raw_bytes, (400, "JSON non valido nel corpo della richiesta")
        if not isinstance(parsed, dict):
            return None, raw_bytes, (400, "Il corpo JSON deve essere un oggetto")
        return parsed, raw_bytes, None

    def _proxy_get(
        self, host: str, port: int, upstream_path: str, label: str, base_url: str
    ) -> None:
        conn: http.client.HTTPConnection | None = None
        try:
            conn = http.client.HTTPConnection(host, port, timeout=5)
            conn.request("GET", upstream_path, headers={"Accept": "application/json"})
            resp = conn.getresponse()
            body = resp.read()
            if resp.status >= 400:
                err_txt = body.decode("utf-8", errors="replace")
                try:
                    err_obj = json.loads(err_txt)
                    if isinstance(err_obj, dict) and "error" in err_obj:
                        self._send_json(resp.status, err_obj)
                        return
                except ValueError:
                    pass
                self._send_json(resp.status, {"error": err_txt or f"HTTP {resp.status}"})
                return

            ctype = resp.getheader("Content-Type") or "application/json; charset=utf-8"
            self.send_response(resp.status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", NO_STORE_HEADER)
            self.send_header("Pragma", "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)
        except Exception as exc:
            self._send_json(503, {"error": f"{label} non raggiungibile su {base_url}: {exc}"})
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def _proxy_post_stream(
        self,
        host: str,
        port: int,
        upstream_path: str,
        label: str,
        base_url: str,
        default_ctype: str,
    ) -> None:
        body, err = self._read_request_body_bytes()
        if err is not None:
            self._send_json(err[0], {"error": err[1]})
            return
        assert body is not None

        conn: http.client.HTTPConnection | None = None
        headers_sent = False
        upstream_ctype = default_ctype
        try:
            conn = http.client.HTTPConnection(host, port, timeout=600)
            conn.request(
                "POST",
                upstream_path,
                body=body,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "text/event-stream, application/x-ndjson, application/json",
                },
            )
            resp = conn.getresponse()

            if resp.status >= 400:
                err_txt = resp.read().decode("utf-8", errors="replace")
                try:
                    err_obj = json.loads(err_txt)
                    if isinstance(err_obj, dict) and "error" in err_obj:
                        err_val = err_obj["error"]
                        if isinstance(err_val, dict) and "message" in err_val:
                            self._send_json(resp.status, {"error": str(err_val["message"])})
                        else:
                            self._send_json(resp.status, {"error": err_val})
                        return
                except ValueError:
                    pass
                self._send_json(resp.status, {"error": err_txt or f"{label} HTTP {resp.status}"})
                return

            upstream_ctype = resp.getheader("Content-Type") or default_ctype
            self.send_response(resp.status)
            self.send_header("Content-Type", upstream_ctype)
            self.send_header("Cache-Control", NO_STORE_HEADER)
            self.send_header("Pragma", "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Connection", "close")
            self.end_headers()
            headers_sent = True

            while True:
                chunk = resp.read1(4096)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception as exc:
            err_msg = f"Errore comunicazione {label} su {base_url}: {exc}"
            try:
                if not headers_sent:
                    self._send_json(503, {"error": err_msg})
                else:
                    if "text/event-stream" in upstream_ctype.lower():
                        payload = (
                            f"data: {json.dumps({'error': err_msg}, ensure_ascii=False)}\n\n"
                            "data: [DONE]\n\n"
                        )
                    else:
                        payload = json.dumps({"error": err_msg}, ensure_ascii=False) + "\n"
                    self.wfile.write(payload.encode("utf-8"))
                    self.wfile.flush()
            except OSError:
                pass
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def do_GET(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        if not self._enforce_request_security("GET", path):
            return

        if path == "/api/auth/session-token":
            self._send_json(200, {"ok": True, "token": get_gateway_token()})
            return

        if path in ("/", "/index.html"):
            dist_index = resolve_web_dist_dir() / "index.html"
            self._send_file(
                dist_index if dist_index.is_file() else (ROOT_DIR / "index.html"),
                "text/html; charset=utf-8",
            )
            return

        if path.startswith("/static/"):
            rel = urllib.parse.unquote(path[len("/static/") :]).lstrip("/")
            dist_static = resolve_web_dist_dir() / "static"
            target: pathlib.Path | None = None
            for base_dir in (dist_static, STATIC_DIR):
                base_resolved = base_dir.resolve()
                cand = (base_resolved / rel).resolve()
                if cand != base_resolved and cand.is_relative_to(base_resolved) and cand.is_file():
                    target = cand
                    break
            if target is None:
                self._send_json(404, {"error": "Static asset not found"})
                return
            mime = _STATIC_MIME_MAP.get(target.suffix.lower(), "application/octet-stream")
            self._send_file(target, mime)
            return

        if path == "/favicon.ico":
            ico_path = PROJECT_ROOT / "launcher" / "local-llm.ico"
            if ico_path.is_file():
                self._send_file(ico_path, "image/x-icon")
            else:
                self.send_response(204)
                self.send_header("Cache-Control", NO_STORE_HEADER)
                self.send_header("Pragma", "no-cache")
                self.end_headers()
            return

        if path == "/api/orchestrator" or path.startswith("/api/orchestrator/"):
            orch_res = handle_orchestrator_request("GET", path)
            if orch_res is not None:
                self._send_json(orch_res[0], orch_res[1])
                return

        if path == "/api/prompts":
            self._send_json(200, {"prompts": load_prompt_presets()})
            return

        if path == "/api/system/telemetry":
            self._send_json(200, get_full_telemetry())
            return

        if path == "/api/registry/local":
            self._send_json(200, get_local_registry())
            return

        ollama_get_map = {
            "/api/ollama/tags": "/api/tags",
            "/api/ollama/ps": "/api/ps",
            "/api/ollama/version": "/api/version",
            "/api/tags": "/api/tags",
            "/api/ps": "/api/ps",
            "/api/version": "/api/version",
        }
        if path in ollama_get_map:
            self._proxy_get(
                OLLAMA_HOST, OLLAMA_PORT, ollama_get_map[path], "Ollama locale", OLLAMA_BASE
            )
            return

        heavy_get_map = {
            "/api/heavy/health": "/health",
            "/api/heavy/models": "/v1/models",
        }
        if path in heavy_get_map:
            self._proxy_get(
                HEAVY_HOST, HEAVY_PORT, heavy_get_map[path], "Heavy MoE Colibri", HEAVY_BASE
            )
            return

        self._send_json(404, {"error": "Endpoint non trovato"})

    def do_POST(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        if not self._enforce_request_security("POST", path):
            return

        if path == "/api/orchestrator" or path.startswith("/api/orchestrator/"):
            payload, _, err = self._read_json_body()
            if err is not None:
                self._send_json(err[0], {"ok": False, "error": err[1]})
                return
            orch_res = handle_orchestrator_request("POST", path, payload)
            if orch_res is not None:
                self._send_json(orch_res[0], orch_res[1])
                return

        ollama_post_map = {
            "/api/ollama/chat": "/api/chat",
            "/api/ollama/generate": "/api/generate",
            "/api/ollama/show": "/api/show",
            "/api/chat": "/api/chat",
            "/api/generate": "/api/generate",
            "/api/show": "/api/show",
        }
        if path in ollama_post_map:
            self._proxy_post_stream(
                OLLAMA_HOST,
                OLLAMA_PORT,
                ollama_post_map[path],
                "Ollama locale",
                OLLAMA_BASE,
                "application/x-ndjson; charset=utf-8",
            )
            return

        if path == "/api/heavy/chat/completions":
            self._proxy_post_stream(
                HEAVY_HOST,
                HEAVY_PORT,
                "/v1/chat/completions",
                "Heavy MoE Colibri",
                HEAVY_BASE,
                "text/event-stream; charset=utf-8",
            )
            return

        if path == "/api/control/engine":
            payload, _, err = self._read_json_body()
            if err is not None:
                self._send_json(err[0], {"ok": False, "error": err[1]})
                return
            assert payload is not None
            status_code, result = execute_engine_action(payload)
            self._send_json(status_code, result)
            return

        if path == "/api/prompts/save":
            payload, _, err = self._read_json_body()
            if err is not None:
                self._send_json(err[0], {"ok": False, "error": err[1]})
                return
            assert payload is not None
            status_code, body = save_prompt_preset(payload)
            self._send_json(status_code, body)
            return

        self._send_json(404, {"error": "Endpoint non consentito"})

    def do_DELETE(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        if not self._enforce_request_security("DELETE", path):
            return

        if path == "/api/orchestrator" or path.startswith("/api/orchestrator/"):
            orch_res = handle_orchestrator_request("DELETE", path)
            if orch_res is not None:
                self._send_json(orch_res[0], orch_res[1])
                return

        if not path.startswith("/api/prompts/"):
            self._send_json(404, {"error": "Endpoint non trovato"})
            return

        prompt_id = urllib.parse.unquote(path[len("/api/prompts/") :]).strip()
        status_code, body = delete_prompt_preset(prompt_id)
        self._send_json(status_code, body)

    def do_PUT(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        if not self._enforce_request_security("PUT", path):
            return
        self._send_json(405, {"ok": False, "error": "Metodo PUT non consentito"})

    def do_PATCH(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        if not self._enforce_request_security("PATCH", path):
            return
        self._send_json(405, {"ok": False, "error": "Metodo PATCH non consentito"})


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main() -> None:
    """Launch the FastAPI + Uvicorn server on `127.0.0.1:PORT`."""
    run_server(HOST, PORT)


if __name__ == "__main__":
    main()
