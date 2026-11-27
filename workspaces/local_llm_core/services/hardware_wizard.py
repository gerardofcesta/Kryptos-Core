"""Hardware profile auto-detector and portable configuration wizard."""

from __future__ import annotations

import ctypes
import os
import pathlib
import re
import shutil
import subprocess
import sys
from typing import Any

from ..config import PROJECT_ROOT

_SENSITIVE_DIR_PARTS = frozenset({".ssh", ".gnupg", ".aws", ".kube", ".azure", ".docker"})


def _subprocess_kwargs() -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "timeout": 4.0,
        "shell": False,
    }
    if sys.platform == "win32" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return kwargs


def _is_same_or_child(candidate: pathlib.Path, parent: pathlib.Path) -> bool:
    try:
        c = pathlib.Path(os.path.normcase(os.path.normpath(str(candidate.resolve()))))
        p = pathlib.Path(os.path.normcase(os.path.normpath(str(parent.resolve()))))
        return c == p or c.is_relative_to(p)
    except Exception:
        return False


def validate_models_dir_path(raw_path: str | pathlib.Path) -> pathlib.Path:
    """
    Validate an external `LOCAL_LLM_MODELS_DIR` location for portable safety:
    rejects traversal, UNC paths, `%APPDATA%` / `%LOCALAPPDATA%`, system dirs, and repo workspaces.
    """
    raw_str = str(raw_path).strip()
    if not raw_str:
        raise ValueError("LOCAL_LLM_MODELS_DIR path cannot be empty.")
    if raw_str.startswith("\\\\") or raw_str.startswith("//"):
        raise PermissionError("UNC/network paths are not allowed for LOCAL_LLM_MODELS_DIR.")
    parts = [p for p in re.split(r"[\\/]+", raw_str) if p]
    if ".." in parts:
        raise PermissionError("Path traversal ('..') is not allowed in LOCAL_LLM_MODELS_DIR.")

    resolved = pathlib.Path(raw_str).expanduser().resolve()
    if resolved == resolved.parent:
        raise PermissionError("Filesystem or drive root cannot be used as LOCAL_LLM_MODELS_DIR.")
    if {p.lower() for p in resolved.parts} & _SENSITIVE_DIR_PARTS:
        raise PermissionError("Sensitive directory cannot be used as LOCAL_LLM_MODELS_DIR.")

    for env_key in ("APPDATA", "LOCALAPPDATA", "WINDIR", "SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramData"):
        val = os.environ.get(env_key, "").strip()
        if val:
            denied_root = pathlib.Path(val)
            # Allow pytest tmp_path under %LOCALAPPDATA%\Temp while blocking AppData/LocalAppData itself
            temp_val = (os.environ.get("TEMP") or os.environ.get("TMP") or "").strip()
            if env_key == "LOCALAPPDATA" and temp_val:
                temp_root = pathlib.Path(temp_val).resolve()
                if resolved != temp_root and _is_same_or_child(resolved, temp_root):
                    continue
            if _is_same_or_child(resolved, denied_root):
                raise PermissionError(
                    f"LOCAL_LLM_MODELS_DIR '{resolved}' must not reside inside %{env_key}%."
                )

    if _is_same_or_child(resolved, PROJECT_ROOT / "workspaces"):
        raise PermissionError("LOCAL_LLM_MODELS_DIR must not reside inside repository workspaces/.")

    return resolved


def _detect_ram_gb() -> float:
    try:
        import psutil  # type: ignore[import-untyped]

        return round(psutil.virtual_memory().total / (1024**3), 2)
    except Exception:
        pass

    if sys.platform == "win32":
        try:
            class _MemStatusEx(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stat = _MemStatusEx()
            stat.dwLength = ctypes.sizeof(_MemStatusEx)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):  # type: ignore[attr-defined]
                return round(stat.ullTotalPhys / (1024**3), 2)
        except Exception:
            pass

    if hasattr(os, "sysconf"):
        try:
            pages = os.sysconf("SC_PHYS_PAGES")
            page_size = os.sysconf("SC_PAGE_SIZE")
            return round((pages * page_size) / (1024**3), 2)
        except Exception:
            pass
    return 16.0


