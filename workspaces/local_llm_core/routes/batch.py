"""FastAPI routes for Overnight Batch Job Runner & Thermal/RAM Watchdog (`/api/batch/*`)."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Body, Query
from fastapi.responses import JSONResponse, Response

from ..services.batch_runner import compute_safe_chunk_chars, get_batch_runner

router = APIRouter(prefix="/api/batch", tags=["batch"])


@router.get("/sizing")
async def batch_chunk_sizing(
    model: str = Query(default="local-fast"),
    chunk_chars: int | None = Query(default=None, ge=200, le=500000),
) -> JSONResponse:
    """Return Layer 1 `max_safe_ctx` and safe chunk sizing bounds for batch jobs."""
    data = await asyncio.to_thread(compute_safe_chunk_chars, model, chunk_chars)
    return JSONResponse(status_code=200, content={"ok": True, **data})


@router.get("/jobs")
async def batch_list_jobs() -> JSONResponse:
    """List all batch jobs and current Thermal & RAM Watchdog telemetry."""
    data = await asyncio.to_thread(get_batch_runner().list_jobs)
    return JSONResponse(status_code=200, content=data)


@router.post("/jobs")
async def batch_create_job(payload: dict[str, Any] = Body(...)) -> JSONResponse:
    """Create and start a chunked resumable batch job."""
    code, data = await get_batch_runner().create_and_start_job(payload)
    return JSONResponse(status_code=code, content=data)


@router.get("/jobs/{job_id}")
async def batch_get_job(job_id: str) -> JSONResponse:
    """Get detailed chunk progress & checkpoint state for a batch job."""
    code, data = await asyncio.to_thread(get_batch_runner().get_job, job_id)
    return JSONResponse(status_code=code, content=data)


@router.post("/jobs/{job_id}/pause")
async def batch_pause_job(job_id: str) -> JSONResponse:
    """Pause an active batch job cleanly at the next chunk boundary."""
    code, data = await asyncio.to_thread(get_batch_runner().pause_job, job_id)
    return JSONResponse(status_code=code, content=data)


@router.post("/jobs/{job_id}/resume")
async def batch_resume_job(job_id: str) -> JSONResponse:
    """Resume a paused or failed batch job from the next pending chunk."""
    code, data = await get_batch_runner().resume_job(job_id)
    return JSONResponse(status_code=code, content=data)


@router.post("/jobs/{job_id}/cancel")
async def batch_cancel_job(job_id: str) -> JSONResponse:
    """Cancel an active or paused batch job."""
    code, data = await asyncio.to_thread(get_batch_runner().cancel_job, job_id)
    return JSONResponse(status_code=code, content=data)


@router.get("/jobs/{job_id}/export")
async def batch_export_job(
    job_id: str,
    format: str = Query(default="md"),  # noqa: A002
) -> Response:
    """Export batch job output as Markdown, JSON, or CSV."""
    code, content, media_type, filename = await asyncio.to_thread(
        get_batch_runner().export_job, job_id, format
    )
    if code != 200:
        return JSONResponse(status_code=code, content=content)
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/watchdog")
async def batch_get_watchdog() -> JSONResponse:
    """Return live Thermal & RAM Watchdog status and thresholds."""
    data = await asyncio.to_thread(get_batch_runner().get_watchdog_status)
    return JSONResponse(status_code=200, content=data)


@router.post("/watchdog")
async def batch_update_watchdog(payload: dict[str, Any] = Body(...)) -> JSONResponse:
    """Update Thermal & RAM Watchdog thresholds."""
    code, data = await asyncio.to_thread(get_batch_runner().update_watchdog_config, payload)
    return JSONResponse(status_code=code, content=data)
