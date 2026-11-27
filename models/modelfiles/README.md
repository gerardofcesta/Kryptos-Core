# Modelfiles & Preset di Inferenza

Questa cartella ospita i `Modelfile` personalizzati per **Ollama** e i file preset JSON per **LM Studio / llama.cpp**, in modo da mantenere configurazioni riproducibili per ogni caso d'uso (contesto, temperatura, system prompt, parametri KV cache).

## Convenzione Nomi

- Ollama: `<ruolo>-<modello>.Modelfile` (es. `coding-qwen14b.Modelfile`)
- LM Studio / Generico: `<ruolo>-<modello>.preset.json`

## Template Base `Modelfile` (Ollama)

```dockerfile
# Modello base (da sostituire con un modello effettivamente installato)
FROM <nome-modello>:<tag>

# Finestra di contesto (calibrare su 16 GB VRAM, es. 16384 o 32768 con KV cache Q8)
PARAMETER num_ctx 16384
PARAMETER temperature 0.2
PARAMETER top_p 0.9

SYSTEM """
Sei un assistente tecnico preciso e conciso. Rispondi in italiano salvo diversa richiesta.
"""
```

## Creazione Modello Derivato (solo quando il modello base è già presente)

```powershell
ollama create <nome-custom> -f .\models\modelfiles\<file>.Modelfile
```
