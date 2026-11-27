"""Unified EngineCoordinator & Tier Switcher integration for Local LLM Gateway."""

from __future__ import annotations

import asyncio
import contextlib
import os
import pathlib
import shutil
import subprocess
import sys
import threading
import weakref
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

from ..config import (
    ALLOWED_ENGINE_ACTIONS,
    HEAVY_HOST,
    HEAVY_PORT,
    MODEL_NAME_RE,
    OLLAMA_HOST,
    OLLAMA_PORT,
    PROJECT_ROOT,
    SCRIPTS_DIR,
    SETTINGS,
)
from ..services.telemetry import (
    _http_json,
    get_orchestrator_telemetry_summary,
    get_ram_telemetry,
    invalidate_telemetry_cache,
    read_active_heavy_model,
)
from .backends.colibri import ColibriBackend
from .backends.llamacpp import LlamaCppBackend
from .backends.lmstudio import LmStudioBackend
from .backends.ollama import OllamaBackend

_FALLBACK_ENGINE_LOCK = threading.RLock()

try:
    from workspaces.orchestrator.engine.tier_switcher import (
        TierSwitcher,
    )
    from workspaces.orchestrator.engine.tier_switcher import (
        get_engine_coordinator as _get_orch_engine_coordinator,
    )
except Exception:
    TierSwitcher = None  # type: ignore[assignment]
    _get_orch_engine_coordinator = None


def _shutdown_orchestrator_safe() -> None:
    try:
        from workspaces.orchestrator.api_router import shutdown_orchestrator

        shutdown_orchestrator()
    except Exception:
        pass


def _resolve_pwsh() -> str | None:
    found = shutil.which("pwsh")
    if found:
        return found
    default_pwsh = pathlib.Path(r"C:\Program Files\PowerShell\7\pwsh.exe")
    if default_pwsh.is_file():
        return str(default_pwsh)
    return shutil.which("powershell")


def _clamp_int(value: Any, default: int, min_val: int, max_val: int) -> int:
    try:
        num = int(value)
    except (TypeError, ValueError):
        return default
    return max(min_val, min(max_val, num))


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


@contextlib.contextmanager
def acquire_engine_lock(timeout: float = 120.0) -> Iterator[None]:
    """Acquire the process/thread-unified EngineCoordinator lock."""
    if _get_orch_engine_coordinator is not None:
        with _get_orch_engine_coordinator().acquire(timeout=timeout):
            yield
    else:
        with _FALLBACK_ENGINE_LOCK:
            yield


