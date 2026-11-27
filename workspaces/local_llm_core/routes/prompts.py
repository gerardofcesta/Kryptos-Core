"""Prompt preset CRUD routes (`/api/prompts*`)."""

from __future__ import annotations

from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse

from ..services.prompts import (
    delete_prompt_preset,
    load_prompt_presets,
    save_prompt_preset,
)

router = APIRouter(tags=["prompts"])


@router.get("/api/prompts")
async def list_prompts() -> dict[str, object]:
    """List all markdown prompt presets."""
    return {"prompts": load_prompt_presets()}


@router.post("/api/prompts/save")
async def save_prompt(payload: dict[str, object] = Body(...)) -> JSONResponse:
    """Create or update a prompt preset in `prompts/<id>.md`."""
    status_code, body = save_prompt_preset(payload)
    return JSONResponse(status_code=status_code, content=body)


@router.delete("/api/prompts/{prompt_id}")
async def remove_prompt(prompt_id: str) -> JSONResponse:
    """Delete a non-protected prompt preset."""
    status_code, body = delete_prompt_preset(prompt_id)
    return JSONResponse(status_code=status_code, content=body)
