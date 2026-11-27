"""Layer 1 deterministic mathematical safety bounds for Local LLM tuning sliders."""

from __future__ import annotations

import json
import math
import pathlib
import sys
from typing import Any

from ..config import PROJECT_ROOT
from ..engine.coordinator import get_gateway_coordinator
from .telemetry import (
    _http_json,
    get_gpu_telemetry,
    get_ram_telemetry,
    read_active_heavy_model,
)

FAST_CTX_STEPS: tuple[int, ...] = (
    2048,
    4096,
    8192,
    16384,
    24576,
    32768,
    49152,
    65536,
    131072,
)

_KV_BYTES_PER_ELEM: dict[str, float] = {
    "q4_0": 0.5,
    "q8_0": 1.0,
    "f16": 2.0,
    "fp16": 2.0,
}


def compute_kv_gb_per_1k(
    *,
    num_layers: int,
    num_kv_heads: int,
    head_dim: int,
    kv_quant: str = "q8_0",
) -> float:
    """Compute KV-cache GiB consumed per 1,024 tokens: 2 * L * H_kv * D_head * B_kv * 1024 / 1024^3."""
    b_kv = _KV_BYTES_PER_ELEM.get((kv_quant or "q8_0").strip().lower(), 1.0)
    bytes_per_1k = 2.0 * max(1, num_layers) * max(1, num_kv_heads) * max(16, head_dim) * b_kv * 1024.0
    return round(bytes_per_1k / float(1024**3), 4)


def snap_ctx_floor(target_ctx: float, *, max_cap: int = 131072) -> int:
    """Snap down to the largest discrete context step <= target_ctx and <= max_cap (minimum 2048)."""
    best = FAST_CTX_STEPS[0]
    for step in FAST_CTX_STEPS:
        if step <= target_ctx and step <= max_cap:
            best = step
    return best


def snap_ctx_nearest(raw_ctx: int, *, min_val: int = 2048, max_val: int = 131072) -> int:
    """Clamp and snap an arbitrary integer context size to the nearest discrete step in [min_val, max_val]."""
    valid = [s for s in FAST_CTX_STEPS if min_val <= s <= max_val]
    if not valid:
        return min_val
    return min(valid, key=lambda s: (abs(s - raw_ctx), s))


def _infer_fast_geometry(params_b: float) -> tuple[int, int, int]:
    """Infer representative GQA geometry (layers, kv_heads, head_dim) from parameter scale."""
    if params_b <= 4.0:
        return 32, 4, 128
    if params_b <= 9.5:
        return 36, 8, 128
    if params_b <= 16.0:
        return 40, 8, 128
    if params_b <= 36.0:
        return 64, 8, 128
    return 64, 8, 128


