"""Layer 2 on-demand Hardware & Tuning Advisor (bundled KB local RAG + optional Fast-LLM, strictly clamped by Layer 1)."""

from __future__ import annotations

import json
import pathlib
import re
from collections import Counter
from typing import Any

from ..config import PROJECT_ROOT, SETTINGS
from ..engine.coordinator import get_gateway_coordinator
from ..rag.store import RagChunkRecord, _score_chunks_bm25_cosine, _tokenize
from .telemetry import get_gpu_telemetry, get_ram_telemetry
from .tuning_bounds import compute_tuning_bounds, snap_ctx_nearest

HARDWARE_KB_DIR = PROJECT_ROOT / "docs" / "hardware-tuning-kb"
_JSON_BLOCK_RE = re.compile(r"\{[\s\S]*\}")


def _load_kb_chunks(kb_dir: pathlib.Path | None = None) -> list[RagChunkRecord]:
    """Read and section-chunk bundled markdown guides in `docs/hardware-tuning-kb/`."""
    target_dir = kb_dir or HARDWARE_KB_DIR
    if not target_dir.is_dir():
        return []

    chunks: list[RagChunkRecord] = []
    for md_path in sorted(target_dir.glob("*.md")):
        try:
            raw = md_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        sections: list[tuple[str | None, list[str]]] = []
        current_heading: str | None = None
        current_lines: list[str] = []
        for line in raw.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                if current_lines:
                    sections.append((current_heading, current_lines))
                    current_lines = []
                current_heading = stripped.lstrip("#").strip()
            else:
                current_lines.append(line)
        if current_lines:
            sections.append((current_heading, current_lines))

        for idx, (heading, lines) in enumerate(sections, start=1):
            body = "\n".join(lines).strip()
            if not body:
                continue
            full_text = f"{heading}\n{body}" if heading else body
            tokens = _tokenize(full_text)
            chunks.append(
                RagChunkRecord(
                    chunk_id=f"{md_path.stem}-{idx}",
                    doc_id=md_path.stem,
                    source_file=md_path.name,
                    page_start=idx,
                    page_end=idx,
                    heading=heading,
                    token_estimate=max(1, len(tokens)),
                    tf=Counter(tokens),
                    length=len(tokens),
                    _text_str=full_text,
                )
            )
    return chunks


def search_hardware_kb(
    query: str,
    *,
    top_k: int = 3,
    kb_dir: pathlib.Path | None = None,
) -> list[dict[str, Any]]:
    """Run local BM25 + TF-IDF retrieval over `docs/hardware-tuning-kb/`."""
    chunks = _load_kb_chunks(kb_dir)
    if not chunks:
        return []
    hits = _score_chunks_bm25_cosine(chunks, query, top_k=top_k)
    if not hits:
        # Return top introductory sections when query has generic tokens
        return [
            {
                "chunk_id": ch.chunk_id,
                "source_file": ch.source_file,
                "heading": ch.heading,
                "citation": f"[{ch.source_file} · §{ch.page_start}]",
                "score": 0.0,
                "text": ch.text,
            }
            for ch in chunks[: max(1, top_k)]
        ]
    return [h.to_dict() for h in hits]


