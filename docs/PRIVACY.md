# Privacy & Zero-Telemetry Model — Kryptos Core v3.0

**Kryptos Core** is engineered from first principles for **computational sovereignty**, **zero telemetry**, and **defense-in-depth** on local workstations.

---

## 1. Zero-Telemetry & Loopback Network Confinement

- **Loopback-Only Enforcement:** Every managed service (`gateway`, `Ollama`, `Colibri`, `LM Studio`, `llama.cpp`) normalizes and binds strictly to `127.0.0.1` / `localhost` / `[::1]`.
- **Telemetry Disabled by Default:** `.env.example` and process runners enforce `DO_NOT_TRACK=1`, `HF_HUB_DISABLE_TELEMETRY=1`, `SCARF_NO_ANALYTICS=true`, `ANONYMIZED_TELEMETRY=false`, `OLLAMA_NOHISTORY=1`, `OLLAMA_NO_CLOUD=1`, and `KVSAVE=0`.
- **Explicit-Consent Model Downloads:** Remote pulls via `/api/models/download/prepare` -> `/api/models/download/start` never run automatically, restrict connections to allowlisted HTTPS model hosts, and verify streaming SHA-256 digests.
- **Zero-CDN Frontend:** All stylesheets, icons, and React bundles are compiled locally inside `workspaces/offline-chat/web/dist/` with zero external URLs.

---

## 2. Volatile RAM-Only Execution (`Ghost Mode`)

- **Ephemeral Desktop Window:** `desktop.py` enforces `pywebview` `private_mode=True` with `storage_path=None`, preventing browser disk caches, cookies, or `localStorage` persistence.
- **RAM-Only Chat & Session Token:** Conversation state and the active gateway session token reside strictly in process/browser RAM.
- **Ghost RAG Ephemeral RAM Indexing (Zero Intentional Disk Persistence):** Document chunks uploaded in Ghost mode are indexed exclusively in volatile RAM (never written to SQLite, vector DB files, or disk temp directories) and mutable `bytearray` chunk stores are overwritten with `0x00` upon TTL expiration, `/api/rag/clear`, or shutdown as a best-effort memory hygiene measure.
- **Transparent Memory Boundaries (Threat Model):** Because Kryptos Core runs in user-space CPython alongside external inference runtimes (`Ollama`, `llama.cpp`, `Colibri`), best-effort `bytearray` zeroization does **not** zeroize transient immutable Python `str`/`bytes` objects created during PDF/DOCX parsing prior to garbage collection, unencrypted OS swap/pagefile pages (note: `privacy_audit.py --strict` checks `EncryptPagingFile=1` on Windows), or active attention KV-caches held inside GPU VRAM during generation.
- **Volatile KV-Cache Policy:** Heavy MoE sessions default to `KVSAVE=0` so intermediate attention states are never intentionally serialized to NVMe/SSD storage in Ghost mode.

---

## 3. Gateway & Filesystem Hardening

- **Anti-DNS-Rebinding & Anti-CSRF:** `GatewaySecurityMiddleware` validates `Host`, `Origin`, and `Sec-Fetch-Site` against loopback allowlists and requires an owner-only `gateway.token` (`0600` POSIX / user-only Windows ACL) via `X-Kryptos-Token` or `Authorization: Bearer`.
- **Strict CSP & No-Store Caching:** UI responses enforce `Content-Security-Policy: default-src 'self'; script-src 'self'; style-src 'self'; object-src 'none'; frame-ancestors 'none'` and `Cache-Control: no-store`.
- **MCP OWASP Guardrails:** `/mcp` enforces token auth, blocks external URLs and cloud metadata IPs (anti-SSRF), and denies access to `.env`, `.ssh`, `.aws`, `.pem`, and `gateway.token`.
- **Go Mode Tier 5 Hard Shield:** System directories (`Windows`, `System32`, `Program Files`, `/etc/shadow`, Boot/EFI, `HKLM`), destructive system binaries (`format`, `diskpart`, `bcdedit`, `reg`, `shutdown`), and credential stores are unconditionally blocked unless the operator completes the manual no-paste Break-Glass ritual.

---

## 4. Automated Verification

Run the cross-platform zero-dependency privacy audit anytime:

```bash
uv run python scripts/privacy_audit.py --strict
```
