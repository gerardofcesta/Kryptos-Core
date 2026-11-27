# KV Cache Math & Context Window (`num_ctx`) Tuning

## Why Context Size Consumes VRAM Fast
Every token in the active context window allocates Key and Value tensors across all transformer layers:

`KV_GB_per_1k = (2 * L * H_kv * D_head * B_kv * 1024) / 1024^3`

- With `q8_0` KV quantization (`B_kv = 1 byte`), a 14B GQA model (`L=40, H_kv=8, D_head=128`) uses roughly **0.078 GB per 1,000 tokens** (~2.5 GB at 32k context).
- With `f16` KV cache (`B_kv = 2 bytes`), KV memory **doubles** (~5.0 GB at 32k context).

## Fast Tier (Ollama) `num_ctx` Propagation
- Ollama defaults to a small context window unless explicitly passed `options.num_ctx` in chat requests or `OLLAMA_CONTEXT_LENGTH` on server launch.
- When you apply `num_ctx` in **Engines**, the gateway persists it for `local-fast` / `local-balanced` and injects `options.num_ctx` automatically into Ollama payloads (unless overridden by the caller).

## Avoiding Shared VRAM Spill
- Always keep `model_weights_gb + (num_ctx / 1000) * kv_gb_per_1k <= usable_vram_gb`.
- Exceeding physical VRAM forces GPU drivers to spill layers or KV cache into system RAM over PCIe, cutting token generation speed by 5x–15x.
