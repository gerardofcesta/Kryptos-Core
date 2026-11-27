# Workspaces — Progetti Basati su LLM Locali

Questa directory ospita i futuri sottoprogetti operativi che consumano i modelli locali esposti da Ollama, LM Studio o llama.cpp.

## Esempi di Progetti Futuri

- `rag-local/` — Indicizzazione e interrogazione di documenti locali (PDF, Markdown, codice)
- `api-clients/` — Script Python/CLI per batch processing, riassunti o estrazione dati via OpenAI-compatible API
- `eval-suite/` — Suite automatica di test di qualità e regressione per confrontare modelli
- `agents-mcp/` — Agenti locali collegati a tool e server MCP

## Regole per Nuovi Progetti

1. Creare una sottocartella dedicata: `workspaces/<nome-progetto>/`.
2. Usare ambienti isolati (`uv venv` con Python 3.12 consigliato per librerie ML/AI).
3. Leggere gli endpoint da `../../config/endpoints.json` o da `.env` invece di hardcodare URL.
4. Aggiornare questa tabella quando viene aggiunto un progetto.

| Progetto | Scopo | Runtime Target | Stato |
| :--- | :--- | :--- | :--- |
| [`offline-chat/`](offline-chat/README.md) | Console Web 100% offline a 5 tab, zero dipendenze esterne, hardening Host/CSRF/Token, cronologia solo RAM | Loopback Gateway (`127.0.0.1:8787`) → Ollama / Colibrì | `Attivo` |
| [`orchestrator/`](orchestrator/__init__.py) | Motore Orchestrator asincrono (`Scout → Strategist → Investigator → Validator → Adjudicator`) con Host Read-Only Safe Mode e sandbox Docker `--network none` | Ollama `local-worker:latest` + Colibrì Heavy MoE + Docker (opzionale) | `Attivo` |
| `vault/` | Archivio locale gitignored per le sole run Persistent dell'Orchestrator (`0` file in Ephemeral/Ghost) | `DiskVaultStore` (`workspaces/vault/`) | `Attivo (gitignored)` |
