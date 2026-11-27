# Metodologia di Benchmark

Per rendere confrontabili i modelli testati su questa workstation, ogni benchmark registrato in [`RESULTS.md`](RESULTS.md) dovrebbe seguire criteri uniformi.

## Metriche Standard

1. **Prompt Eval Speed (`tok/s`):** velocità di elaborazione del prompt in ingresso (prefill).
2. **Generation Speed (`tok/s`):** velocità di generazione dei token in uscita (decode).
3. **VRAM Picco (`GiB`):** memoria GPU occupata al picco durante la generazione (rilevata via `nvidia-smi`).
4. **Offload GPU:** percentuale o numero di layer caricati in VRAM (es. `100%` oppure `40/48 layer`).
5. **Valutazione Qualitativa:** giudizio sintetico su aderenza alle istruzioni, qualità italiano, codice e affidabilità JSON.

## Condizioni di Test Consigliate

- Chiudere applicazioni GPU pesanti (giochi, editor video, browser con decine di schede) prima di misurare.
- Annotare sempre `num_ctx`, eventuale `OLLAMA_FLASH_ATTENTION` e tipo di KV cache (`f16` / `q8_0` / `q4_0`).
- Eseguire almeno un warm-up (per escludere il tempo di caricamento da disco NVMe dalla misura `tok/s`).
