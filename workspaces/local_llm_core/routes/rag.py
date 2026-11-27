"""Local RAG API routes (`/api/rag/ingest`, `/api/rag/query`, `/api/rag/clear`, `/api/rag/status`)."""

from __future__ import annotations

import asyncio
import base64
import time
from typing import Literal

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..rag.store import get_rag_manager

router = APIRouter(prefix="/api/rag", tags=["rag"])
chat_ephemeral_router = APIRouter(tags=["chat-ephemeral-rag"])


class EphemeralDocItem(BaseModel):
    """Single dropped document for RAM-only Drop-to-Chat Ghost RAG."""

    filename: str = Field(min_length=1, max_length=260)
    content: str | None = None
    content_base64: str | None = None


class EphemeralDocsUploadRequest(BaseModel):
    """Payload for `POST /api/chat/ephemeral-docs` (single file or batch `files`)."""

    filename: str | None = Field(default=None, max_length=260)
    content: str | None = None
    content_base64: str | None = None
    files: list[EphemeralDocItem] | None = None
    target_tokens: int = Field(default=600, ge=50, le=2000)


class EphemeralDocsQueryRequest(BaseModel):
    """Payload for `POST /api/chat/ephemeral-docs/query`."""

    query: str = Field(min_length=1, max_length=4000)
    top_k: int = Field(default=4, ge=1, le=20)


class RagIngestRequest(BaseModel):
    """JSON payload for `/api/rag/ingest` (text or base64 document buffer)."""

    filename: str = Field(min_length=1, max_length=260)
    content: str | None = None
    content_base64: str | None = None
    mode: Literal["ghost", "vault"] = "ghost"
    collection: str = Field(default="default", min_length=1, max_length=64)
    ttl_seconds: float | None = Field(default=None, ge=0.05, le=86400.0)
    target_tokens: int = Field(default=350, ge=50, le=2000)
    enable_ocr: bool = True


class RagQueryRequest(BaseModel):
    """JSON payload for `/api/rag/query`."""

    query: str = Field(min_length=1, max_length=4000)
    mode: Literal["ghost", "vault"] = "ghost"
    collection: str = Field(default="default", min_length=1, max_length=64)
    top_k: int = Field(default=5, ge=1, le=50)


class RagClearRequest(BaseModel):
    """JSON payload for `/api/rag/clear`."""

    mode: Literal["ghost", "vault", "all"] = "ghost"
    collection: str | None = Field(default=None, max_length=64)


@router.post("/ingest")
async def rag_ingest(req: RagIngestRequest) -> JSONResponse:
    """Ingest a document via `doc-ingest-core` into Ghost (RAM-only) or Vault RAG."""
    t0 = time.perf_counter()
    if req.content_base64 is not None:
        try:
            raw_bytes = base64.b64decode(req.content_base64, validate=True)
        except Exception as exc:
            return JSONResponse(
                status_code=400,
                content={"ok": False, "error": f"Base64 non valido in content_base64: {exc}"},
            )
    elif req.content is not None:
        raw_bytes = req.content.encode("utf-8")
    else:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": "Fornire 'content' o 'content_base64'."},
        )

    try:
        mgr = get_rag_manager()
        result = await asyncio.to_thread(
            mgr.ingest,
            raw_bytes,
            req.filename,
            mode=req.mode,
            collection=req.collection,
            ttl_seconds=req.ttl_seconds,
            target_tokens=req.target_tokens,
            enable_ocr=req.enable_ocr,
        )
    except PermissionError as exc:
        return JSONResponse(status_code=403, content={"ok": False, "error": str(exc)})
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"ok": False, "error": str(exc)})

    duration_ms = round((time.perf_counter() - t0) * 1000.0, 2)
    return JSONResponse(status_code=200, content={"ok": True, **result, "duration_ms": duration_ms})


@router.post("/query")
async def rag_query(req: RagQueryRequest) -> JSONResponse:
    """Retrieve top-k chunks with citations from Ghost or Vault RAG."""
    t0 = time.perf_counter()
    try:
        mgr = get_rag_manager()
        hits = await asyncio.to_thread(
            mgr.query,
            req.query,
            mode=req.mode,
            collection=req.collection,
            top_k=req.top_k,
        )
    except PermissionError as exc:
        return JSONResponse(status_code=403, content={"ok": False, "error": str(exc)})
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"ok": False, "error": str(exc)})

    duration_ms = round((time.perf_counter() - t0) * 1000.0, 2)
    return JSONResponse(
        status_code=200,
        content={
            "ok": True,
            "mode": req.mode,
            "collection": req.collection,
            "query": req.query,
            "count": len(hits),
            "results": [h.to_dict() for h in hits],
            "duration_ms": duration_ms,
        },
    )


