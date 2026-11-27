# Hardware Compatibility Matrix (`Kryptos Core v3.0`)

> Deterministic 5-tier sizing verified by [`workspaces/tests/test_hardware_tiers_v3.py`](../workspaces/tests/test_hardware_tiers_v3.py) and `compute_hardware_tier_plan()` in [`workspaces/local_llm_core/services/hardware_wizard.py`](../workspaces/local_llm_core/services/hardware_wizard.py).

## 5-Tier Reference Profiles

| Tier | Hardware Class | RAM / VRAM | Backend | Default Model Target | `num_ctx` | `n_gpu_layers` | CPU Threads | Heavy MoE / Hybrid Offload |
| :---: | :--- | :--- | :--- | :--- | :---: | :---: | :---: | :---: |
| **Tier A** | Low CPU-Only | `8 GB` RAM · `0 GB` VRAM | CPU | `<= 3B Q4_K_M` | `2048` | `0` | `cores - 1` | Disabled |
| **Tier B** | Entry GPU | `16 GB` RAM · `8 GB` VRAM | CUDA / ROCm | `7B–8B Q4_K_M` | `8192` | `-1` (Full) | `min(8, cores // 2)` | Disabled |
| **Tier C** | Enthusiast GPU | `32 GB` RAM · `16 GB` VRAM | CUDA / ROCm | `14B–27B Q4_K_M` | `16384–32768` | `-1` (Full/Hybrid) | `min(12, cores // 2)` | Hybrid MoE enabled |
| **Tier D** | Workstation GPU | `64+ GB` RAM · `24+ GB` VRAM | CUDA / ROCm | `32B–70B Q4_K_M` / Heavy MoE | `32768` | `-1` (Full/Hybrid) | `min(16, cores // 2)` | Full + Heavy MoE |
| **Tier E** | Unified Memory (Apple Silicon / Linux APU) | `16 GB` or `32 GB` Unified | Metal / Unified | `7B Q4_K_M` (`16 GB`) · `14B Q4_K_M` (`32 GB`) | `8192–16384` | `-1` | `cores - 2` | `70%` hard memory cap |

---

## Memory-Fit Classification (`evaluate_model_fit`)

Formula: `required_gb = model_size_gb * 1.15 + kv_cache_gb(ctx)`.

| Traffic Light | Condition (Discrete GPU / CPU) | Condition (Unified Memory `Tier E`) | Expected Throughput & Behavior |
| :--- | :--- | :--- | :--- |
| **Green (`gpu_fluid`)** | `required_gb <= available_vram_gb * 0.90` | `required_gb <= total_ram_gb * 0.70` | Full GPU / Unified residence (`~20–120 tok/s`). |
| **Yellow (`hybrid_offload`)** | `required_gb <= available_vram_gb + free_ram_gb * 0.75` | `required_gb <= total_ram_gb * 0.85` | Partial CPU/RAM offload (`~6–18 tok/s`). |
| **Red (`too_heavy`)** | Exceeds hybrid threshold | Exceeds `85%` unified RAM | Explicit warning prior to download/load to prevent swap/OOM. |

## Cross-Platform Acceleration Notes

- **NVIDIA CUDA (Windows / Linux):** Detected via `nvidia-smi` with flash-attention and `q8_0` KV-cache enabled at `>= 6 GB` VRAM.
- **AMD ROCm (Linux):** Detected via `rocm-smi` / sysfs (`amdgpu`); GPU offload activates at `>= 6 GB` VRAM.
- **Apple Silicon Metal (macOS) & Unified APUs (Linux):** Strictly capped at **`70%`** of unified RAM so OS compositor buffers never starve or trigger swap thrashing.
- **CPU-Only Hosts:** Forces `n_gpu_layers=0`, conservative `num_ctx=2048` on `<= 12 GB` hosts, and zero GPU driver dependency.
