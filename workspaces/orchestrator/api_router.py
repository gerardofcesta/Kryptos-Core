"""
Orchestrator HTTP API Router — 100% Python stdlib, Loopback-Only.
Espone le route /api/orchestrator/* integrate nel server della Console Offline (:8787)
wrappando in modo sicuro e lazy l'OrchestratorManager di `workspaces.orchestrator`.
"""

from __future__ import annotations

import os
import pathlib
import re
import shutil
import subprocess
import sys
import urllib.parse
from typing import Any

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
API_PREFIX = "/api/orchestrator"
RUN_ID_RE = re.compile(r"^[a-zA-Z0-9._-]{1,128}$")

_ACTIVE_STATES = frozenset(
    {
        "STARTING",
        "RUNNING",
        "SCOUT",
        "STRATEGIST",
        "INVESTIGATOR",
        "VALIDATOR",
        "ADJUDICATOR",
        "PAUSED",
    }
)


def _ensure_project_root_in_sys_path() -> None:
    root_str = str(PROJECT_ROOT)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)


def _get_manager() -> tuple[Any | None, str | None]:
    """Importa e restituisce lazy l'istanza singleton di OrchestratorManager."""
    _ensure_project_root_in_sys_path()
    try:
        from workspaces.orchestrator import get_orchestrator_manager

        mgr = get_orchestrator_manager()
        return mgr, None
    except Exception as exc:
        return None, f"Modulo Orchestrator non disponibile: {exc}"


def _normalize_result(result: Any, default_code: int = 200) -> tuple[int, dict[str, Any]]:
    """Normalizza il valore di ritorno del manager in una tupla (http_status, dict)."""
    if (
        isinstance(result, tuple)
        and len(result) == 2
        and isinstance(result[0], int)
        and isinstance(result[1], dict)
    ):
        return result[0], result[1]
    if isinstance(result, dict):
        raw_code = result.get("status_code") or result.get("http_status")
        if isinstance(raw_code, int) and 100 <= raw_code <= 599:
            return raw_code, result
        if result.get("ok") is False and default_code == 200:
            return 400, result
        return default_code, result
    return default_code, {"ok": True, "data": result}


def _validate_run_id(raw_id: str) -> str | None:
    run_id = urllib.parse.unquote(raw_id).strip()
    if not run_id or run_id in (".", "..") or ".." in run_id:
        return None
    if not RUN_ID_RE.match(run_id):
        return None
    return run_id


def _run_orchestrator_preflight() -> dict[str, Any]:
    """Verifica prerequisiti locali per Orchestrator (Docker daemon, immagine sandbox, RAM)."""
    docker_exe = shutil.which("docker")
    docker_cli = bool(docker_exe)
    docker_daemon = False
    sandbox_image_ready = False
    warnings: list[str] = []

    sub_kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "timeout": 2.5,
        "shell": False,
    }
    if sys.platform == "win32" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        sub_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

    if docker_exe:
        try:
            info_proc = subprocess.run([docker_exe, "info", "--format", "{{.ServerVersion}}"], **sub_kwargs)
            docker_daemon = info_proc.returncode == 0
        except Exception:
            docker_daemon = False
        if docker_daemon:
            try:
                img_proc = subprocess.run(
                    [docker_exe, "image", "inspect", "local-llm-sandbox:offline"], **sub_kwargs
                )
                sandbox_image_ready = img_proc.returncode == 0
            except Exception:
                sandbox_image_ready = False
        else:
            warnings.append("Docker daemon non attivo: avvia Docker Desktop per eseguire la sandbox isolata.")
    else:
        warnings.append("Docker CLI non trovata nel PATH.")

    if docker_daemon and not sandbox_image_ready:
        warnings.append("Immagine 'local-llm-sandbox:offline' non trovata: esegui workspaces/orchestrator/sandbox/build-sandbox.ps1.")

    free_ram_gb: float | None = None
    try:
        from workspaces.orchestrator.engine.tier_switcher import TierSwitcher

        free_ram_gb = TierSwitcher.get_free_ram_gb()
    except Exception:
        free_ram_gb = None

    try:
        min_heavy_ram = float(os.environ.get("HEAVY_MOE_MIN_FREE_RAM_GB", "14.0"))
    except ValueError:
        min_heavy_ram = 14.0
    heavy_ready = free_ram_gb is None or free_ram_gb >= min_heavy_ram
    if not heavy_ready:
        warnings.append(
            f"RAM fisica libera ({free_ram_gb} GiB) sotto soglia Heavy MoE ({min_heavy_ram} GiB)."
        )

    return {
        "ok": True,
        "dockerCli": docker_cli,
        "dockerDaemon": docker_daemon,
        "sandboxImage": "local-llm-sandbox:offline",
        "sandboxImageReady": sandbox_image_ready,
        "freeRamGb": free_ram_gb,
        "minHeavyRamGb": min_heavy_ram,
        "heavyReady": heavy_ready,
        "readyForSandbox": docker_daemon and sandbox_image_ready,
        "hostReadOnlyReady": True,
        "recommendedExecutionMode": "docker" if (docker_daemon and sandbox_image_ready) else "host_readonly",
        "warnings": warnings,
    }