def _try_colibri_geometry(heavy_meta: dict[str, Any] | None) -> dict[str, Any]:
    """
    Optionally inspect Colibri `resource_plan.py` and active model `config.json`
    with deterministic fallback when running standalone without engine files.
    """
    result: dict[str, Any] = {
        "colibri_planner_used": False,
        "layers": 64,
        "kv_heads": 8,
        "head_dim": 128,
        "max_context": 65536,
        "vk_pool_gb": 1.5,
    }
    colibri_dir = PROJECT_ROOT / "engines" / "colibri"
    resource_plan_py = colibri_dir / "resource_plan.py"
    if resource_plan_py.is_file():
        colibri_str = str(colibri_dir)
        inserted = False
        try:
            if colibri_str not in sys.path:
                sys.path.insert(0, colibri_str)
                inserted = True
            import resource_plan  # type: ignore[import-not-found]

            if hasattr(resource_plan, "vk_fit_pools"):
                pools = resource_plan.vk_fit_pools()
                if isinstance(pools, dict) and pools:
                    total_pool_bytes = sum(int(v) for v in pools.values() if isinstance(v, (int, float)))
                    if total_pool_bytes > 0:
                        result["vk_pool_gb"] = round(max(1.0, total_pool_bytes / float(1024**3)), 2)
                result["colibri_planner_used"] = True
        except Exception:
            pass
        finally:
            if inserted:
                try:
                    sys.path.remove(colibri_str)
                except ValueError:
                    pass

    if isinstance(heavy_meta, dict) and heavy_meta.get("colibriDir"):
        raw_dir = pathlib.Path(str(heavy_meta["colibriDir"]))
        if not raw_dir.is_absolute():
            raw_dir = PROJECT_ROOT / raw_dir
        cfg_file = raw_dir / "config.json"
        if cfg_file.is_file():
            try:
                cfg = json.loads(cfg_file.read_text(encoding="utf-8", errors="replace"))
                text_cfg = cfg.get("text_config") if isinstance(cfg.get("text_config"), dict) else cfg
                if isinstance(text_cfg, dict):
                    layers = int(text_cfg.get("num_hidden_layers") or result["layers"])
                    kv_heads = int(text_cfg.get("num_key_value_heads") or result["kv_heads"])
                    head_dim = int(
                        text_cfg.get("head_dim")
                        or (
                            int(text_cfg.get("hidden_size", 4096))
                            // max(1, int(text_cfg.get("num_attention_heads", 32)))
                        )
                    )
                    max_pos = int(text_cfg.get("max_position_embeddings") or result["max_context"])
                    result["layers"] = max(1, layers)
                    result["kv_heads"] = max(1, kv_heads)
                    result["head_dim"] = max(32, head_dim)
                    result["max_context"] = max(4096, min(131072, max_pos))
                    result["colibri_planner_used"] = True
            except Exception:
                pass

    return result


def _probe_loaded_ollama_vram_gb() -> float:
    ok, _, ps_data, _ = _http_json("127.0.0.1", 11434, "GET", "/api/ps", timeout=0.35)
    if not ok or not isinstance(ps_data, dict):
        return 0.0
    total_bytes = 0
    for item in ps_data.get("models", []):
        if isinstance(item, dict):
            total_bytes += int(item.get("size_vram") or 0)
    return round(total_bytes / float(1024**3), 2)


