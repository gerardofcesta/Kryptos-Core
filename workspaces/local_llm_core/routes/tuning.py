"""Hardware tuning bounds & on-demand advisor routes (`/api/system/tuning-bounds`, `/api/system/tuning-advisor`)."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..services.tuning_advisor import run_tuning_advisor
from ..services.tuning_bounds import compute_tuning_bounds

router = APIRouter(tags=["tuning"])


class TuningAdvisorRequest(BaseModel):
    """Request payload for `POST /api/system/tuning-advisor`."""

    question: str = Field(
        default="Quali impostazioni di Context e Heavy MoE RAM Limit sono sicure per il mio hardware?",
        max_length=2000,
    )
    model_params_b: float | None = Field(default=None, gt=0.1, le=2000.0)
    model_weights_gb: float | None = Field(default=None, ge=0.0, le=2000.0)
    kv_quant: str = Field(default="q8_0", max_length=16)
    use_llm: bool = True


@router.get("/api/system/tuning-bounds")
async def system_tuning_bounds(
    model_params_b: float | None = Query(default=None, gt=0.1, le=2000.0),
    model_weights_gb: float | None = Query(default=None, ge=0.0, le=2000.0),
    kv_quant: str = Query(default="q8_0", max_length=16),
) -> JSONResponse:
    """Return Layer 1 deterministic math safety bounds for tuning sliders."""
    data = await asyncio.to_thread(
        compute_tuning_bounds,
        model_params_b=model_params_b,
        model_weights_gb=model_weights_gb,
        kv_quant=kv_quant,
    )
    return JSONResponse(status_code=200, content=data)


@router.post("/api/system/tuning-advisor")
async def system_tuning_advisor(req: TuningAdvisorRequest) -> JSONResponse:
    """Query Layer 2 Hardware Advisor (local KB RAG + optional `local-fast`, strictly clamped)."""
    data = await run_tuning_advisor(
        question=req.question,
        model_params_b=req.model_params_b,
        model_weights_gb=req.model_weights_gb,
        kv_quant=req.kv_quant,
        use_llm=req.use_llm,
    )
    return JSONResponse(status_code=200, content=data)