def handle_orchestrator_request(
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]] | None:
    """
    Gestisce tutte le route `/api/orchestrator/*`.
    Restituisce `(status_code, response_dict)` oppure `None` se `path` non appartiene al router.
    """
    clean_path = urllib.parse.urlsplit(path).path
    if clean_path != API_PREFIX and not clean_path.startswith(API_PREFIX + "/"):
        return None

    verb = method.upper().strip()
    norm_path = clean_path.rstrip("/") or "/"
    body = payload if isinstance(payload, dict) else {}

    # GET /api/orchestrator/preflight (non richiede l'inizializzazione completa del manager)
    if norm_path == "/api/orchestrator/preflight":
        if verb != "GET":
            return 405, {"ok": False, "error": f"Metodo {verb} non consentito su {norm_path}"}
        return 200, _run_orchestrator_preflight()

    # POST /api/orchestrator/code-audit (scan deterministico Anti-Slopsquatting + Secret Scan)
    if norm_path == "/api/orchestrator/code-audit":
        if verb != "POST":
            return 405, {"ok": False, "error": f"Metodo {verb} non consentito su {norm_path}"}
        from workspaces.orchestrator.engine.code_audit import run_code_audit
        from workspaces.orchestrator.engine.schemas import SchemaValidationError

        try:
            res = run_code_audit(
                body.get("target_path", ""),
                check_secrets=bool(body.get("check_secrets", True)),
                check_slopsquatting=bool(body.get("check_slopsquatting", True)),
                use_gitleaks=bool(body.get("use_gitleaks", True)),
            )
            return 200, res
        except SchemaValidationError as exc:
            return exc.http_status, {"ok": False, "error": exc.message}
        except PermissionError as exc:
            return 403, {"ok": False, "error": str(exc)}
        except ValueError as exc:
            return 400, {"ok": False, "error": str(exc)}

    mgr, err = _get_manager()
    if mgr is None:
        return 503, {
            "ok": False,
            "available": False,
            "state": "UNAVAILABLE",
            "error": err or "OrchestratorManager non disponibile.",
        }

    try:
        # GET /api/orchestrator/status
        if norm_path == "/api/orchestrator/status":
            if verb != "GET":
                return 405, {"ok": False, "error": f"Metodo {verb} non consentito su {norm_path}"}
            res = mgr.get_status()
            if isinstance(res, dict):
                return 200, res
            return _normalize_result(res, 200)

        # POST /api/orchestrator/start
        if norm_path == "/api/orchestrator/start":
            if verb != "POST":
                return 405, {"ok": False, "error": f"Metodo {verb} non consentito su {norm_path}"}
            return _normalize_result(mgr.start_run(body), 200)

        # POST /api/orchestrator/pause
        if norm_path == "/api/orchestrator/pause":
            if verb != "POST":
                return 405, {"ok": False, "error": f"Metodo {verb} non consentito su {norm_path}"}
            return _normalize_result(mgr.pause_run(), 200)

        # POST /api/orchestrator/resume
        if norm_path == "/api/orchestrator/resume":
            if verb != "POST":
                return 405, {"ok": False, "error": f"Metodo {verb} non consentito su {norm_path}"}
            run_id_arg = body.get("run_id") or body.get("runId")
            if run_id_arg is not None:
                run_id_str = _validate_run_id(str(run_id_arg))
                if run_id_str is None:
                    return 400, {"ok": False, "error": "run_id non valido per la ripresa."}
                run_id_arg = run_id_str
            return _normalize_result(mgr.resume_run(run_id_arg), 200)

        # POST /api/orchestrator/stop
        if norm_path == "/api/orchestrator/stop":
            if verb != "POST":
                return 405, {"ok": False, "error": f"Metodo {verb} non consentito su {norm_path}"}
            return _normalize_result(mgr.stop_run(), 200)

        # GET /api/orchestrator/report
        if norm_path == "/api/orchestrator/report":
            if verb != "GET":
                return 405, {"ok": False, "error": f"Metodo {verb} non consentito su {norm_path}"}
            return _normalize_result(mgr.get_report(), 200)

        # POST /api/orchestrator/purge-report
        if norm_path == "/api/orchestrator/purge-report":
            if verb != "POST":
                return 405, {"ok": False, "error": f"Metodo {verb} non consentito su {norm_path}"}
            return _normalize_result(mgr.purge_report(), 200)

        # GET /api/orchestrator/vaults
        if norm_path == "/api/orchestrator/vaults":
            if verb != "GET":
                return 405, {"ok": False, "error": f"Metodo {verb} non consentito su {norm_path}"}
            vaults_res = mgr.list_vaults()
            if isinstance(vaults_res, tuple):
                return _normalize_result(vaults_res, 200)
            if isinstance(vaults_res, dict):
                if "ok" not in vaults_res:
                    vaults_res = {"ok": True, **vaults_res}
                return 200, vaults_res
            return 200, {"ok": True, "vaults": list(vaults_res) if vaults_res is not None else []}

        # GET|DELETE /api/orchestrator/vaults/<run_id>
        vault_prefix = "/api/orchestrator/vaults/"
        if norm_path.startswith(vault_prefix):
            raw_id = norm_path[len(vault_prefix) :]
            if "/" in raw_id:
                return 404, {"ok": False, "error": f"Endpoint orchestrator non trovato: {norm_path}"}
            run_id = _validate_run_id(raw_id)
            if run_id is None:
                return 400, {"ok": False, "error": "run_id non valido."}
            if verb == "GET":
                return _normalize_result(mgr.get_vault_detail(run_id), 200)
            if verb == "DELETE":
                return _normalize_result(mgr.delete_vault(run_id), 200)
            return 405, {"ok": False, "error": f"Metodo {verb} non consentito su {norm_path}"}

        return 404, {"ok": False, "error": f"Endpoint orchestrator non trovato: {norm_path}"}
    except ValueError as exc:
        code = getattr(exc, "http_status", 400)
        if not isinstance(code, int) or not (400 <= code <= 599):
            code = 400
        return code, {"ok": False, "error": str(exc)}
    except PermissionError as exc:
        return 403, {"ok": False, "error": str(exc)}
    except Exception as exc:
        return 500, {"ok": False, "error": f"Errore interno Orchestrator: {exc}"}


