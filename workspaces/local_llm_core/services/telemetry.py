"""Hardware, backend and registry telemetry collector with 1.5s TTL cache."""

from __future__ import annotations

import asyncio
import concurrent.futures
import ctypes
import http.client
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from typing import Any

from ..config import (
    BENCHMARKS_RESULTS_FILE,
    HEAVY_BASE,
    HEAVY_HOST,
    HEAVY_MOE_DIR,
    HEAVY_PORT,
    LMS_BASE,
    LMS_HOST,
    LMS_PORT,
    OLLAMA_BASE,
    OLLAMA_HOST,
    OLLAMA_PORT,
    REGISTRY_MD_FILE,
)

_TELEMETRY_TTL_SEC = 1.5
_TELEMETRY_CACHE_LOCK = threading.Lock()
_TELEMETRY_CACHE_TS = 0.0
_TELEMETRY_CACHE_DATA: dict[str, Any] | None = None


def get_orchestrator_telemetry_summary() -> dict[str, Any]:
    """Safely read Orchestrator telemetry summary via lazy import."""
    try:
        from workspaces.orchestrator.api_router import (
            get_orchestrator_telemetry_summary as _orch_summary,
        )

        return _orch_summary()
    except Exception:
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


def _http_json(
    host: str, port: int, method: str, path: str, timeout: float = 0.9
) -> tuple[bool, int, Any, str | None]:
    """Perform a short loopback-only HTTP JSON probe using stdlib `http.client`."""
    if host not in ("127.0.0.1", "localhost", "::1"):
        return False, 0, None, f"Blocked non-loopback host: {host}"
    conn: http.client.HTTPConnection | None = None
    try:
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
        conn.request(method, path, headers={"Accept": "application/json"})
        resp = conn.getresponse()
        raw = resp.read()
        text = raw.decode("utf-8", errors="replace")
        if 200 <= resp.status < 300:
            try:
                return True, resp.status, json.loads(text) if text.strip() else {}, None
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


