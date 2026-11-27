"""WU-41 Step 41.1 — 5-Tier Deterministic Hardware Simulation & Sizing Tests."""

from __future__ import annotations

import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from workspaces.local_llm_core.services.hardware_wizard import compute_hardware_tier_plan  # noqa: E402
from workspaces.local_llm_core.services.model_manager import evaluate_model_fit  # noqa: E402


def test_tier_a_low_cpu_only_8gb_ram() -> None:
    plan = compute_hardware_tier_plan(
        vram_gb=0.0,
        ram_gb=8.0,
        cpu_threads=4,
        unified_memory=False,
        gpu_backend="cpu",
    )
    assert plan["tier_id"] == "tier_a"
    assert plan["recommended_max_params_b"] <= 3
    assert plan["num_ctx"] == 2048
    assert plan["n_gpu_layers"] == 0
    assert plan["threads"] == 3
    assert plan["oom_safe"] is True

    # Oversized 14B Q4 model (9 GB) must be flagged 🔴 on Tier A 8GB RAM
    heavy_fit = evaluate_model_fit(
        9.0,
        quant="Q4_K_M",
        hardware={"vram_total_gb": 0.0, "ram_total_gb": 8.0, "ram_free_gb": 6.5},
        ctx=2048,
    )
    assert heavy_fit["level"] == "red"
    assert heavy_fit["status"] == "too_heavy"


def test_tier_b_entry_8gb_vram_cuda_and_rocm() -> None:
    for backend in ("cuda", "rocm"):
        plan = compute_hardware_tier_plan(
            vram_gb=8.0,
            ram_gb=16.0,
            cpu_threads=8,
            unified_memory=False,
            gpu_backend=backend,
        )
        assert plan["tier_id"] == "tier_b"
        assert plan["gpu_backend"] == backend
        assert 7 <= plan["recommended_max_params_b"] <= 8
        assert plan["n_gpu_layers"] == -1
        assert plan["num_ctx"] == 8192
        assert plan["hardware_fit"]["level"] == "green"
        assert plan["oom_safe"] is True


def test_tier_c_enthusiast_16gb_vram_hybrid_moe() -> None:
    plan = compute_hardware_tier_plan(
        vram_gb=16.0,
        ram_gb=32.0,
        cpu_threads=16,
        unified_memory=False,
        gpu_backend="cuda",
        free_disk_gb=250.0,
    )
    assert plan["tier_id"] == "tier_c"
    assert 14 <= plan["recommended_max_params_b"] <= 27
    assert plan["num_ctx"] in (16384, 32768)
    assert plan["n_gpu_layers"] == -1
    assert plan["hybrid_moe_offload"] is True
    assert plan["heavy_moe_enabled"] is True
    assert plan["oom_safe"] is True

    # Large 27B/MoE model (~17 GB) triggers hybrid offload (🟡) instead of green or crash
    moe_fit = evaluate_model_fit(
        17.0,
        quant="Q4_K_M",
        hardware={"vram_total_gb": 16.0, "ram_total_gb": 32.0, "ram_free_gb": 24.0},
        ctx=8192,
    )
    assert moe_fit["level"] == "yellow"
    assert moe_fit["status"] == "hybrid_offload"


def test_tier_d_workstation_24gb_vram_64gb_ram() -> None:
    plan = compute_hardware_tier_plan(
        vram_gb=24.0,
        ram_gb=64.0,
        cpu_threads=24,
        unified_memory=False,
        gpu_backend="cuda",
        free_disk_gb=500.0,
    )
    assert plan["tier_id"] == "tier_d"
    assert plan["recommended_max_params_b"] >= 32
    assert plan["num_ctx"] == 32768
    assert plan["heavy_moe_enabled"] is True
    assert plan["n_gpu_layers"] == -1
    assert plan["oom_safe"] is True


def test_tier_e_unified_memory_16gb_and_32gb_respects_70pct_cap() -> None:
    plan_16 = compute_hardware_tier_plan(
        vram_gb=0.0,
        ram_gb=16.0,
        cpu_threads=8,
        unified_memory=True,
        gpu_backend="metal",
    )
    assert plan_16["tier_id"] == "tier_e"
    assert plan_16["gpu_backend"] == "metal"
    assert plan_16["memory_budget_gb"] == 11.2
    assert plan_16["hardware_fit"]["unified_memory"] is True
    assert plan_16["hardware_fit"]["unified_cap_gb"] == 11.2
    assert plan_16["oom_safe"] is True

    plan_32 = compute_hardware_tier_plan(
        vram_gb=0.0,
        ram_gb=32.0,
        cpu_threads=12,
        unified_memory=True,
        gpu_backend="rocm",
    )
    assert plan_32["tier_id"] == "tier_e"
    assert plan_32["gpu_backend"] == "unified"
    assert plan_32["memory_budget_gb"] == 22.4
    assert plan_32["num_ctx"] == 16384
    assert plan_32["oom_safe"] is True

    # 16 GB Unified host attempting a 20 GB model MUST be blocked (🔴) at 70% cap (11.2 GB)
    oversized_unified = evaluate_model_fit(
        20.0,
        quant="Q4_K_M",
        hardware={
            "vram_total_gb": 16.0,
            "ram_total_gb": 16.0,
            "ram_free_gb": 14.0,
            "unified_memory": True,
        },
        ctx=4096,
    )
    assert oversized_unified["level"] == "red"
    assert oversized_unified["unified_cap_gb"] == 11.2