def get_orchestrator_telemetry_summary() -> dict[str, Any]:
    """Restituisce un riepilogo compatto per `/api/system/telemetry` e System Tray."""
    mgr, err = _get_manager()
    if mgr is None:
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
            "error": err,
        }

    try:
        st = mgr.get_status()
        if isinstance(st, tuple) and len(st) == 2 and isinstance(st[1], dict):
            st = st[1]
        if not isinstance(st, dict):
            st = {}

        state_val = str(st.get("state") or "IDLE").upper()
        active_val = bool(st["active"]) if "active" in st else (state_val in _ACTIVE_STATES)
        retention_val = st.get("retention_mode") if "retention_mode" in st else st.get("retentionMode")
        has_report = bool(st["has_report"]) if "has_report" in st else bool(st.get("hasReport", False))

        budget = st.get("budget") if isinstance(st.get("budget"), dict) else {}
        step_val = int(st.get("step") or st.get("current_step") or 0)
        max_steps_val = int(
            st.get("max_steps") or st.get("maxSteps") or budget.get("max_steps") or 0
        )

        auto_purge_sec = (
            st.get("auto_purge_remaining_sec")
            if "auto_purge_remaining_sec" in st
            else st.get("autoPurgeRemainingSec")
        )

        return {
            "available": True,
            "state": state_val,
            "active": active_val,
            "retentionMode": retention_val,
            "hasReport": has_report,
            "step": step_val,
            "maxSteps": max_steps_val,
            "runId": st.get("run_id") or st.get("runId"),
            "verdict": st.get("verdict"),
            "activeEngine": st.get("active_engine") or st.get("activeEngine"),
            "autoPurgeRemainingSec": auto_purge_sec,
        }
    except Exception as exc:
        return {
            "available": False,
            "state": "ERROR",
            "active": False,
            "retentionMode": None,
            "hasReport": False,
            "step": 0,
            "maxSteps": 0,
            "runId": None,
            "verdict": None,
            "activeEngine": None,
            "autoPurgeRemainingSec": None,
            "error": str(exc),
        }


def shutdown_orchestrator() -> None:
    """Arresta in sicurezza qualsiasi run/container/buffer RAM dell'Orchestrator."""
    mgr, _ = _get_manager()
    if mgr is None:
        return
    try:
        cleanup = getattr(mgr, "shutdown_cleanup", None)
        if callable(cleanup):
            cleanup()
    except Exception:
        pass
