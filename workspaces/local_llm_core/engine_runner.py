"""Pure-Python cross-platform engine runner and multi-OS hardware profiler."""

from __future__ import annotations

import json
import os
import pathlib
import platform
import shutil
import signal
import subprocess
import sys
import threading
import time
from typing import Any

from .config import (
    HEAVY_HOST,
    HEAVY_MOE_DIR,
    HEAVY_PORT,
    OLLAMA_HOST,
    OLLAMA_PORT,
    PROJECT_ROOT,
)


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


def _parse_float(value: Any) -> float | None:
    if value is None:
        return None
    s = str(value).strip()
    if not s or s.upper() in ("N/A", "[N/A]", "[NOT SUPPORTED]", "NONE"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _probe_nvidia_cuda() -> dict[str, Any] | None:
    nvsmi = shutil.which("nvidia-smi")
    if not nvsmi:
        return None
    try:
        proc = subprocess.run(
            [
                nvsmi,
                "--query-gpu=name,memory.total,memory.free,temperature.gpu,power.draw,utilization.gpu",
                "--format=csv,noheader,nounits",
            ],
            timeout=2.0,
            shell=False,
            **_subprocess_kwargs(),
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return None
        parts = [p.strip() for p in proc.stdout.strip().splitlines()[0].split(",")]
        if len(parts) < 4:
            return None
        total_mb = _parse_float(parts[1]) or 0.0
        free_mb = _parse_float(parts[2]) or 0.0
        used_mb = max(0.0, total_mb - free_mb)
        temp_c = _parse_float(parts[3]) if len(parts) > 3 else None
        power_w = _parse_float(parts[4]) if len(parts) > 4 else None
        util_pct = _parse_float(parts[5]) if len(parts) > 5 else None
        return {
            "gpu_available": True,
            "gpu_backend": "cuda",
            "gpu_name": parts[0],
            "unified_memory": False,
            "vram_total_mb": round(total_mb, 1),
            "vram_free_mb": round(free_mb, 1),
            "vram_used_mb": round(used_mb, 1),
            "vram_total_gb": round(total_mb / 1024.0, 2),
            "vram_free_gb": round(free_mb / 1024.0, 2),
            "vram_used_gb": round(used_mb / 1024.0, 2),
            "temperature_c": temp_c,
            "power_draw_w": power_w,
            "utilization_pct": util_pct,
        }
    except Exception:
        return None


def _probe_amd_rocm(sys_drm_root: pathlib.Path | None = None) -> dict[str, Any] | None:
    rocm_smi = shutil.which("rocm-smi")
    if rocm_smi:
        try:
            proc = subprocess.run(
                [rocm_smi, "--showmeminfo", "vram", "--json"],
                timeout=2.5,
                shell=False,
                **_subprocess_kwargs(),
            )
            if proc.returncode == 0 and proc.stdout.strip():
                data = json.loads(proc.stdout)
                if isinstance(data, dict):
                    for card_key, card_info in data.items():
                        if not isinstance(card_info, dict):
                            continue
                        total_bytes = _parse_float(
                            card_info.get("VRAM Total Memory (B)")
                            or card_info.get("vram_total")
                        )
                        used_bytes = _parse_float(
                            card_info.get("VRAM Total Used Memory (B)")
                            or card_info.get("vram_used")
                        ) or 0.0
                        if total_bytes and total_bytes > 0:
                            free_bytes = max(0.0, total_bytes - used_bytes)
                            return {
                                "gpu_available": True,
                                "gpu_backend": "rocm",
                                "gpu_name": f"AMD ROCm ({card_key})",
                                "unified_memory": False,
                                "vram_total_mb": round(total_bytes / (1024**2), 1),
                                "vram_free_mb": round(free_bytes / (1024**2), 1),
                                "vram_used_mb": round(used_bytes / (1024**2), 1),
                                "vram_total_gb": round(total_bytes / (1024**3), 2),
                                "vram_free_gb": round(free_bytes / (1024**3), 2),
                                "vram_used_gb": round(used_bytes / (1024**3), 2),
                                "temperature_c": None,
                                "power_draw_w": None,
                                "utilization_pct": None,
                            }
        except Exception:
            pass

    drm_root = sys_drm_root or pathlib.Path("/sys/class/drm")
    if drm_root.is_dir():
        for total_file in sorted(drm_root.glob("card*/device/mem_info_vram_total")):
            try:
                total_bytes = float(total_file.read_text(encoding="utf-8").strip())
                if total_bytes <= 0:
                    continue
                used_file = total_file.parent / "mem_info_vram_used"
                used_bytes = (
                    float(used_file.read_text(encoding="utf-8").strip())
                    if used_file.is_file()
                    else 0.0
                )
                free_bytes = max(0.0, total_bytes - used_bytes)
                card_name = total_file.parents[1].name
                return {
                    "gpu_available": True,
                    "gpu_backend": "rocm",
                    "gpu_name": f"AMD ROCm ({card_name})",
                    "unified_memory": False,
                    "vram_total_mb": round(total_bytes / (1024**2), 1),
                    "vram_free_mb": round(free_bytes / (1024**2), 1),
                    "vram_used_mb": round(used_bytes / (1024**2), 1),
                    "vram_total_gb": round(total_bytes / (1024**3), 2),
                    "vram_free_gb": round(free_bytes / (1024**3), 2),
                    "vram_used_gb": round(used_bytes / (1024**3), 2),
                    "temperature_c": None,
                    "power_draw_w": None,
                    "utilization_pct": None,
                }
            except (OSError, ValueError):
                continue
    return None


def _probe_apple_metal(ram_total_gb: float, ram_available_gb: float) -> dict[str, Any] | None:
    sysctl_exe = shutil.which("sysctl")
    brand = ""
    mem_bytes: float | None = None
    if sysctl_exe:
        try:
            brand_proc = subprocess.run(
                [sysctl_exe, "-n", "machdep.cpu.brand_string"],
                timeout=1.5,
                shell=False,
                **_subprocess_kwargs(),
            )
            if brand_proc.returncode == 0:
                brand = brand_proc.stdout.strip()
        except Exception:
            brand = ""
        try:
            mem_proc = subprocess.run(
                [sysctl_exe, "-n", "hw.memsize"],
                timeout=1.5,
                shell=False,
                **_subprocess_kwargs(),
            )
            if mem_proc.returncode == 0:
                mem_bytes = _parse_float(mem_proc.stdout.strip())
        except Exception:
            mem_bytes = None

    machine = platform.machine().lower()
    is_apple_silicon = ("apple" in brand.lower()) or (machine in ("arm64", "aarch64"))
    if not is_apple_silicon:
        return None

    total_ram_gb = round(mem_bytes / (1024**3), 2) if (mem_bytes and mem_bytes > 0) else ram_total_gb
    alloc_total_gb = round(total_ram_gb * 0.70, 2)
    alloc_free_gb = round(min(alloc_total_gb, max(0.0, ram_available_gb) * 0.70), 2)
    used_gb = round(max(0.0, alloc_total_gb - alloc_free_gb), 2)
    return {
        "gpu_available": True,
        "gpu_backend": "metal",
        "gpu_name": brand or "Apple Silicon Unified GPU",
        "unified_memory": True,
        "vram_total_mb": round(alloc_total_gb * 1024.0, 1),
        "vram_free_mb": round(alloc_free_gb * 1024.0, 1),
        "vram_used_mb": round(used_gb * 1024.0, 1),
        "vram_total_gb": alloc_total_gb,
        "vram_free_gb": alloc_free_gb,
        "vram_used_gb": used_gb,
        "temperature_c": None,
        "power_draw_w": None,
        "utilization_pct": None,
    }


def _detect_simd_flags(target_platform: str) -> list[str]:
    flags: list[str] = []
    if target_platform.startswith("linux"):
        cpuinfo = pathlib.Path("/proc/cpuinfo")
        if cpuinfo.is_file():
            try:
                text = cpuinfo.read_text(encoding="utf-8", errors="ignore").lower()
                for cand in ("avx512", "avx2", "avx", "neon", "asimd"):
                    if cand in text:
                        flags.append("neon" if cand == "asimd" else cand)
            except OSError:
                pass
    elif target_platform == "darwin":
        if platform.machine().lower() in ("arm64", "aarch64"):
            flags.append("neon")
        else:
            flags.append("avx2")
    elif platform.machine().lower() in ("arm64", "aarch64"):
        flags.append("neon")
    else:
        flags.append("avx2")
    return sorted(set(flags))


def detect_hardware_profile(
    *,
    platform_name: str | None = None,
    sys_drm_root: pathlib.Path | None = None,
) -> dict[str, Any]:
    """Detect GPU (CUDA / ROCm / Apple Metal), system RAM, physical/logical CPU, and SIMD flags."""
    target_platform = (platform_name or sys.platform).lower()

    ram_total_gb = 0.0
    ram_free_gb = 0.0
    ram_used_gb = 0.0
    ram_used_pct = 0.0
    physical_cores = os.cpu_count() or 1
    logical_cores = os.cpu_count() or 1

    try:
        import psutil

        vm = psutil.virtual_memory()
        ram_total_gb = round(vm.total / (1024**3), 2)
        ram_free_gb = round(vm.available / (1024**3), 2)
        ram_used_gb = round((vm.total - vm.available) / (1024**3), 2)
        ram_used_pct = round(float(vm.percent), 1)
        physical_cores = psutil.cpu_count(logical=False) or logical_cores
        logical_cores = psutil.cpu_count(logical=True) or logical_cores
    except Exception:
        pass

    gpu_info = _probe_nvidia_cuda()
    if gpu_info is None and target_platform.startswith("linux"):
        gpu_info = _probe_amd_rocm(sys_drm_root=sys_drm_root)
    if gpu_info is None and target_platform == "darwin":
        gpu_info = _probe_apple_metal(ram_total_gb, ram_free_gb)

    if gpu_info is None:
        gpu_info = {
            "gpu_available": False,
            "gpu_backend": "cpu",
            "gpu_name": "CPU / Integrated Graphics",
            "unified_memory": False,
            "vram_total_mb": 0.0,
            "vram_free_mb": 0.0,
            "vram_used_mb": 0.0,
            "vram_total_gb": 0.0,
            "vram_free_gb": 0.0,
            "vram_used_gb": 0.0,
            "temperature_c": None,
            "power_draw_w": None,
            "utilization_pct": None,
        }

    return {
        "platform": target_platform,
        "cpu_physical_cores": int(physical_cores),
        "cpu_logical_cores": int(logical_cores),
        "simd_flags": _detect_simd_flags(target_platform),
        "ram_total_gb": ram_total_gb,
        "ram_free_gb": ram_free_gb,
        "ram_used_gb": ram_used_gb,
        "ram_used_pct": ram_used_pct,
        **gpu_info,
    }


def build_privacy_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Return process environment enforced with zero-telemetry / loopback-only flags."""
    env = os.environ.copy()
    env.update(
        {
            "DO_NOT_TRACK": "1",
            "OLLAMA_NOHISTORY": "1",
            "OLLAMA_NO_CLOUD": "1",
            "OFFLINE_MODE": "true",
            "HF_HUB_DISABLE_TELEMETRY": "1",
            "SCARF_NO_ANALYTICS": "true",
            "ANONYMIZED_TELEMETRY": "false",
            "KVSAVE": "0",
            "OLLAMA_HOST": f"{OLLAMA_HOST}:{OLLAMA_PORT}",
        }
    )
    if extra:
        env.update(extra)
    return env


class PurePythonEngineRunner:
    """Cross-platform pure-Python subprocess manager for Ollama and llama-server."""

    def __init__(self, project_root: pathlib.Path | None = None) -> None:
        self.project_root = project_root or PROJECT_ROOT
        self._lock = threading.Lock()
        self._procs: dict[str, subprocess.Popen[Any]] = {}

    @staticmethod
    def _spawn_detached(cmd: list[str], env: dict[str, str], cwd: pathlib.Path) -> subprocess.Popen[Any]:
        kwargs: dict[str, Any] = {
            "cwd": str(cwd),
            "env": env,
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "shell": False,
        }
        if sys.platform == "win32":
            flags = 0
            if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
                flags |= subprocess.CREATE_NEW_PROCESS_GROUP
            if hasattr(subprocess, "CREATE_NO_WINDOW"):
                flags |= subprocess.CREATE_NO_WINDOW
            kwargs["creationflags"] = flags
        else:
            kwargs["start_new_session"] = True
        return subprocess.Popen(cmd, **kwargs)  # nosec B603

    @staticmethod
    def _terminate_proc_tree(proc: subprocess.Popen[Any], timeout: float = 3.0) -> None:
        if proc.poll() is not None:
            return
        if sys.platform != "win32":
            try:
                pgid = os.getpgid(proc.pid)
                os.killpg(pgid, signal.SIGTERM)
            except (OSError, ProcessLookupError):
                try:
                    proc.terminate()
                except OSError:
                    pass
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    return
                time.sleep(0.05)
            try:
                pgid = os.getpgid(proc.pid)
                os.killpg(pgid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                try:
                    proc.kill()
                except OSError:
                    pass
        else:
            try:
                proc.terminate()
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
            except OSError:
                pass

    def _find_llama_server(self) -> str | None:
        for cand in (
            self.project_root / "engines" / "llama.cpp" / "llama-server",
            self.project_root / "engines" / "llama.cpp" / "llama-server.exe",
        ):
            if cand.is_file():
                return str(cand)
        return shutil.which("llama-server")

    def _resolve_heavy_model_path(self) -> pathlib.Path | None:
        active_file = HEAVY_MOE_DIR / "ACTIVE_MODEL.json"
        if active_file.is_file():
            try:
                data = json.loads(active_file.read_text(encoding="utf-8"))
                raw_path = data.get("path") or data.get("modelPath") or data.get("gguf")
                if isinstance(raw_path, str) and raw_path.strip():
                    cand = pathlib.Path(raw_path.strip())
                    if not cand.is_absolute():
                        cand = HEAVY_MOE_DIR / cand
                    if cand.is_file():
                        return cand
            except (OSError, ValueError):
                pass
        if HEAVY_MOE_DIR.is_dir():
            ggufs = sorted(HEAVY_MOE_DIR.rglob("*.gguf"))
            if ggufs:
                return ggufs[0]
        return None

    def start_fast(self, *, ctx: int | None = None, restart: bool = False) -> tuple[int, dict[str, Any]]:
        """Start ``ollama serve`` on loopback in pure Python."""
        ollama_exe = shutil.which("ollama")
        if not ollama_exe:
            return 500, {"ok": False, "output": "Comando 'ollama' non trovato nel PATH."}
        with self._lock:
            existing = self._procs.get("fast")
            if existing is not None and existing.poll() is None:
                if not restart:
                    return 200, {
                        "ok": True,
                        "pid": existing.pid,
                        "output": f"Motore Fast (ollama) già attivo (PID {existing.pid}).",
                    }
                self._terminate_proc_tree(existing)
                self._procs.pop("fast", None)
            extra_env: dict[str, str] = {}
            if ctx is not None:
                extra_env["OLLAMA_CONTEXT_LENGTH"] = str(ctx)
            env = build_privacy_env(extra_env)
            proc = self._spawn_detached([ollama_exe, "serve"], env, self.project_root)
            self._procs["fast"] = proc
            return 200, {
                "ok": True,
                "pid": proc.pid,
                "restarted": restart,
                "output": f"Motore Fast (ollama serve) avviato su {OLLAMA_HOST}:{OLLAMA_PORT} (PID {proc.pid}).",
            }

    def start_heavy(
        self,
        *,
        model_path: pathlib.Path | None = None,
        ram_limit_gb: int = 12,
        ctx: int = 16384,
        restart: bool = False,
    ) -> tuple[int, dict[str, Any]]:
        """Start ``llama-server`` on loopback in pure Python."""
        llama_exe = self._find_llama_server()
        if not llama_exe:
            return 500, {"ok": False, "output": "Eseguibile 'llama-server' non trovato."}
        resolved_model = model_path or self._resolve_heavy_model_path()
        if resolved_model is None or not resolved_model.is_file():
            return 400, {"ok": False, "output": "Nessun file modello .gguf trovato per Heavy MoE."}

        with self._lock:
            existing = self._procs.get("heavy")
            if existing is not None and existing.poll() is None:
                if not restart:
                    return 200, {
                        "ok": True,
                        "pid": existing.pid,
                        "output": f"Motore Heavy (llama-server) già attivo (PID {existing.pid}).",
                    }
                self._terminate_proc_tree(existing)
                self._procs.pop("heavy", None)
            env = build_privacy_env({"HEAVY_MOE_RAM_GB": str(ram_limit_gb)})
            cmd = [
                llama_exe,
                "-m",
                str(resolved_model),
                "--host",
                HEAVY_HOST,
                "--port",
                str(HEAVY_PORT),
                "-c",
                str(ctx),
            ]
            proc = self._spawn_detached(cmd, env, self.project_root)
            self._procs["heavy"] = proc
            return 200, {
                "ok": True,
                "pid": proc.pid,
                "restarted": restart,
                "ramLimitGb": ram_limit_gb,
                "ctx": ctx,
                "output": f"Motore Heavy (llama-server) avviato su {HEAVY_HOST}:{HEAVY_PORT} (PID {proc.pid}).",
            }

    def stop_all(self) -> tuple[int, dict[str, Any]]:
        """Stop all managed engine subprocesses cleanly via process-group termination."""
        with self._lock:
            stopped: list[str] = []
            for key in list(self._procs.keys()):
                proc = self._procs.pop(key)
                self._terminate_proc_tree(proc)
                stopped.append(f"{key}(pid={proc.pid})")
        return 200, {
            "ok": True,
            "stopped": stopped,
            "output": (
                f"Arrestati motori gestiti: {', '.join(stopped)}."
                if stopped
                else "Nessun processo motore gestito attivo."
            ),
        }

    def status(self) -> dict[str, Any]:
        """Return running state of managed subprocesses."""
        with self._lock:
            return {
                key: {"running": proc.poll() is None, "pid": proc.pid}
                for key, proc in self._procs.items()
            }


_ENGINE_RUNNER_SINGLETON = PurePythonEngineRunner()


def get_python_engine_runner() -> PurePythonEngineRunner:
    """Return the process-wide ``PurePythonEngineRunner`` singleton."""
    return _ENGINE_RUNNER_SINGLETON


__all__ = [
    "PurePythonEngineRunner",
    "build_privacy_env",
    "detect_hardware_profile",
    "get_python_engine_runner",
]