def compute_tuning_bounds(
    *,
    model_params_b: float | None = None,
    model_weights_gb: float | None = None,
    kv_quant: str = "q8_0",
    current_colibri_ram_gb: float | None = None,
    gpu: dict[str, Any] | None = None,
    ram: dict[str, Any] | None = None,
    heavy_meta: dict[str, Any] | None = None,
    loaded_ollama_vram_gb: float | None = None,
) -> dict[str, Any]:
    """
    Compute Layer 1 deterministic mathematical safety bounds for Fast `num_ctx`,
    Heavy MoE `ramLimitGb`, and Heavy `ctx`, enforcing strict OS safety reserves.
    """
    gpu_data = gpu if gpu is not None else get_gpu_telemetry()
    ram_data = ram if ram is not None else get_ram_telemetry()
    active_heavy = heavy_meta if heavy_meta is not None else read_active_heavy_model()
    colibri_geom = _try_colibri_geometry(active_heavy)

    vram_total_gb = max(
        0.0,
        float(gpu_data.get("vramTotalGb") or gpu_data.get("memoryTotalGb") or 0.0),
    )
    vram_free_gb = max(
        0.0,
        float(gpu_data.get("vramFreeGb") or gpu_data.get("memoryFreeGb") or 0.0),
    )
    ram_total_gb = max(0.0, float(ram_data.get("totalGb") or 0.0))
    ram_free_gb = max(0.0, float(ram_data.get("freeGb") or 0.0))

    os_vram_reserve_gb = round(max(1.5, 0.10 * vram_total_gb), 2)
    os_ram_reserve_gb = round(max(6.0, 0.15 * ram_total_gb), 2)

    ollama_loaded_gb = (
        max(0.0, float(loaded_ollama_vram_gb))
        if loaded_ollama_vram_gb is not None
        else (_probe_loaded_ollama_vram_gb() if gpu is None else 0.0)
    )

    if vram_total_gb > 0.0:
        base_usable_vram = max(0.0, vram_total_gb - os_vram_reserve_gb)
        if vram_free_gb > 0.0:
            usable_vram_gb = round(min(base_usable_vram, vram_free_gb + ollama_loaded_gb), 2)
        else:
            usable_vram_gb = round(base_usable_vram, 2)
    else:
        usable_vram_gb = 0.0

    if ram_total_gb > 0.0:
        base_usable_ram = max(0.0, ram_total_gb - os_ram_reserve_gb)
        if ram_free_gb > 0.0:
            usable_ram_gb = round(min(base_usable_ram, ram_free_gb), 2)
        else:
            usable_ram_gb = round(base_usable_ram, 2)
    else:
        usable_ram_gb = 0.0

    if model_params_b is not None and model_params_b > 0:
        eff_params_b = float(model_params_b)
    elif model_weights_gb is not None and model_weights_gb > 0:
        eff_params_b = round(float(model_weights_gb) / 0.65, 2)
    elif vram_total_gb >= 20.0:
        eff_params_b = 27.0
    elif vram_total_gb >= 11.0:
        eff_params_b = 14.0
    elif vram_total_gb >= 5.5:
        eff_params_b = 7.0
    else:
        eff_params_b = 3.0

    if model_weights_gb is not None and model_weights_gb >= 0.0:
        est_weights_gb = round(float(model_weights_gb), 2)
    elif model_params_b is None and ollama_loaded_gb > 0.5:
        est_weights_gb = round(ollama_loaded_gb, 2)
    else:
        est_weights_gb = round(eff_params_b * 0.65, 2)

    norm_kv_quant = (kv_quant or "q8_0").strip().lower()
    if norm_kv_quant not in _KV_BYTES_PER_ELEM:
        norm_kv_quant = "q8_0"

    layers, kv_heads, head_dim = _infer_fast_geometry(eff_params_b)
    kv_gb_per_1k = compute_kv_gb_per_1k(
        num_layers=layers,
        num_kv_heads=kv_heads,
        head_dim=head_dim,
        kv_quant=norm_kv_quant,
    )

    if vram_total_gb > 0.0:
        kv_budget_vram_gb = round(max(0.0, usable_vram_gb - est_weights_gb), 2)
        raw_max_fast_k = (kv_budget_vram_gb / kv_gb_per_1k) if kv_gb_per_1k > 0 else 2.0
        max_fast_ctx = snap_ctx_floor(raw_max_fast_k * 1024.0, max_cap=131072)
        rec_fast_ctx = snap_ctx_floor((raw_max_fast_k * 0.72) * 1024.0, max_cap=max_fast_ctx)
        if rec_fast_ctx == max_fast_ctx and max_fast_ctx > 4096:
            idx = FAST_CTX_STEPS.index(max_fast_ctx)
            rec_fast_ctx = FAST_CTX_STEPS[max(0, idx - 1)]
    else:
        kv_budget_vram_gb = 0.0
        cpu_kv_budget_gb = max(0.0, usable_ram_gb - est_weights_gb)
        raw_max_cpu_k = (cpu_kv_budget_gb / kv_gb_per_1k) if kv_gb_per_1k > 0 else 2.0
        max_fast_ctx = snap_ctx_floor(raw_max_cpu_k * 1024.0, max_cap=32768)
        rec_fast_ctx = snap_ctx_floor((raw_max_cpu_k * 0.60) * 1024.0, max_cap=min(8192, max_fast_ctx))

    colibri_running_gb = max(0.0, float(current_colibri_ram_gb or 0.0))
    vk_host_pool_gb = float(colibri_geom["vk_pool_gb"])
    ram_ceiling_gb = max(0.0, ram_total_gb - os_ram_reserve_gb - vk_host_pool_gb)
    avail_for_heavy_gb = min(
        ram_ceiling_gb,
        max(0.0, usable_ram_gb + colibri_running_gb - vk_host_pool_gb),
    )
    max_safe_ram = max(4, math.floor(avail_for_heavy_gb))
    rec_ram = max(4, min(max_safe_ram, round(max_safe_ram * 0.75)))
    hard_max_ram = max(max_safe_ram, min(128, max(32, round(ram_total_gb) if ram_total_gb > 0 else 64)))

    heavy_kv_gb_per_1k = compute_kv_gb_per_1k(
        num_layers=int(colibri_geom["layers"]),
        num_kv_heads=int(colibri_geom["kv_heads"]),
        head_dim=int(colibri_geom["head_dim"]),
        kv_quant=norm_kv_quant,
    )
    heavy_trunk_vram_gb = 3.5 if vram_total_gb >= 8.0 else 0.0
    heavy_kv_budget_gb = (
        max(0.0, usable_vram_gb - heavy_trunk_vram_gb)
        if vram_total_gb > 0.0
        else max(0.0, avail_for_heavy_gb - float(max_safe_ram))
    )
    raw_max_heavy_k = (heavy_kv_budget_gb / heavy_kv_gb_per_1k) if heavy_kv_gb_per_1k > 0 else 4.0
    max_heavy_ctx = snap_ctx_floor(raw_max_heavy_k * 1024.0, max_cap=int(colibri_geom["max_context"]))
    rec_heavy_ctx = snap_ctx_floor((raw_max_heavy_k * 0.65) * 1024.0, max_cap=min(32768, max_heavy_ctx))
    if rec_heavy_ctx == max_heavy_ctx and max_heavy_ctx > 8192:
        idx = FAST_CTX_STEPS.index(max_heavy_ctx)
        rec_heavy_ctx = FAST_CTX_STEPS[max(0, idx - 1)]

    coord = get_gateway_coordinator()
    tuning_state = coord.get_tuning_state()

    return {
        "ok": True,
        "layer": "layer1_math",
        "fast_ctx": {
            "min": 2048,
            "recommended": rec_fast_ctx,
            "max_safe": max_fast_ctx,
            "active": tuning_state.get("fast_num_ctx"),
            "steps": list(FAST_CTX_STEPS),
            "zones": {
                "green_max": rec_fast_ctx,
                "amber_max": max_fast_ctx,
                "hard_max": 131072,
            },
        },
        "heavy_ram_limit_gb": {
            "min": 4,
            "recommended": rec_ram,
            "max_safe": max_safe_ram,
            "active": tuning_state.get("heavy_ram_limit_gb"),
            "zones": {
                "green_max": rec_ram,
                "amber_max": max_safe_ram,
                "hard_max": hard_max_ram,
            },
        },
        "heavy_ctx": {
            "min": 2048,
            "recommended": rec_heavy_ctx,
            "max_safe": max_heavy_ctx,
            "active": tuning_state.get("heavy_ctx"),
            "steps": list(FAST_CTX_STEPS),
            "zones": {
                "green_max": rec_heavy_ctx,
                "amber_max": max_heavy_ctx,
                "hard_max": 131072,
            },
        },
        "budget": {
            "vram_total_gb": round(vram_total_gb, 2),
            "vram_free_gb": round(vram_free_gb, 2),
            "ram_total_gb": round(ram_total_gb, 2),
            "ram_free_gb": round(ram_free_gb, 2),
            "os_vram_reserve_gb": os_vram_reserve_gb,
            "os_ram_reserve_gb": os_ram_reserve_gb,
            "usable_vram_gb": usable_vram_gb,
            "usable_ram_gb": usable_ram_gb,
            "model_params_b": eff_params_b,
            "model_weights_gb": est_weights_gb,
            "kv_quant": norm_kv_quant,
            "kv_gb_per_1k": kv_gb_per_1k,
            "heavy_kv_gb_per_1k": heavy_kv_gb_per_1k,
            "kv_budget_vram_gb": kv_budget_vram_gb,
            "vk_host_pool_gb": vk_host_pool_gb,
            "colibri_planner_used": bool(colibri_geom["colibri_planner_used"]),
        },
    }
