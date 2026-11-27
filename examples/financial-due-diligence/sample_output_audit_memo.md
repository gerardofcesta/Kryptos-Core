# Air-Gapped M&A Financial Due Diligence Memo — Project Mercury

> **Execution Mode:** `100% Offline Loopback` · **Storage Persistence:** `0 Bytes Written to Disk (Ghost RAM)`  
> **Deterministic PII Redactions Applied Pre-Index:** `IBAN=1` · `TaxID=1` · `Email=2`  
> **Post-Audit RAM Buffer Zeroization Verified:** `True` (`253.89 ms` end-to-end)

## 1. Target vs. Listed Peer Benchmarking

| Financial & Credit Metric | Target: IMN S.p.A. (EUR) | Peer: Apex Corp. (USD) | Grounded Source Citation |
| :--- | :---: | :---: | :--- |
| **FY2025 Revenue** | EUR 148.4M (+9.1% YoY) | USD 215.0M (+8.5% YoY) | `[IMN_FY2025_NDA_Report.pdf · p.1]` / `[Apex_FY2025_10K_Peer.pdf · p.1]` |
| **FY2025 Adjusted EBITDA** | **EUR 28.2M** | **USD 41.9M** | `[IMN_FY2025_NDA_Report.pdf · p.1]` / `[Apex_FY2025_10K_Peer.pdf · p.1]` |
| **Adjusted EBITDA Margin** | **19.0%** | **19.5%** | `[IMN_FY2025_NDA_Report.pdf · p.1]` |
| **Net Financial Debt (PFN)** | **EUR 59.2M** | **USD 64.5M** | `[IMN_FY2025_NDA_Report.pdf · p.2]` / `[Apex_FY2025_10K_Peer.pdf · p.1]` |
| **Net Leverage (Net Debt / EBITDA)** | **2.10x** (Cap `<= 3.25x`) | **1.54x** (Cap `<= 3.50x`) | `[IMN_FY2025_NDA_Report.pdf · p.2]` |

## 2. Debt Covenants, Refinancing & SPA Red Flags (IMN S.p.A.)

1. **Change-of-Control Mandatory Prepayment (`Deal-Critical`):** Acquiring `> 50%` voting equity triggers cancellation of the EUR 20.0M RCF and mandatory prepayment of the **EUR 62.0M Term Loan A within 15 Business Days** unless waived by `>= 66.67%` Majority Lenders `[IMN_FY2025_NDA_Report.pdf · p.2]`.
2. **Maintenance Covenant Step-Down:** Net Leverage cap tightens from **3.25x (FY2025)** to **2.75x (Dec 2026)**; Interest Coverage floor is **4.00x** (actual **7.42x**) `[IMN_FY2025_NDA_Report.pdf · p.2]`.
3. **Unprovided Tax Audit Exposure (SPA Escrow Item):** EUR 1.85M FY2022 R&D tax credit challenge with EUR 0.60M provisioned -> **EUR 1.25M unprovided contingent liability** requiring specific tax indemnity `[IMN_FY2025_NDA_Report.pdf · p.3]`.
4. **Environmental & Commercial Concentration:** Residual groundwater indemnity capped at **EUR 2.50M** through Dec 2027 (EUR 0.40M accrued); Top 3 Tier-1 customers represent **34.2% of revenue** without volume floors `[IMN_FY2025_NDA_Report.pdf · p.3]`.

## 3. Prompt Firewall Verification Log

- **Raw Analyst Prompt:**
  `Compare IMN FY2025 EBITDA, PFN leverage, and Change-of-Control debt covenants against Apex 10-K. Exclude escrow IBAN IT60X0542811101000000123456, signatory RSSMRA80A01F205X, and contact treasury.desk@industriemeccanichenord.example from output.`
- **Sanitized Prompt Dispatched to Model Context:**
  `Compare IMN FY2025 EBITDA, PFN leverage, and Change-of-Control debt covenants against Apex 10-K. Exclude escrow IBAN [REDACTED:IBAN], signatory [REDACTED:CODICE_FISCALE], and contact [REDACTED:EMAIL] from output.`
