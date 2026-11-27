"""
Mutually Exclusive Dual-Tier Engine Switcher & Local Loopback Inference Client.

Manages exclusive switching between:
- Fast Tier: Ollama (`http://127.0.0.1:11434`)
- Heavy MoE Tier: Colibri 177B (`http://127.0.0.1:11435`)

Hardware & Privacy invariants:
- Strictly loopback (`127.0.0.1`) via Python stdlib `http.client`.
- Fast and Heavy never run concurrently.
- Before mounting Heavy MoE, verifies zero active orchestrator Docker containers and
  checks available physical RAM (anti-swap guard).
- Enforces `KVSAVE=0` ALWAYS in `ephemeral` mode (and in `persistent` when `allow_kv_save=False`).
- In `ephemeral` mode, enforces anti-swap profile `RAM_GB=12`.
"""

from __future__ import annotations

import contextlib
import ctypes
import http.client
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from typing import Any, Iterator

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[3]
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
ENDPOINTS_JSON = PROJECT_ROOT / "config" / "endpoints.json"
ORCHESTRATOR_CONTAINER_LABEL = "local-llm-orchestrator=true"


class TierInferenceError(RuntimeError):
    """Raised when local Fast or Heavy tier inference fails."""


def _default_engine_lock_path() -> pathlib.Path:
    override = os.environ.get("LOCAL_LLM_ENGINE_LOCK_FILE", "").strip()
    if override:
        return pathlib.Path(override).expanduser()
    if sys.platform == "win32":
        local_app = os.environ.get("LOCALAPPDATA", "").strip()
        if local_app:
            return pathlib.Path(local_app) / "LocalLLM" / "engine.lock"
    return pathlib.Path(tempfile.gettempdir()) / "LocalLLM-engine.lock"


class EngineCoordinator:
    """
    Unified in-process + inter-process engine lock shared by TierSwitcher and server.py.
    Serializes engine start/stop/switch operations so Fast and Heavy never collide.
    """

    def __init__(self, lock_path: pathlib.Path | None = None) -> None:
        self._lock_path = lock_path
        self._thread_lock = threading.RLock()
        self._local = threading.local()

    @property
    def lock_path(self) -> pathlib.Path:
        return self._lock_path if self._lock_path is not None else _default_engine_lock_path()

    @contextlib.contextmanager
    def acquire(self, timeout: float = 120.0) -> Iterator[None]:
        deadline = time.monotonic() + max(0.1, float(timeout))
        acquired_thread = self._thread_lock.acquire(timeout=max(0.1, float(timeout)))
        if not acquired_thread:
            raise TimeoutError("Timed out waiting for EngineCoordinator thread lock.")

        depth = getattr(self._local, "depth", 0)
        self._local.depth = depth + 1
        fh = None
        try:
            if depth == 0:
                lp = self.lock_path
                lp.parent.mkdir(parents=True, exist_ok=True)
                fh = open(lp, "a+b")
                fh.seek(0, os.SEEK_END)
                if fh.tell() == 0:
                    fh.write(b"0")
                    fh.flush()
                fh.seek(0)
                while True:
                    try:
                        if sys.platform == "win32":
                            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                        else:
                            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise TimeoutError(
                                f"Timed out waiting for EngineCoordinator file lock: {lp}"
                            )
                        time.sleep(0.05)
            yield
        finally:
            if fh is not None:
                try:
                    fh.seek(0)
                    if sys.platform == "win32":
                        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
                try:
                    fh.close()
                except OSError:
                    pass
            self._local.depth = max(0, getattr(self._local, "depth", 1) - 1)
            self._thread_lock.release()


_COORDINATOR_SINGLETON = EngineCoordinator()


def get_engine_coordinator() -> EngineCoordinator:
    """Return the process-wide EngineCoordinator singleton."""
    return _COORDINATOR_SINGLETON


class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def _normalize_loopback(raw: str | None, default_host: str, default_port: int) -> tuple[str, int]:
    candidate = (raw or "").strip()
    if not candidate:
        return default_host, default_port
    if "://" not in candidate:
        candidate = f"http://{candidate}"
    parsed = urllib.parse.urlsplit(candidate)
    host = parsed.hostname or default_host
    if host not in ("127.0.0.1", "localhost", "::1"):
        host = default_host
    if host == "::1":
        host = "127.0.0.1"
    port = parsed.port or default_port
    return host, port


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


def _resolve_pwsh() -> str | None:
    found = shutil.which("pwsh")
    if found:
        return found
    default_pwsh = pathlib.Path(r"C:\Program Files\PowerShell\7\pwsh.exe")
    if default_pwsh.is_file():
        return str(default_pwsh)
    return shutil.which("powershell")


