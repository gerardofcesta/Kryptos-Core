# Benchmark Methodology & Reproducible Cards — Kryptos Core v3.0

**Kryptos Core** measures real local inference performance across quantized weights, backends, and KV-cache configurations without masking cold-load latency or reasoning-token overhead.

---

## 1. Metrics Captured (`POST /api/benchmark/run`)

Every benchmark run records:

- **Cold Start vs. Warm Residency:** Whether model weights were loaded from NVMe/SSD into VRAM/RAM during the request.
- **TTFT (`ttft_ms`):** Time to first generated token (including prompt prefill).
- **Separated Token Accounting:**
  - `thinking_tokens` & `thinking_tok_s` (chain-of-thought inside `<think>` / `reasoning_content`)
  - `visible_tokens` & `visible_tok_s` (final user-visible output)
  - `total_tokens` & `tok_per_sec` (aggregate decode throughput)
- **Sanitized Hardware Context:** GPU tier, total/free VRAM, and total/free system RAM.

---

## 2. Running a Benchmark

### Desktop UI
1. Open the **ModelManager & Wizard** tab.
2. Select a virtual routing alias (`local-fast`, `local-balanced`, `local-heavy`) or any discovered local model.
3. Toggle **Warm-up** (optional) and click **Run Benchmark**.
4. Click **Copy Markdown Card** or **Export JSON** for reproducible comparisons.

### PowerShell CLI
```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\benchmark-model.ps1 -Tier fast
```

---

## 3. Example Reproducible Benchmark Card

```markdown
### Kryptos Core Benchmark Card — `local-fast`

| Metric | Value |
| :--- | :--- |
| Model / Tier | `local-fast` (fast via ollama) |
| Run Type | Warm |
| TTFT | 145 ms |
| Throughput (Total) | **62.4 tok/s** (256 tok in 4.10 s) |
| Thinking Tokens | 96 tok (61.2 tok/s) |
| Visible Tokens | 160 tok (63.1 tok/s) |
| Hardware | 16 GB Discrete GPU · RAM 32 GB |
```

See [`benchmarks/RESULTS.md`](../benchmarks/RESULTS.md) for the reference template.
