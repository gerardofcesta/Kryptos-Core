"""FastAPI routes for Permission-Gated Go Mode (`/api/go-mode/*`)."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Body, Header
from fastapi.responses import JSONResponse

from ..services.go_mode import get_go_mode_manager

router = APIRouter(prefix="/api/go-mode", tags=["go-mode"])


@router.get("/config")
async def go_mode_get_config() -> JSONResponse:
    """Return Go Mode guardrail configuration (Levels 1-4 + immutable Level 5 shield)."""
    data = await asyncio.to_thread(get_go_mode_manager().get_config)
    return JSONResponse(status_code=200, content=data)


@router.post("/config")
async def go_mode_update_config(payload: dict[str, Any] = Body(...)) -> JSONResponse:
    """Update user-configurable Go Mode guardrails (Levels 1-4). Level 5 cannot be disabled here."""
    code, data = await asyncio.to_thread(get_go_mode_manager().update_config, payload)
    return JSONResponse(status_code=code, content=data)


@router.post("/propose")
async def go_mode_propose(
    payload: dict[str, Any] = Body(...),
    x_go_mode_break_glass: str | None = Header(default=None),
) -> JSONResponse:
    """Propose a file write, delete, shell command, or external action under Go Mode guardrails."""
    enriched = dict(payload)
    if x_go_mode_break_glass and "break_glass_token" not in enriched:
        enriched["break_glass_token"] = x_go_mode_break_glass
    code, data = await asyncio.to_thread(get_go_mode_manager().propose_action, enriched)
    return JSONResponse(status_code=code, content=data)


@router.post("/approve")
async def go_mode_approve(
    payload: dict[str, Any] = Body(...),
    x_go_mode_break_glass: str | None = Header(default=None),
) -> JSONResponse:
    """Approve and execute a pending Go Mode proposal (once or folder session auto-approve)."""
    enriched = dict(payload)
    if x_go_mode_break_glass and "break_glass_token" not in enriched:
        enriched["break_glass_token"] = x_go_mode_break_glass
    code, data = await asyncio.to_thread(get_go_mode_manager().approve_proposal, enriched)
    return JSONResponse(status_code=code, content=data)


@router.post("/reject")
async def go_mode_reject(payload: dict[str, Any] = Body(...)) -> JSONResponse:
    """Reject a pending Go Mode proposal."""
    code, data = await asyncio.to_thread(get_go_mode_manager().reject_proposal, payload)
    return JSONResponse(status_code=code, content=data)


@router.post("/rollback")
async def go_mode_rollback(payload: dict[str, Any] = Body(default={})) -> JSONResponse:
    """Undo last step or rollback all recorded changes in the Atomic Rollback Journal."""
    code, data = await asyncio.to_thread(get_go_mode_manager().rollback, payload)
    return JSONResponse(status_code=code, content=data)


@router.get("/break-glass")
async def go_mode_break_glass_status() -> JSONResponse:
    """Return Break-Glass ritual requirements (3 checkboxes + phrase) and current unlock status."""
    data = await asyncio.to_thread(get_go_mode_manager().get_break_glass_status)
    return JSONResponse(status_code=200, content=data)


@router.post("/break-glass")
async def go_mode_break_glass_unlock(payload: dict[str, Any] = Body(...)) -> JSONResponse:
    """Validate >= 3 risk checkboxes + exact manual phrase to issue a short-lived Break-Glass token."""
    code, data = await asyncio.to_thread(get_go_mode_manager().unlock_break_glass, payload)
    return JSONResponse(status_code=code, content=data)


@router.post("/break-glass/lock")
async def go_mode_break_glass_lock() -> JSONResponse:
    """Immediately re-lock the Level 5 Hard System Protection Shield."""
    data = await asyncio.to_thread(get_go_mode_manager().relock_break_glass)
    return JSONResponse(status_code=200, content=data)


@router.get("/sessions")
async def go_mode_sessions() -> JSONResponse:
    """Return active Go Mode sessions, pending HITL queue, undo journal, and audit log."""
    data = await asyncio.to_thread(get_go_mode_manager().get_sessions)
    return JSONResponse(status_code=200, content=data)