def _parse_float(value: str) -> float | None:
    s = value.strip()
    if not s or s.upper() in ("N/A", "[N/A]", "[NOT SUPPORTED]", "NONE"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


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


def get_gpu_telemetry() -> dict[str, Any]:
    """Read local GPU telemetry via `nvidia-smi` with safe fallback."""
    fallback = {
        "available": False,
        "name": None,
        "memoryUsedMb": 0.0,
        "memoryFreeMb": 0.0,
        "memoryTotalMb": 0.0,
        "memoryUsedGb": 0.0,
        "memoryFreeGb": 0.0,
        "memoryTotalGb": 0.0,
        "memoryUsedPct": 0.0,
        "temperatureC": None,
        "powerDrawW": None,
        "utilizationPct": None,
        "vramUsedMb": 0.0,
        "vramFreeMb": 0.0,
        "vramTotalMb": 0.0,
        "vramUsedGb": 0.0,
        "vramFreeGb": 0.0,
        "vramTotalGb": 0.0,
        "vramUsedPct": 0.0,
        "tempC": None,
        "powerW": None,
        "utilPct": None,
    }
    nvidia_smi = shutil.which("nvidia-smi")
    if not nvidia_smi:
        try:
            from ..engine_runner import detect_hardware_profile

            hw = detect_hardware_profile()
            if hw.get("gpu_available"):
                total_mb = float(hw.get("vram_total_mb") or 0.0)
                used_mb = float(hw.get("vram_used_mb") or 0.0)
                free_mb = float(hw.get("vram_free_mb") or 0.0)
                used_pct = round((used_mb / total_mb) * 100.0, 1) if total_mb > 0 else 0.0
                used_gb = float(hw.get("vram_used_gb") or 0.0)
                free_gb = float(hw.get("vram_free_gb") or 0.0)
                total_gb = float(hw.get("vram_total_gb") or 0.0)
                return {
                    "available": True,
                    "backend": hw.get("gpu_backend"),
                    "unifiedMemory": bool(hw.get("unified_memory", False)),
                    "name": hw.get("gpu_name"),
                    "memoryUsedMb": used_mb,
                    "memoryFreeMb": free_mb,
                    "memoryTotalMb": total_mb,
                    "memoryUsedGb": used_gb,
                    "memoryFreeGb": free_gb,
                    "memoryTotalGb": total_gb,
                    "memoryUsedPct": used_pct,
                    "temperatureC": hw.get("temperature_c"),
                    "powerDrawW": hw.get("power_draw_w"),
                    "utilizationPct": hw.get("utilization_pct"),
                    "vramUsedMb": used_mb,
                    "vramFreeMb": free_mb,
                    "vramTotalMb": total_mb,
                    "vramUsedGb": used_gb,
                    "vramFreeGb": free_gb,
                    "vramTotalGb": total_gb,
                    "vramUsedPct": used_pct,
                    "tempC": hw.get("temperature_c"),
                    "powerW": hw.get("power_draw_w"),
                    "utilPct": hw.get("utilization_pct"),
                }
        except Exception:
            pass
        return fallback
    try:
        proc = subprocess.run(
            [
                nvidia_smi,
                "--query-gpu=name,memory.used,memory.free,memory.total,temperature.gpu,power.draw,utilization.gpu",
                "--format=csv,noheader,nounits",
            ],
            timeout=1.2,
            shell=False,
            **_subprocess_kwargs(),
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return fallback
        line = proc.stdout.strip().splitlines()[0]
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 7:
            return fallback
        used_mb = _parse_float(parts[1]) or 0.0
        free_mb = _parse_float(parts[2]) or 0.0
        total_mb = _parse_float(parts[3]) or 0.0
        used_pct = round((used_mb / total_mb) * 100.0, 1) if total_mb > 0 else 0.0
        used_mb_r = round(used_mb, 1)
        free_mb_r = round(free_mb, 1)
        total_mb_r = round(total_mb, 1)
        used_gb_r = round(used_mb / 1024.0, 2)
        free_gb_r = round(free_mb / 1024.0, 2)
        total_gb_r = round(total_mb / 1024.0, 2)
        temp_c = _parse_float(parts[4])
        power_w = _parse_float(parts[5])
        util_pct = _parse_float(parts[6])
        return {
            "available": True,
            "name": parts[0],
            "memoryUsedMb": used_mb_r,
            "memoryFreeMb": free_mb_r,
            "memoryTotalMb": total_mb_r,
            "memoryUsedGb": used_gb_r,
            "memoryFreeGb": free_gb_r,
            "memoryTotalGb": total_gb_r,
            "memoryUsedPct": used_pct,
            "temperatureC": temp_c,
            "powerDrawW": power_w,
            "utilizationPct": util_pct,
            "vramUsedMb": used_mb_r,
            "vramFreeMb": free_mb_r,
            "vramTotalMb": total_mb_r,
            "vramUsedGb": used_gb_r,
            "vramFreeGb": free_gb_r,
            "vramTotalGb": total_gb_r,
            "vramUsedPct": used_pct,
            "tempC": temp_c,
            "powerW": power_w,
            "utilPct": util_pct,
        }
    except Exception:
        return fallback


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


def _min_heavy_free_ram_gb() -> float:
    try:
        return float(os.environ.get("HEAVY_MOE_MIN_FREE_RAM_GB", "14.0"))
    except ValueError:
        return 14.0


def get_ram_telemetry() -> dict[str, Any]:
    """Read physical system RAM cross-platform via ``psutil`` with Win32 fallback."""
    min_heavy_gb = _min_heavy_free_ram_gb()
    try:
        import psutil

        vm = psutil.virtual_memory()
        total_gb = round(vm.total / (1024**3), 2)
        free_gb = round(vm.available / (1024**3), 2)
        used_gb = round((vm.total - vm.available) / (1024**3), 2)
        used_pct = round(float(vm.percent), 1)
        heavy_ready = free_gb >= min_heavy_gb
        return {
            "totalGb": total_gb,
            "usedGb": used_gb,
            "freeGb": free_gb,
            "usedPct": used_pct,
            "swapWarning": free_gb < 6.0,
            "heavyReady": heavy_ready,
            "heavyMoeSafe": heavy_ready,
            "heavyMoeMinFreeGb": min_heavy_gb,
        }
    except Exception:
        pass

    if sys.platform == "win32":
        try:
            stat = _MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                total_gb = round(stat.ullTotalPhys / (1024**3), 2)
                free_gb = round(stat.ullAvailPhys / (1024**3), 2)
                used_gb = round((stat.ullTotalPhys - stat.ullAvailPhys) / (1024**3), 2)
                used_pct = (
                    round(((stat.ullTotalPhys - stat.ullAvailPhys) / stat.ullTotalPhys) * 100.0, 1)
                    if stat.ullTotalPhys > 0
                    else 0.0
                )
                heavy_ready = free_gb >= min_heavy_gb
                return {
                    "totalGb": total_gb,
                    "usedGb": used_gb,
                    "freeGb": free_gb,
                    "usedPct": used_pct,
                    "swapWarning": free_gb < 6.0,
                    "heavyReady": heavy_ready,
                    "heavyMoeSafe": heavy_ready,
                    "heavyMoeMinFreeGb": min_heavy_gb,
                }
        except Exception:
            pass
    return {
        "totalGb": 0.0,
        "usedGb": 0.0,
        "freeGb": 0.0,
        "usedPct": 0.0,
        "swapWarning": False,
        "heavyReady": False,
        "heavyMoeSafe": False,
        "heavyMoeMinFreeGb": min_heavy_gb,
    }


def get_disk_telemetry() -> dict[str, Any]:
    """Read local model storage drive usage."""
    drive = "C:\\" if sys.platform == "win32" else "/"
    try:
        usage = shutil.disk_usage(drive)
        total_gb = round(usage.total / (1024**3), 2)
        free_gb = round(usage.free / (1024**3), 2)
        used_gb = round(usage.used / (1024**3), 2)
        used_pct = round((usage.used / usage.total) * 100.0, 1) if usage.total > 0 else 0.0
        return {
            "drive": drive,
            "totalGb": total_gb,
            "freeGb": free_gb,
            "usedGb": used_gb,
            "usedPct": used_pct,
        }
    except OSError:
        return {"drive": drive, "totalGb": 0.0, "freeGb": 0.0, "usedGb": 0.0, "usedPct": 0.0}


def read_active_heavy_model() -> dict[str, Any] | None:
    """Read `models/heavy-moe/ACTIVE_MODEL.json` if present."""
    active_file = HEAVY_MOE_DIR / "ACTIVE_MODEL.json"
    if not active_file.is_file():
        return None
    try:
        data = json.loads(active_file.read_text(encoding="utf-8", errors="replace"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _extract_heavy_meta_fields(meta: dict[str, Any] | None) -> tuple[str | None, str | None, str | None]:
    if not meta:
        return None, None, None
    name = (
        meta.get("modelId")
        or meta.get("model")
        or meta.get("name")
        or meta.get("id")
        or meta.get("modelName")
        or meta.get("displayName")
        or meta.get("repo")
    )
    quant = meta.get("quant") or meta.get("quantization")
    backend = meta.get("backend") or meta.get("engine") or meta.get("runtime") or "Colibri"
    return (
        str(name) if name else None,
        str(quant) if quant else None,
        str(backend) if backend else None,
    )


def _probe_ollama_status(http_json_fn: Callable[..., Any] = _http_json) -> dict[str, Any]:
    ol_ok, _, tags_data, _ = http_json_fn(OLLAMA_HOST, OLLAMA_PORT, "GET", "/api/tags", timeout=0.45)
    ol_models: list[str] = []
    ol_active: str | None = None
    ol_active_models: list[dict[str, Any]] = []
    if ol_ok and isinstance(tags_data, dict):
        ol_models = [str(m.get("name")) for m in tags_data.get("models", []) if m.get("name")]
        ps_ok, _, ps_data, _ = http_json_fn(OLLAMA_HOST, OLLAMA_PORT, "GET", "/api/ps", timeout=0.45)
        if ps_ok and isinstance(ps_data, dict):
            for pm in ps_data.get("models", []):
                if isinstance(pm, dict) and pm.get("name"):
                    ol_active_models.append(
                        {
                            "name": pm.get("name"),
                            "sizeVramGb": round((pm.get("size_vram") or 0) / (1024**3), 2),
                        }
                    )
            if ol_active_models:
                ol_active = str(ol_active_models[0]["name"])
    return {
        "online": ol_ok,
        "url": OLLAMA_BASE,
        "models": ol_models,
        "activeModel": ol_active,
        "activeModels": ol_active_models,
    }


def _probe_heavy_status(http_json_fn: Callable[..., Any] = _http_json) -> dict[str, Any]:
    active_meta = read_active_heavy_model()
    cfg_name, cfg_quant, cfg_backend = _extract_heavy_meta_fields(active_meta)
    hv_health_ok, _, _, _ = http_json_fn(HEAVY_HOST, HEAVY_PORT, "GET", "/health", timeout=0.45)
    hv_models_ok = False
    hv_models_data: Any = None
    if hv_health_ok:
        hv_models_ok, _, hv_models_data, _ = http_json_fn(
            HEAVY_HOST, HEAVY_PORT, "GET", "/v1/models", timeout=0.45
        )
    hv_online = hv_health_ok or hv_models_ok
    hv_models: list[str] = []
    if hv_models_ok and isinstance(hv_models_data, dict):
        for item in hv_models_data.get("data", []):
            if isinstance(item, dict) and item.get("id"):
                hv_models.append(str(item["id"]))
    if not hv_models and cfg_name:
        hv_models = [cfg_name]
    hv_active = hv_models[0] if (hv_online and hv_models) else None
    return {
        "online": hv_online,
        "url": HEAVY_BASE,
        "models": hv_models,
        "activeModel": hv_active,
        "name": cfg_name or (hv_models[0] if hv_models else None),
        "quant": cfg_quant,
        "backend": cfg_backend,
        "configuredModel": active_meta,
    }


def _probe_lmstudio_status(http_json_fn: Callable[..., Any] = _http_json) -> dict[str, Any]:
    lms_ok, _, lms_data, _ = http_json_fn(LMS_HOST, LMS_PORT, "GET", "/v1/models", timeout=0.45)
    lms_models: list[str] = []
    if lms_ok and isinstance(lms_data, dict):
        for item in lms_data.get("data", []):
            if isinstance(item, dict) and item.get("id"):
                lms_models.append(str(item["id"]))
    return {
        "online": lms_ok,
        "url": LMS_BASE,
        "models": lms_models,
        "activeModel": lms_models[0] if lms_models else None,
    }


def get_backends_status(http_json_fn: Callable[..., Any] = _http_json) -> dict[str, Any]:
    """Probe Ollama, Heavy MoE Colibri, and LM Studio backends in parallel."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        f_ol = pool.submit(_probe_ollama_status, http_json_fn)
        f_hv = pool.submit(_probe_heavy_status, http_json_fn)
        f_lms = pool.submit(_probe_lmstudio_status, http_json_fn)
        heavy_status = f_hv.result()
        return {
            "ollama": f_ol.result(),
            "heavyMoe": heavy_status,
            "heavy": heavy_status,
            "lmstudio": f_lms.result(),
        }


def _collect_full_telemetry(
    http_json_fn: Callable[..., Any] = _http_json,
    gpu_fn: Callable[[], dict[str, Any]] = get_gpu_telemetry,
    orch_summary_fn: Callable[[], dict[str, Any]] = get_orchestrator_telemetry_summary,
) -> dict[str, Any]:
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        f_gpu = pool.submit(gpu_fn)
        f_backends = pool.submit(get_backends_status, http_json_fn)
        f_orch = pool.submit(orch_summary_fn)
        ram_data = get_ram_telemetry()
        disk_data = get_disk_telemetry()
        return {
            "gpu": f_gpu.result(),
            "ram": ram_data,
            "disk": disk_data,
            "backends": f_backends.result(),
            "orchestrator": f_orch.result(),
            "privacy": {
                "loopbackOnly": True,
                "offlineMode": True,
                "diskChatHistory": False,
                "cspEnforced": True,
                "noStoreHeaders": True,
            },
        }


def invalidate_telemetry_cache() -> None:
    """Clear the 1.5s telemetry cache."""
    global _TELEMETRY_CACHE_TS, _TELEMETRY_CACHE_DATA
    with _TELEMETRY_CACHE_LOCK:
        _TELEMETRY_CACHE_TS = 0.0
        _TELEMETRY_CACHE_DATA = None


def get_full_telemetry(
    force_refresh: bool = False,
    http_json_fn: Callable[..., Any] | None = None,
    gpu_fn: Callable[[], dict[str, Any]] | None = None,
    orch_summary_fn: Callable[[], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return parallel-collected telemetry with a 1.5s TTL cache."""
    global _TELEMETRY_CACHE_TS, _TELEMETRY_CACHE_DATA
    now = time.monotonic()
    if not force_refresh:
        with _TELEMETRY_CACHE_LOCK:
            if _TELEMETRY_CACHE_DATA is not None and (now - _TELEMETRY_CACHE_TS) < _TELEMETRY_TTL_SEC:
                return _TELEMETRY_CACHE_DATA

    fresh = _collect_full_telemetry(
        http_json_fn=http_json_fn or _http_json,
        gpu_fn=gpu_fn or get_gpu_telemetry,
        orch_summary_fn=orch_summary_fn or get_orchestrator_telemetry_summary,
    )
    with _TELEMETRY_CACHE_LOCK:
        _TELEMETRY_CACHE_DATA = fresh
        _TELEMETRY_CACHE_TS = time.monotonic()
        return fresh


async def async_get_full_telemetry(force_refresh: bool = False) -> dict[str, Any]:
    """Async wrapper over `get_full_telemetry` executed off the event loop."""
    return await asyncio.to_thread(get_full_telemetry, force_refresh)


def _parse_markdown_table(md_text: str) -> dict[str, Any] | None:
    lines = md_text.splitlines()
    for i in range(len(lines) - 1):
        line = lines[i].strip()
        next_line = lines[i + 1].strip()
        if line.startswith("|") and next_line.startswith("|") and "---" in next_line:
            headers = [c.strip() for c in line.strip("|").split("|")]
            rows: list[list[str]] = []
            for j in range(i + 2, len(lines)):
                row_line = lines[j].strip()
                if not row_line.startswith("|"):
                    break
                cells = [c.strip() for c in row_line.strip("|").split("|")]
                rows.append(cells)
            return {"headers": headers, "rows": rows}
    return None


def get_local_registry(http_json_fn: Callable[..., Any] = _http_json) -> dict[str, Any]:
    """Read local Ollama models, Heavy MoE GGUF inventory, benchmarks, and registry markdown."""
    ol_ok, _, tags_data, _ = http_json_fn(OLLAMA_HOST, OLLAMA_PORT, "GET", "/api/tags", timeout=1.0)
    ollama_models: list[dict[str, Any]] = []
    if ol_ok and isinstance(tags_data, dict):
        for m in tags_data.get("models", []):
            if not isinstance(m, dict):
                continue
            size_bytes = int(m.get("size") or 0)
            size_gb = round(size_bytes / (1024**3), 2)
            ollama_models.append(
                {
                    "name": m.get("name", ""),
                    "sizeBytes": size_bytes,
                    "sizeGb": size_gb,
                    "size_gb": size_gb,
                    "modifiedAt": m.get("modified_at", ""),
                    "details": m.get("details") if isinstance(m.get("details"), dict) else {},
                }
            )

    active_heavy = read_active_heavy_model()
    heavy_files: list[dict[str, Any]] = []
    total_heavy_bytes = 0
    if HEAVY_MOE_DIR.is_dir():
        for gguf in sorted(HEAVY_MOE_DIR.rglob("*.gguf")):
            try:
                sz = gguf.stat().st_size
            except OSError:
                sz = 0
            total_heavy_bytes += sz
            rel_posix = gguf.relative_to(HEAVY_MOE_DIR).as_posix()
            sz_gb = round(sz / (1024**3), 2)
            heavy_files.append(
                {
                    "name": gguf.name,
                    "relativePath": str(gguf.relative_to(HEAVY_MOE_DIR)),
                    "rel_path": rel_posix,
                    "sizeBytes": sz,
                    "sizeGb": sz_gb,
                    "size_gb": sz_gb,
                }
            )

    benchmarks_md = ""
    benchmark_table = None
    if BENCHMARKS_RESULTS_FILE.is_file():
        benchmarks_md = BENCHMARKS_RESULTS_FILE.read_text(encoding="utf-8", errors="replace")
        benchmark_table = _parse_markdown_table(benchmarks_md)

    registry_md = ""
    if REGISTRY_MD_FILE.is_file():
        registry_md = REGISTRY_MD_FILE.read_text(encoding="utf-8", errors="replace")

    heavy_section = {
        "activeModel": active_heavy,
        "files": heavy_files,
        "totalSizeGb": round(total_heavy_bytes / (1024**3), 2),
    }

    return {
        "ollama": {"online": ol_ok, "models": ollama_models},
        "heavyMoe": heavy_section,
        "heavy": heavy_section,
        "benchmarksMarkdown": benchmarks_md,
        "benchmarkTable": benchmark_table,
        "registryMarkdown": registry_md,
    }
