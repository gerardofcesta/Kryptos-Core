# VRAM & System RAM Troubleshooting Guide

## Symptom 1: Sudden Drop from 50+ tok/s to < 5 tok/s
- **Cause**: VRAM overflow into Windows Shared GPU Memory (PCIe system RAM spill).
- **Fix**: Reduce `num_ctx` by one discrete step (e.g., `32768 -> 16384`), switch KV cache quantization to `q8_0`, or choose a smaller Q4_K_M / Q5_K_M quantization that fits inside `usable_vram_gb`.

## Symptom 2: System Stutter, High Disk Activity, or Anti-Swap Guard Error
- **Cause**: Heavy MoE `ramLimitGb` plus resident desktop apps exceed `ram_total_gb - OS_RAM_RESERVE`.
- **Fix**: Unload Ollama VRAM first (`unload_vram`), close heavy background tabs, and lower `ramLimitGb` into the **Green (Safe)** zone before clicking **Apply & Restart Engine**.

## Symptom 3: Mutual Exclusion (Fast vs Heavy MoE)
- Fast Tier (Ollama) and Heavy MoE (Colibri) never run concurrently to protect your VRAM and RAM budgets.
- Switching tiers requires explicit confirmation (`X-Allow-Tier-Switch: true`) or using the Engines control panel.
