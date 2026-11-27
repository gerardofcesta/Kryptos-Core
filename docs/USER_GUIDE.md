# Kryptos Core v3.0 — User Guide

## 1. First Launch & Dual-Binding Loopback Gateway
When you launch the desktop shell (`kryptos-desktop`), **Kryptos Core** starts an embedded loopback server bound to `127.0.0.1:0` (ephemeral kernel-assigned port) and atomically writes `<data_dir>/runtime.json` (`kryptos.runtime.v1`) with strict owner-only permissions (`0600` on POSIX, user-only ACL on Windows).

- **Standard Privacy UX:** Zero hardcoded localhost ports or IP clutter appear in the primary workspace header.
- **Developer & IDE Integration Drawer:** Open **Settings -> Developer & IDE Integration** and enable **Fixed IDE Port (`:8787`)** whenever you want **Cursor**, **Continue**, **Cline**, or **MCP clients** to connect on `:8787` (with automatic `8787–8795` fallback if occupied).

## 2. Smart Model Manager & Zero-Copy Discovery
- **Hardware-Fit Traffic Light:** Every catalog or custom GGUF entry displays `🟢 GPU Fluid`, `🟡 Hybrid GPU + RAM`, or `🔴 Too Heavy` alongside estimated `tok/s` based on your detected VRAM/RAM and KV-cache math.
- **Two-Step Explicit Authorization:**
  1. Click **Prepare** (`POST /api/models/download/prepare`) to inspect exact byte size, free disk margin (`>= 110%`), SPDX license, and expected SHA-256 digest from allowlisted HTTPS registries.
  2. Click **Authorize Download** (`POST /api/models/download/start`) to stream into `<models_dir>/.downloads/*.part` with **HTTP Range (`206`)** resume support and **4 MiB streaming SHA-256 verification**.
- **Zero-Copy Discovery:** Run **Scan Local Models** (`kryptos-core models discover`) to link existing weights from `~/.ollama/models`, `~/.cache/huggingface/hub`, or `~/.lmstudio/models` with `0 GB` extra disk consumption.

## 3. Drop-to-Chat Ghost RAG
Drag and drop `.pdf`, `.md`, `.txt`, `.py`, `.ts`, `.json`, `.csv`, or `.docx` files directly onto the chat composer:
- **Ghost Mode (default):** Chunks reside strictly in volatile RAM (`bytearray` zero-wiped with `0x00` on clear/shutdown).
- **Interactive Citations:** Responses cite `[filename, p.X]` or `[filename, L.A-B]`; click any citation pill to inspect the exact source passage.

## 4. Go Mode, Prompt Firewall & Overnight Batch
- **Go Mode (`L1–L5`):** Per-diff HITL approval, atomic rollback journal, allowlisted dev commands, workspace symlink containment, and **Level 5 Hard System Shield** guarding OS/Registry/Credential roots.
- **PII Prompt Firewall:** Toggle `X-Prompt-Firewall: true` to deterministically redact private keys, API tokens, IBANs, tax IDs, and emails before inference.
- **Headless SSH CLI:** Operate remotely over SSH via `kryptos-core serve`, `kryptos-core chat`, `kryptos-core models`, and `kryptos-core doctor --support-bundle bundle.zip`.
