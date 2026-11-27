# Offline M&A & Credit Financial Due Diligence (`NDA-Safe`)

## Business Problem
Investment Banking (M&A / Leveraged Finance), Private Equity, and Strategy Consulting teams routinely review confidential Information Memoranda, audited financial statements, and credit agreements governed by strict **Non-Disclosure Agreements (NDAs)**. Forwarding these files to external cloud LLM APIs violates data-room compliance policies and exposes banking details, tax IDs, and unannounced transaction terms.

This reproducible example demonstrates how **Kryptos Core** executes a **100% air-gapped financial due diligence audit** on local hardware with:
1. **Pre-Inference PII Redaction (`prompt_firewall.py`):** Deterministically strips IBANs, Italian Tax IDs (`Codice Fiscale`), and deal-room email addresses *before* chunks or prompts enter model context.
2. **Ephemeral RAM-Only Ghost RAG (`mode="ghost"`):** Parses multi-page PDFs strictly in volatile RAM (zero SQLite or vector-DB files written to disk) and attaches exact `[DOC · p.X]` page citations to every extracted metric.
3. **Post-Session Buffer Zeroization:** Clears mutable chunk buffers immediately upon audit completion.

---

## Included Data-Room Scenario (`filings/`)

- [`filings/01_Industrie_Meccaniche_Nord_FY2025_Report.md`](filings/01_Industrie_Meccaniche_Nord_FY2025_Report.md) — Multi-page IFRS financial report for an Italian precision-automation buyout target (**EUR 148.4M Revenue**, **EUR 28.2M Adjusted EBITDA**, **EUR 59.2M Net Financial Position / PFN**, Senior Facilities covenants, Change-of-Control prepayment clause, and R&D tax audit contingency + synthetic escrow IBAN/Tax ID).
- [`filings/02_Apex_Precision_Systems_FY2025_10K_Excerpt.md`](filings/02_Apex_Precision_Systems_FY2025_10K_Excerpt.md) — US GAAP Form 10-K peer benchmark (**USD 215.0M Revenue**, **USD 41.9M Adjusted EBITDA**, **1.54x Net Leverage**, Senior Notes incurrence covenants).

---

## Run in 1 Command (`100% Offline` — Disconnect Wi-Fi to Verify)

```bash
# Deterministic RAM-only RAG + PII Firewall extraction (< 1 second, zero cloud calls):
uv run --extra ocr python examples/financial-due-diligence/run_nda_due_diligence.py

# Optional: append live commentary from your local Ollama / llama.cpp model via Kryptos Gateway:
uv run --extra ocr python examples/financial-due-diligence/run_nda_due_diligence.py --live-llm http://127.0.0.1:8787
```

Inspect [`sample_output_audit_memo.md`](sample_output_audit_memo.md) for the exact generated Tear Sheet output.
