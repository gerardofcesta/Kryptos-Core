# Third-Party Open-Source Notices & Upstream Engine Credits

**Kryptos Core** is an independent local AI workstation shell, zero-telemetry loopback gateway (`127.0.0.1:0` + `runtime.json`), RAM-only Ghost RAG engine, deterministic 5-tier Hardware-Fit Model Manager, and 5-tier OS security guardrail layer.

**Kryptos Core does NOT claim authorship of, nor redistribute binaries or model weights for, the upstream open-source inference engines it orchestrates.** All inference execution builds upon the extraordinary work of the open-source AI systems community.

---

## 1. Upstream Local Inference Runtimes (External / User-Installed)

When configured on a local workstation, Kryptos Core discovers and coordinates local loopback instances of the following open-source inference engines (none of whose binaries or weights are tracked in this repository):

| Upstream Project | Role in Kryptos Core | License | Notes |
| :--- | :--- | :---: | :--- |
| **Colibri** | Optional Heavy MoE Tier (`:11435`) — hybrid VRAM + System RAM + NVMe expert offloading for large Mixture-of-Experts models | **Apache License 2.0** | Excluded via `.gitignore` (`engines/`). Full credit to the upstream Colibri authors for high-efficiency hybrid MoE inference. |
| **Ollama** | Default Fast / Balanced Tier (`:11434`) & Zero-Copy manifest discovery | **MIT License** | Invoked locally with `OLLAMA_NO_CLOUD=1` and `OLLAMA_NOHISTORY=1`. |
| **llama.cpp** (`ggml-org/llama.cpp`) | Underlying GGUF quantization format & `llama-server` OpenAI-compatible runtime | **MIT License** | Powers quantized `Q4_K_M` / `Q8_0` CPU, CUDA, ROCm, and Apple Metal execution. |
| **LM Studio** (Optional) | Read-only Zero-Copy `.gguf` directory discovery (`~/.lmstudio/models`) | Upstream Terms | Read-only local path indexing (`0 GB` duplication); never modified. |

---

## 2. Direct Python & Frontend Libraries (Installed via `pyproject.toml` / `package.json`)

| Package | Ecosystem | License | Purpose |
| :--- | :---: | :---: | :--- |
| `fastapi` & `starlette` | PyPI | MIT | ASGI loopback API router & `GatewaySecurityMiddleware` |
| `uvicorn` | PyPI | BSD-3-Clause | Embedded loopback HTTP/1.1 & SSE server |
| `pydantic` | PyPI | MIT | Strict schema validation & path models |
| `httpx` | PyPI | BSD-3-Clause | Hop-by-hop anti-SSRF downloader & loopback backend client |
| `pywebview` | PyPI | BSD-3-Clause | Native ephemeral desktop window (`private_mode=True`) |
| `psutil` | PyPI | BSD-3-Clause | Cross-platform RAM/CPU inspection & `runtime.json` PID liveness |
| `defusedxml` | PyPI | PSF-2.0 | Safe `.docx` XML parsing (anti-XXE / billion-laughs) in Ghost RAG |
| `pymupdf` *(optional `ocr`)* | PyPI | AGPL-3.0 / Commercial (Optional Extra) | Optional local PDF page extraction; core runs 100% without it |
| `react`, `react-dom`, `lucide-react`, `tailwindcss`, `vite` | npm | MIT / ISC | Self-hosted Zero-CDN SPA interface |

---

## 3. Apache License 2.0 Attribution Notice (for Optional Colibri Integration)

If you install and connect **Colibri** under `engines/colibri/` on your workstation, your use of the Colibri runtime is governed by the **Apache License, Version 2.0** (`http://www.apache.org/licenses/LICENSE-2.0`), included alongside upstream Colibri distributions.