@router.post("/clear")
@router.delete("/clear")
async def rag_clear(req: RagClearRequest | None = None) -> JSONResponse:
    """Explicitly wipe Ghost RAM buffers and/or remove Vault RAG collections."""
    body = req or RagClearRequest()
    try:
        mgr = get_rag_manager()
        res = await asyncio.to_thread(mgr.clear, mode=body.mode, collection=body.collection)
    except PermissionError as exc:
        return JSONResponse(status_code=403, content={"ok": False, "error": str(exc)})
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"ok": False, "error": str(exc)})
    return JSONResponse(status_code=200, content={"ok": True, **res})


@router.get("/status")
async def rag_status() -> JSONResponse:
    """Return RAG collection counts without exposing document contents."""
    mgr = get_rag_manager()
    return JSONResponse(status_code=200, content={"ok": True, **mgr.status()})


@chat_ephemeral_router.post("/api/chat/ephemeral-docs")
async def chat_ephemeral_docs_upload(req: EphemeralDocsUploadRequest) -> JSONResponse:
    """Ingest dropped documents into RAM-only `session_docs` Ghost RAG (zero disk writes)."""
    items: list[EphemeralDocItem] = []
    if req.files:
        items.extend(req.files)
    elif req.filename:
        items.append(
            EphemeralDocItem(
                filename=req.filename,
                content=req.content,
                content_base64=req.content_base64,
            )
        )
    else:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": "Provide 'files' or 'filename' + 'content'/'content_base64'."},
        )

    mgr = get_rag_manager()
    ingested: list[dict[str, object]] = []
    for item in items:
        if item.content_base64 is not None:
            try:
                raw_bytes = base64.b64decode(item.content_base64, validate=True)
            except Exception as exc:
                return JSONResponse(status_code=400, content={"ok": False, "error": f"Invalid base64: {exc}"})
        elif item.content is not None:
            raw_bytes = item.content.encode("utf-8")
        else:
            return JSONResponse(
                status_code=400,
                content={"ok": False, "error": f"Missing content for '{item.filename}'."},
            )
        res = await asyncio.to_thread(
            mgr.ingest,
            raw_bytes,
            item.filename,
            mode="ghost",
            collection="session_docs",
            target_tokens=req.target_tokens,
            enable_ocr=False,
        )
        ingested.append(res)

    return JSONResponse(
        status_code=200,
        content={
            "ok": True,
            "mode": "ghost",
            "collection": "session_docs",
            "disk_writes": 0,
            "ingested": ingested,
            "documents": mgr.ghost.list_documents("session_docs"),
        },
    )


@chat_ephemeral_router.get("/api/chat/ephemeral-docs")
async def chat_ephemeral_docs_list() -> JSONResponse:
    """List currently attached RAM-only Drop-to-Chat documents."""
    mgr = get_rag_manager()
    docs = mgr.ghost.list_documents("session_docs")
    return JSONResponse(
        status_code=200,
        content={"ok": True, "mode": "ghost", "collection": "session_docs", "documents": docs, "count": len(docs)},
    )


@chat_ephemeral_router.delete("/api/chat/ephemeral-docs")
async def chat_ephemeral_docs_clear() -> JSONResponse:
    """Wipe all Drop-to-Chat Ghost RAG buffers from RAM."""
    mgr = get_rag_manager()
    res = await asyncio.to_thread(mgr.clear, mode="ghost", collection="session_docs")
    return JSONResponse(status_code=200, content={"ok": True, "wiped_chunks": res["cleared_chunks"]})


@chat_ephemeral_router.post("/api/chat/ephemeral-docs/query")
async def chat_ephemeral_docs_query(req: EphemeralDocsQueryRequest) -> JSONResponse:
    """Retrieve top-k RAM-only chunks and format numbered `[DOC: ...]` context."""
    t0 = time.perf_counter()
    mgr = get_rag_manager()
    hits = await asyncio.to_thread(
        mgr.query,
        req.query,
        mode="ghost",
        collection="session_docs",
        top_k=req.top_k,
    )
    context_lines: list[str] = []
    formatted_results: list[dict[str, object]] = []
    for h in hits:
        if h.line_start is not None:
            loc = (
                f"L.{h.line_start}-{h.line_end}"
                if (h.line_end is not None and h.line_end > h.line_start)
                else f"L.{h.line_start}"
            )
            display_cit = f"[{h.source_file}, {loc}]"
        else:
            loc = f"p.{h.page_start}"
            display_cit = f"[{h.source_file}, {loc}]"
        item = h.to_dict()
        item["citation"] = display_cit
        formatted_results.append(item)
        context_lines.append(f"[DOC: {h.source_file} | {loc}] {display_cit}\n{h.text}")
    duration_ms = round((time.perf_counter() - t0) * 1000.0, 2)
    return JSONResponse(
        status_code=200,
        content={
            "ok": True,
            "mode": "ghost",
            "collection": "session_docs",
            "count": len(hits),
            "results": formatted_results,
            "context_block": "\n\n---\n\n".join(context_lines),
            "duration_ms": duration_ms,
        },
    )