def _detect_gpu_vram() -> tuple[str, float]:
    nvsmi = shutil.which("nvidia-smi")
    if nvsmi:
        try:
            proc = subprocess.run(
                [nvsmi, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
                **_subprocess_kwargs(),
            )
            if proc.returncode == 0 and proc.stdout.strip():
                first_line = proc.stdout.strip().splitlines()[0]
                parts = [p.strip() for p in first_line.split(",")]
                if len(parts) >= 2:
                    gpu_name = parts[0]
                    vram_mb = float(parts[1])
                    return gpu_name, round(vram_mb / 1024.0, 2)
        except Exception:
            pass
    try:
        from ..engine_runner import detect_hardware_profile

        hw = detect_hardware_profile()
        if hw.get("gpu_available") and float(hw.get("vram_total_gb") or 0.0) > 0:
            return str(hw.get("gpu_name") or "GPU"), float(hw["vram_total_gb"])
    except Exception:
        pass
    return "CPU / Integrated Graphics", 0.0


def detect_hardware_specs() -> dict[str, Any]:
    """Detect local GPU VRAM, system RAM, CPU cores, and free disk space."""
    gpu_name, vram_gb = _detect_gpu_vram()
    ram_gb = _detect_ram_gb()
    cpu_count = os.cpu_count() or 4
    try:
        disk = shutil.disk_usage(PROJECT_ROOT)
        free_disk_gb = round(disk.free / (1024**3), 2)
    except OSError:
        free_disk_gb = 0.0

    return {
        "gpu_name": gpu_name,
        "vram_gb": vram_gb,
        "ram_gb": ram_gb,
        "cpu_threads": cpu_count,
        "free_disk_gb": free_disk_gb,
    }


def _default_external_models_dir() -> str:
    env_dir = os.environ.get("LOCAL_LLM_MODELS_DIR", "").strip()
    if env_dir:
        return str(validate_models_dir_path(env_dir))
    if sys.platform == "win32":
        system_drive = os.environ.get("SystemDrive", "C:")
        return f"{system_drive}\\LocalLLM-Models"
    return str(pathlib.Path.home() / "LocalLLM-Models")


def recommend_hardware_profile(
    vram_gb: float,
    ram_gb: float,
    *,
    gpu_name: str = "",
    free_disk_gb: float = 200.0,
    models_dir: str | pathlib.Path | None = None,
) -> dict[str, Any]:
    """
    Compute portable tier, `num_ctx`, Heavy MoE anti-swap RAM guard, and
    safe `LOCAL_LLM_MODELS_DIR` configuration across multiple hardware classes.
    """
    resolved_models_dir = (
        str(validate_models_dir_path(models_dir))
        if models_dir is not None
        else _default_external_models_dir()
    )

    vram = max(0.0, float(vram_gb))
    ram = max(1.0, float(ram_gb))

    if vram >= 22.0:
        profile_id = "enthusiast_24gb_vram"
        label = "Enthusiast Workstation (24+ GB VRAM)"
        tier_mode = "dual_tier"
        num_ctx = 32768
        heavy_ctx = 32768
        heavy_enabled = ram >= 28.0
        fast_model = "local-worker:latest"
        balanced_model = "local-uncensored:latest"
        heavy_model = "heavy-moe-uncensored" if heavy_enabled else None
        heavy_ram_gb = max(8, min(20, int(ram * 0.4)))
        heavy_min_free_gb = max(10, int(ram * 0.4))
        wsl_memory_gb = min(6, max(2, int(ram // 8)))
    elif vram >= 14.0:
        profile_id = "workstation_16gb_vram"
        label = "Desktop Workstation (16 GB VRAM)"
        tier_mode = "dual_tier" if ram >= 28.0 else "fast_only"
        num_ctx = 32768
        heavy_ctx = 16384
        heavy_enabled = ram >= 28.0 and free_disk_gb >= 60.0
        fast_model = "local-worker:latest"
        balanced_model = "local-uncensored:latest"
        heavy_model = "heavy-moe-uncensored" if heavy_enabled else None
        heavy_ram_gb = max(8, min(14, int(ram * 0.375)))
        heavy_min_free_gb = max(10, int(ram * 0.4375))
        wsl_memory_gb = min(4, max(2, int(ram // 8)))
    elif vram >= 6.0:
        profile_id = "laptop_8gb_vram"
        label = "Compact GPU Laptop (8 GB VRAM)"
        tier_mode = "fast_only"
        num_ctx = 16384 if ram >= 16.0 else 8192
        heavy_ctx = 8192
        heavy_enabled = False
        fast_model = "qwen2.5:7b-instruct-q5_K_M"
        balanced_model = "qwen2.5:7b-instruct-q5_K_M"
        heavy_model = None
        heavy_ram_gb = 4
        heavy_min_free_gb = max(6, int(ram * 0.5))
        wsl_memory_gb = 2
    elif ram >= 14.0:
        profile_id = "cpu_only_16gb"
        label = "CPU-Only Laptop (16+ GB RAM)"
        tier_mode = "cpu_only"
        num_ctx = 8192
        heavy_ctx = 4096
        heavy_enabled = False
        fast_model = "qwen2.5:3b-instruct-q4_K_M"
        balanced_model = "qwen2.5:7b-instruct-q4_K_M"
        heavy_model = None
        heavy_ram_gb = 4
        heavy_min_free_gb = 8
        wsl_memory_gb = 2
    else:
        profile_id = "cpu_compact"
        label = "Compact Low-Memory Host (<14 GB RAM)"
        tier_mode = "cpu_only"
        num_ctx = 4096
        heavy_ctx = 4096
        heavy_enabled = False
        fast_model = "qwen2.5:1.5b-instruct-q4_K_M"
        balanced_model = "qwen2.5:3b-instruct-q4_K_M"
        heavy_model = None
        heavy_ram_gb = 2
        heavy_min_free_gb = 4
        wsl_memory_gb = 2

    env_overrides = {
        "LOCAL_LLM_HARDWARE_PROFILE": profile_id,
        "LOCAL_LLM_MODELS_DIR": resolved_models_dir,
        "LOCAL_FAST_MODEL": fast_model,
        "LOCAL_BALANCED_MODEL": balanced_model,
        "LOCAL_NUM_CTX": str(num_ctx),
        "HEAVY_MOE_CTX": str(heavy_ctx),
        "HEAVY_MOE_RAM_GB": str(heavy_ram_gb),
        "HEAVY_MOE_MIN_FREE_RAM_GB": str(heavy_min_free_gb),
        "OLLAMA_FLASH_ATTENTION": "1" if vram >= 6.0 else "0",
        "OLLAMA_KV_CACHE_TYPE": "q8_0" if vram >= 6.0 else "f16",
    }

    return {
        "profile_id": profile_id,
        "label": label,
        "gpu_name": gpu_name or ("Dedicated GPU" if vram >= 6.0 else "CPU-Only"),
        "vram_gb": round(vram, 2),
        "ram_gb": round(ram, 2),
        "free_disk_gb": round(float(free_disk_gb), 2),
        "recommended_tier": tier_mode,
        "num_ctx": num_ctx,
        "heavy_moe_enabled": heavy_enabled,
        "heavy_moe_ctx": heavy_ctx,
        "heavy_moe_ram_gb": heavy_ram_gb,
        "heavy_moe_min_free_ram_gb": heavy_min_free_gb,
        "wsl_memory_gb": wsl_memory_gb,
        "local_llm_models_dir": resolved_models_dir,
        "recommended_models": {
            "fast": fast_model,
            "balanced": balanced_model,
            "heavy": heavy_model,
        },
        "env_overrides": env_overrides,
    }


def compute_hardware_tier_plan(
    vram_gb: float,
    ram_gb: float,
    *,
    cpu_threads: int = 4,
    unified_memory: bool = False,
    gpu_backend: str = "cpu",
    gpu_name: str = "",
    free_disk_gb: float = 200.0,
) -> dict[str, Any]:
    """
    Compute deterministic 5-Tier hardware simulation sizing (`tier_a`..`tier_e`),
    including `num_ctx`, `n_gpu_layers`, `threads`, `hardware_fit`, and `oom_safe`.
    """
    from .model_manager import evaluate_model_fit

    vram = max(0.0, float(vram_gb))
    ram = max(2.0, float(ram_gb))
    cores = max(1, int(cpu_threads))
    backend_norm = (gpu_backend or "cpu").strip().lower()

    if unified_memory:
        tier_id = "tier_e"
        tier_label = "Tier E — Unified Memory (Apple Silicon / Linux APU)"
        memory_budget_gb = round(ram * 0.70, 2)
        n_gpu_layers = -1
        resolved_backend = "metal" if backend_norm == "metal" else "unified"
        threads = max(2, cores - 2 if cores > 4 else cores)
        hybrid_moe_offload = ram >= 30.0
        heavy_moe_enabled = False
        if ram >= 30.0:
            recommended_model = "qwen2.5:14b-instruct-q4_K_M"
            model_size_gb = 9.0
            num_ctx = 16384
            recommended_max_params_b = 14
        else:
            recommended_model = "qwen2.5-coder:7b-instruct-q4_K_M"
            model_size_gb = 4.7
            num_ctx = 8192
            recommended_max_params_b = 8
    elif vram >= 22.0 and ram >= 48.0:
        tier_id = "tier_d"
        tier_label = "Tier D — Workstation GPU (24+ GB VRAM / 64+ GB RAM)"
        memory_budget_gb = round(vram * 0.90, 2)
        n_gpu_layers = -1
        resolved_backend = backend_norm if backend_norm in ("cuda", "rocm") else "cuda"
        threads = max(4, min(16, cores // 2))
        recommended_model = "qwen2.5:32b-instruct-q4_K_M"
        model_size_gb = 19.8
        num_ctx = 32768
        recommended_max_params_b = 70
        heavy_moe_enabled = True
        hybrid_moe_offload = True
    elif vram >= 14.0:
        tier_id = "tier_c"
        tier_label = "Tier C — Enthusiast GPU (16 GB VRAM / 32 GB RAM)"
        memory_budget_gb = round(vram * 0.90, 2)
        n_gpu_layers = -1
        resolved_backend = backend_norm if backend_norm in ("cuda", "rocm") else "cuda"
        threads = max(4, min(12, cores // 2))
        recommended_model = "qwen2.5:14b-instruct-q4_K_M"
        model_size_gb = 9.0
        num_ctx = 32768 if ram >= 32.0 else 16384
        recommended_max_params_b = 27
        heavy_moe_enabled = bool(ram >= 28.0 and free_disk_gb >= 60.0)
        hybrid_moe_offload = True
    elif vram >= 6.0:
        tier_id = "tier_b"
        tier_label = "Tier B — Entry GPU (8 GB VRAM CUDA/ROCm / 16 GB RAM)"
        memory_budget_gb = round(vram * 0.90, 2)
        n_gpu_layers = -1
        resolved_backend = backend_norm if backend_norm in ("cuda", "rocm") else "cuda"
        threads = max(2, min(8, cores // 2))
        recommended_model = "qwen2.5-coder:7b-instruct-q4_K_M"
        model_size_gb = 4.7
        num_ctx = 8192
        recommended_max_params_b = 8
        heavy_moe_enabled = False
        hybrid_moe_offload = False
    else:
        tier_id = "tier_a"
        tier_label = "Tier A — CPU-Only Host (<=8 GB RAM, 0 VRAM)"
        memory_budget_gb = round(ram * 0.75, 2)
        n_gpu_layers = 0
        resolved_backend = "cpu"
        threads = max(1, cores - 1 if cores > 2 else cores)
        recommended_model = (
            "llama3.2:1b-instruct-q4_K_M" if ram < 7.0 else "llama3.2:3b-instruct-q4_K_M"
        )
        model_size_gb = 1.1 if ram < 7.0 else 2.0
        num_ctx = 2048 if ram <= 12.0 else 4096
        recommended_max_params_b = 3
        heavy_moe_enabled = False
        hybrid_moe_offload = False

    hw_snapshot = {
        "vram_total_gb": memory_budget_gb if unified_memory else vram,
        "ram_total_gb": ram,
        "ram_free_gb": round(ram * 0.85, 2),
        "unified_memory": unified_memory,
    }
    hardware_fit = evaluate_model_fit(
        model_size_gb,
        quant="Q4_K_M",
        hardware=hw_snapshot,
        ctx=num_ctx,
    )
    effective_oom_ceiling_gb = (
        round(vram + ram * 0.60, 2)
        if (hybrid_moe_offload and not unified_memory)
        else memory_budget_gb
    )
    oom_safe = bool(
        float(hardware_fit["required_gb"]) <= effective_oom_ceiling_gb
        and hardware_fit["level"] != "red"
    )

    return {
        "tier_id": tier_id,
        "tier_label": tier_label,
        "gpu_name": gpu_name or resolved_backend.upper(),
        "gpu_backend": resolved_backend,
        "vram_gb": round(vram, 2),
        "ram_gb": round(ram, 2),
        "unified_memory": unified_memory,
        "memory_budget_gb": memory_budget_gb,
        "safe_ceiling_gb": effective_oom_ceiling_gb,
        "recommended_model": recommended_model,
        "model_size_gb": model_size_gb,
        "quant": "Q4_K_M",
        "recommended_max_params_b": recommended_max_params_b,
        "num_ctx": num_ctx,
        "n_gpu_layers": n_gpu_layers,
        "threads": threads,
        "heavy_moe_enabled": heavy_moe_enabled,
        "hybrid_moe_offload": hybrid_moe_offload,
        "hardware_fit": hardware_fit,
        "oom_safe": oom_safe,
    }
