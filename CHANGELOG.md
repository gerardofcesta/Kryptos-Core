# Changelog — Kryptos Core

All notable public changes to **Kryptos Core** are documented in this file.

> **Repository History Note:** Public history begins at `v3.0.0` via a sanitized open-source release cut (`scripts/prepare_public_release.py` + `scripts/privacy_audit.py --strict`) extracted from an internal multi-phase R&D repository to ensure zero historical workstation paths, local test artifacts, or private environment metadata enter public Git objects.

## [3.1.0] — 2026-11-27

### Added
- **NDA Financial Due Diligence Workflow (`examples/financial-due-diligence/`):** Reproducible 100% offline M&A / corporate credit analysis pipeline demonstrating ephemeral RAM-only RAG ingestion, deterministic PII redaction (`IBAN`, `Codice Fiscale`, emails, VDR tokens), and cited KPI/covenant extraction across Italian corporate and US 10-K filings.
- **Upstream Comparison Matrix (`README.md`):** Explicit architectural comparison clarifying how Kryptos Core complements `Ollama`, `LM Studio`, and `llama.cpp` with loopback security middleware, PII firewalling, and agent guardrails.
- **Environment Variable Alias (`prompt_firewall.py`):** Added native support for `KRYPTOS_PROMPT_FIREWALL=1` alongside `LOCAL_LLM_PROMPT_FIREWALL=1`.

### Changed
- **Threat Model & Memory Boundary Transparency (`SECURITY.md`, `docs/PRIVACY.md`, `README.md`):** Recalibrated Ghost RAG documentation to specify ephemeral RAM-only indexing with best-effort `bytearray` zeroization (`0x00`) and explicitly documented CPython immutable heap string copies, OS swap/pagefile boundaries, and GPU VRAM KV-cache residency.
- **VRAM Sizing Confidence Notes (`docs/HARDWARE_COMPATIBILITY_MATRIX.md`, `README.md`):** Documented heuristic confidence margins on `1.15 * Weights + KV` for sparse Mixture-of-Experts (MoE) and Multi-Head Latent Attention (MLA) architectures.

## [3.0.0] — 2026-11-27

### Added
- Initial sanitized open-source release of **Kryptos Core**: ephemeral port-0 loopback topology (`127.0.0.1`), `GatewaySecurityMiddleware` (anti-DNS-rebinding, anti-CSRF, constant-time bearer auth), 5-tier hardware sizing wizard, hop-by-hop anti-SSRF downloader, zero-copy model discovery, Ghost RAG, Deterministic Prompt Firewall, and Go Mode `L1–L5` guardrails (`67/67` adversarial security tests passing).