def clamp_recommendations_to_bounds(
    raw_rec: dict[str, Any],
    bounds: dict[str, Any],
) -> tuple[dict[str, int], bool, list[str]]:
    """
    Strictly clamp any proposed tuning values (`fast_ctx`, `heavy_ram_limit_gb`, `heavy_ctx`)
    inside Layer 1 deterministic math bounds.
    """
    fast_b = bounds["fast_ctx"]
    heavy_ram_b = bounds["heavy_ram_limit_gb"]
    heavy_ctx_b = bounds["heavy_ctx"]

    def _to_int(val: Any, fallback: int) -> int:
        try:
            return int(round(float(val)))
        except (TypeError, ValueError):
            return fallback

    req_fast_ctx = _to_int(raw_rec.get("fast_ctx"), int(fast_b["recommended"]))
    req_heavy_ram = _to_int(raw_rec.get("heavy_ram_limit_gb"), int(heavy_ram_b["recommended"]))
    req_heavy_ctx = _to_int(raw_rec.get("heavy_ctx"), int(heavy_ctx_b["recommended"]))

    clamped_fast_ctx = snap_ctx_nearest(
        req_fast_ctx,
        min_val=int(fast_b["min"]),
        max_val=int(fast_b["max_safe"]),
    )
    clamped_heavy_ram = max(
        int(heavy_ram_b["min"]),
        min(int(heavy_ram_b["max_safe"]), req_heavy_ram),
    )
    clamped_heavy_ctx = snap_ctx_nearest(
        req_heavy_ctx,
        min_val=int(heavy_ctx_b["min"]),
        max_val=int(heavy_ctx_b["max_safe"]),
    )

    notes: list[str] = []
    was_clamped = False
    if clamped_fast_ctx != req_fast_ctx:
        was_clamped = True
        notes.append(
            f"fast_ctx clamped from {req_fast_ctx} to {clamped_fast_ctx} (safe max={fast_b['max_safe']})."
        )
    if clamped_heavy_ram != req_heavy_ram:
        was_clamped = True
        notes.append(
            f"heavy_ram_limit_gb clamped from {req_heavy_ram} GB to {clamped_heavy_ram} GB (safe max={heavy_ram_b['max_safe']} GB)."
        )
    if clamped_heavy_ctx != req_heavy_ctx:
        was_clamped = True
        notes.append(
            f"heavy_ctx clamped from {req_heavy_ctx} to {clamped_heavy_ctx} (safe max={heavy_ctx_b['max_safe']})."
        )

    return (
        {
            "fast_ctx": clamped_fast_ctx,
            "heavy_ram_limit_gb": clamped_heavy_ram,
            "heavy_ctx": clamped_heavy_ctx,
        },
        was_clamped,
        notes,
    )


def _extract_json_object(text: str) -> dict[str, Any] | None:
    cleaned = (text or "").strip()
    if not cleaned:
        return None
    try:
        parsed = json.loads(cleaned)
        return parsed if isinstance(parsed, dict) else None
    except ValueError:
        pass
    match = _JSON_BLOCK_RE.search(cleaned)
    if match:
        try:
            parsed = json.loads(match.group(0))
            return parsed if isinstance(parsed, dict) else None
        except ValueError:
            return None
    return None


def _build_deterministic_explanation(
    question: str,
    bounds: dict[str, Any],
    kb_hits: list[dict[str, Any]],
) -> str:
    b = bounds["budget"]
    f_ctx = bounds["fast_ctx"]
    h_ram = bounds["heavy_ram_limit_gb"]
    h_ctx = bounds["heavy_ctx"]
    citations = ", ".join(h["source_file"] for h in kb_hits) if kb_hits else "Layer 1 math bounds"
    return (
        f"Deterministic KB & Layer 1 analysis for: '{question}'. "
        f"Usable VRAM: {b['usable_vram_gb']} GB (OS reserve {b['os_vram_reserve_gb']} GB), "
        f"Usable RAM: {b['usable_ram_gb']} GB (OS reserve {b['os_ram_reserve_gb']} GB), "
        f"KV cost ({b['kv_quant']}): {b['kv_gb_per_1k']} GB/1k tokens. "
        f"Safe Fast num_ctx: {f_ctx['recommended']} (max safe {f_ctx['max_safe']}). "
        f"Safe Heavy MoE RAM Limit: {h_ram['recommended']} GB (max safe {h_ram['max_safe']} GB), "
        f"Heavy ctx: {h_ctx['recommended']} (max safe {h_ctx['max_safe']}). "
        f"Sources: {citations}."
    )


