"""Engine lifecycle control routes (`/api/control/engine`)."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse

from ..engine.coordinator import execute_engine_action

router = APIRouter(tags=["control"])


@router.post("/api/control/engine")
async def control_engine(payload: dict[str, object] = Body(...)) -> JSONResponse:
    """Execute a mutually exclusive engine lifecycle action via `EngineCoordinator`."""
    status_code, result = await asyncio.to_thread(execute_engine_action, dict(payload))
    return JSONResponse(status_code=status_code, content=result)
