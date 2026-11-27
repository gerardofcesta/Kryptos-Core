# Contributing to Kryptos Core

Thank you for your interest in contributing to **Kryptos Core**!

> **License:** Contributions are accepted under the [MIT License](LICENSE). Please review [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) when integrating or documenting external inference runtimes.

---

## 1. Local Development Setup

```bash
# Create Python 3.12 virtual environment with uv
uv venv .venv --python 3.12
uv pip install -e ".[dev,ocr]"

# Build the self-hosted Zero-CDN frontend
cd workspaces/offline-chat/web
npm ci
npm run build
```

---

## 2. Repository Layout

- `workspaces/local_llm_core/` — FastAPI loopback gateway, `GatewaySecurityMiddleware`, desktop shell (`desktop.py` / `desktop_shell.py`), Ghost/Vault RAG stores, 5-tier hardware sizing & tuning bounds, Smart Model Downloader, Prompt Firewall, Go Mode L1–L5, Batch Runner, and unit/integration tests.
- `workspaces/orchestrator/` — Autonomous state machine, `code_audit` (anti-slopsquatting & secret scan), and `--network none` sandbox runners.
- `workspaces/offline-chat/web/` — React + TypeScript + Vite + Tailwind CSS SPA with self-contained `src/ui-kit/`.
- `config/` — Portable configuration templates (`endpoints.json`, `model-catalog.json`, `hardware-profile.example.json`).
- `scripts/` & `launcher/` — Multi-OS build pipelines, zero-dependency `privacy_audit.py`, release SBOM/SHA-256 generator, and git history sanitizer (`prepare_public_release.py`).

---

## 3. Non-Negotiable Architectural & Privacy Invariants

Every pull request **must** uphold these guarantees:

1. **100% Standalone & Zero-CDN:** Zero external runtime CDN calls, fonts, scripts, or telemetry beacons in `web/dist/` or Python modules.
2. **Strict Loopback Binding:** Services bind exclusively to `127.0.0.1` / `localhost` / `[::1]`.
3. **Zero Disk Chat Logs & Ghost RAM Wipe:** Never persist user prompts, completions, ephemeral RAG chunks, or gateway tokens to unencrypted logs.
4. **Zero Weights, Binaries, or Workstation Leaks:** Never commit `.gguf` weights, external inference binaries (`engines/` is gitignored), `.env` files, personal usernames, or machine-specific hardware profiles (`config/hardware-profile.json` is gitignored).

---

## 4. Pre-PR Verification Gate

```bash
# 1. Full unit, hardware-tier & adversarial security suite
uv run pytest -m "not docker" -q

# 2. Frontend TypeScript typecheck & Zero-CDN bundle
npm --prefix workspaces/offline-chat/web run build

# 3. Strict 10-check privacy audit & release sanitizer
uv run python scripts/privacy_audit.py --strict
uv run python scripts/prepare_public_release.py --check
```
