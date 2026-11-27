"""Air-gapped Model Manager & Hardware Wizard routes (`/api/models/*`, `/api/system/hardware-wizard`)."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from ..services.hardware_wizard import detect_hardware_specs, recommend_hardware_profile
from ..services.model_manager import (
    discover_local_models,
    get_airgapped_inventory,
    get_annotated_model_catalog,
    load_model_catalog,
    pin_model_sha256,
    prepare_model_download,
    stream_catalog_model_pull,
    stream_resumable_model_download,
    verify_model_sha256,
)

router = APIRouter(tags=["models"])


class ModelVerifyRequest(BaseModel):
    """Payload for offline SHA-256 verification of a model artifact."""

    path: str = Field(min_length=1, max_length=320)
    expected_sha256: str | None = Field(default=None, max_length=128)


class ModelPinShaRequest(BaseModel):
    """Payload for computing and pinning SHA-256 into `manifest.sha256.json`."""

    path: str = Field(min_length=1, max_length=320)
    expected_sha256: str | None = Field(default=None, max_length=128)


class ModelPullRequest(BaseModel):
    """Payload for opt-in model pull from curated catalog (`confirm_online_pull` required)."""

    model_id: str = Field(min_length=1, max_length=160)
    confirm_online_pull: bool = False


class ModelDownloadPrepareRequest(BaseModel):
    """Payload for pre-flight download consent preparation."""

    model_id: str | None = Field(default=None, max_length=160)
    custom_url: str | None = Field(default=None, max_length=512)
    ollama_tag: str | None = Field(default=None, max_length=160)


class ModelDownloadStartRequest(BaseModel):
    """Payload to start an explicitly authorized resumable download."""

    download_token: str = Field(min_length=8, max_length=128)
    confirm_authorize: bool = False


@router.get("/api/models/inventory")
async def models_inventory() -> JSONResponse:
    """Return air-gapped GGUF/Colibri/Modelfile inventory from `LOCAL_LLM_MODELS_DIR`."""
    data = await asyncio.to_thread(get_airgapped_inventory)
    return JSONResponse(status_code=200, content=data)


@router.post("/api/models/verify")
async def models_verify(req: ModelVerifyRequest) -> JSONResponse:
    """Verify offline SHA-256 checksum of a model file in `LOCAL_LLM_MODELS_DIR`."""
    try:
        res = await asyncio.to_thread(
            verify_model_sha256,
            req.path,
            req.expected_sha256,
        )
    except PermissionError as exc:
        return JSONResponse(status_code=403, content={"ok": False, "error": str(exc)})
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"ok": False, "error": str(exc)})

    status_code = 404 if res.get("status") == "MISSING" else 200
    return JSONResponse(status_code=status_code, content=res)


@router.post("/api/models/pin-sha256")
async def models_pin_sha256(req: ModelPinShaRequest) -> JSONResponse:
    """Compute SHA-256 offline and pin it into `<models_dir>/manifest.sha256.json`."""
    try:
        res = await asyncio.to_thread(
            pin_model_sha256,
            req.path,
            req.expected_sha256,
        )
    except PermissionError as exc:
        return JSONResponse(status_code=403, content={"ok": False, "error": str(exc)})
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"ok": False, "error": str(exc)})

    if res.get("status") == "MISSING":
        return JSONResponse(status_code=404, content=res)
    if res.get("status") == "MISMATCH":
        return JSONResponse(status_code=409, content=res)
    return JSONResponse(status_code=200, content=res)


@router.get("/api/models/catalog")
async def models_catalog() -> JSONResponse:
    """Return curated model catalog annotated with live `fits_hardware` badges."""
    data = await asyncio.to_thread(get_annotated_model_catalog)
    return JSONResponse(status_code=200, content=data)


@router.post("/api/models/pull")
async def models_pull(req: ModelPullRequest):
    """
    Opt-in curated model downloader with SSE progress.
    FAIL-CLOSED unless `confirm_online_pull=True` (air-gapped default invariant).
    """
    if req.confirm_online_pull is not True:
        return JSONResponse(
            status_code=403,
            content={
                "ok": False,
                "air_gapped": True,
                "code": "air_gapped_confirmation_required",
                "error": (
                    "Modalità Air-Gapped attiva: conferma esplicitamente la connessione online "
                    "temporanea (`confirm_online_pull: true`) per scaricare questo modello."
                ),
            },
        )

    catalog = await asyncio.to_thread(load_model_catalog)
    entry = next((item for item in catalog if item.get("id") == req.model_id), None)
    if entry is None:
        return JSONResponse(
            status_code=404,
            content={"ok": False, "error": f"Catalog model_id '{req.model_id}' not found."},
        )

    return StreamingResponse(
        stream_catalog_model_pull(entry),
        media_type="text/event-stream; charset=utf-8",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/api/models/download/prepare")
async def models_download_prepare(req: ModelDownloadPrepareRequest) -> JSONResponse:
    """Prepare download metadata, Hardware-Fit, free disk, and resume offset without downloading."""
    try:
        data = await prepare_model_download(
            model_id=req.model_id,
            custom_url=req.custom_url,
            ollama_tag=req.ollama_tag,
        )
    except PermissionError as exc:
        return JSONResponse(status_code=403, content={"ok": False, "error": str(exc)})
    except LookupError as exc:
        return JSONResponse(status_code=404, content={"ok": False, "error": str(exc)})
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"ok": False, "error": str(exc)})
    except Exception as exc:
        return JSONResponse(status_code=502, content={"ok": False, "error": str(exc)})
    return JSONResponse(status_code=200, content=data)


@router.post("/api/models/download/start")
async def models_download_start(req: ModelDownloadStartRequest):
    """Start resumable Range download ONLY after explicit user authorization (`confirm_authorize=true`)."""
    if req.confirm_authorize is not True:
        return JSONResponse(
            status_code=403,
            content={
                "ok": False,
                "air_gapped": True,
                "code": "air_gapped_confirmation_required",
                "error": "Autorizzazione esplicita richiesta prima di avviare il download.",
            },
        )
    return StreamingResponse(
        stream_resumable_model_download(
            req.download_token,
            confirm_authorize=True,
        ),
        media_type="text/event-stream; charset=utf-8",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/api/models/discover-local")
async def models_discover_local() -> JSONResponse:
    """Read-only Zero-Copy scan of existing Ollama, HuggingFace, and LM Studio caches."""
    data = await asyncio.to_thread(discover_local_models)
    return JSONResponse(status_code=200, content=data)


@router.get("/api/system/hardware-wizard")
async def hardware_wizard_recommendation(
    vram_gb: float | None = Query(default=None, ge=0.0, le=512.0),
    ram_gb: float | None = Query(default=None, ge=1.0, le=2048.0),
) -> JSONResponse:
    """Return dynamic hardware profile & `LOCAL_LLM_MODELS_DIR` recommendations."""
    if vram_gb is not None and ram_gb is not None:
        rec = recommend_hardware_profile(vram_gb=vram_gb, ram_gb=ram_gb)
        return JSONResponse(status_code=200, content={"ok": True, **rec})

    detected = await asyncio.to_thread(detect_hardware_specs)
    rec = recommend_hardware_profile(
        vram_gb=detected["vram_gb"] if vram_gb is None else vram_gb,
        ram_gb=detected["ram_gb"] if ram_gb is None else ram_gb,
        gpu_name=detected["gpu_name"],
        free_disk_gb=detected["free_disk_gb"],
    )
    return JSONResponse(status_code=200, content={"ok": True, "detected": detected, **rec})
