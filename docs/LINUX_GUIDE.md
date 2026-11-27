# Linux Deployment & GPU Acceleration Guide (`Kryptos Core v3.0`)

## 1. Quick User-Space Installation (Zero Root Required)

```bash
git clone <your-repository-url> kryptos-core
cd kryptos-core
uv venv .venv --python 3.12
uv pip install -e ".[dev,ocr]"
./scripts/install.sh
```

What `install.sh` configures:
- Installs `kryptos-core`, `kryptos`, and `kryptos-desktop` into `~/.local/bin/` (via `uv tool install` or bundle symlink).
- Registers `~/.local/share/applications/kryptos-core.desktop` following XDG Desktop Entry specifications.
- Stores runtime discovery and gateway tokens under `~/.local/share/kryptoscore` (respecting `XDG_DATA_HOME`, `0700` dir / `0600` files) and ephemeral runtime files under `/tmp` or `XDG_RUNTIME_DIR` with zero persistent browser cache when ephemeral mode is active.

---

## 2. GPU Acceleration Backends

### NVIDIA CUDA
- Ensure `nvidia-smi` is on `PATH` and your local Ollama / `llama.cpp` / Colibri CUDA runtime matches your driver.
- Run `kryptos-core doctor` to verify `gpu_backend=cuda` and detected free/total VRAM.

### AMD ROCm
- Ensure `rocm-smi` (or `/sys/class/drm/card*/device/mem_info_vram_total`) is readable by your user (`video` / `render` groups).
- `detect_hardware_profile()` probes ROCm VRAM automatically without root privileges; `>= 6 GB` VRAM enables Tier B/C GPU profiles (`OLLAMA_FLASH_ATTENTION=1`).

### Unified Memory APUs
- Unified memory sizing (`compute_hardware_tier_plan(..., unified_memory=True)`) enforces the **`70%`** memory cap (`Tier E`) so Wayland/X11 compositors never starve or trigger Linux OOM-killer events.

---

## 3. Desktop Surface & Headless SSH Mode

- **Wayland / X11 Native Shell:** Uses `WebKit2GTK` (`pywebview`) when GTK/GI bindings (`python3-gi`, `gir1.2-webkit2-4.1`) are present.
- **Automatic Isolated Browser App Fallback:** When `WebKit2GTK` is absent, `kryptos-desktop` automatically probes Chromium, Brave, Google Chrome, or Microsoft Edge and launches a dedicated chromeless `--app=http://127.0.0.1:<port> --new-window` surface.
- **Headless SSH Servers:** Operate with zero X11/Wayland packages installed:
  ```bash
  kryptos-core serve --host 127.0.0.1 --port 0 &
  kryptos-core chat --firewall "Summarize error patterns in /var/log/syslog"
  ```

---

## 4. Building Tarball & AppImage Releases

```bash
./scripts/build_linux.sh
```

Artifacts emitted to `installer/out/`:
- `KryptosCore-v3.0.0-linux-x86_64.tar.gz`
- `KryptosCore-x86_64.AppImage` (when `appimagetool` is on `PATH`)
- `SHA256SUMS.txt` + `sbom.cyclonedx.json`
