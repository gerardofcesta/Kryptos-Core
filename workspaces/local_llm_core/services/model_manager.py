"""Air-gapped GGUF/Modelfile inventory & offline SHA-256 verification using `LOCAL_LLM_MODELS_DIR`."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import pathlib
import re
import secrets
import shutil
import threading
import time
import urllib.parse
from collections.abc import AsyncIterator
from typing import Any

import httpx

from ..config import OLLAMA_BASE, PROJECT_ROOT
from .telemetry import (
    _http_json,
    get_disk_telemetry,
    get_gpu_telemetry,
    get_ram_telemetry,
)

_SHA256_HEX_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_CHUNK_BYTES = 1024 * 1024  # 1 MiB streaming buffer for large 87+ GiB weights
_VERIFY_CHUNK_BYTES = 4 * 1024 * 1024  # 4 MiB streaming verification blocks
_WEIGHT_EXTS = frozenset({".gguf", ".colibri", ".bin", ".safetensors"})
_MAX_SCAN_DEPTH = 3
_MODEL_CATALOG_FILE = PROJECT_ROOT / "config" / "model-catalog.json"
_ALLOWED_DOWNLOAD_HOSTS = frozenset(
    {
        "huggingface.co",
        "cdn-lfs.huggingface.co",
        "cdn-lfs-us-1.huggingface.co",
        "cdn-lfs.hf.co",
        "registry.ollama.ai",
        "objects.githubusercontent.com",
    }
)
_PREPARED_LOCK = threading.Lock()
_PREPARED_DOWNLOADS: dict[str, dict[str, Any]] = {}
_PREPARED_TTL_SEC = 900.0


def resolve_models_dir(explicit_dir: pathlib.Path | str | None = None) -> tuple[pathlib.Path, str]:
    """
    Resolve the model weights directory prioritizing external `LOCAL_LLM_MODELS_DIR`
    so multi-GiB weights are never forced into `%APPDATA%` or the source repository.
    """
    if explicit_dir is not None:
        raw = str(explicit_dir).strip()
        if not raw:
            raise ValueError("models_dir cannot be empty.")
        return pathlib.Path(raw).expanduser().resolve(), "explicit"

    env_val = os.environ.get("LOCAL_LLM_MODELS_DIR", "").strip()
    if env_val:
        return pathlib.Path(env_val).expanduser().resolve(), "LOCAL_LLM_MODELS_DIR"

    return (PROJECT_ROOT / "models").resolve(), "repo_default_models"


def _is_inside_dir(candidate: pathlib.Path, root: pathlib.Path) -> bool:
    try:
        c = pathlib.Path(os.path.normcase(os.path.normpath(str(candidate.resolve()))))
        r = pathlib.Path(os.path.normcase(os.path.normpath(str(root.resolve()))))
        return c == r or c.is_relative_to(r)
    except Exception:
        return False


def _resolve_safe_model_path(models_dir: pathlib.Path, rel_path: str) -> pathlib.Path:
    raw = (rel_path or "").strip()
    if not raw:
        raise ValueError("Model relative path is required.")
    if "\x00" in raw or raw.startswith(("\\\\", "//")):
        raise PermissionError("Invalid or network model path rejected.")
    parts = [p for p in re.split(r"[\\/]+", raw) if p]
    if ".." in parts:
        raise PermissionError("Path traversal ('..') is forbidden in model verification.")
    cand_path = pathlib.Path(raw)
    if cand_path.is_absolute():
        resolved = cand_path.resolve()
    else:
        resolved = (models_dir / cand_path).resolve()
    if not _is_inside_dir(resolved, models_dir) or resolved == models_dir.resolve():
        raise PermissionError(
            f"Model path '{raw}' resolves outside configured LOCAL_LLM_MODELS_DIR '{models_dir}'."
        )
    return resolved


def compute_file_sha256(file_path: pathlib.Path) -> str:
    """Compute SHA-256 hex digest streaming in 1 MiB blocks."""
    digest = hashlib.sha256()
    with open(file_path, "rb") as fh:
        while True:
            block = fh.read(_CHUNK_BYTES)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest().lower()


def _parse_modelfile(path: pathlib.Path) -> dict[str, Any]:
    base_from: str | None = None
    params: dict[str, str] = {}
    try:
        for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            upper = line.upper()
            if upper.startswith("FROM "):
                base_from = line[5:].strip()
            elif upper.startswith("PARAMETER "):
                rest = line[10:].strip().split(None, 1)
                if len(rest) == 2:
                    params[rest[0].strip()] = rest[1].strip()
    except OSError:
        pass
    return {
        "name": path.stem,
        "filename": path.name,
        "path": str(path),
        "from": base_from,
        "num_ctx": int(params["num_ctx"]) if params.get("num_ctx", "").isdigit() else None,
        "parameters": params,
    }


def load_offline_sha256_manifest(models_dir: pathlib.Path) -> dict[str, str]:
    """
    Load expected SHA-256 hashes from `manifest.sha256.json` or `SHA256SUMS` in `models_dir`.
    Keys are normalized POSIX-style relative paths.
    """
    expected: dict[str, str] = {}
    json_manifest = models_dir / "manifest.sha256.json"
    if json_manifest.is_file():
        try:
            data = json.loads(json_manifest.read_text(encoding="utf-8", errors="replace"))
            entries = data.get("files", data) if isinstance(data, dict) else {}
            if isinstance(entries, dict):
                for rel_k, val in entries.items():
                    if isinstance(val, dict):
                        val = val.get("sha256", "")
                    if isinstance(val, str) and _SHA256_HEX_RE.fullmatch(val.strip()):
                        norm_k = str(rel_k).replace("\\", "/").lstrip("./")
                        expected[norm_k] = val.strip().lower()
        except (OSError, ValueError):
            pass

    sums_file = models_dir / "SHA256SUMS"
    if sums_file.is_file():
        try:
            for raw_line in sums_file.read_text(encoding="utf-8", errors="replace").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split(None, 1)
                if len(parts) == 2 and _SHA256_HEX_RE.fullmatch(parts[0].strip()):
                    rel_part = parts[1].strip().lstrip("*").replace("\\", "/").lstrip("./")
                    expected.setdefault(rel_part, parts[0].strip().lower())
        except OSError:
            pass

    return expected


def get_airgapped_inventory(models_dir: pathlib.Path | str | None = None) -> dict[str, Any]:
    """
    Scan `LOCAL_LLM_MODELS_DIR` (or fallback repo `models/`) offline for GGUF/Colibri
    weights, Modelfiles, and local SHA-256 manifest entries without any network I/O.
    """
    resolved_dir, source = resolve_models_dir(models_dir)
    manifest_hashes = load_offline_sha256_manifest(resolved_dir) if resolved_dir.is_dir() else {}

    weights: list[dict[str, Any]] = []
    modelfiles: list[dict[str, Any]] = []
    seen_modelfiles: set[str] = set()

    if resolved_dir.is_dir():
        root_parts = len(resolved_dir.parts)
        for walk_root, dirs, files in os.walk(resolved_dir):
            cur_path = pathlib.Path(walk_root)
            depth = len(cur_path.parts) - root_parts
            if depth >= _MAX_SCAN_DEPTH:
                dirs[:] = []
            else:
                dirs[:] = [d for d in sorted(dirs) if not d.startswith(".")]

            for fname in sorted(files):
                fpath = cur_path / fname
                rel = fpath.relative_to(resolved_dir).as_posix()
                ext = fpath.suffix.lower()
                if ext in _WEIGHT_EXTS:
                    try:
                        size_bytes = fpath.stat().st_size
                    except OSError:
                        size_bytes = 0
                    weights.append(
                        {
                            "name": fname,
                            "relative_path": rel,
                            "rel_path": rel,
                            "format": ext.lstrip("."),
                            "ext": ext.lstrip("."),
                            "size_bytes": size_bytes,
                            "size_gb": round(size_bytes / (1024**3), 2),
                            "manifest_sha256": manifest_hashes.get(rel) or manifest_hashes.get(fname),
                        }
                    )
                elif fname.endswith(".Modelfile") or ext == ".modelfile":
                    seen_modelfiles.add(str(fpath.resolve()))
                    modelfiles.append(_parse_modelfile(fpath))

    # Also include repo-tracked Modelfile templates if scanning external LOCAL_LLM_MODELS_DIR
    repo_modelfiles_dir = (PROJECT_ROOT / "models" / "modelfiles").resolve()
    if repo_modelfiles_dir.is_dir():
        for mf in sorted(repo_modelfiles_dir.glob("*.Modelfile")):
            if str(mf.resolve()) not in seen_modelfiles:
                modelfiles.append(_parse_modelfile(mf))

    warnings: list[str] = []
    for env_name in ("APPDATA", "LOCALAPPDATA"):
        app_dir = os.environ.get(env_name, "").strip()
        if app_dir and _is_inside_dir(resolved_dir, pathlib.Path(app_dir)):
            warnings.append(
                f"LOCAL_LLM_MODELS_DIR '{resolved_dir}' is inside %{env_name}%; "
                "configure an external path for multi-GiB weights."
            )

    return {
        "ok": True,
        "air_gapped": True,
        "models_dir": str(resolved_dir),
        "models_dir_exists": resolved_dir.is_dir(),
        "models_dir_source": source,
        "models": weights,
        "modelfiles": modelfiles,
        "manifest_entries": len(manifest_hashes),
        "count": len(weights),
        "warnings": warnings,
    }


def verify_model_sha256(
    rel_path: str,
    expected_sha256: str | None = None,
    *,
    models_dir: pathlib.Path | str | None = None,
) -> dict[str, Any]:
    """
    Verify a model weight file offline against an expected SHA-256 hex digest
    (explicitly supplied or loaded from `manifest.sha256.json` / `SHA256SUMS`).
    """
    resolved_dir, source = resolve_models_dir(models_dir)
    target = _resolve_safe_model_path(resolved_dir, rel_path)
    norm_rel = target.relative_to(resolved_dir).as_posix()

    if not target.is_file():
        return {
            "ok": False,
            "verified": False,
            "status": "MISSING",
            "air_gapped": True,
            "models_dir": str(resolved_dir),
            "models_dir_source": source,
            "path": norm_rel,
            "rel_path": norm_rel,
            "error": f"Model file not found: {norm_rel}",
        }

    size_bytes = target.stat().st_size
    size_gb = round(size_bytes / (1024**3), 2)
    expected = (expected_sha256 or "").strip().lower()
    if not expected:
        manifest_hashes = load_offline_sha256_manifest(resolved_dir)
        expected = manifest_hashes.get(norm_rel) or manifest_hashes.get(target.name) or ""

    if not expected:
        actual = compute_file_sha256(target)
        return {
            "ok": True,
            "verified": False,
            "status": "UNVERIFIED_NO_MANIFEST",
            "air_gapped": True,
            "models_dir": str(resolved_dir),
            "models_dir_source": source,
            "path": norm_rel,
            "rel_path": norm_rel,
            "size_bytes": size_bytes,
            "size_gb": size_gb,
            "expected_sha256": None,
            "actual_sha256": actual,
            "computed_sha256": actual,
        }

    if not _SHA256_HEX_RE.fullmatch(expected):
        raise ValueError(
            "Valid 64-char hex SHA-256 digest is required (either in request or manifest.sha256.json)."
        )

    actual = compute_file_sha256(target)
    matched = secrets.compare_digest(actual, expected)
    return {
        "ok": matched,
        "verified": matched,
        "status": "VERIFIED" if matched else "MISMATCH",
        "air_gapped": True,
        "models_dir": str(resolved_dir),
        "models_dir_source": source,
        "path": norm_rel,
        "rel_path": norm_rel,
        "size_bytes": size_bytes,
        "size_gb": size_gb,
        "expected_sha256": expected,
        "actual_sha256": actual,
        "computed_sha256": actual,
    }


def pin_model_sha256(
    rel_path: str,
    expected_sha256: str | None = None,
    *,
    models_dir: pathlib.Path | str | None = None,
) -> dict[str, Any]:
    """
    Compute the offline SHA-256 digest of a local model artifact and pin it
    into `<models_dir>/manifest.sha256.json`.
    """
    resolved_dir, source = resolve_models_dir(models_dir)
    target = _resolve_safe_model_path(resolved_dir, rel_path)
    norm_rel = target.relative_to(resolved_dir).as_posix()

    if not target.is_file():
        return {
            "ok": False,
            "pinned": False,
            "status": "MISSING",
            "path": norm_rel,
            "rel_path": norm_rel,
            "error": f"Model file not found: {norm_rel}",
        }

    actual = compute_file_sha256(target)
    expected = (expected_sha256 or "").strip().lower()
    if expected:
        if not _SHA256_HEX_RE.fullmatch(expected):
            raise ValueError("Valid 64-char hex SHA-256 digest is required when expected_sha256 is supplied.")
        if not secrets.compare_digest(actual, expected):
            return {
                "ok": False,
                "pinned": False,
                "status": "MISMATCH",
                "path": norm_rel,
                "rel_path": norm_rel,
                "expected_sha256": expected,
                "actual_sha256": actual,
                "computed_sha256": actual,
                "error": "Computed SHA-256 does not match supplied expected_sha256.",
            }

    manifest_path = resolved_dir / "manifest.sha256.json"
    existing_doc: dict[str, Any] = {}
    if manifest_path.is_file():
        try:
            loaded = json.loads(manifest_path.read_text(encoding="utf-8", errors="replace"))
            if isinstance(loaded, dict):
                existing_doc = loaded
        except (OSError, ValueError):
            existing_doc = {}

    files_map = existing_doc.get("files") if isinstance(existing_doc.get("files"), dict) else {}
    if not files_map and existing_doc and all(isinstance(v, str) for v in existing_doc.values()):
        files_map = dict(existing_doc)
        existing_doc = {}

    files_map = dict(files_map)
    files_map[norm_rel] = actual
    out_doc = {**existing_doc, "files": files_map}
    tmp_manifest = manifest_path.with_suffix(".json.tmp")
    tmp_manifest.write_text(json.dumps(out_doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp_manifest, manifest_path)

    size_bytes = target.stat().st_size
    return {
        "ok": True,
        "pinned": True,
        "verified": True,
        "status": "VERIFIED",
        "air_gapped": True,
        "models_dir": str(resolved_dir),
        "models_dir_source": source,
        "path": norm_rel,
        "rel_path": norm_rel,
        "size_bytes": size_bytes,
        "size_gb": round(size_bytes / (1024**3), 2),
        "sha256": actual,
        "expected_sha256": actual,
        "actual_sha256": actual,
        "computed_sha256": actual,
        "manifest_path": str(manifest_path),
    }


def load_model_catalog(catalog_path: pathlib.Path | None = None) -> list[dict[str, Any]]:
    """Load curated model entries from `config/model-catalog.json`."""
    target = catalog_path or _MODEL_CATALOG_FILE
    if not target.is_file():
        return []
    try:
        raw = json.loads(target.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return []
    models = raw.get("models") if isinstance(raw, dict) else raw
    if not isinstance(models, list):
        return []
    return [dict(item) for item in models if isinstance(item, dict) and item.get("id")]


def _classify_catalog_fit(
    entry: dict[str, Any],
    *,
    vram_total_gb: float,
    ram_total_gb: float,
    disk_free_gb: float,
) -> tuple[str, str]:
    vram_req = max(0.0, float(entry.get("vram_required_gb") or 0.0))
    ram_req = max(0.0, float(entry.get("ram_required_gb") or 0.0))
    disk_req = max(0.0, float(entry.get("disk_required_gb") or 0.0))
    role = str(entry.get("role") or "fast_tools")

    if disk_free_gb > 0.0 and disk_req > disk_free_gb:
        return "too_large", f"Requires {disk_req} GB disk ({disk_free_gb} GB free)."

    if role == "heavy_moe":
        if (vram_req > 0.0 and vram_total_gb > 0.0 and vram_req > vram_total_gb * 1.05) or (
            ram_total_gb > 0.0 and ram_req > ram_total_gb * 0.85
        ):
            return "too_large", f"Needs ~{ram_req} GB RAM + {vram_req} GB VRAM (host has {ram_total_gb} GB RAM)."
        if ram_req <= ram_total_gb * 0.65 and (vram_req == 0.0 or vram_total_gb >= vram_req * 1.15):
            return "optimal", f"Fits host RAM ({ram_total_gb} GB) and VRAM ({vram_total_gb} GB) comfortably."
        return "tight", f"Fits with tight memory headroom (needs {ram_req} GB RAM / {vram_req} GB VRAM)."

    if vram_total_gb > 0.0:
        if vram_req <= vram_total_gb * 0.80 and (ram_total_gb <= 0.0 or ram_req <= ram_total_gb * 0.85):
            return "optimal", f"Fits comfortably in {vram_total_gb} GB VRAM."
        if vram_req <= vram_total_gb * 1.05 and (ram_total_gb <= 0.0 or ram_req <= ram_total_gb * 0.95):
            return "tight", f"Tight VRAM fit ({vram_req} GB req vs {vram_total_gb} GB VRAM); use smaller ctx."
        if ram_total_gb > 0.0 and ram_req <= ram_total_gb * 0.70:
            return "tight", f"Exceeds {vram_total_gb} GB VRAM; requires partial CPU/RAM offload."
        return "too_large", f"Requires {vram_req} GB VRAM / {ram_req} GB RAM."

    if vram_req == 0.0 and (ram_total_gb <= 0.0 or ram_req <= ram_total_gb * 0.65):
        return "optimal", f"CPU-friendly model within {ram_total_gb} GB RAM."
    if ram_total_gb > 0.0 and ram_req <= ram_total_gb * 0.80:
        return "tight", f"CPU mode tight fit ({ram_req} GB req vs {ram_total_gb} GB RAM)."
    return "too_large", f"Requires {vram_req} GB VRAM or {ram_req} GB RAM."


def get_annotated_model_catalog(
    *,
    catalog_path: pathlib.Path | None = None,
    models_dir: pathlib.Path | str | None = None,
    gpu: dict[str, Any] | None = None,
    ram: dict[str, Any] | None = None,
    disk: dict[str, Any] | None = None,
    ollama_tags: list[str] | None = None,
) -> dict[str, Any]:
    """
    Return the curated catalog annotated with `fits_hardware` (`optimal | tight | too_large`)
    and `installed`, omitting external download URLs from the API payload.
    """
    gpu_data = gpu if gpu is not None else get_gpu_telemetry()
    ram_data = ram if ram is not None else get_ram_telemetry()
    disk_data = disk if disk is not None else get_disk_telemetry()

    vram_total = float(gpu_data.get("vramTotalGb") or gpu_data.get("memoryTotalGb") or 0.0)
    ram_total = float(ram_data.get("totalGb") or 0.0)
    disk_free = float(disk_data.get("freeGb") or 0.0)

    inventory = get_airgapped_inventory(models_dir)
    local_names = {str(m.get("name") or "").lower() for m in inventory.get("models", [])}

    if ollama_tags is None:
        ol_ok, _, tags_data, _ = _http_json("127.0.0.1", 11434, "GET", "/api/tags", timeout=0.4)
        installed_ollama = (
            {str(m.get("name") or "").lower() for m in tags_data.get("models", []) if isinstance(m, dict)}
            if ol_ok and isinstance(tags_data, dict)
            else set()
        )
    else:
        installed_ollama = {t.lower() for t in ollama_tags}

    annotated: list[dict[str, Any]] = []
    for raw_entry in load_model_catalog(catalog_path):
        fits, reason = _classify_catalog_fit(
            raw_entry,
            vram_total_gb=vram_total,
            ram_total_gb=ram_total,
            disk_free_gb=disk_free,
        )
        backend = str(raw_entry.get("backend") or "ollama").lower()
        installed = False
        if backend == "ollama":
            tag = str(raw_entry.get("ollama_tag") or "").lower()
            installed = bool(tag and (tag in installed_ollama or f"{tag}:latest" in installed_ollama))
        else:
            fname = str(raw_entry.get("filename") or "").lower()
            installed = bool(fname and fname in local_names)

        # Strip external download_url from UI/API response to keep SPA external-URL-free
        public_entry = {k: v for k, v in raw_entry.items() if k != "download_url"}
        public_entry["has_download_source"] = bool(
            raw_entry.get("ollama_tag") if backend == "ollama" else raw_entry.get("download_url")
        )
        public_entry["fits_hardware"] = fits
        public_entry["fits_reason"] = reason
        public_entry["hardware_fit"] = evaluate_model_fit(
            float(raw_entry.get("disk_required_gb") or raw_entry.get("vram_required_gb") or 4.0),
            quant=str(raw_entry.get("quant") or "Q4_K_M"),
            hardware={
                "vram_total_gb": vram_total,
                "ram_free_gb": float(ram_data.get("freeGb") or ram_total),
                "ram_total_gb": ram_total,
            },
        )
        public_entry["installed"] = installed
        annotated.append(public_entry)

    return {
        "ok": True,
        "hardware": {
            "gpu_name": gpu_data.get("name"),
            "vram_total_gb": vram_total,
            "ram_total_gb": ram_total,
            "disk_free_gb": disk_free,
        },
        "models": annotated,
        "count": len(annotated),
    }


def _is_allowlisted_host(host: str) -> bool:
    clean = (host or "").strip().strip("[]").lower().rstrip(".")
    if not clean:
        return False
    # Block decimal/octal/hex numeric IP obfuscations (e.g. 2130706433, 0177.0.0.1, 0x7f000001)
    if re.fullmatch(r"(?:0x[0-9a-f]+|\d+|(?:0[0-7]+\.){1,3}0?[0-7]+)", clean):
        return False
    try:
        ipaddress.ip_address(clean.split("%", 1)[0])
        return False
    except ValueError:
        pass
    if clean in _ALLOWED_DOWNLOAD_HOSTS:
        return True
    return clean.endswith((".huggingface.co", ".hf.co"))


def _validate_allowlisted_download_url(raw_url: str) -> str:
    trimmed = (raw_url or "").strip()
    if any(ch in trimmed for ch in ("\r", "\n", "\x00")):
        raise PermissionError("Control characters are forbidden in download URL.")
    parsed = urllib.parse.urlsplit(trimmed)
    if parsed.username is not None or parsed.password is not None:
        raise PermissionError("Userinfo credentials are forbidden in download URLs.")
    try:
        port = parsed.port
    except ValueError as exc:
        raise PermissionError("Malformed port in download URL.") from exc
    if port is not None and port != 443:
        raise PermissionError(f"Non-HTTPS standard port '{port}' is forbidden.")
    host = (parsed.hostname or "").lower()
    if parsed.scheme.lower() != "https" or not _is_allowlisted_host(host):
        raise PermissionError(f"Download host '{host or 'unknown'}' is not in the safe HTTPS allowlist.")
    return trimmed


def evaluate_model_fit(
    model_size_gb: float,
    quant: str = "Q4_K_M",
    hardware: dict[str, Any] | None = None,
    *,
    ctx: int = 4096,
) -> dict[str, Any]:
    """Deterministically score model memory fit (🟢/🟡/🔴) and estimate tok/s."""
    size_gb = max(0.1, float(model_size_gb))
    base_kv = 0.6 if size_gb <= 6.5 else 1.2
    kv_cache_gb = round(base_kv * (max(1024, int(ctx)) / 4096.0), 2)
    required_gb = round(size_gb * 1.15 + kv_cache_gb, 2)

    if hardware is None:
        gpu_data = get_gpu_telemetry()
        ram_data = get_ram_telemetry()
        available_vram_gb = float(
            gpu_data.get("vramTotalGb") or gpu_data.get("memoryTotalGb") or 0.0
        )
        ram_total_gb = float(ram_data.get("totalGb") or 0.0)
        free_ram_gb = float(ram_data.get("freeGb") or ram_total_gb or 0.0)
        unified = bool(gpu_data.get("unifiedMemory", False))
        mem_bw_gbps = 180.0 if unified else (320.0 if available_vram_gb >= 6.0 else 45.0)
    else:
        available_vram_gb = float(
            hardware.get("vram_total_gb")
            or hardware.get("available_vram_gb")
            or hardware.get("vram_gb")
            or 0.0
        )
        ram_total_gb = float(
            hardware.get("ram_total_gb")
            or hardware.get("ram_gb")
            or hardware.get("ram_free_gb")
            or hardware.get("free_ram_gb")
            or 0.0
        )
        free_ram_gb = float(
            hardware.get("ram_free_gb")
            or hardware.get("free_ram_gb")
            or ram_total_gb
            or 0.0
        )
        unified = bool(hardware.get("unified_memory", False))
        mem_bw_gbps = float(
            hardware.get("mem_bandwidth_gbps")
            or (180.0 if unified else (320.0 if available_vram_gb >= 6.0 else 45.0))
        )

    if unified:
        total_unified = max(ram_total_gb, free_ram_gb, available_vram_gb)
        unified_cap_gb = round(total_unified * 0.70, 2)
        effective_vram = (
            min(available_vram_gb, unified_cap_gb)
            if available_vram_gb > 0.0
            else unified_cap_gb
        )
        if required_gb <= effective_vram:
            est_tok_s = round(min(120.0, (mem_bw_gbps / size_gb) * 0.65), 1)
            return {
                "emoji": "🟢",
                "level": "green",
                "status": "gpu_fluid",
                "label": "Gira Fluido in Memoria Unificata",
                "quant": quant,
                "required_gb": required_gb,
                "available_vram_gb": round(effective_vram, 2),
                "free_ram_gb": round(free_ram_gb, 2),
                "unified_memory": True,
                "unified_cap_gb": unified_cap_gb,
                "est_tok_s": est_tok_s,
            }
        if required_gb <= total_unified * 0.85:
            est_tok_s = round(max(6.0, min(16.0, 16.0 - max(0.0, required_gb - effective_vram))), 1)
            return {
                "emoji": "🟡",
                "level": "yellow",
                "status": "hybrid_offload",
                "label": "Ibrido Memoria Unificata (>70% cap)",
                "quant": quant,
                "required_gb": required_gb,
                "available_vram_gb": round(effective_vram, 2),
                "free_ram_gb": round(free_ram_gb, 2),
                "unified_memory": True,
                "unified_cap_gb": unified_cap_gb,
                "est_tok_s": est_tok_s,
            }
        est_tok_s = round(max(0.8, min(4.0, 4.0 * total_unified / max(1.0, required_gb))), 1)
        return {
            "emoji": "🔴",
            "level": "red",
            "status": "too_heavy",
            "label": "Troppo Pesante per Memoria Unificata",
            "quant": quant,
            "required_gb": required_gb,
            "available_vram_gb": round(effective_vram, 2),
            "free_ram_gb": round(free_ram_gb, 2),
            "unified_memory": True,
            "unified_cap_gb": unified_cap_gb,
            "est_tok_s": est_tok_s,
        }

    if available_vram_gb > 0.0 and required_gb <= available_vram_gb * 0.90:
        est_tok_s = round(min(120.0, (mem_bw_gbps / size_gb) * 0.65), 1)
        return {
            "emoji": "🟢",
            "level": "green",
            "status": "gpu_fluid",
            "label": "Gira Fluido in GPU",
            "quant": quant,
            "required_gb": required_gb,
            "available_vram_gb": round(available_vram_gb, 2),
            "free_ram_gb": round(free_ram_gb, 2),
            "est_tok_s": est_tok_s,
        }

    if required_gb <= available_vram_gb + free_ram_gb * 0.75:
        offload_excess = max(0.0, required_gb - available_vram_gb)
        est_tok_s = round(max(6.0, min(18.0, 18.0 - offload_excess * 0.8)), 1)
        return {
            "emoji": "🟡",
            "level": "yellow",
            "status": "hybrid_offload",
            "label": "Ibrido GPU + RAM Sistema",
            "quant": quant,
            "required_gb": required_gb,
            "available_vram_gb": round(available_vram_gb, 2),
            "free_ram_gb": round(free_ram_gb, 2),
            "est_tok_s": est_tok_s,
        }

    est_tok_s = round(
        max(0.8, min(4.0, 4.0 * (available_vram_gb + free_ram_gb) / max(1.0, required_gb))), 1
    )
    return {
        "emoji": "🔴",
        "level": "red",
        "status": "too_heavy",
        "label": "Troppo Pesante per questo Sistema",
        "quant": quant,
        "required_gb": required_gb,
        "available_vram_gb": round(available_vram_gb, 2),
        "free_ram_gb": round(free_ram_gb, 2),
        "est_tok_s": est_tok_s,
    }


async def stream_catalog_model_pull(
    entry: dict[str, Any],
    *,
    models_dir: pathlib.Path | str | None = None,
    ollama_base: str = OLLAMA_BASE,
) -> AsyncIterator[bytes]:
    """
    Stream SSE progress events while pulling an Ollama tag or downloading a GGUF artifact
    from an allowlisted host with automatic post-download SHA-256 verification.
    """
    model_id = str(entry.get("id") or "")
    backend = str(entry.get("backend") or "ollama").lower()

    def _sse(payload: dict[str, Any]) -> bytes:
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()

    yield _sse({"model_id": model_id, "backend": backend, "stage": "starting", "status": "initializing", "pct": 0})

    if backend == "ollama":
        tag = str(entry.get("ollama_tag") or "").strip()
        if not tag:
            yield _sse({"model_id": model_id, "stage": "error", "error": "Missing ollama_tag in catalog."})
            return
        try:
            async with (
                httpx.AsyncClient(base_url=ollama_base, timeout=600.0) as client,
                client.stream(
                    "POST",
                    "/api/pull",
                    json={"name": tag, "stream": True},
                    headers={"Content-Type": "application/json", "Accept": "application/x-ndjson"},
                ) as resp,
            ):
                    if resp.status_code >= 400:
                        body_txt = (await resp.aread()).decode("utf-8", errors="replace")
                        yield _sse(
                            {
                                "model_id": model_id,
                                "stage": "error",
                                "error": f"Ollama pull failed (HTTP {resp.status_code}): {body_txt}",
                            }
                        )
                        return
                    async for line in resp.aiter_lines():
                        if not line.strip():
                            continue
                        try:
                            evt = json.loads(line)
                        except ValueError:
                            continue
                        if isinstance(evt, dict):
                            if evt.get("error"):
                                yield _sse({"model_id": model_id, "stage": "error", "error": str(evt["error"])})
                                return
                            completed = int(evt.get("completed") or 0)
                            total = int(evt.get("total") or 0)
                            pct = round((completed / total) * 100.0, 1) if total > 0 else 0.0
                            yield _sse(
                                {
                                    "model_id": model_id,
                                    "backend": "ollama",
                                    "stage": "downloading",
                                    "status": str(evt.get("status") or "pulling"),
                                    "completed": completed,
                                    "total": total,
                                    "pct": pct,
                                }
                            )
            yield _sse(
                {
                    "model_id": model_id,
                    "backend": "ollama",
                    "stage": "completed",
                    "status": "completed",
                    "pct": 100,
                    "verified": True,
                }
            )
        except Exception as exc:
            yield _sse({"model_id": model_id, "stage": "error", "error": f"Ollama pull error: {exc}"})
        return

    # GGUF allowlisted HTTPS download + post-download SHA-256 verification
    try:
        download_url = _validate_allowlisted_download_url(str(entry.get("download_url") or ""))
        resolved_dir, _ = resolve_models_dir(models_dir)
        resolved_dir.mkdir(parents=True, exist_ok=True)
        filename = str(entry.get("filename") or f"{model_id}.gguf").strip()
        target_path = _resolve_safe_model_path(resolved_dir, filename)
        part_path = target_path.with_name(target_path.name + ".part")
    except Exception as exc:
        yield _sse({"model_id": model_id, "stage": "error", "error": str(exc)})
        return

    try:
        downloaded = 0
        total_bytes = 0
        current_url = _validate_allowlisted_download_url(download_url)
        async with httpx.AsyncClient(follow_redirects=False, timeout=600.0) as client:
            for _hop in range(5):
                current_url = _validate_allowlisted_download_url(current_url)
                async with client.stream("GET", current_url) as resp:
                    if resp.status_code in (301, 302, 303, 307, 308):
                        location = resp.headers.get("location")
                        if not location:
                            yield _sse({"model_id": model_id, "stage": "error", "error": "Empty redirect location."})
                            return
                        current_url = _validate_allowlisted_download_url(
                            urllib.parse.urljoin(current_url, location)
                        )
                        continue
                    final_host = (resp.url.host or "").lower()
                    if resp.url.scheme.lower() != "https" or not _is_allowlisted_host(final_host):
                        yield _sse(
                            {
                                "model_id": model_id,
                                "stage": "error",
                                "error": f"Redirect host '{final_host}' blocked by allowlist.",
                            }
                        )
                        return
                    if resp.status_code >= 400:
                        yield _sse(
                            {
                                "model_id": model_id,
                                "stage": "error",
                                "error": f"Download failed with HTTP {resp.status_code}.",
                            }
                        )
                        return
                    total_bytes = int(resp.headers.get("content-length") or 0)
                    with open(part_path, "wb") as out_fh:
                        last_pct = -1.0
                        async for chunk in resp.aiter_bytes(chunk_size=262144):
                            if not chunk:
                                continue
                            out_fh.write(chunk)
                            downloaded += len(chunk)
                            pct = round((downloaded / total_bytes) * 100.0, 1) if total_bytes > 0 else 0.0
                            if pct - last_pct >= 2.0 or (total_bytes > 0 and downloaded == total_bytes):
                                last_pct = pct
                                yield _sse(
                                    {
                                        "model_id": model_id,
                                        "backend": "gguf",
                                        "stage": "downloading",
                                        "status": "downloading",
                                        "completed": downloaded,
                                        "total": total_bytes,
                                        "pct": pct,
                                    }
                                )
                    break
            else:
                raise PermissionError("Too many redirects during model download.")

        yield _sse(
            {
                "model_id": model_id,
                "backend": "gguf",
                "stage": "verifying",
                "status": "verifying_sha256",
                "pct": 99,
            }
        )
        actual_sha = compute_file_sha256(part_path)
        expected_sha = str(entry.get("expected_sha256") or "").strip().lower()
        if expected_sha and _SHA256_HEX_RE.fullmatch(expected_sha):
            if not secrets.compare_digest(actual_sha, expected_sha):
                part_path.unlink(missing_ok=True)
                yield _sse(
                    {
                        "model_id": model_id,
                        "stage": "error",
                        "code": "sha256_mismatch",
                        "error": f"SHA-256 mismatch! Expected {expected_sha}, got {actual_sha}.",
                    }
                )
                return
            os.replace(part_path, target_path)
            pin_res = pin_model_sha256(
                target_path.relative_to(resolved_dir).as_posix(),
                expected_sha,
                models_dir=resolved_dir,
            )
            yield _sse(
                {
                    "model_id": model_id,
                    "backend": "gguf",
                    "stage": "completed",
                    "status": "VERIFIED",
                    "pct": 100,
                    "verified": True,
                    "path": pin_res.get("rel_path"),
                    "sha256": actual_sha,
                }
            )
            return

        os.replace(part_path, target_path)
        rel = target_path.relative_to(resolved_dir).as_posix()
        yield _sse(
            {
                "model_id": model_id,
                "backend": "gguf",
                "stage": "completed",
                "status": "UNVERIFIED_NO_MANIFEST",
                "pct": 100,
                "verified": False,
                "path": rel,
                "sha256": actual_sha,
            }
        )
    except Exception as exc:
        if "part_path" in locals() and part_path.exists():
            part_path.unlink(missing_ok=True)
        yield _sse({"model_id": model_id, "stage": "error", "error": f"Download failed: {exc}"})


def compute_file_sha256_4m(file_path: pathlib.Path) -> str:
    """Compute SHA-256 hex digest streaming in 4 MiB blocks."""
    digest = hashlib.sha256()
    with open(file_path, "rb") as fh:
        while True:
            block = fh.read(_VERIFY_CHUNK_BYTES)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest().lower()


def _cleanup_expired_prepared_locked(now: float | None = None) -> None:
    current = time.monotonic() if now is None else now
    expired = [k for k, v in _PREPARED_DOWNLOADS.items() if current >= float(v.get("expires_at") or 0.0)]
    for k in expired:
        _PREPARED_DOWNLOADS.pop(k, None)


async def _head_with_safe_redirects(
    url: str,
    client: httpx.AsyncClient,
    *,
    max_hops: int = 5,
) -> tuple[str, httpx.Response]:
    current = _validate_allowlisted_download_url(url)
    for _ in range(max_hops):
        resp = await client.head(current, follow_redirects=False)
        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("location")
            if not location:
                break
            next_url = urllib.parse.urljoin(current, location)
            current = _validate_allowlisted_download_url(next_url)
            continue
        return current, resp
    raise PermissionError("Too many redirects during model download pre-flight.")


async def prepare_model_download(
    *,
    model_id: str | None = None,
    custom_url: str | None = None,
    ollama_tag: str | None = None,
    models_dir: pathlib.Path | str | None = None,
    head_client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Pre-flight HEAD check + Hardware-Fit + disk/resume inspection before user consent."""
    resolved_dir, _ = resolve_models_dir(models_dir)
    resolved_dir.mkdir(parents=True, exist_ok=True)
    disk_usage = shutil.disk_usage(resolved_dir)
    free_disk_bytes = int(disk_usage.free)

    catalog = load_model_catalog()
    entry: dict[str, Any] | None = None
    if model_id:
        entry = next((item for item in catalog if item.get("id") == model_id), None)
        if entry is None and not custom_url and not ollama_tag:
            raise LookupError(f"Catalog model_id '{model_id}' not found.")

    if ollama_tag or (entry and str(entry.get("backend") or "ollama").lower() == "ollama"):
        tag = str(ollama_tag or (entry or {}).get("ollama_tag") or "").strip()
        resolved_id = str((entry or {}).get("id") or tag)
        est_gb = float((entry or {}).get("disk_required_gb") or 4.7)
        exact_bytes = int(est_gb * (1024**3))
        fit = evaluate_model_fit(est_gb)
        token = secrets.token_urlsafe(24)
        with _PREPARED_LOCK:
            _cleanup_expired_prepared_locked()
            _PREPARED_DOWNLOADS[token] = {
                "token": token,
                "model_id": resolved_id,
                "backend": "ollama",
                "ollama_tag": tag,
                "source_url": f"ollama://{tag}",
                "exact_bytes": exact_bytes,
                "license_spdx": str((entry or {}).get("license_spdx") or "Apache-2.0"),
                "expected_sha256": None,
                "etag": None,
                "filename": None,
                "expires_at": time.monotonic() + _PREPARED_TTL_SEC,
            }
        return {
            "ok": True,
            "download_token": token,
            "model_id": resolved_id,
            "backend": "ollama",
            "source_url": f"ollama://{tag}",
            "exact_bytes": exact_bytes,
            "exact_gb": round(exact_bytes / (1024**3), 2),
            "free_disk_bytes": free_disk_bytes,
            "free_disk_gb": round(free_disk_bytes / (1024**3), 2),
            "sufficient_disk": free_disk_bytes >= int(exact_bytes * 1.10),
            "resume_offset_bytes": 0,
            "license_spdx": str((entry or {}).get("license_spdx") or "Apache-2.0"),
            "expected_sha256": None,
            "hardware_fit": fit,
        }

    raw_url = str(custom_url or (entry or {}).get("download_url") or "").strip()
    if not raw_url:
        raise ValueError("Specify a valid model_id, custom_url, or ollama_tag.")
    safe_url = _validate_allowlisted_download_url(raw_url)
    parsed_path_name = pathlib.Path(urllib.parse.urlsplit(safe_url).path).name
    filename = str((entry or {}).get("filename") or parsed_path_name or "model.gguf").strip()
    if not filename.lower().endswith(".gguf"):
        filename = f"{filename}.gguf"
    safe_target = _resolve_safe_model_path(resolved_dir, filename)
    downloads_dir = resolved_dir / ".downloads"
    downloads_dir.mkdir(parents=True, exist_ok=True)
    part_path = downloads_dir / f"{safe_target.name}.part"
    resume_offset = part_path.stat().st_size if part_path.is_file() else 0

    own_client = head_client is None
    client = head_client or httpx.AsyncClient(follow_redirects=False, timeout=20.0)
    try:
        final_url, resp = await _head_with_safe_redirects(safe_url, client)
    finally:
        if own_client:
            await client.aclose()

    if resp.status_code >= 400:
        raise RuntimeError(f"HEAD preflight failed with HTTP {resp.status_code}.")

    exact_bytes = int(resp.headers.get("content-length") or 0)
    if exact_bytes <= 0 and entry:
        exact_bytes = int(float(entry.get("disk_required_gb") or 4.0) * (1024**3))
    etag = (resp.headers.get("etag") or "").strip() or None
    linked_etag = (resp.headers.get("x-linked-etag") or "").strip().strip('"').lower()
    expected_sha = str((entry or {}).get("expected_sha256") or "").strip().lower()
    if not expected_sha and _SHA256_HEX_RE.fullmatch(linked_etag):
        expected_sha = linked_etag

    est_gb = round(exact_bytes / (1024**3), 2) if exact_bytes > 0 else float((entry or {}).get("disk_required_gb") or 4.0)
    fit = evaluate_model_fit(est_gb)
    license_spdx = str((entry or {}).get("license_spdx") or "see-model-card")
    resolved_id = str((entry or {}).get("id") or safe_target.stem)

    token = secrets.token_urlsafe(24)
    with _PREPARED_LOCK:
        _cleanup_expired_prepared_locked()
        _PREPARED_DOWNLOADS[token] = {
            "token": token,
            "model_id": resolved_id,
            "backend": "gguf",
            "source_url": final_url,
            "filename": safe_target.name,
            "exact_bytes": exact_bytes,
            "etag": etag,
            "expected_sha256": expected_sha or None,
            "license_spdx": license_spdx,
            "expires_at": time.monotonic() + _PREPARED_TTL_SEC,
        }

    return {
        "ok": True,
        "download_token": token,
        "model_id": resolved_id,
        "backend": "gguf",
        "source_url": final_url,
        "filename": safe_target.name,
        "exact_bytes": exact_bytes,
        "exact_gb": est_gb,
        "free_disk_bytes": free_disk_bytes,
        "free_disk_gb": round(free_disk_bytes / (1024**3), 2),
        "sufficient_disk": free_disk_bytes >= int(exact_bytes * 1.10) if exact_bytes > 0 else True,
        "resume_offset_bytes": resume_offset,
        "etag": etag,
        "license_spdx": license_spdx,
        "expected_sha256": expected_sha or None,
        "hardware_fit": fit,
    }


