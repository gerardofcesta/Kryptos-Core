# Adversarial Security & Privacy Audit V3 — Kryptos Core

**Release Target:** `Kryptos Core v3.0.0`  
**Audit Date:** `2026-11-27`  
**Overall Verdict:** `PASS` (`0` High/Critical findings, `0` DOM-XSS findings, `0` vulnerable dependencies, full adversarial pytest & strict privacy gates passing)

---

## 1. Automated Gate & Privacy Verification Summary

| Verification Gate | Command / Scope | Observed Result |
| :--- | :--- | :--- |
| **Pytest Full + Adversarial Suite** | `workspaces/local_llm_core/tests` + `workspaces/tests` (`test_hardware_tiers_v3.py`, `test_adversarial_security_v3.py`, `test_wu42_cli_and_release.py`) | **`PASS`** (`0 failed`) |
| **Standalone Privacy Audit (`--strict`)** | `scripts/privacy_audit.py --strict` (10 checks: personal leaks, `.env`/weights, external URLs, CSP/no-store, `.gitignore`, standalone scope, secrets, gateway/orchestrator hardening, runtime hygiene) | **`10/10 PASS`** (`0 violations`) |
| **Release History & Size Sanitizer** | `scripts/prepare_public_release.py --check` (`<= 5 MB` tracked limit, binary/weight block, full-history secret scan) | **`PASS`** (`0 issues`) |
| **SAST & Frontend DOM-XSS Scan** | `bandit -ll` across Python modules + `.ts`/`.tsx` `dangerouslySetInnerHTML`/`innerHTML` audit | **`PASS`** (`0 HIGH`, `0 XSS`) |
| **Python & Frontend SCA** | `pip-audit` (`pyproject.toml`) + `npm audit --omit=dev --audit-level=high` | **`PASS`** (`0` High/Critical CVEs) |

---

## 2. Adversarial Controls Verified (`test_adversarial_security_v3.py`)

### A. Anti-SSRF & Downloader Allowlist Hardening
- **Implemented in:** `workspaces/local_llm_core/services/model_manager.py`, `workspaces/local_llm_core/routes/mcp.py`.
- **Blocked Vectors (Verified):**
  - Cloud metadata endpoints (`169.254.169.254`, `100.100.100.200`, `metadata.google.internal`).
  - Direct loopback/internal ports (`http://127.0.0.1:22/`), raw IPv6 (`http://[::1]`), and IPv6-mapped IPv4 (`http://[::ffff:127.0.0.1]`).
  - Numeric IP obfuscation in decimal (`2130706433`, `2852039166`), octal (`0177.0.0.1`), and hexadecimal (`0x7f000001`).
  - Userinfo authority spoofing (`https://huggingface.co@127.0.0.1/...`), non-443 explicit HTTPS ports, and `file://` / `gopher://` / `ftp://` / `dict://` schemes.
  - Hop-by-hop redirect validation (`follow_redirects=False`, max 5 hops) blocking `302` redirects from allowlisted hosts to internal/metadata IPs.

### B. Gateway Anti-DNS-Rebinding, CSRF & Auth Enforcement
- **Implemented in:** `workspaces/local_llm_core/security.py` (`GatewaySecurityMiddleware`).
- **Blocked Vectors (Verified):**
  - Untrusted `Host` header (`evil.com`) -> `403 Forbidden`.
  - Cross-site `Origin` (`https://attacker.example`), `Origin: null`, and `Sec-Fetch-Site: cross-site` -> `403 Forbidden`.
  - Missing or invalid `X-Kryptos-Token` / `Authorization: Bearer` -> `401 Unauthorized`.
  - Non-JSON mutating requests (`Content-Type: text/plain`) -> `415 Unsupported Media Type`.

### C. Go Mode L1–L5 Hard Shield, Traversal & Symlink Containment
- **Implemented in:** `workspaces/local_llm_core/services/go_mode.py`.
- **Blocked Vectors (Verified):**
  - Direct OS system root targets (`..\..\Windows\System32\drivers\etc\hosts`, `/etc/shadow`).
  - Double-URL-encoded traversal (`%252e%252e%252f%252e%252e%252fetc%252fshadow`) and null bytes (`safe.txt\x00.md`).
  - Workspace symlink escape detection (`_symlink_escapes_workspace`) blocking workspace-relative symlinks resolving outside `workspace_root` (`403`, `blocked_by="symlink_workspace_escape"`).
  - Break-Glass ritual tampering (2 of 3 checkboxes -> `403`, incorrect manual phrase -> `403`), with all denials logged in RAM-only audit trail.

### D. Deterministic PII & Secret Prompt Firewall
- **Implemented in:** `workspaces/local_llm_core/prompt_firewall.py`, wired into `routes/openai_v1.py` (`/v1/chat/completions`) and `routes/mcp.py` (`/mcp`).
- **Redacted Classes (Opt-in via `X-Prompt-Firewall: true` or `KRYPTOS_PROMPT_FIREWALL=1`):**
  - Private key PEM blocks -> `[REDACTED:PRIVATE_KEY]`
  - API tokens (`sk-*`, `ghp_*`, `github_pat_*`, `AKIA*`, `xox*-*`) -> `[REDACTED:API_TOKEN]`
  - IBAN numbers -> `[REDACTED:IBAN]`
  - Italian Codice Fiscale -> `[REDACTED:CODICE_FISCALE]`
  - Email addresses -> `[REDACTED:EMAIL]`
