# Local Benchmark Methodology & Reference Template

> **Detailed Guide:** See [`docs/BENCHMARKS.md`](../docs/BENCHMARKS.md)  
> **In-App Runner:** Use **ModelManager & Wizard -> Run Benchmark** (`POST /api/benchmark/run`) to measure TTFT, thinking tokens, visible tokens/sec, and export a **Shareable Benchmark Card** (JSON / Markdown).

---

## 1. Standardized Suite (`scripts/benchmark-model.ps1`)

The CLI and in-app benchmark suite evaluates three repeatable categories with deterministic parameters:

1. **Technical Comprehension & Synthesis** — structured explanation & formatting.
2. **Multi-Step Logical Reasoning** — constraint satisfaction & step-by-step verification.
3. **Defensive Security / Code Review** — vulnerability & secret pattern analysis without cloud refusals.

---

## 2. Example Result Template

Replace the sample rows below with exports from your own hardware using **Export Benchmark Card (MD)**:

| Hardware Tier | Model / Alias | Quant | Runtime | Ctx | TTFT (ms) | Decode (tok/s) | Notes |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| *Example: 16 GB VRAM* | `local-fast` (14B) | `Q5_K_M` | Ollama (FA + KV `q8_0`) | 32768 | ~100–350 (warm) | ~55–70 | 100% GPU resident; ideal for Overnight Batch & Tool-Calling |
| *Example: 16 GB VRAM* | `local-balanced` (27B) | `UD-IQ4_XS` | Ollama (FA + KV `q8_0`) | 16384 | ~350–500 (warm) | ~24–28 | Balanced quality vs latency |
| *Example: 16 GB VRAM + 32 GB RAM* | `local-heavy` (MoE) | `UD-IQ4_XS` | Colibrì (VRAM+RAM+NVMe) | 16384 | ~8000–19000 | ~4.5–6.5 | Deep reasoning via RAM/NVMe offload; best for unattended overnight jobs |

---

## 3. Tier Selection Guidelines

- **Interactive Chat & Agentic Loops:** Prefer `local-fast` or `local-balanced` when 100% GPU resident for sub-second warm TTFT.
- **Overnight Batch & Deep Analysis:** Use `local-heavy` (with `Thermal & RAM Watchdog` enabled in the **Batch Overnight** tab) when depth matters more than interactive speed.
