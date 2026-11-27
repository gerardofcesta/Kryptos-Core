# Changelog — Kryptos Core

## [3.0.0] — 2026-11-27

### Added & Rebranded
- **Kryptos Core v3.0 Identity & Open-Source Attribution:** Unified package (`kryptos-core`), CLI entrypoints (`kryptos-core`, `kryptos`, `kryptos-desktop`), runtime schema (`kryptos.runtime.v1`), MIT license finalization, and [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) crediting upstream local inference runtimes (**Colibri** Apache 2.0, **Ollama** MIT, **llama.cpp** MIT).
- **Cross-Platform Core:** Cross-platform `instance_lock.py` (POSIX `fcntl.flock` + Win32 `CreateMutexW`), XDG/macOS/Win32 `paths.py`, pure-Python `engine_runner.py` with multi-OS GPU detection (CUDA, ROCm, Metal, CPU), Linux `--app=` browser fallback, and zero-dependency `scripts/privacy_audit.py`.
- **Dual-Binding UX (`runtime.json`):** Ephemeral `127.0.0.1:0` default gateway with atomic `<data_dir>/runtime.json` (`0600`) discovery and opt-in *Developer & IDE Integration* toggle for fixed `:8787`.
- **Smart Model Downloader v3:** `evaluate_model_fit()` (`🟢/🟡/🔴` + `tok/s`), 2-step explicit consent (`prepare -> start`), `HTTP Range` (`206`) resumable `.part` downloads, 4 MiB streaming SHA-256 verification, and Zero-Copy discovery (`discover-local`) for Ollama, HuggingFace Hub, and LM Studio.
- **Drop-to-Chat Ghost RAG:** In-memory `/api/chat/ephemeral-docs` ingestion (`0x00` wipe on clear) for PDF/Markdown/Text/Code/DOCX with clickable source excerpt pills (`[File, p.X]` / `[File, L.A-B]`).
- **5-Tier Hardware Benchmark Matrix & Compatibility Docs:** Deterministic Tiers A–E (`compute_hardware_tier_plan`) including strict `70%` Unified Memory cap (`Tier E`).
- **Prompt Firewall & Deep Adversarial Security Audit:** Opt-in local secret/PII redactor (`prompt_firewall.py`), SSRF/redirect/IP-obfuscation hardening, workspace symlink escape blocking, `workspaces/tests/test_adversarial_security_v3.py`, and `docs/SECURITY_AUDIT_V3.md`.
- **Headless CLI, Multi-OS Packaging & Release Sanitizer:** `kryptos-core` CLI (`serve`, `chat`, `models`, `doctor --support-bundle`), `build_linux.sh`, `install.sh`, `build_macos.sh`, `build_dist.ps1`, `.github/workflows/{ci,release}.yml`, `generate_release_manifest.py` (`SHA256SUMS.txt` + CycloneDX SBOM), and `prepare_public_release.py`.

## [2.1.0] — Architecture Baseline
- Standalone zero-CDN SPA, IDE config generators, Native Desktop Shell, Dual-Tier Engine Coordinator, Orchestrator Safe Mode, Go Mode 5-Tier Guardrails, and Overnight Batch Runner.
