# Libreria System Prompt & Template

Spazio dedicato alla raccolta e al versioning dei **system prompt** riutilizzabili nei modelli locali (`Fast Tier` Ollama e `Heavy MoE` Colibrì), nei Modelfile e nella **Console Web Offline** (`workspaces/offline-chat/`).

---

## Preset Disponibili

| File / ID | Nome | Temp. Consigliata | Caso d'Uso Principale | Protetto |
| :--- | :--- | :---: | :--- | :---: |
| [`uncensored-direct.md`](uncensored-direct.md) | `uncensored-direct` | `0.7` | Assistente generale 100% locale, diretto, oggettivo, zero prediche | Sì |
| [`deep-reasoning-architect.md`](deep-reasoning-architect.md) | `deep-reasoning-architect` | `0.6` | Ragionamento rigoroso multi-step, architettura sistemi, analisi trade-off e failure mode | No |
| [`coding-specialist.md`](coding-specialist.md) | `coding-specialist` | `0.2` | Codice pulito production-ready, debugging avanzato, refactoring e security audit difensivo | No |
| [`creative-unfiltered.md`](creative-unfiltered.md) | `creative-unfiltered` | `0.85` | Scrittura creativa e narrativa in italiano senza censure né avvisi superflui | No |

---

## Integrazione con la Console Web Offline (`http://127.0.0.1:8787`)

Tutti i file `.md` presenti in questa cartella (escluso `README.md`) vengono letti automaticamente dal server locale (`workspaces/offline-chat/server.py`):

1. **Caricamento in Chat (Tab 1 — Chat Dual-Tier):**
   - Il menu a tendina **Preset System Prompt** elenca tutti i template disponibili e popola live la textarea **System Prompt Attivo** (modificabile al volo per la sessione corrente).
   - Se il frontmatter definisce `recommended_temperature`, selezionando il preset la UI propone automaticamente la temperatura consigliata.
2. **Gestione Completa (Tab 4 — Libreria Prompt):**
   - Puoi visualizzare, modificare, creare nuovi preset personalizzati e salvare su disco (`POST /api/prompts/save`) oppure eliminare preset custom (`DELETE /api/prompts/<id>`).
   - Gli ID dei preset sono validati con regex `^[a-zA-Z0-9_-]{2,64}$`.
   - I file `README.md` e `uncensored-direct.md` sono protetti contro la cancellazione (e `README.md` non può mai essere sovrascritto).
3. **Privacy:** solo il salvataggio esplicito dei template in `prompts/` scrive su disco; le conversazioni e i prompt inviati in chat restano esclusivamente in RAM.

---

## Frontmatter Standard per ogni Prompt

```yaml
---
name: nome-prompt
version: 1.0
target_models: ["Qwen3.8-27B-abliterated", "Heavy-MoE-122B-177B"]
recommended_temperature: 0.2
language: it
description: Breve descrizione operativa del preset
---
```
