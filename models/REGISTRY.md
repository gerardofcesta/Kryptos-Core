# Model Registry & Tiering Guide

> **Curated Catalog:** [`config/model-catalog.json`](../config/model-catalog.json)  
> **Hardware Sizing:** Use the built-in **Hardware Setup Wizard** (`GET /api/system/hardware-wizard`) or `scripts/setup-wizard.ps1` to auto-match models to your VRAM and system RAM.

---

## 1. Canonical Gateway Aliases

| Gateway Alias | Default Role | Typical Hardware Tier | Notes |
| :--- | :--- | :--- | :--- |
| `local-fast` | Fast Chat, Tool-Calling & Overnight Batch | 8–16 GB VRAM (or CPU fallback) | Mapped to `local-worker:latest` (e.g., 14B Q5_K_M or 7B/8B Q4_K_M on 8 GB VRAM) |
| `local-balanced` | Deep Coding & General Reasoning | 16 GB+ VRAM | Mapped to `local-uncensored:latest` (e.g., 24B–27B Q4_K_M / IQ4_XS) |
| `local-heavy` | Heavy MoE Overnight & Deep Analysis | 16 GB+ VRAM + 32 GB+ RAM + NVMe | Served via Colibrì hybrid offload (`RAM_GB` bounded by `tuning-bounds`) |
| `auto` | Cascade Router | Automatic | Routes to `local-fast` and escalates on schema/validation failure |

---

## 2. Example Local Modelfiles

- [`models/modelfiles/local-worker.Modelfile`](modelfiles/local-worker.Modelfile) — Structured tool-calling & coding worker preset.
- [`models/modelfiles/qwen38-27b-uncensored.Modelfile`](modelfiles/qwen38-27b-uncensored.Modelfile) — Balanced uncensored preset with Flash Attention & KV `q8_0`.

---

## 3. Air-Gapped Integrity Verification

All local GGUF / safetensors weights in `LOCAL_LLM_MODELS_DIR` can be inventoried and SHA-256 verified against `manifest.sha256.json` via the **ModelManager** tab or `POST /api/models/verify`.
