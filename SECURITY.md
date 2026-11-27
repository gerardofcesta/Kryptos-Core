# Security Policy & Threat Model (`Kryptos Core v3.1`)

## Security Architecture & Invariants

1. **Zero Telemetry & Strict Loopback Isolation:**
   - All gateway and UI HTTP traffic binds exclusively to loopback (`127.0.0.1` / `localhost` / `[::1]`). Non-loopback bind attempts (`0.0.0.0`) are rejected by `kryptos-core serve` and desktop configuration validators.
   - `GatewaySecurityMiddleware` enforces loopback `Host` header allowlists (anti-DNS rebinding -> `403`), `Origin`/`Sec-Fetch-Site` validation (anti-CSRF -> `403`), constant-time `X-Kryptos-Token` / `Authorization: Bearer` authentication (`401`), `Content-Type: application/json` (`415`), `Cache-Control: no-store`, and strict CSP (`script-src 'self'`, zero `'unsafe-inline'`).
2. **Owner-Only File ACLs & Dual-Binding Discovery:**
   - `<data_dir>/gateway.token` and `<data_dir>/runtime.json` (`kryptos.runtime.v1`) are written atomically with owner-only ACLs (`0600` on POSIX, `icacls /inheritance:r` on Windows).
   - Process-wide single-instance locking (`instance_lock.py`) uses `fcntl.flock` on POSIX and `CreateMutexW` (`Local\KryptosCore_SingleInstance_v1`) on Win32.
3. **Anti-SSRF & Cryptographic Supply-Chain Integrity:**
   - Model downloads require explicit 2-step user authorization (`prepare -> start`), restrict origins to allowlisted HTTPS model registries, validate redirects hop-by-hop (`follow_redirects=False`), reject raw IPv4/IPv6/IPv6-mapped IPv4/decimal/octal/hex hosts and userinfo spoofing, and verify 4 MiB streaming SHA-256 digests before promoting `.part` files.
   - Release pipelines emit deterministic `SHA256SUMS.txt` checksums and `CycloneDX 1.5` SBOMs (`sbom.cyclonedx.json`).
4. **Deterministic Prompt Firewall & Go Mode Level 5 Hard Shield:**
   - Opt-in `prompt_firewall.py` redacts PEM private keys, API tokens, IBANs, fiscal codes, and email addresses prior to `/v1/chat/completions` and `/mcp` dispatch (`X-Prompt-Firewall: true` or `KRYPTOS_PROMPT_FIREWALL=1`).
   - Go Mode enforces Level 5 OS/Registry/Credential protection (`check_level5_hard_shield`), blocks null bytes and double-URL-encoded traversal (`%252e%252e`), prevents workspace symlink escapes (`_symlink_escapes_workspace`), and requires a multi-checkbox + manual-phrase Break-Glass ritual.
5. **Known Physical & Runtime Memory Boundaries (Ephemeral Ghost RAG):**
   - Ghost RAG enforces **zero intentional disk persistence** (no SQLite/vector-file writes) and performs best-effort `0x00` overwrites on mutable `bytearray` chunk stores upon TTL expiry or `/api/rag/clear`.
   - **Out-of-Scope Memory Artifacts:** Standard user-space CPython allocates transient immutable `str`/`bytes` buffers during document parsing (`pymupdf` / `defusedxml`) and JSON serialization that remain on the heap until overwritten by the allocator; OS virtual-memory paging may swap unlocked pages unless disk/swap encryption is enabled (`EncryptPagingFile=1` / LUKS / FileVault); and inference backends (`Ollama` / `llama.cpp`) retain prompt context in GPU VRAM KV-caches until evicted or process exit. For strict NDA workflows, operate on full-disk-encrypted workstations with encrypted swap.

## Reporting a Vulnerability

Before reporting a security issue:
1. Generate a sanitized diagnostic archive via `kryptos-core doctor --support-bundle bundle.zip` (which automatically strips usernames, hostnames, home paths, emails, IBANs, and API tokens).
2. Report privately through **GitHub Security Advisories** on this repository rather than opening public issues with unredacted logs.
3. Never attach `.env`, `gateway.token`, `runtime.json`, or raw model weights.
