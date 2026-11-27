# Console Web Offline Privata (`workspaces/offline-chat`)

Console operativa locale **100% offline, zero dipendenze esterne, zero telemetria e zero cronologia chat su disco**, progettata per orchestrare e usare entrambi i livelli di inferenza locali:

- **Fast Tier:** Ollama (`http://127.0.0.1:11434`, es. `local-uncensored` Qwen 3.8 27B Abliterated)
- **Heavy MoE Tier:** Colibrì (`http://127.0.0.1:11435`, API OpenAI-compatibile con streaming SSE, `reasoning_content`, `enable_thinking`, `reasoning_effort`)

---

## Garanzie di Privacy & Architettura

- **Zero dipendenze Python esterne:** `server.py` usa esclusivamente la Python standard library (`http.server`, `http.client`, `ctypes`, `subprocess`, `shutil`, `json`).
- **Zero CDN, font o script remoti:** `index.html` è HTML/CSS/JS vanilla autonomo.
- **Bind esclusivo loopback (`127.0.0.1:8787`):** nessuna esposizione verso rete locale o Internet.
- **Hardening HTTP Anti-CSRF / Anti-DNS-Rebinding / Auth Token:** whitelist rigorosa dell'header `Host` (`127.0.0.1`, `localhost`, `[::1]`), validazione `Origin`/`Sec-Fetch-Site`, `415 Unsupported Media Type` per `POST`/`PUT`/`PATCH` non-JSON, e token locale `%LOCALAPPDATA%\LocalLLM\gateway.token` (ACL NTFS utente corrente, ruotato al boot, servito alla SPA via `GET /api/auth/session-token` same-origin `no-store` e verificato tramite `X-Local-LLM-Token` o `Authorization: Bearer`).
- **Lock unificato `EngineCoordinator`:** serializza switch/arresto motori con `TierSwitcher` e restituisce `409 Conflict` se una run Orchestrator è attiva.
- **CSP rigorosa:** `default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:;`.
- **Zero log chat su disco o stdout:** `log_message` disabilitato, nessun salvataggio automatico di prompt o risposte. Le conversazioni vivono solo nella RAM del browser fino a **Pulisci RAM** o esportazione manuale (**Esporta MD** / **Esporta JSON**). Uniche scritture esplicite consentite su disco: salvataggio/eliminazione dei template in `prompts/` (e i vault Persistent dell'Orchestrator in `workspaces/vault/`).

---

## Funzionalità della Console (5 Tab)

1. **Chat Dual-Tier (Ghost RAM-Only):**
   - Switch rapido tra **Fast Tier (Ollama)** e **Heavy MoE (Colibrì)** con badge `ONLINE`/`OFFLINE` live e selettore modello.
   - Controlli completi: `Temperature`, `Top-P`, `Context (num_ctx)`, `Max Tokens`, toggle **Think ON/OFF**, **Reasoning Effort** (`low`/`medium`/`high`/`xhigh`), selettore Preset da `prompts/` + editor live del System Prompt.
   - Renderer Markdown pure JS sicuro (anti-XSS, tabelle, liste, blockquote, blocchi di codice con etichetta linguaggio e tasto **Copia**).
   - Sezione collassabile `<details>` per il ragionamento interno unificata per Ollama (`message.thinking`), Colibrì (`reasoning_content`) e tag inline `<think>...</think>`.
   - Azioni rapide: `Invia`, `Stop`, `Rigenera`, `Copia` per singolo messaggio, `Esporta MD`, `Esporta JSON`, `Pulisci RAM`.
   - Metriche per risposta: `TTFT`, `Prefill tok/s`, `Decode tok/s`, durata totale e token in/out.

2. **Motori & Risorse (Telemetria Live & Controllo Anti-Swap):**
   - Monitoraggio parallelo (cache TTL `1.5s`, sospeso quando `document.hidden === true`) di **GPU VRAM** (`nvidia-smi`), **RAM di sistema** (`GlobalMemoryStatusEx` con soglia `HEAVY_MOE_MIN_FREE_RAM_GB` e badge **Anti-Swap OK / Warning < 6 GB**) e **NVMe `C:\`**.
   - Stato live di Ollama (`11434`), Colibrì (`11435`, inclusa lettura di `models/heavy-moe/ACTIVE_MODEL.json` anche a motore spento) e LM Studio (`1234`).
   - Controlli operativi con mutua esclusione: **Avvia Fast Tier**, **Avvia Heavy MoE** (con `RAM Limit GB` e `Context` configurabili), **Libera VRAM (`ollama stop`)**, **Arresta Motori & Libera VRAM** (`stop-llm.ps1 -KeepUI`).

3. **Modelli & Benchmark Rapido:**
   - Inventario locale di modelli Ollama, file `.gguf` in `models/heavy-moe/`, tabella `models/REGISTRY.md` e storico `benchmarks/RESULTS.md`.
   - **Benchmark Rapido** sul backend attivo con cattura picco VRAM e generazione immediata della riga Markdown pronta per `benchmarks/RESULTS.md`.

4. **Libreria Prompt (`prompts/*.md`):**
   - Visualizzazione, modifica, creazione ed eliminazione dei preset YAML+Markdown in `prompts/` con aggiornamento immediato del selettore Chat. (`README.md` e `uncensored-direct.md` sono protetti).

5. **Orchestrator Autonomo (Ephemeral Privacy | Full Audit Persistent):**
   - Wizard missione con policy bloccata all'avvio (`ephemeral` solo RAM/tmpfs + `[Distruggi Report]` + *Dead-Man's Switch* vs `persistent` con archivio atomico in `workspaces/vault/` e Pausa/Ripresa) e sanitizzazione `target_path` (`ORCHESTRATOR_ALLOWED_ROOTS` + denylist sistema).

---

## Endpoint Esposti da `server.py`

- **UI:** `GET /`, `GET /index.html`, `GET /favicon.ico`
- **Auth SPA Locale:** `GET /api/auth/session-token` (`Host` + `Sec-Fetch-Site: same-origin` + `no-store`)
- **Proxy Ollama:** `GET /api/ollama/tags|ps|version` (e alias `/api/tags|ps|version`), `POST /api/ollama/chat|generate|show` (e alias `/api/chat|generate|show`)
- **Proxy Heavy MoE (Colibrì):** `GET /api/heavy/health`, `GET /api/heavy/models`, `POST /api/heavy/chat/completions`
- **Telemetria & Registry:** `GET /api/system/telemetry`, `GET /api/registry/local`
- **Controllo Motori:** `POST /api/control/engine` (`start_fast`, `start_heavy`, `stop_all`, `unload_vram`, `shutdown_app`)
- **Libreria Prompt:** `GET /api/prompts`, `POST /api/prompts/save`, `DELETE /api/prompts/<id>`
- **Orchestrator:** `GET /api/orchestrator/preflight|status|report|vaults|vaults/<id>`, `POST /api/orchestrator/start|pause|resume|stop|purge-report`, `DELETE /api/orchestrator/vaults/<id>`

---

## Avvio

```powershell
# Avvia la Console Web Offline insieme a Fast Tier (o Heavy MoE)
pwsh -File .\scripts\start-llm.ps1 -WithUI -OpenBrowser

# Oppure avvio standalone del server UI
python .\workspaces\offline-chat\server.py
```

Indirizzo locale: `http://127.0.0.1:8787`
