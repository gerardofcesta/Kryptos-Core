"""Session token endpoint for the offline browser UI."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from ..security import NO_STORE_HEADER, get_gateway_token

router = APIRouter(tags=["auth"])


@router.get("/api/auth/session-token")
async def get_session_token() -> JSONResponse:
    """Return the active gateway token to same-origin browser requests (`no-store`)."""
    return JSONResponse(
        status_code=200,
        content={"ok": True, "token": get_gateway_token()},
        headers={
            "Cache-Control": NO_STORE_HEADER,
            "Pragma": "no-cache",
        },
    )
