"""Static UI routes (`/`, `/index.html`, `/static/*`, `/assets/*`, `/favicon.ico`) for Offline Chat."""

from __future__ import annotations

import pathlib

from fastapi import APIRouter
from fastapi.responses import FileResponse, JSONResponse, Response

from ..config import OFFLINE_CHAT_DIR, PROJECT_ROOT, STATIC_DIR, resolve_web_dist_dir
from ..security import NO_STORE_HEADER

router = APIRouter(tags=["ui"])

_MIME_TYPES: dict[str, str] = {
    ".js": "application/javascript; charset=utf-8",
    ".mjs": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}


def _safe_child_file(base_dir: pathlib.Path, rel_path: str) -> pathlib.Path | None:
    clean = rel_path.lstrip("/\\")
    if not clean or "\x00" in clean:
        return None
    try:
        base_resolved = base_dir.resolve()
        candidate = (base_resolved / clean).resolve()
        if candidate == base_resolved or not candidate.is_relative_to(base_resolved):
            return None
        if candidate.is_file():
            return candidate
    except OSError:
        return None
    return None


def _no_store_file_response(path: pathlib.Path, media_type: str | None = None) -> FileResponse:
    resolved_mime = media_type or _MIME_TYPES.get(path.suffix.lower(), "application/octet-stream")
    return FileResponse(
        path,
        media_type=resolved_mime,
        headers={
            "Cache-Control": NO_STORE_HEADER,
            "Pragma": "no-cache",
        },
    )


@router.get("/")
@router.get("/index.html")
async def serve_index() -> Response:
    """Serve the compiled Stage 2 React SPA (`web/dist/index.html`) or static fallback."""
    dist_index = resolve_web_dist_dir() / "index.html"
    if dist_index.is_file():
        return _no_store_file_response(dist_index, "text/html; charset=utf-8")
    fallback_index = OFFLINE_CHAT_DIR / "index.html"
    if fallback_index.is_file():
        return _no_store_file_response(fallback_index, "text/html; charset=utf-8")
    return JSONResponse(status_code=404, content={"error": "Risorsa UI non trovata"})


@router.get("/static/{file_path:path}")
async def serve_static_asset(file_path: str) -> Response:
    """Serve Stage 2 bundled `/static/*` assets first, falling back to `offline-chat/static/`."""
    dist_static = resolve_web_dist_dir() / "static"
    for base in (dist_static, STATIC_DIR):
        target = _safe_child_file(base, file_path)
        if target is not None:
            return _no_store_file_response(target)
    return JSONResponse(status_code=404, content={"error": "Static asset not found"})


@router.get("/assets/{file_path:path}")
async def serve_dist_assets(file_path: str) -> Response:
    """Serve optional Vite `/assets/*` chunks from `web/dist/assets/`."""
    dist_assets = resolve_web_dist_dir() / "assets"
    target = _safe_child_file(dist_assets, file_path)
    if target is not None:
        return _no_store_file_response(target)
    return JSONResponse(status_code=404, content={"error": "Asset not found"})


@router.get("/favicon.ico")
async def serve_favicon() -> Response:
    """Serve local application icon without external network requests."""
    ico_path = PROJECT_ROOT / "launcher" / "local-llm.ico"
    if ico_path.is_file():
        return _no_store_file_response(ico_path, "image/x-icon")
    return Response(status_code=204)