def get_prepared_download(download_token: str) -> dict[str, Any] | None:
    """Look up a non-expired prepared download entry."""
    with _PREPARED_LOCK:
        _cleanup_expired_prepared_locked()
        item = _PREPARED_DOWNLOADS.get(download_token)
        return dict(item) if item is not None else None


async def stream_resumable_model_download(
    download_token: str,
    *,
    confirm_authorize: bool,
    models_dir: pathlib.Path | str | None = None,
    ollama_base: str = OLLAMA_BASE,
    http_client: httpx.AsyncClient | None = None,
) -> AsyncIterator[bytes]:
    """Stream resumable HTTPS Range download with `.part` + 4 MiB SHA-256 verification."""

    def _sse(payload: dict[str, Any]) -> bytes:
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()

    if confirm_authorize is not True:
        yield _sse({"stage": "error", "code": "consent_required", "error": "Explicit authorization required."})
        return

    prepared = get_prepared_download(download_token)
    if prepared is None:
        yield _sse({"stage": "error", "code": "invalid_token", "error": "Prepared download token expired or invalid."})
        return

    model_id = str(prepared["model_id"])
    backend = str(prepared["backend"])
    if backend == "ollama":
        async for chunk in stream_catalog_model_pull(
            {"id": model_id, "backend": "ollama", "ollama_tag": prepared.get("ollama_tag")},
            models_dir=models_dir,
            ollama_base=ollama_base,
        ):
            yield chunk
        return

    resolved_dir, _ = resolve_models_dir(models_dir)
    resolved_dir.mkdir(parents=True, exist_ok=True)
    exact_bytes = int(prepared.get("exact_bytes") or 0)
    free_bytes = int(shutil.disk_usage(resolved_dir).free)
    if exact_bytes > 0 and free_bytes < int(exact_bytes * 1.10):
        yield _sse(
            {
                "model_id": model_id,
                "stage": "error",
                "code": "insufficient_disk",
                "error": "Insufficient free disk space (10% safety margin required).",
            }
        )
        return

    target_path = _resolve_safe_model_path(resolved_dir, str(prepared["filename"]))
    downloads_dir = resolved_dir / ".downloads"
    downloads_dir.mkdir(parents=True, exist_ok=True)
    part_path = downloads_dir / f"{target_path.name}.part"
    meta_path = downloads_dir / f"{target_path.name}.part.meta.json"

    resume_offset = part_path.stat().st_size if part_path.is_file() else 0
    saved_etag = prepared.get("etag")
    if meta_path.is_file():
        try:
            meta_doc = json.loads(meta_path.read_text(encoding="utf-8"))
            if isinstance(meta_doc, dict) and meta_doc.get("etag") and saved_etag and meta_doc.get("etag") != saved_etag:
                resume_offset = 0
                part_path.unlink(missing_ok=True)
        except (OSError, ValueError):
            pass

    meta_path.write_text(
        json.dumps({"model_id": model_id, "etag": saved_etag, "expected_sha256": prepared.get("expected_sha256")}),
        encoding="utf-8",
    )

    yield _sse(
        {
            "model_id": model_id,
            "backend": "gguf",
            "stage": "starting",
            "status": "resuming" if resume_offset > 0 else "initializing",
            "resume_offset": resume_offset,
            "pct": round((resume_offset / exact_bytes) * 100.0, 1) if (exact_bytes > 0 and resume_offset > 0) else 0.0,
        }
    )

    current_url = _validate_allowlisted_download_url(str(prepared["source_url"]))
    req_headers: dict[str, str] = {}
    if resume_offset > 0:
        req_headers["Range"] = f"bytes={resume_offset}-"
        if saved_etag:
            req_headers["If-Range"] = str(saved_etag)

    own_client = http_client is None
    client = http_client or httpx.AsyncClient(follow_redirects=False, timeout=600.0)
    try:
        for _ in range(5):
            current_url = _validate_allowlisted_download_url(current_url)
            async with client.stream("GET", current_url, headers=req_headers) as resp:
                if resp.status_code in (301, 302, 303, 307, 308):
                    location = resp.headers.get("location")
                    if not location:
                        yield _sse({"model_id": model_id, "stage": "error", "error": "Empty redirect location."})
                        return
                    current_url = _validate_allowlisted_download_url(urllib.parse.urljoin(current_url, location))
                    continue

                if resp.status_code >= 400:
                    yield _sse(
                        {
                            "model_id": model_id,
                            "stage": "error",
                            "error": f"Download failed with HTTP {resp.status_code}.",
                        }
                    )
                    return

                is_partial = resp.status_code == 206 and resume_offset > 0
                file_mode = "ab" if is_partial else "wb"
                downloaded = resume_offset if is_partial else 0
                content_len = int(resp.headers.get("content-length") or 0)
                total_bytes = (downloaded + content_len) if (is_partial and content_len > 0) else (content_len or exact_bytes)

                with open(part_path, file_mode) as out_fh:
                    last_pct = -1.0
                    async for chunk in resp.aiter_bytes(chunk_size=262144):
                        if not chunk:
                            continue
                        out_fh.write(chunk)
                        downloaded += len(chunk)
                        pct = round((downloaded / total_bytes) * 100.0, 1) if total_bytes > 0 else 0.0
                        if pct - last_pct >= 2.0 or (total_bytes > 0 and downloaded >= total_bytes):
                            last_pct = pct
                            yield _sse(
                                {
                                    "model_id": model_id,
                                    "backend": "gguf",
                                    "stage": "downloading",
                                    "status": "resuming_206" if is_partial else "downloading",
                                    "completed": downloaded,
                                    "total": total_bytes,
                                    "pct": min(99.0, pct),
                                }
                            )
                break
        else:
            yield _sse({"model_id": model_id, "stage": "error", "error": "Too many redirects."})
            return

        yield _sse({"model_id": model_id, "backend": "gguf", "stage": "verifying", "status": "verifying_sha256", "pct": 99.0})
        actual_sha = compute_file_sha256_4m(part_path)
        expected_sha = str(prepared.get("expected_sha256") or "").strip().lower()
        if expected_sha and _SHA256_HEX_RE.fullmatch(expected_sha):
            if not secrets.compare_digest(actual_sha, expected_sha):
                part_path.unlink(missing_ok=True)
                meta_path.unlink(missing_ok=True)
                yield _sse(
                    {
                        "model_id": model_id,
                        "stage": "error",
                        "code": "sha256_mismatch",
                        "error": f"SHA-256 mismatch! Expected {expected_sha}, got {actual_sha}.",
                    }
                )
                return

        os.replace(part_path, target_path)
        meta_path.unlink(missing_ok=True)
        rel = target_path.relative_to(resolved_dir).as_posix()
        pin_res = pin_model_sha256(rel, actual_sha, models_dir=resolved_dir)
        yield _sse(
            {
                "model_id": model_id,
                "backend": "gguf",
                "stage": "completed",
                "status": "VERIFIED",
                "pct": 100.0,
                "verified": True,
                "path": pin_res.get("rel_path", rel),
                "sha256": actual_sha,
            }
        )
    except PermissionError as exc:
        yield _sse({"model_id": model_id, "stage": "error", "code": "blocked_host", "error": str(exc)})
    except Exception as exc:
        yield _sse({"model_id": model_id, "stage": "error", "error": f"Download failed: {exc}"})
    finally:
        if own_client:
            await client.aclose()


