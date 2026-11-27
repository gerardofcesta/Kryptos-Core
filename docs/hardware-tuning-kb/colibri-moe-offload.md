# Colibri Heavy MoE Offload & RAM Limit Tuning

## How Colibri Expert Offload Works
Colibri splits a large Mixture-of-Experts (MoE) model into two parts:
- **Dense trunk & attention layers**: kept resident in GPU VRAM (via CUDA or Vulkan `vk_chain_fit`) when GPU headroom allows, or in pinned host RAM.
- **Sparse MoE experts**: cached dynamically in a fixed-size RAM pool controlled by `--ram` (`HEAVY_MOE_RAM_GB` / `ramLimitGb`), streaming cold experts from NVMe SSD when needed.

## Safe `ramLimitGb` Sizing
- Always preserve an OS RAM safety floor: `OS_RAM_RESERVE = max(6.0 GB, 15% of physical RAM)` plus ~1.5 GB for Vulkan/runtime host pools.
- Setting `ramLimitGb` inside the **Green (Safe)** zone prevents Windows pagefile thrashing and severe PCIe/disk stalls.
- Values in the **Amber (Tight)** zone can work if no background browsers, IDEs, or Docker containers are competing for memory.
- Values in the **Red (Blocked)** zone exceed available physical RAM and risk immediate OOM or OS freeze.

## Applying Changes (`-Restart`) & Privacy Policy
- Changing `ramLimitGb` or `ctx` requires restarting the running Colibri process (`-Restart` flag via **Apply & Restart Engine**), because expert pools are allocated at server startup.
- Keep `KVSAVE=0` to enforce Zero-Disk Retention (no KV cache files written to disk).