class GatewayEngineError(RuntimeError):
    """Structured HTTP-mappable error raised by `GatewayEngineCoordinator`."""

    def __init__(
        self,
        status_code: int,
        message: str,
        *,
        error_type: str = "engine_error",
        code: str = "engine_error",
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.error_type = error_type
        self.code = code
        self.retry_after = retry_after


@dataclass(frozen=True)
class ResolvedModelSpec:
    """Resolved target backend and concrete model for a requested model/alias."""

    requested_model: str
    alias: str
    tier: str
    backend_name: str
    concrete_model: str


class GatewayEngineCoordinator:
    """
    Unified coordinator managing exclusive Fast/Heavy tier execution,
    OpenAI alias resolution, anti-swap guards, and lifecycle control actions.
    """

    def __init__(self) -> None:
        self.ollama = OllamaBackend()
        self.colibri = ColibriBackend()
        self.llamacpp = LlamaCppBackend()
        self.lmstudio = LmStudioBackend()
        self._loop_locks: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = (
            weakref.WeakKeyDictionary()
        )
        self._tuning_lock = threading.Lock()
        self._fast_num_ctx: int = self._initial_fast_num_ctx()
        self._heavy_ram_limit_gb: int | None = None
        self._heavy_ctx: int | None = None

    @staticmethod
    def _initial_fast_num_ctx() -> int:
        for env_key in ("LOCAL_FAST_NUM_CTX", "OLLAMA_CONTEXT_LENGTH", "OLLAMA_NUM_CTX"):
            raw = os.environ.get(env_key, "").strip()
            if raw:
                return _clamp_int(raw, 16384, 1024, 131072)
        return 16384

    def get_fast_num_ctx(self) -> int:
        """Return the active Fast-tier (Ollama) `num_ctx` setting."""
        with self._tuning_lock:
            return self._fast_num_ctx

    def set_fast_num_ctx(self, num_ctx: int) -> int:
        """Persist the active Fast-tier (Ollama) `num_ctx` setting."""
        clamped = _clamp_int(num_ctx, 16384, 1024, 131072)
        with self._tuning_lock:
            self._fast_num_ctx = clamped
        return clamped

    def set_heavy_tuning(self, *, ram_limit_gb: int | None = None, ctx: int | None = None) -> None:
        """Record the latest Heavy MoE RAM limit and context length."""
        with self._tuning_lock:
            if ram_limit_gb is not None:
                self._heavy_ram_limit_gb = _clamp_int(ram_limit_gb, 12, 4, 256)
            if ctx is not None:
                self._heavy_ctx = _clamp_int(ctx, 16384, 1024, 131072)

    def get_tuning_state(self) -> dict[str, int | None]:
        """Return currently persisted Fast/Heavy tuning parameters."""
        with self._tuning_lock:
            return {
                "fast_num_ctx": self._fast_num_ctx,
                "heavy_ram_limit_gb": self._heavy_ram_limit_gb,
                "heavy_ctx": self._heavy_ctx,
            }

    def _get_async_lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        lock = self._loop_locks.get(loop)
        if lock is None:
            lock = asyncio.Lock()
            self._loop_locks[loop] = lock
        return lock

    def get_heavy_model_id(self) -> str:
        meta = read_active_heavy_model()
        if meta:
            for key in ("modelId", "model", "name", "id"):
                val = meta.get(key)
                if isinstance(val, str) and val.strip():
                    return val.strip()
        return os.environ.get("HEAVY_MOE_MODEL_ID", SETTINGS.heavy_model)

    def resolve_model(self, requested_model: str) -> ResolvedModelSpec:
        """Resolve canonical virtual alias (`auto`, `local-fast`, `local-balanced`, `local-heavy`) or concrete ID."""
        raw = (requested_model or "").strip()
        if not raw or not MODEL_NAME_RE.match(raw):
            raise GatewayEngineError(
                400,
                f"Invalid model identifier: {requested_model!r}",
                error_type="invalid_request_error",
                code="invalid_model",
            )

        lowered = raw.lower()
        if lowered == "auto":
            return ResolvedModelSpec(
                requested_model=raw,
                alias="auto",
                tier="fast",
                backend_name="ollama",
                concrete_model=os.environ.get("LOCAL_FAST_MODEL", SETTINGS.fast_model),
            )
        if lowered == "local-fast":
            return ResolvedModelSpec(
                requested_model=raw,
                alias="local-fast",
                tier="fast",
                backend_name="ollama",
                concrete_model=os.environ.get("LOCAL_FAST_MODEL", SETTINGS.fast_model),
            )
        if lowered == "local-balanced":
            return ResolvedModelSpec(
                requested_model=raw,
                alias="local-balanced",
                tier="fast",
                backend_name="ollama",
                concrete_model=os.environ.get("LOCAL_BALANCED_MODEL", SETTINGS.balanced_model),
            )
        if lowered == "local-heavy":
            return ResolvedModelSpec(
                requested_model=raw,
                alias="local-heavy",
                tier="heavy",
                backend_name="colibri",
                concrete_model=self.get_heavy_model_id(),
            )

        heavy_id = self.get_heavy_model_id()
        if raw == heavy_id or lowered in ("heavy", "heavy-moe", "colibri"):
            return ResolvedModelSpec(
                requested_model=raw,
                alias="local-heavy",
                tier="heavy",
                backend_name="colibri",
                concrete_model=heavy_id if lowered in ("heavy", "heavy-moe", "colibri") else raw,
            )

        return ResolvedModelSpec(
            requested_model=raw,
            alias=raw,
            tier="fast",
            backend_name="ollama",
            concrete_model=raw,
        )

    def get_backend(self, backend_name: str):
        mapping = {
            "ollama": self.ollama,
            "colibri": self.colibri,
            "llamacpp": self.llamacpp,
            "lmstudio": self.lmstudio,
        }
        return mapping.get(backend_name, self.ollama)

    async def get_active_tier(self) -> str:
        """Probe Fast and Heavy backends concurrently to determine active tier."""
        heavy_ok, fast_ok = await asyncio.gather(self.colibri.health(), self.ollama.health())
        if heavy_ok:
            return "heavy"
        if fast_ok:
            return "fast"
        return "none"

    def _switch_tier_sync(self, target_tier: str) -> tuple[bool, str]:
        if TierSwitcher is None:
            return False, "TierSwitcher module unavailable."
        switcher = TierSwitcher(
            project_root=PROJECT_ROOT,
            ollama_host=OLLAMA_HOST,
            ollama_port=OLLAMA_PORT,
            heavy_host=HEAVY_HOST,
            heavy_port=HEAVY_PORT,
        )
        return switcher.ensure_tier(
            target_tier=target_tier,
            retention_mode="ephemeral",
            allow_kv_save=False,
        )

    async def ensure_tier(
        self,
        target_tier: str,
        *,
        allow_tier_switch: bool = False,
        orch_summary_fn: Callable[[], dict[str, Any]] | None = None,
    ) -> bool:
        """
        Ensure `target_tier` ('fast' or 'heavy') is active.
        Returns True if a cold-swap/tier switch was performed, False if already online.
        """
        active = await self.get_active_tier()
        if active == target_tier:
            return False

        orch_fn = orch_summary_fn or get_orchestrator_telemetry_summary
        if orch_fn().get("active"):
            raise GatewayEngineError(
                503,
                "Orchestrator run is active; automatic tier switching is temporarily blocked.",
                error_type="server_error",
                code="orchestrator_active",
                retry_after=30,
            )

        if not allow_tier_switch:
            if target_tier == "heavy" or active in ("fast", "heavy"):
                raise GatewayEngineError(
                    409,
                    (
                        f"Automatic tier switch from '{active}' to '{target_tier}' is disabled. "
                        "Pass header 'X-Allow-Tier-Switch: true' to authorize cold-swap."
                    ),
                    error_type="conflict_error",
                    code="tier_switch_not_allowed",
                )
            raise GatewayEngineError(
                503,
                (
                    f"Target tier '{target_tier}' is offline. Start it manually or pass "
                    "header 'X-Allow-Tier-Switch: true'."
                ),
                error_type="service_unavailable",
                code="backend_offline",
                retry_after=5,
            )

        if target_tier == "heavy":
            ram = get_ram_telemetry()
            free_gb = float(ram.get("freeGb") or 0.0)
            if free_gb > 0.0 and free_gb < 10.0:
                raise GatewayEngineError(
                    503,
                    f"Anti-swap guard: free RAM ({free_gb:.2f} GiB) below critical 10.0 GiB threshold.",
                    error_type="insufficient_resources",
                    code="anti_swap_guard",
                    retry_after=15,
                )

        async with self._get_async_lock():
            active_now = await self.get_active_tier()
            if active_now == target_tier:
                return False
            if orch_fn().get("active"):
                raise GatewayEngineError(
                    503,
                    "Orchestrator run is active; cannot switch tier.",
                    error_type="server_error",
                    code="orchestrator_active",
                    retry_after=30,
                )
            ok, msg = await asyncio.to_thread(self._switch_tier_sync, target_tier)
            invalidate_telemetry_cache()
            if not ok:
                raise GatewayEngineError(
                    503,
                    f"Failed switching to tier '{target_tier}': {msg}",
                    error_type="service_unavailable",
                    code="tier_switch_failed",
                    retry_after=10,
                )
            return True


def execute_engine_action(
    payload: dict[str, Any],
    orch_summary_fn: Callable[[], dict[str, Any]] | None = None,
) -> tuple[int, dict[str, Any]]:
    """Execute explicit engine control action (`start_fast`, `start_heavy`, `stop_all`, `unload_vram`, `shutdown_app`)."""
    action = str(payload.get("action") or "").strip()
    if action not in ALLOWED_ENGINE_ACTIONS:
        return 400, {"ok": False, "error": f"Azione non consentita: {action}"}

    force_override = bool(payload.get("force"))
    summary_fn = orch_summary_fn or get_orchestrator_telemetry_summary
    if action != "shutdown_app" and not force_override:
        orch_summary = summary_fn()
        if orch_summary.get("active"):
            return 409, {
                "ok": False,
                "error": "Conflitto (409): una run Orchestrator è attualmente attiva. Usa force=true solo in emergenza.",
            }

    try:
        with acquire_engine_lock():
            res = _execute_engine_action_locked(action, payload)
        invalidate_telemetry_cache()
        return res
    except TimeoutError as exc:
        return 409, {"ok": False, "error": f"Lock EngineCoordinator occupato: {exc}"}


def _should_use_python_runner(pwsh_exe: str | None, target_script: pathlib.Path) -> bool:
    return (
        sys.platform != "win32"
        or os.environ.get("LOCAL_LLM_USE_PYTHON_RUNNER") == "1"
        or not pwsh_exe
        or not target_script.is_file()
    )


def _execute_engine_action_locked(action: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    if action == "unload_vram":
        ollama_exe = shutil.which("ollama")
        if not ollama_exe:
            return 500, {"ok": False, "output": "Comando 'ollama' non trovato nel PATH."}

        active_names: list[str] = []
        ps_ok, _, ps_data, _ = _http_json(OLLAMA_HOST, OLLAMA_PORT, "GET", "/api/ps", timeout=1.5)
        if ps_ok and isinstance(ps_data, dict):
            for m in ps_data.get("models", []):
                name = str(m.get("name") or "").strip()
                if name and MODEL_NAME_RE.match(name) and not name.startswith("-"):
                    active_names.append(name)
        else:
            try:
                ps_proc = subprocess.run(
                    [ollama_exe, "ps"],
                    timeout=5,
                    shell=False,
                    **_subprocess_kwargs(),
                )
                if ps_proc.returncode == 0:
                    for line in ps_proc.stdout.splitlines()[1:]:
                        parts = line.split()
                        if parts:
                            name = parts[0].strip()
                            if MODEL_NAME_RE.match(name) and not name.startswith("-"):
                                active_names.append(name)
            except Exception as exc:
                return 500, {"ok": False, "output": f"Errore interrogazione ollama ps: {exc}"}

        if not active_names:
            return 200, {
                "ok": True,
                "unloaded": [],
                "output": "Nessun modello attivo in VRAM su Ollama.",
            }

        outputs: list[str] = []
        unloaded: list[str] = []
        all_ok = True
        for name in active_names:
            try:
                stop_proc = subprocess.run(
                    [ollama_exe, "stop", name],
                    timeout=15,
                    shell=False,
                    **_subprocess_kwargs(),
                )
                msg = (stop_proc.stdout or stop_proc.stderr or "").strip()
                if stop_proc.returncode == 0:
                    unloaded.append(name)
                    outputs.append(f"[OK] Unload {name}" + (f": {msg}" if msg else ""))
                else:
                    all_ok = False
                    outputs.append(f"[ERR] Unload {name}: {msg or stop_proc.returncode}")
            except Exception as exc:
                all_ok = False
                outputs.append(f"[ERR] Unload {name}: {exc}")

        return 200, {"ok": all_ok, "unloaded": unloaded, "output": "\n".join(outputs)}

    restart = bool(payload.get("restart"))
    applied_fast_ctx: int | None = None
    applied_heavy_ram: int | None = None
    applied_heavy_ctx: int | None = None

    if action == "start_fast" and "ctx" in payload and payload["ctx"] is not None:
        applied_fast_ctx = _clamp_int(payload.get("ctx"), 16384, 1024, 131072)
    elif action == "start_heavy":
        raw_ram = payload.get("ramLimitGb")
        if raw_ram is None:
            raw_ram = payload.get("ram_limit_gb")
        applied_heavy_ram = _clamp_int(raw_ram, 12, 4, 256)
        applied_heavy_ctx = _clamp_int(payload.get("ctx"), 16384, 1024, 131072)

    pwsh_exe = _resolve_pwsh()
    target_script = (
        SCRIPTS_DIR / "stop-llm.ps1"
        if action in ("shutdown_app", "stop_all")
        else SCRIPTS_DIR / "start-llm.ps1"
    )
    use_python_runner = _should_use_python_runner(pwsh_exe, target_script)

    if use_python_runner:
        from ..engine_runner import get_python_engine_runner

        runner = get_python_engine_runner()
        if action == "shutdown_app":
            _shutdown_orchestrator_safe()
            _, stop_res = runner.stop_all()
            return 200, {
                "ok": True,
                "runner": "python",
                "output": f"Arresto completo avviato (pure-Python runner): {stop_res.get('output', '')}",
            }
        if action == "stop_all":
            status_code, res = runner.stop_all()
            res["runner"] = "python"
            return status_code, res
        if action == "start_fast":
            status_code, res = runner.start_fast(ctx=applied_fast_ctx, restart=restart)
            res["runner"] = "python"
            if res.get("ok") and applied_fast_ctx is not None:
                get_gateway_coordinator().set_fast_num_ctx(applied_fast_ctx)
                res["ctx"] = applied_fast_ctx
            return status_code, res
        status_code, res = runner.start_heavy(
            ram_limit_gb=applied_heavy_ram or 12,
            ctx=applied_heavy_ctx or 16384,
            restart=restart,
        )
        res["runner"] = "python"
        if res.get("ok"):
            get_gateway_coordinator().set_heavy_tuning(
                ram_limit_gb=applied_heavy_ram, ctx=applied_heavy_ctx
            )
        return status_code, res

    if action == "shutdown_app":
        _shutdown_orchestrator_safe()
        shutdown_cmd = [pwsh_exe, "-NoProfile", "-File", str(target_script)]

        def _delayed_shutdown() -> None:
            import time

            time.sleep(0.35)
            try:
                subprocess.Popen(
                    shutdown_cmd,
                    cwd=str(PROJECT_ROOT),
                    shell=False,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=(
                        subprocess.CREATE_NO_WINDOW
                        if sys.platform == "win32" and hasattr(subprocess, "CREATE_NO_WINDOW")
                        else 0
                    ),
                )
            except Exception:
                pass

        threading.Thread(target=_delayed_shutdown, daemon=True).start()
        return 200, {
            "ok": True,
            "output": "Arresto completo avviato: chiusura Orchestrator/RAM, motori, Tray, finestra App e liberazione VRAM/RAM.",
        }

    if action == "stop_all":
        cmd = [pwsh_exe, "-NoProfile", "-File", str(target_script), "-KeepUI"]
    elif action == "start_fast":
        cmd = [pwsh_exe, "-NoProfile", "-File", str(target_script), "-Mode", "fast"]
        if applied_fast_ctx is not None:
            cmd.extend(["-Ctx", str(applied_fast_ctx)])
        if restart:
            cmd.append("-Restart")
    else:
        cmd = [
            pwsh_exe,
            "-NoProfile",
            "-File",
            str(target_script),
            "-Mode",
            "heavy",
            "-RamLimitGB",
            str(applied_heavy_ram),
            "-Ctx",
            str(applied_heavy_ctx),
        ]
        if restart:
            cmd.append("-Restart")

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(PROJECT_ROOT),
            timeout=110,
            shell=False,
            **_subprocess_kwargs(),
        )
        combined = "\n".join(
            part for part in ((proc.stdout or "").strip(), (proc.stderr or "").strip()) if part
        )
        ok = proc.returncode == 0
        if ok:
            coord = get_gateway_coordinator()
            if action == "start_fast" and applied_fast_ctx is not None:
                coord.set_fast_num_ctx(applied_fast_ctx)
            elif action == "start_heavy":
                coord.set_heavy_tuning(ram_limit_gb=applied_heavy_ram, ctx=applied_heavy_ctx)
        res_payload: dict[str, Any] = {
            "ok": ok,
            "exitCode": proc.returncode,
            "restarted": restart,
            "output": combined or f"Comando '{action}' completato (exit {proc.returncode}).",
        }
        if applied_fast_ctx is not None:
            res_payload["ctx"] = applied_fast_ctx
        if applied_heavy_ram is not None:
            res_payload["ramLimitGb"] = applied_heavy_ram
            res_payload["ctx"] = applied_heavy_ctx
        return 200, res_payload
    except subprocess.TimeoutExpired as exc:
        out_txt = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        err_txt = (exc.stderr or "") if isinstance(exc.stderr, str) else ""
        return 504, {
            "ok": False,
            "output": f"Timeout esecuzione '{action}' (>110s).\n{out_txt}\n{err_txt}".strip(),
        }
    except Exception as exc:
        return 500, {"ok": False, "output": f"Errore avvio comando '{action}': {exc}"}


_GATEWAY_COORDINATOR_SINGLETON = GatewayEngineCoordinator()


def get_gateway_coordinator() -> GatewayEngineCoordinator:
    """Return the process-wide `GatewayEngineCoordinator` singleton."""
    return _GATEWAY_COORDINATOR_SINGLETON