class TierSwitcher:
    """Loopback-only engine switcher and inference client for Ollama (:11434) & Colibri (:11435)."""

    def __init__(
        self,
        project_root: pathlib.Path | str | None = None,
        ollama_host: str | None = None,
        ollama_port: int | None = None,
        heavy_host: str | None = None,
        heavy_port: int | None = None,
        coordinator: EngineCoordinator | None = None,
    ) -> None:
        self._project_root = (
            pathlib.Path(project_root).resolve() if project_root is not None else PROJECT_ROOT
        )
        self._scripts_dir = self._project_root / "scripts"
        self._coordinator = coordinator or get_engine_coordinator()
        self._switch_lock = self._coordinator._thread_lock
        self._current_tier: str = "none"
        self._last_kv_save: str = "0"

        cfg = self._load_endpoints_cfg()
        ol_default = (cfg.get("ollama") or {}).get("baseUrl") if isinstance(cfg.get("ollama"), dict) else None
        hv_cfg = cfg.get("colibriHeavyMoe") or cfg.get("heavyMoe") or {}
        hv_default = hv_cfg.get("baseUrl") if isinstance(hv_cfg, dict) else None

        ol_h, ol_p = _normalize_loopback(
            os.environ.get("OLLAMA_HOST") or ol_default, "127.0.0.1", 11434
        )
        hv_h, hv_p = _normalize_loopback(
            os.environ.get("HEAVY_MOE_BASE_URL")
            or (
                f"http://{os.environ.get('HEAVY_MOE_HOST', '127.0.0.1')}:{os.environ['HEAVY_MOE_PORT']}"
                if os.environ.get("HEAVY_MOE_PORT")
                else hv_default
            ),
            "127.0.0.1",
            11435,
        )

        # Enforce loopback even if caller passes custom values
        self.ollama_host, self.ollama_port = _normalize_loopback(
            f"http://{ollama_host or ol_h}:{ollama_port or ol_p}", "127.0.0.1", 11434
        )
        self.heavy_host, self.heavy_port = _normalize_loopback(
            f"http://{heavy_host or hv_h}:{heavy_port or hv_p}", "127.0.0.1", 11435
        )

    def _load_endpoints_cfg(self) -> dict[str, Any]:
        cfg_file = self._project_root / "config" / "endpoints.json"
        if not cfg_file.is_file():
            return {}
        try:
            data = json.loads(cfg_file.read_text(encoding="utf-8", errors="replace"))
            return data.get("endpoints", {}) if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    @staticmethod
    def get_free_ram_gb() -> float | None:
        if sys.platform == "win32":
            try:
                stat = _MEMORYSTATUSEX()
                stat.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
                if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                    return round(stat.ullAvailPhys / (1024**3), 2)
            except Exception:
                return None
        return None

    def cleanup_orchestrator_containers(self) -> tuple[bool, str]:
        """Remove any leftover Docker containers labeled local-llm-orchestrator=true."""
        docker_exe = shutil.which("docker")
        if not docker_exe:
            return True, "docker CLI not installed; skipped container cleanup."
        try:
            ps = subprocess.run(
                [docker_exe, "ps", "-aq", "--filter", f"label={ORCHESTRATOR_CONTAINER_LABEL}"],
                timeout=10,
                shell=False,
                **_subprocess_kwargs(),
            )
            if ps.returncode != 0:
                return True, f"docker daemon not active ({(ps.stderr or '').strip()})"
            ids = [line.strip() for line in (ps.stdout or "").splitlines() if line.strip()]
            if not ids:
                return True, "0 active orchestrator containers."
            rm = subprocess.run(
                [docker_exe, "rm", "-f", *ids],
                timeout=20,
                shell=False,
                **_subprocess_kwargs(),
            )
            if rm.returncode == 0:
                return True, f"Removed {len(ids)} orchestrator sandbox container(s)."
            return False, f"Failed to remove containers {ids}: {(rm.stderr or '').strip()}"
        except Exception as exc:
            return False, f"Container cleanup error: {exc}"

    def _http_request(
        self,
        host: str,
        port: int,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        timeout: float = 3.0,
    ) -> tuple[bool, int, Any, str | None]:
        # Hard loopback invariant check
        if host not in ("127.0.0.1", "localhost"):
            raise TierInferenceError(f"Non-loopback inference host blocked: {host}")

        conn: http.client.HTTPConnection | None = None
        try:
            conn = http.client.HTTPConnection(host, port, timeout=timeout)
            headers = {"Accept": "application/json"}
            payload_bytes: bytes | None = None
            if body is not None:
                payload_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")
                headers["Content-Type"] = "application/json"
            conn.request(method, path, body=payload_bytes, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
            text = raw.decode("utf-8", errors="replace")
            if 200 <= resp.status < 300:
                if not text.strip():
                    return True, resp.status, {}, None
                try:
                    return True, resp.status, json.loads(text), None
                except ValueError:
                    return True, resp.status, text, None
            return False, resp.status, None, text or f"HTTP {resp.status}"
        except Exception as exc:
            return False, 0, None, str(exc)
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def check_health(self, tier: str) -> bool:
        if tier == "fast":
            ok, _, _, _ = self._http_request(
                self.ollama_host, self.ollama_port, "GET", "/api/version", timeout=1.5
            )
            return ok
        if tier == "heavy":
            ok, _, _, _ = self._http_request(
                self.heavy_host, self.heavy_port, "GET", "/health", timeout=1.5
            )
            if ok:
                return True
            ok2, _, _, _ = self._http_request(
                self.heavy_host, self.heavy_port, "GET", "/v1/models", timeout=1.5
            )
            return ok2
        return False

    def get_active_tier(self) -> str:
        if self.check_health("heavy"):
            return "heavy"
        if self.check_health("fast"):
            return "fast"
        return "none"

    def ensure_tier(
        self,
        target_tier: str,
        retention_mode: str,
        allow_kv_save: bool = False,
        kv_cache_dir: pathlib.Path | None = None,
        ram_limit_gb: int = 12,
        ctx: int = 16384,
    ) -> tuple[bool, str]:
        """
        Ensure the requested tier ('fast' or 'heavy') is exclusively online.

        - In `ephemeral` mode, ALWAYS forces `KVSAVE=0` and `ram_limit_gb <= 12`.
        - Before mounting `heavy`, guarantees zero active orchestrator Docker containers.
        """
        tier = str(target_tier or "").strip().lower()
        if tier not in ("fast", "heavy"):
            return False, f"Unknown tier '{target_tier}' (expected 'fast' or 'heavy')."

        mode = str(retention_mode or "ephemeral").strip().lower()
        # Colibri v2.1.0 hardcodes KV output to <model_dir>/.coli_kv and does not support
        # an isolated per-vault directory (COLI_KV_DIR is unsupported), so KVSAVE stays 0 here.
        _ = (allow_kv_save, kv_cache_dir)
        kv_save_val = "0"
        effective_ram = max(8, min(22, int(ram_limit_gb)))
        if mode != "persistent":
            effective_ram = min(effective_ram, 12)
        effective_ctx = max(2048, min(131072, int(ctx)))

        with self._coordinator.acquire():
            if tier == "heavy":
                clean_ok, clean_msg = self.cleanup_orchestrator_containers()
                if not clean_ok:
                    return False, f"Cannot switch to Heavy tier: {clean_msg}"

                if (
                    self.check_health("heavy")
                    and not self.check_health("fast")
                    and self._last_kv_save == kv_save_val
                ):
                    self._current_tier = "heavy"
                    return True, "Heavy tier already online with matching KVSAVE policy."
            else:
                if self.check_health("fast") and not self.check_health("heavy"):
                    self._current_tier = "fast"
                    return True, "Fast tier already online."

            pwsh_exe = _resolve_pwsh()
            if not pwsh_exe:
                return False, "PowerShell (pwsh) not found in PATH."

            script_path = self._scripts_dir / "start-llm.ps1"
            if not script_path.is_file():
                return False, f"Engine startup script missing: {script_path}"

            cmd = [
                pwsh_exe,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script_path),
                "-Mode",
                tier,
            ]
            if tier == "heavy":
                cmd.extend(["-RamLimitGB", str(effective_ram), "-Ctx", str(effective_ctx)])

            env = os.environ.copy()
            env["DO_NOT_TRACK"] = "1"
            env["OFFLINE_MODE"] = "true"
            env["OLLAMA_NO_CLOUD"] = "1"
            env["OLLAMA_NOHISTORY"] = "1"
            env["KVSAVE"] = kv_save_val
            env["HEAVY_MOE_RAM_GB"] = str(effective_ram)
            env.pop("COLI_KV_DIR", None)

            try:
                proc = subprocess.run(
                    cmd,
                    cwd=str(self._project_root),
                    env=env,
                    timeout=120,
                    shell=False,
                    **_subprocess_kwargs(),
                )
            except subprocess.TimeoutExpired:
                return False, f"Timed out switching to tier '{tier}' (>120s)."
            except Exception as exc:
                return False, f"Failed invoking start-llm.ps1 for tier '{tier}': {exc}"

            combined = "\n".join(
                p for p in ((proc.stdout or "").strip(), (proc.stderr or "").strip()) if p
            )
            if proc.returncode != 0:
                return False, f"start-llm.ps1 -Mode {tier} failed (exit {proc.returncode}): {combined}"

            self._current_tier = tier
            self._last_kv_save = kv_save_val
            free_ram = self.get_free_ram_gb()
            ram_note = f", free_ram={free_ram}GB" if free_ram is not None else ""
            return True, f"Switched to '{tier}' tier (KVSAVE={kv_save_val}{ram_note})."

    def stop_engines_keep_ui(self) -> tuple[bool, str]:
        """Stop Ollama and Colibri while keeping the Offline Console UI alive."""
        with self._coordinator.acquire():
            pwsh_exe = _resolve_pwsh()
            if not pwsh_exe:
                return False, "PowerShell (pwsh) not found in PATH."
            script_path = self._scripts_dir / "stop-llm.ps1"
            if not script_path.is_file():
                return False, f"Engine stop script missing: {script_path}"

            cmd = [
                pwsh_exe,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script_path),
                "-KeepUI",
            ]
            try:
                proc = subprocess.run(
                    cmd,
                    cwd=str(self._project_root),
                    timeout=45,
                    shell=False,
                    **_subprocess_kwargs(),
                )
            except Exception as exc:
                return False, f"Failed running stop-llm.ps1 -KeepUI: {exc}"

            self._current_tier = "none"
            out = (proc.stdout or proc.stderr or "").strip()
            return proc.returncode == 0, out or f"stop-llm.ps1 exited with {proc.returncode}"

    def chat_ollama(
        self,
        messages: list[dict[str, str]],
        model: str = "local-uncensored:latest",
        temperature: float = 0.2,
        num_ctx: int = 16384,
        json_schema: dict[str, Any] | None = None,
        think: bool = False,
        timeout: int = 180,
    ) -> dict[str, Any]:
        """Call local Ollama `/api/chat` on 127.0.0.1:11434 with stream=False."""
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": False,
            "think": bool(think),
            "options": {
                "temperature": float(temperature),
                "num_ctx": int(num_ctx),
            },
        }
        if json_schema is not None:
            payload["format"] = json_schema

        t0 = time.monotonic()
        ok, status, data, err = self._http_request(
            self.ollama_host,
            self.ollama_port,
            "POST",
            "/api/chat",
            body=payload,
            timeout=float(timeout),
        )
        duration = round(time.monotonic() - t0, 3)

        if not ok or not isinstance(data, dict):
            raise TierInferenceError(
                f"Ollama /api/chat failed (status={status}): {err or 'invalid JSON response'}"
            )

        msg = data.get("message") if isinstance(data.get("message"), dict) else {}
        content = str(msg.get("content") or "")
        thinking = str(msg.get("thinking") or "")
        return {
            "ok": True,
            "tier": "fast",
            "model": str(data.get("model") or model),
            "content": content,
            "thinking": thinking,
            "prompt_tokens": int(data.get("prompt_eval_count") or 0),
            "completion_tokens": int(data.get("eval_count") or 0),
            "duration_sec": duration,
            "raw": data,
        }

    def chat_heavy(
        self,
        messages: list[dict[str, str]],
        model: str = "heavy-moe-uncensored",
        temperature: float = 0.2,
        max_tokens: int = 2048,
        timeout: int = 600,
    ) -> dict[str, Any]:
        """Call local Colibri Heavy MoE `/v1/chat/completions` on 127.0.0.1:11435."""
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": float(temperature),
            "max_tokens": int(max_tokens),
            "stream": False,
        }

        t0 = time.monotonic()
        ok, status, data, err = self._http_request(
            self.heavy_host,
            self.heavy_port,
            "POST",
            "/v1/chat/completions",
            body=payload,
            timeout=float(timeout),
        )
        duration = round(time.monotonic() - t0, 3)

        if not ok or not isinstance(data, dict):
            raise TierInferenceError(
                f"Colibri /v1/chat/completions failed (status={status}): {err or 'invalid JSON response'}"
            )

        choices = data.get("choices") if isinstance(data.get("choices"), list) else []
        first = choices[0] if choices and isinstance(choices[0], dict) else {}
        msg = first.get("message") if isinstance(first.get("message"), dict) else {}
        content = str(msg.get("content") or "")
        thinking = str(msg.get("reasoning_content") or "")
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        return {
            "ok": True,
            "tier": "heavy",
            "model": str(data.get("model") or model),
            "content": content,
            "thinking": thinking,
            "prompt_tokens": int(usage.get("prompt_tokens") or 0),
            "completion_tokens": int(usage.get("completion_tokens") or 0),
            "duration_sec": duration,
            "raw": data,
        }