def discover_local_models(
    *,
    ollama_dir: pathlib.Path | str | None = None,
    hf_cache_dir: pathlib.Path | str | None = None,
    lmstudio_dir: pathlib.Path | str | None = None,
) -> dict[str, Any]:
    """Read-only Zero-Copy scan of Ollama, HuggingFace Hub, and LM Studio local caches."""
    home = pathlib.Path.home()
    ol_root = pathlib.Path(ollama_dir).expanduser() if ollama_dir is not None else (home / ".ollama" / "models")
    hf_root = pathlib.Path(hf_cache_dir).expanduser() if hf_cache_dir is not None else (home / ".cache" / "huggingface" / "hub")
    lms_root = pathlib.Path(lmstudio_dir).expanduser() if lmstudio_dir is not None else (home / ".lmstudio" / "models")

    discovered: list[dict[str, Any]] = []
    seen_paths: set[str] = set()

    # 1. Ollama manifests + blobs (read-only)
    manifests_dir = ol_root / "manifests"
    blobs_dir = ol_root / "blobs"
    if manifests_dir.is_dir():
        for mf in sorted(manifests_dir.rglob("*")):
            if not mf.is_file():
                continue
            try:
                rel_parts = mf.relative_to(manifests_dir).parts
                model_label = ":".join(rel_parts[-2:]) if len(rel_parts) >= 2 else mf.name
                raw = json.loads(mf.read_text(encoding="utf-8", errors="replace"))
                layers = raw.get("layers", []) if isinstance(raw, dict) else []
                total_bytes = 0
                for layer in layers:
                    if isinstance(layer, dict):
                        total_bytes += int(layer.get("size") or 0)
                        digest = str(layer.get("digest") or "")
                        if total_bytes == 0 and digest.startswith("sha256:") and blobs_dir.is_dir():
                            blob_file = blobs_dir / digest.replace(":", "-")
                            if blob_file.is_file():
                                total_bytes += blob_file.stat().st_size
                size_gb = round(total_bytes / (1024**3), 2)
                key = str(mf.resolve())
                if key not in seen_paths:
                    seen_paths.add(key)
                    discovered.append(
                        {
                            "source": "ollama",
                            "name": model_label,
                            "path": str(mf),
                            "size_bytes": total_bytes,
                            "size_gb": size_gb,
                            "zero_copy": True,
                            "extra_disk_gb": 0.0,
                            "hardware_fit": evaluate_model_fit(max(0.5, size_gb)),
                        }
                    )
            except (OSError, ValueError):
                continue

    # 2. HuggingFace Hub GGUF files (read-only)
    if hf_root.is_dir():
        for model_dir in sorted(hf_root.glob("models--*")):
            if not model_dir.is_dir():
                continue
            for gguf in sorted(model_dir.rglob("*.gguf")):
                if not gguf.is_file():
                    continue
                try:
                    sz = gguf.stat().st_size
                    size_gb = round(sz / (1024**3), 2)
                    key = str(gguf.resolve())
                    if key not in seen_paths:
                        seen_paths.add(key)
                        discovered.append(
                            {
                                "source": "huggingface_cache",
                                "name": gguf.name,
                                "path": str(gguf),
                                "size_bytes": sz,
                                "size_gb": size_gb,
                                "zero_copy": True,
                                "extra_disk_gb": 0.0,
                                "hardware_fit": evaluate_model_fit(max(0.5, size_gb)),
                            }
                        )
                except OSError:
                    continue

    # 3. LM Studio GGUF files (read-only)
    if lms_root.is_dir():
        for gguf in sorted(lms_root.rglob("*.gguf")):
            if not gguf.is_file():
                continue
            try:
                sz = gguf.stat().st_size
                size_gb = round(sz / (1024**3), 2)
                key = str(gguf.resolve())
                if key not in seen_paths:
                    seen_paths.add(key)
                    discovered.append(
                        {
                            "source": "lmstudio",
                            "name": gguf.name,
                            "path": str(gguf),
                            "size_bytes": sz,
                            "size_gb": size_gb,
                            "zero_copy": True,
                            "extra_disk_gb": 0.0,
                            "hardware_fit": evaluate_model_fit(max(0.5, size_gb)),
                        }
                    )
            except OSError:
                continue

    return {
        "ok": True,
        "read_only": True,
        "extra_disk_gb": 0.0,
        "count": len(discovered),
        "models": discovered,
    }