async def run_tuning_advisor(
    *,
    question: str,
    model_params_b: float | None = None,
    model_weights_gb: float | None = None,
    kv_quant: str = "q8_0",
    use_llm: bool = True,
    kb_dir: pathlib.Path | None = None,
) -> dict[str, Any]:
    """
    Execute Layer 2 Hardware Advisor:
    1) Retrieve relevant local KB guides from `docs/hardware-tuning-kb/`.
    2) Compute Layer 1 deterministic bounds from live GPU/RAM telemetry.
    3) Optionally query `local-fast` with live telemetry + Layer 1 bounds JSON injected.
    4) Strictly clamp any recommendation to Layer 1 hard mathematical bounds.
    """
    clean_q = (question or "").strip() or "Quali impostazioni di context e Heavy MoE RAM limit sono sicure?"
    gpu = get_gpu_telemetry()
    ram = get_ram_telemetry()
    bounds = compute_tuning_bounds(
        model_params_b=model_params_b,
        model_weights_gb=model_weights_gb,
        kv_quant=kv_quant,
        gpu=gpu,
        ram=ram,
    )
    kb_hits = search_hardware_kb(clean_q, top_k=3, kb_dir=kb_dir)

    raw_rec: dict[str, Any] = {
        "fast_ctx": bounds["fast_ctx"]["recommended"],
        "heavy_ram_limit_gb": bounds["heavy_ram_limit_gb"]["recommended"],
        "heavy_ctx": bounds["heavy_ctx"]["recommended"],
    }
    mode = "kb_only_fallback"
    explanation = _build_deterministic_explanation(clean_q, bounds, kb_hits)
    warnings: list[str] = []

    coord = get_gateway_coordinator()
    if use_llm and await coord.ollama.health():
        kb_context = "\n\n".join(
            f"[{h['source_file']} - {h.get('heading') or 'Guide'}]\n{h['text'][:700]}"
            for h in kb_hits
        )
        system_prompt = (
            "You are a local hardware tuning advisor. Analyze live GPU/RAM telemetry, "
            "Layer 1 hard math bounds, and local KB excerpts. Respond ONLY with valid JSON: "
            '{"explanation": "<concise advice>", "fast_ctx": <int>, '
            '"heavy_ram_limit_gb": <int>, "heavy_ctx": <int>, "warnings": [<str>]}.'
        )
        user_prompt = (
            f"Question: {clean_q}\n\n"
            f"Live Telemetry:\n{json.dumps({'gpu': gpu, 'ram': ram}, ensure_ascii=False)}\n\n"
            f"Layer 1 Bounds:\n{json.dumps(bounds, ensure_ascii=False)}\n\n"
            f"Local KB:\n{kb_context}"
        )
        try:
            status, resp_json = await coord.ollama.chat_completions(
                {
                    "model": SETTINGS.fast_model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": 0.1,
                    "max_tokens": 450,
                    "options": {"num_ctx": min(8192, int(bounds["fast_ctx"]["max_safe"]))},
                }
            )
            if 200 <= status < 300 and isinstance(resp_json, dict):
                choices = resp_json.get("choices") or []
                content = ""
                if choices and isinstance(choices[0], dict):
                    msg = choices[0].get("message") or {}
                    content = str(msg.get("content") or "")
                parsed = _extract_json_object(content)
                if parsed:
                    mode = "llm+kb"
                    raw_rec = {
                        "fast_ctx": parsed.get("fast_ctx", raw_rec["fast_ctx"]),
                        "heavy_ram_limit_gb": parsed.get(
                            "heavy_ram_limit_gb", raw_rec["heavy_ram_limit_gb"]
                        ),
                        "heavy_ctx": parsed.get("heavy_ctx", raw_rec["heavy_ctx"]),
                    }
                    if isinstance(parsed.get("explanation"), str) and parsed["explanation"].strip():
                        explanation = parsed["explanation"].strip()
                    if isinstance(parsed.get("warnings"), list):
                        warnings.extend(str(w) for w in parsed["warnings"] if str(w).strip())
                elif content.strip():
                    mode = "llm+kb"
                    explanation = content.strip()
        except Exception as exc:
            warnings.append(f"local-fast unreachable; used deterministic KB fallback ({exc}).")

    clamped_rec, was_clamped, clamp_notes = clamp_recommendations_to_bounds(raw_rec, bounds)
    if clamp_notes:
        warnings.extend(clamp_notes)

    return {
        "ok": True,
        "mode": mode,
        "question": clean_q,
        "recommended": clamped_rec,
        "raw_recommendation": raw_rec if was_clamped else clamped_rec,
        "clamped": was_clamped,
        "explanation": explanation,
        "warnings": warnings,
        "kb_hits": kb_hits,
        "bounds": bounds,
    }
