"""Prompt preset management (`prompts/*.md`) with frontmatter parsing."""

from __future__ import annotations

import json
import pathlib
from typing import Any

from ..config import (
    PROMPT_ID_RE,
    PROMPTS_DIR,
    PROTECTED_DELETE_IDS,
    PROTECTED_SAVE_IDS,
)


def _strip_quotes(text: str) -> str:
    s = text.strip()
    if len(s) >= 2 and ((s[0] == s[-1] == '"') or (s[0] == s[-1] == "'")):
        return s[1:-1]
    return s


def _parse_yaml_scalar(value: str) -> Any:
    v = value.strip()
    if not v:
        return ""
    if (v.startswith("[") and v.endswith("]")) or (v.startswith("{") and v.endswith("}")):
        try:
            return json.loads(v)
        except ValueError:
            pass
    if v.lower() == "true":
        return True
    if v.lower() == "false":
        return False
    if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
        return v[1:-1]
    try:
        if "." in v:
            return float(v)
        return int(v)
    except ValueError:
        return v


def parse_frontmatter(raw: str) -> tuple[dict[str, Any], str]:
    """Extract YAML frontmatter and Markdown body from prompt text."""
    normalized = raw.replace("\r\n", "\n").strip()
    if not normalized.startswith("---"):
        return {}, normalized

    lines = normalized.split("\n")
    if lines[0].strip() != "---":
        return {}, normalized

    end_idx = None
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            end_idx = idx
            break
    if end_idx is None:
        return {}, normalized

    meta: dict[str, Any] = {}
    current_list_key: str | None = None
    for line in lines[1:end_idx]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("- ") and current_list_key is not None:
            existing = meta.get(current_list_key)
            if not isinstance(existing, list):
                existing = []
                meta[current_list_key] = existing
            existing.append(_strip_quotes(stripped[2:]))
            continue
        if ":" not in line:
            continue
        key, val = line.split(":", 1)
        key = key.strip()
        val_stripped = val.strip()
        if not key:
            continue
        if val_stripped == "":
            meta[key] = []
            current_list_key = key
        else:
            meta[key] = _parse_yaml_scalar(val_stripped)
            current_list_key = None

    body = "\n".join(lines[end_idx + 1 :]).strip()
    return meta, body


def build_prompt_markdown(prompt_id: str, meta: dict[str, Any], content: str) -> str:
    """Serialize coherent YAML frontmatter and Markdown body."""
    name = str(meta.get("name") or prompt_id).strip() or prompt_id
    version = meta.get("version", "1.0")
    language = str(meta.get("language") or "it").strip() or "it"

    raw_targets = meta.get("target_models")
    if isinstance(raw_targets, list):
        targets = [str(x).strip() for x in raw_targets if str(x).strip()]
    elif isinstance(raw_targets, str) and raw_targets.strip():
        targets = [x.strip() for x in raw_targets.split(",") if x.strip()]
    else:
        targets = ["Qwen3.8-27B-abliterated", "Heavy-MoE-122B-177B"]

    try:
        temp_val = float(meta.get("recommended_temperature", 0.7))
        temp_val = max(0.0, min(2.0, round(temp_val, 2)))
    except (TypeError, ValueError):
        temp_val = 0.7

    lines = [
        "---",
        f"name: {name}",
        f"version: {version}",
        f"target_models: {json.dumps(targets, ensure_ascii=False)}",
        f"recommended_temperature: {temp_val}",
        f"language: {language}",
    ]
    desc = str(meta.get("description") or "").strip()
    if desc:
        lines.append(f"description: {json.dumps(desc, ensure_ascii=False)}")
    lines.append("---")
    lines.append("")
    lines.append(content.strip())
    lines.append("")
    return "\n".join(lines)


def load_prompt_presets(prompts_dir: pathlib.Path = PROMPTS_DIR) -> list[dict[str, Any]]:
    """Load all Markdown prompt presets from `prompts/`."""
    presets: list[dict[str, Any]] = []
    if not prompts_dir.exists():
        return presets
    for md in sorted(prompts_dir.glob("*.md")):
        if md.name.lower() == "readme.md":
            continue
        raw = md.read_text(encoding="utf-8", errors="replace")
        meta, body = parse_frontmatter(raw)
        presets.append(
            {
                "id": md.stem,
                "name": str(meta.get("name") or md.stem),
                "meta": meta,
                "content": body,
                "raw": raw,
                "protected": md.stem.lower() in PROTECTED_DELETE_IDS,
            }
        )
    return presets


def save_prompt_preset(
    payload: dict[str, Any], prompts_dir: pathlib.Path = PROMPTS_DIR
) -> tuple[int, dict[str, Any]]:
    """Validate and save a prompt preset to `prompts/<id>.md`."""
    prompt_id = str(payload.get("id") or "").strip()
    if not PROMPT_ID_RE.match(prompt_id):
        return 400, {
            "ok": False,
            "error": "ID preset non valido (usa 2-64 caratteri: lettere, numeri, '_' o '-').",
        }
    if prompt_id.lower() in PROTECTED_SAVE_IDS:
        return 403, {"ok": False, "error": "Impossibile sovrascrivere README.md."}

    prompts_dir.mkdir(parents=True, exist_ok=True)
    target = (prompts_dir / f"{prompt_id}.md").resolve()
    if target.parent != prompts_dir.resolve():
        return 400, {"ok": False, "error": "Percorso non consentito"}

    raw_text = payload.get("raw")
    if isinstance(raw_text, str) and raw_text.strip():
        final_md = raw_text.replace("\r\n", "\n").strip() + "\n"
    else:
        content = str(payload.get("content") or "").strip()
        if not content:
            return 400, {"ok": False, "error": "Il contenuto del prompt non può essere vuoto."}
        meta_in = payload.get("meta")
        meta = dict(meta_in) if isinstance(meta_in, dict) else {}
        final_md = build_prompt_markdown(prompt_id, meta, content)

    try:
        target.write_text(final_md, encoding="utf-8")
    except OSError as exc:
        return 500, {"ok": False, "error": f"Errore scrittura file: {exc}"}

    parsed_meta, parsed_body = parse_frontmatter(final_md)
    return 200, {
        "ok": True,
        "prompt": {
            "id": prompt_id,
            "name": str(parsed_meta.get("name") or prompt_id),
            "meta": parsed_meta,
            "content": parsed_body,
            "raw": final_md,
            "protected": prompt_id.lower() in PROTECTED_DELETE_IDS,
        },
    }


def delete_prompt_preset(
    prompt_id: str, prompts_dir: pathlib.Path = PROMPTS_DIR
) -> tuple[int, dict[str, Any]]:
    """Validate and delete a non-protected prompt preset."""
    clean_id = prompt_id.strip()
    if not PROMPT_ID_RE.match(clean_id):
        return 400, {"ok": False, "error": "ID preset non valido"}
    if clean_id.lower() in PROTECTED_DELETE_IDS:
        return 403, {
            "ok": False,
            "error": f"Il preset protetto '{clean_id}' non può essere eliminato.",
        }
    if not prompts_dir.is_dir():
        return 404, {"ok": False, "error": "Cartella prompts non trovata"}
    target = (prompts_dir / f"{clean_id}.md").resolve()
    if target.parent != prompts_dir.resolve():
        return 400, {"ok": False, "error": "Percorso non consentito"}
    if not target.is_file():
        return 404, {"ok": False, "error": "Preset non trovato"}

    try:
        target.unlink()
        return 200, {"ok": True, "deleted": clean_id}
    except OSError as exc:
        return 500, {"ok": False, "error": f"Errore eliminazione preset: {exc}"}
