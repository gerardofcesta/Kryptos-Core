"""Ghost (RAM-only) & Vault (isolated disk) RAG stores with BM25 + TF-IDF cosine retrieval."""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import pathlib
import re
import secrets
import shutil
import threading
import time
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from ..config import PROJECT_ROOT

DEFAULT_VAULT_RAG_ROOT = PROJECT_ROOT / "workspaces" / "vault" / "rag"
COLLECTION_RE = re.compile(r"^[a-zA-Z0-9._-]{1,64}$")
_TOKEN_RE = re.compile(r"[a-zA-Z0-9_]+")

_SENSITIVE_ROOT_PARTS = frozenset({".ssh", ".gnupg", ".aws", ".kube", ".azure", ".docker"})


def _tokenize(text: str) -> list[str]:
    """Tokenize text into lowercase alphanumeric terms for BM25/TF-IDF scoring."""
    return [tok.lower() for tok in _TOKEN_RE.findall(text or "") if tok]


def _format_citation(
    source_file: str,
    page_start: int,
    page_end: int,
    line_start: int | None = None,
    line_end: int | None = None,
) -> str:
    if page_end > page_start:
        return f"[{source_file} · p.{page_start}-{page_end}]"
    return f"[{source_file} · p.{page_start}]"


def _wipe_bytearray(buf: bytearray) -> None:
    """Explicitly overwrite a mutable bytearray in RAM with zero bytes."""
    if len(buf) > 0:
        buf[:] = b"\x00" * len(buf)


def validate_collection_name(collection: str | None) -> str:
    """Validate RAG collection name against path traversal and unsafe characters."""
    clean = (collection or "default").strip()
    if not clean or clean in (".", "..") or ".." in clean or not COLLECTION_RE.fullmatch(clean):
        raise ValueError(
            "Invalid collection name: must match ^[a-zA-Z0-9._-]{1,64}$ without '..'."
        )
    return clean


def _norm_cmp(path: pathlib.Path) -> str:
    return os.path.normcase(os.path.normpath(str(path.resolve())))


def _is_same_or_child(candidate: pathlib.Path, parent: pathlib.Path) -> bool:
    try:
        c = pathlib.Path(_norm_cmp(candidate))
        p = pathlib.Path(_norm_cmp(parent))
        return c == p or c.is_relative_to(p)
    except Exception:
        return False


def validate_vault_root(vault_root: pathlib.Path | str | None = None) -> pathlib.Path:
    """
    Resolve and validate the Vault RAG root directory:
    - Rejects UNC paths, traversal, drive roots, and sensitive credential directories.
    - Respects `ORCHESTRATOR_ALLOWED_ROOTS` when configured.
    """
    if vault_root is None:
        env_dir = os.environ.get("LOCAL_LLM_RAG_VAULT_DIR", "").strip()
        candidate = pathlib.Path(env_dir).expanduser() if env_dir else DEFAULT_VAULT_RAG_ROOT
    else:
        raw = str(vault_root).strip()
        if raw.startswith("\\\\") or raw.startswith("//"):
            raise PermissionError("UNC/network paths are forbidden for Vault RAG.")
        candidate = pathlib.Path(raw).expanduser()

    resolved = candidate.resolve()
    if str(resolved).startswith("\\\\") or str(resolved).startswith("//"):
        raise PermissionError("UNC/network paths are forbidden for Vault RAG.")
    if resolved == resolved.parent:
        raise PermissionError("Drive or filesystem root cannot be used as Vault RAG root.")
    if {p.lower() for p in resolved.parts} & _SENSITIVE_ROOT_PARTS:
        raise PermissionError("Sensitive directory cannot be used as Vault RAG root.")

    raw_allowed = os.environ.get("ORCHESTRATOR_ALLOWED_ROOTS", "").strip()
    if raw_allowed:
        allowed_roots = [
            pathlib.Path(item.strip()).expanduser().resolve()
            for item in raw_allowed.split(os.pathsep)
            if item.strip()
        ]
        if allowed_roots and not any(_is_same_or_child(resolved, r) for r in allowed_roots):
            raise PermissionError(
                f"Vault RAG root '{resolved}' is outside ORCHESTRATOR_ALLOWED_ROOTS."
            )

    return resolved


@dataclass
class RagChunkRecord:
    """Indexed chunk representation (text stored in mutable bytearray in Ghost mode)."""

    chunk_id: str
    doc_id: str
    source_file: str
    page_start: int
    page_end: int
    heading: str | None
    token_estimate: int
    line_start: int | None = None
    line_end: int | None = None
    tf: Counter[str] = field(default_factory=Counter)
    length: int = 0
    _text_buf: bytearray | None = field(default=None, repr=False)
    _text_str: str | None = field(default=None, repr=False)

    @property
    def text(self) -> str:
        if self._text_buf is not None:
            return bytes(self._text_buf).decode("utf-8", errors="replace")
        return self._text_str or ""

    def wipe(self) -> None:
        if self._text_buf is not None:
            _wipe_bytearray(self._text_buf)
            self._text_buf = None
        self._text_str = None
        self.tf.clear()
        self.length = 0

    def to_persisted_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "doc_id": self.doc_id,
            "source_file": self.source_file,
            "page_start": self.page_start,
            "page_end": self.page_end,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "heading": self.heading,
            "token_estimate": self.token_estimate,
            "text": self.text,
        }

    @classmethod
    def from_persisted_dict(cls, data: dict[str, Any]) -> RagChunkRecord:
        text = str(data.get("text") or "")
        tokens = _tokenize(text)
        return cls(
            chunk_id=str(data["chunk_id"]),
            doc_id=str(data["doc_id"]),
            source_file=str(data["source_file"]),
            page_start=int(data.get("page_start", 1)),
            page_end=int(data.get("page_end", 1)),
            line_start=int(data["line_start"]) if data.get("line_start") is not None else None,
            line_end=int(data["line_end"]) if data.get("line_end") is not None else None,
            heading=data.get("heading"),
            token_estimate=int(data.get("token_estimate", len(tokens))),
            tf=Counter(tokens),
            length=len(tokens),
            _text_str=text,
        )


@dataclass(frozen=True)
class RagQueryHit:
    """Single ranked retrieval hit with page/file citation."""

    chunk_id: str
    doc_id: str
    source_file: str
    page_start: int
    page_end: int
    heading: str | None
    citation: str
    score: float
    text: str
    line_start: int | None = None
    line_end: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "doc_id": self.doc_id,
            "source_file": self.source_file,
            "filename": self.source_file,
            "page": self.page_start,
            "page_start": self.page_start,
            "page_end": self.page_end,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "heading": self.heading,
            "citation": self.citation,
            "score": round(self.score, 6),
            "text": self.text,
        }


def _score_chunks_bm25_cosine(
    chunks: list[RagChunkRecord],
    query: str,
    top_k: int = 5,
) -> list[RagQueryHit]:
    """Compute hybrid BM25 + TF-IDF cosine similarity over local chunks."""
    if not chunks:
        return []
    q_tokens = _tokenize(query)
    if not q_tokens:
        return []

    n_docs = len(chunks)
    avgdl = sum(max(1, c.length) for c in chunks) / max(1, n_docs)

    df: Counter[str] = Counter()
    for ch in chunks:
        for term in ch.tf:
            df[term] += 1

    q_tf = Counter(q_tokens)
    # Query TF-IDF vector
    q_weights: dict[str, float] = {}
    for term, freq in q_tf.items():
        idf = math.log((n_docs + 1.0) / (df.get(term, 0) + 1.0)) + 1.0
        q_weights[term] = (1.0 + math.log(freq)) * idf
    q_norm = math.sqrt(sum(w * w for w in q_weights.values())) or 1.0

    k1 = 1.5
    b = 0.75
    raw_results: list[tuple[float, float, RagChunkRecord]] = []

    for ch in chunks:
        if not ch.tf:
            continue
        dl = max(1, ch.length)
        bm25_score = 0.0
        dot = 0.0

        # Document norm for TF-IDF cosine
        doc_sq = 0.0
        for term, d_freq in ch.tf.items():
            idf_cos = math.log((n_docs + 1.0) / (df.get(term, 0) + 1.0)) + 1.0
            w = (1.0 + math.log(d_freq)) * idf_cos
            doc_sq += w * w
            if term in q_weights:
                dot += w * q_weights[term]

        for term, q_freq in q_tf.items():
            d_freq = ch.tf.get(term, 0)
            if d_freq <= 0:
                continue
            idf_bm25 = math.log(1.0 + (n_docs - df.get(term, 0) + 0.5) / (df.get(term, 0) + 0.5))
            denom = d_freq + k1 * (1.0 - b + b * (dl / avgdl))
            bm25_score += idf_bm25 * ((d_freq * (k1 + 1.0)) / denom) * (1.0 + 0.1 * (q_freq - 1))

        doc_norm = math.sqrt(doc_sq) or 1.0
        cosine_sim = dot / (doc_norm * q_norm)

        if bm25_score > 0.0 or cosine_sim > 0.0:
            raw_results.append((bm25_score, cosine_sim, ch))

    if not raw_results:
        return []

    max_bm25 = max(item[0] for item in raw_results) or 1.0
    scored: list[tuple[float, RagChunkRecord]] = []
    for bm25_val, cos_val, ch in raw_results:
        hybrid = 0.65 * (bm25_val / max_bm25) + 0.35 * cos_val
        scored.append((hybrid, ch))

    scored.sort(key=lambda item: item[0], reverse=True)
    hits: list[RagQueryHit] = []
    for score, ch in scored[: max(1, top_k)]:
        hits.append(
            RagQueryHit(
                chunk_id=ch.chunk_id,
                doc_id=ch.doc_id,
                source_file=ch.source_file,
                page_start=ch.page_start,
                page_end=ch.page_end,
                line_start=ch.line_start,
                line_end=ch.line_end,
                heading=ch.heading,
                citation=_format_citation(
                    ch.source_file,
                    ch.page_start,
                    ch.page_end,
                    ch.line_start,
                    ch.line_end,
                ),
                score=score,
                text=ch.text,
            )
        )
    return hits


@dataclass(frozen=True)
class _IngestChunk:
    chunk_id: str
    doc_id: str
    source_file: str
    page_start: int
    page_end: int
    heading: str | None
    token_estimate: int
    text: str
    line_start: int | None = None
    line_end: int | None = None


@dataclass(frozen=True)
class _IngestDocumentMeta:
    doc_id: str
    source_file: str
    page_count: int
    method: str
    language: str
    sha256: str


@dataclass(frozen=True)
class _IngestManifest:
    schema_version: str
    documents: list[_IngestDocumentMeta]
    chunks: list[_IngestChunk]


def _extract_docx_pages(content: bytes) -> list[tuple[int, str]]:
    """Extract paragraph text from a `.docx` ZIP archive using `defusedxml`."""
    import defusedxml.ElementTree as ET

    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        if "word/document.xml" not in zf.namelist():
            raise ValueError("Invalid DOCX archive: missing word/document.xml")
        xml_bytes = zf.read("word/document.xml")

    root = ET.fromstring(xml_bytes)
    paragraphs: list[str] = []
    for elem in root.iter():
        tag = elem.tag.split("}")[-1]
        if tag == "p":
            parts = [
                node.text
                for node in elem.iter()
                if node.tag.split("}")[-1] == "t" and node.text
            ]
            line = "".join(parts).strip()
            if line:
                paragraphs.append(line)
    return [(1, "\n\n".join(paragraphs))]


def _extract_pdf_pages_fallback(content: bytes) -> tuple[list[tuple[int, str]], str]:
    """Minimal pure-Python fallback PDF text extractor when ``pymupdf`` is unavailable."""
    raw_latin = content.decode("latin-1", errors="ignore")
    streams = re.findall(r"stream\r?\n(.*?)\r?\nendstream", raw_latin, re.DOTALL)
    pages: list[tuple[int, str]] = []
    for idx, stream_body in enumerate(streams or [raw_latin], start=1):
        tj_parts = re.findall(r"\(([^()\\]*(?:\\.[^()\\]*)*)\)\s*Tj", stream_body)
        if not tj_parts:
            tj_parts = re.findall(r"\(([^()\\]{3,})\)", stream_body)
        cleaned = " ".join(p.replace("\\(", "(").replace("\\)", ")").strip() for p in tj_parts).strip()
        if cleaned:
            pages.append((idx, cleaned))
    if not pages:
        readable = " ".join(re.findall(r"[A-Za-z0-9.,;:!?()'\"/\-\s]{16,}", raw_latin)).strip()
        pages = [(1, readable)] if readable else [(1, "")]
    return pages, "pdf_pure_fallback"


def _extract_pdf_pages(content: bytes) -> tuple[list[tuple[int, str]], str]:
    """Extract PDF text page-by-page using optional `pymupdf` with pure-Python fallback."""
    try:
        import pymupdf  # type: ignore[import-untyped]
    except ImportError:
        return _extract_pdf_pages_fallback(content)

    pages: list[tuple[int, str]] = []
    doc = pymupdf.open(stream=content, filetype="pdf")
    try:
        for idx, page in enumerate(doc, start=1):
            text = (page.get_text("text") or "").strip()
            pages.append((idx, text))
    finally:
        doc.close()
    return pages, "pymupdf"


def _chunk_line_tracked_text(
    decoded: str,
    *,
    doc_id: str,
    source_file: str,
    target_tokens: int,
    overlap_tokens: int,
) -> list[_IngestChunk]:
    """Split text/code into chunks while tracking 1-based line_start..line_end."""
    lines = decoded.splitlines()
    if not lines:
        return []
    chunks: list[_IngestChunk] = []
    chunk_idx = 0
    i = 0
    while i < len(lines):
        cur_lines: list[str] = []
        word_count = 0
        start_ln = i + 1
        j = i
        heading: str | None = None
        while j < len(lines):
            ln = lines[j]
            if heading is None and ln.strip().startswith("#"):
                heading = ln.strip().lstrip("#").strip()[:120] or None
            ln_words = len(ln.split())
            if cur_lines and (word_count + ln_words > target_tokens):
                break
            cur_lines.append(ln)
            word_count += max(1, ln_words) if ln.strip() else 0
            j += 1
        end_ln = max(start_ln, j)
        chunk_text = "\n".join(cur_lines).strip()
        if chunk_text:
            chunk_idx += 1
            chunks.append(
                _IngestChunk(
                    chunk_id=f"{doc_id}-c{chunk_idx:04d}",
                    doc_id=doc_id,
                    source_file=source_file,
                    page_start=1,
                    page_end=1,
                    line_start=start_ln,
                    line_end=end_ln,
                    heading=heading,
                    token_estimate=max(1, len(chunk_text.split())),
                    text=chunk_text,
                )
            )
        if j >= len(lines):
            break
        # Step back slightly for line overlap
        overlap_lines = 1 if (j - i) > 2 and overlap_tokens > 0 else 0
        i = max(i + 1, j - overlap_lines)
    return chunks


def _chunk_extracted_pages(
    pages: list[tuple[int, str]],
    *,
    doc_id: str,
    source_file: str,
    target_tokens: int,
    overlap_tokens: int,
) -> list[_IngestChunk]:
    """Split extracted `(page_num, text)` pairs into token-bounded chunks."""
    chunks: list[_IngestChunk] = []
    chunk_idx = 0
    for page_num, page_text in pages:
        cleaned = (page_text or "").strip()
        if not cleaned:
            continue
        heading: str | None = None
        for line in cleaned.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                heading = stripped.lstrip("#").strip()[:120] or None
                break

        words = cleaned.split()
        if not words:
            continue
        step = max(1, target_tokens - overlap_tokens)
        pos = 0
        while pos < len(words):
            slice_words = words[pos : pos + target_tokens]
            chunk_text = " ".join(slice_words)
            chunk_idx += 1
            chunks.append(
                _IngestChunk(
                    chunk_id=f"{doc_id}-c{chunk_idx:04d}",
                    doc_id=doc_id,
                    source_file=source_file,
                    page_start=page_num,
                    page_end=page_num,
                    heading=heading,
                    token_estimate=len(slice_words),
                    text=chunk_text,
                )
            )
            if pos + target_tokens >= len(words):
                break
            pos += step
    return chunks


def _ingest_document_bytes(
    content: bytes,
    filename: str,
    target_tokens: int = 350,
    enable_ocr: bool = True,
) -> _IngestManifest:
    """Self-contained in-memory document parser and chunker (no disk writes)."""
    safe_name = pathlib.Path(filename or "document.txt").name
    ext = pathlib.Path(safe_name).suffix.lower()
    t_tokens = max(50, min(2000, int(target_tokens)))
    overlap = min(60, max(10, t_tokens // 6))
    sha256_hex = hashlib.sha256(content).hexdigest()
    doc_id = f"doc-{sha256_hex[:12]}"

    if ext == ".pdf":
        try:
            pages, method = _extract_pdf_pages(content)
        except RuntimeError:
            # Optional fallback if an external PDF parser module is present
            try:
                from doc_ingest import IngestOptions, ingest_bytes  # type: ignore[import-not-found]

                opts = IngestOptions(
                    enable_ocr=enable_ocr,
                    enable_chunking=True,
                    target_tokens=t_tokens,
                    chunk_overlap_tokens=overlap,
                    write_manifest_json=False,
                )
                ext_manifest = ingest_bytes(content=content, filename=safe_name, options=opts, output_dir=None)
                return _IngestManifest(
                    schema_version=str(getattr(ext_manifest, "schema_version", "local_rag_ingest.v1")),
                    documents=[
                        _IngestDocumentMeta(
                            doc_id=d.doc_id,
                            source_file=d.source_file,
                            page_count=d.page_count,
                            method=d.method,
                            language=d.language,
                            sha256=d.sha256,
                        )
                        for d in ext_manifest.documents
                    ],
                    chunks=[
                        _IngestChunk(
                            chunk_id=c.chunk_id,
                            doc_id=c.doc_id,
                            source_file=c.source_file,
                            page_start=c.page_start,
                            page_end=c.page_end,
                            heading=c.heading,
                            token_estimate=c.token_estimate,
                            text=c.text,
                        )
                        for c in ext_manifest.chunks
                    ],
                )
            except ImportError as exc:
                raise RuntimeError(
                    "PDF ingestion requires optional 'pymupdf' package (install with: pip install pymupdf)."
                ) from exc
    elif ext == ".docx":
        pages = _extract_docx_pages(content)
        method = "docx_xml"
        chunks = _chunk_extracted_pages(
            pages,
            doc_id=doc_id,
            source_file=safe_name,
            target_tokens=t_tokens,
            overlap_tokens=overlap,
        )
    else:
        decoded = content.decode("utf-8", errors="replace")
        pages = [(1, decoded)]
        method = f"text_{ext.lstrip('.') or 'plain'}"
        chunks = _chunk_line_tracked_text(
            decoded,
            doc_id=doc_id,
            source_file=safe_name,
            target_tokens=t_tokens,
            overlap_tokens=overlap,
        )

    if ext == ".pdf":
        chunks = _chunk_extracted_pages(
            pages,
            doc_id=doc_id,
            source_file=safe_name,
            target_tokens=t_tokens,
            overlap_tokens=overlap,
        )
    meta = _IngestDocumentMeta(
        doc_id=doc_id,
        source_file=safe_name,
        page_count=max(1, len(pages)),
        method=method,
        language="auto",
        sha256=sha256_hex,
    )
    return _IngestManifest(
        schema_version="local_rag_ingest.v1",
        documents=[meta],
        chunks=chunks,
    )


class GhostRagStore:
    """
    100% RAM-only RAG store with mutable bytearray buffers, TTL expiry, and explicit wipe.
    NEVER touches the filesystem.
    """

    def __init__(self, default_ttl_seconds: float = 2700.0) -> None:
        self._lock = threading.RLock()
        self._default_ttl_seconds = max(1.0, float(default_ttl_seconds))
        # collection -> list[RagChunkRecord]
        self._collections: dict[str, list[RagChunkRecord]] = {}
        # collection -> list[dict] (metadata only, no raw file bytes)
        self._docs: dict[str, list[dict[str, Any]]] = {}
        self._expires_at: dict[str, float] = {}

    def _purge_expired_locked(self, now: float | None = None) -> None:
        current = time.monotonic() if now is None else now
        expired = [col for col, exp in self._expires_at.items() if current >= exp]
        for col in expired:
            self._wipe_collection_locked(col)

    def _wipe_collection_locked(self, collection: str) -> int:
        chunks = self._collections.pop(collection, [])
        wiped = len(chunks)
        for ch in chunks:
            ch.wipe()
        self._docs.pop(collection, None)
        self._expires_at.pop(collection, None)
        return wiped

    def ingest(
        self,
        content: bytes,
        filename: str,
        *,
        collection: str = "default",
        ttl_seconds: float | None = None,
        target_tokens: int = 350,
        enable_ocr: bool = True,
    ) -> dict[str, Any]:
        col = validate_collection_name(collection)
        manifest = _ingest_document_bytes(
            content=content,
            filename=filename,
            target_tokens=target_tokens,
            enable_ocr=enable_ocr,
        )
        ttl = (
            max(0.05, float(ttl_seconds))
            if ttl_seconds is not None
            else self._default_ttl_seconds
        )
        records: list[RagChunkRecord] = []
        for sc in manifest.chunks:
            tokens = _tokenize(sc.text)
            buf = bytearray(sc.text.encode("utf-8", errors="replace"))
            records.append(
                RagChunkRecord(
                    chunk_id=sc.chunk_id,
                    doc_id=sc.doc_id,
                    source_file=sc.source_file,
                    page_start=sc.page_start,
                    page_end=sc.page_end,
                    line_start=sc.line_start,
                    line_end=sc.line_end,
                    heading=sc.heading,
                    token_estimate=sc.token_estimate,
                    tf=Counter(tokens),
                    length=len(tokens),
                    _text_buf=buf,
                )
            )

        doc_meta = manifest.documents[0] if manifest.documents else None
        with self._lock:
            self._purge_expired_locked()
            self._collections.setdefault(col, []).extend(records)
            if doc_meta is not None:
                self._docs.setdefault(col, []).append(
                    {
                        "doc_id": doc_meta.doc_id,
                        "source_file": doc_meta.source_file,
                        "page_count": doc_meta.page_count,
                        "method": doc_meta.method,
                        "language": doc_meta.language,
                        "sha256": doc_meta.sha256,
                    }
                )
            self._expires_at[col] = time.monotonic() + ttl

        return {
            "mode": "ghost",
            "collection": col,
            "doc_id": doc_meta.doc_id if doc_meta else "",
            "filename": doc_meta.source_file if doc_meta else filename,
            "page_count": doc_meta.page_count if doc_meta else 0,
            "chunk_count": len(records),
            "manifest_schema": manifest.schema_version,
            "ttl_seconds": ttl,
            "disk_writes": 0,
        }

    def query(
        self,
        query: str,
        *,
        collection: str = "default",
        top_k: int = 5,
    ) -> list[RagQueryHit]:
        col = validate_collection_name(collection)
        with self._lock:
            self._purge_expired_locked()
            chunks = list(self._collections.get(col, []))
        return _score_chunks_bm25_cosine(chunks, query, top_k=top_k)

    def clear(self, collection: str | None = None) -> int:
        with self._lock:
            if collection is not None:
                col = validate_collection_name(collection)
                return self._wipe_collection_locked(col)
            total = 0
            for col in list(self._collections.keys()):
                total += self._wipe_collection_locked(col)
            return total

    def list_documents(self, collection: str = "default") -> list[dict[str, Any]]:
        col = validate_collection_name(collection)
        with self._lock:
            self._purge_expired_locked()
            return [dict(d) for d in self._docs.get(col, [])]

    def status(self) -> dict[str, Any]:
        with self._lock:
            self._purge_expired_locked()
            now = time.monotonic()
            cols = {
                col: {
                    "documents": len(self._docs.get(col, [])),
                    "chunks": len(chunks),
                    "ttl_remaining_sec": round(max(0.0, self._expires_at.get(col, now) - now), 1),
                }
                for col, chunks in self._collections.items()
            }
            return {"collections": cols, "total_chunks": sum(len(c) for c in self._collections.values())}


class VaultRagStore:
    """
    Persistent Vault RAG store strictly isolated under `workspaces/vault/rag/<collection>/`
    (or injected `vault_root` for tests), using atomic writes and path containment.
    """

    def __init__(self, vault_root: pathlib.Path | str | None = None) -> None:
        self._lock = threading.RLock()
        self._explicit_root = vault_root

    def _resolve_root(self) -> pathlib.Path:
        return validate_vault_root(self._explicit_root)

    def _collection_dir(self, collection: str) -> pathlib.Path:
        col = validate_collection_name(collection)
        root = self._resolve_root()
        target = (root / col).resolve()
        if target.parent != root or target == root:
            raise PermissionError(f"Vault RAG collection '{col}' escapes root '{root}'.")
        return target

    @staticmethod
    def _atomic_write_json(target: pathlib.Path, payload: Any) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.parent / f".{target.name}.{secrets.token_hex(4)}.tmp"
        raw = json.dumps(payload, ensure_ascii=False, indent=2)
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(raw)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, target)
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass

    def _load_collection_chunks(self, col_dir: pathlib.Path) -> list[RagChunkRecord]:
        chunks_file = col_dir / "chunks.json"
        if not chunks_file.is_file():
            return []
        try:
            raw = json.loads(chunks_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        if not isinstance(raw, list):
            return []
        return [RagChunkRecord.from_persisted_dict(item) for item in raw if isinstance(item, dict)]

    def _load_collection_docs(self, col_dir: pathlib.Path) -> list[dict[str, Any]]:
        docs_file = col_dir / "documents.json"
        if not docs_file.is_file():
            return []
        try:
            raw = json.loads(docs_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return [d for d in raw if isinstance(d, dict)] if isinstance(raw, list) else []

    def ingest(
        self,
        content: bytes,
        filename: str,
        *,
        collection: str = "default",
        target_tokens: int = 350,
        enable_ocr: bool = True,
    ) -> dict[str, Any]:
        col = validate_collection_name(collection)
        manifest = _ingest_document_bytes(
            content=content,
            filename=filename,
            target_tokens=target_tokens,
            enable_ocr=enable_ocr,
        )
        doc_meta = manifest.documents[0] if manifest.documents else None
        new_records: list[RagChunkRecord] = []
        for sc in manifest.chunks:
            tokens = _tokenize(sc.text)
            new_records.append(
                RagChunkRecord(
                    chunk_id=sc.chunk_id,
                    doc_id=sc.doc_id,
                    source_file=sc.source_file,
                    page_start=sc.page_start,
                    page_end=sc.page_end,
                    line_start=sc.line_start,
                    line_end=sc.line_end,
                    heading=sc.heading,
                    token_estimate=sc.token_estimate,
                    tf=Counter(tokens),
                    length=len(tokens),
                    _text_str=sc.text,
                )
            )

        with self._lock:
            col_dir = self._collection_dir(col)
            existing_chunks = self._load_collection_chunks(col_dir)
            existing_docs = self._load_collection_docs(col_dir)

            existing_chunks.extend(new_records)
            if doc_meta is not None:
                existing_docs.append(
                    {
                        "doc_id": doc_meta.doc_id,
                        "source_file": doc_meta.source_file,
                        "page_count": doc_meta.page_count,
                        "method": doc_meta.method,
                        "language": doc_meta.language,
                        "sha256": doc_meta.sha256,
                    }
                )
            self._atomic_write_json(
                col_dir / "chunks.json", [ch.to_persisted_dict() for ch in existing_chunks]
            )
            self._atomic_write_json(col_dir / "documents.json", existing_docs)

        return {
            "mode": "vault",
            "collection": col,
            "doc_id": doc_meta.doc_id if doc_meta else "",
            "filename": doc_meta.source_file if doc_meta else filename,
            "page_count": doc_meta.page_count if doc_meta else 0,
            "chunk_count": len(new_records),
            "manifest_schema": manifest.schema_version,
            "vault_collection_dir": str(col_dir),
        }

    def query(
        self,
        query: str,
        *,
        collection: str = "default",
        top_k: int = 5,
    ) -> list[RagQueryHit]:
        col_dir = self._collection_dir(collection)
        with self._lock:
            chunks = self._load_collection_chunks(col_dir)
        return _score_chunks_bm25_cosine(chunks, query, top_k=top_k)

    def clear(self, collection: str | None = None) -> int:
        with self._lock:
            root = self._resolve_root()
            if collection is not None:
                col_dir = self._collection_dir(collection)
                if not col_dir.exists():
                    return 0
                chunks = self._load_collection_chunks(col_dir)
                shutil.rmtree(col_dir, ignore_errors=False)
                return len(chunks)
            if not root.exists():
                return 0
            total = 0
            for child in list(root.iterdir()):
                if child.is_dir():
                    total += len(self._load_collection_chunks(child))
                    shutil.rmtree(child, ignore_errors=False)
            return total

    def status(self) -> dict[str, Any]:
        with self._lock:
            root = self._resolve_root()
            if not root.exists():
                return {"collections": {}, "total_chunks": 0}
            cols: dict[str, Any] = {}
            total = 0
            for child in sorted(root.iterdir()):
                if child.is_dir():
                    chunks = self._load_collection_chunks(child)
                    docs = self._load_collection_docs(child)
                    cols[child.name] = {"documents": len(docs), "chunks": len(chunks)}
                    total += len(chunks)
            return {"collections": cols, "total_chunks": total}


class RagStoreManager:
    """Unified facade isolating Ghost (RAM-only) and Vault (disk) RAG modes."""

    def __init__(
        self,
        vault_root: pathlib.Path | str | None = None,
        default_ttl_seconds: float = 2700.0,
    ) -> None:
        self.ghost = GhostRagStore(default_ttl_seconds=default_ttl_seconds)
        self.vault = VaultRagStore(vault_root=vault_root)

    def ingest(
        self,
        content: bytes | str,
        filename: str,
        *,
        mode: str = "ghost",
        collection: str = "default",
        ttl_seconds: float | None = None,
        target_tokens: int = 350,
        enable_ocr: bool = True,
    ) -> dict[str, Any]:
        raw_bytes = content.encode("utf-8") if isinstance(content, str) else content
        norm_mode = (mode or "ghost").strip().lower()
        if norm_mode == "ghost":
            return self.ghost.ingest(
                raw_bytes,
                filename,
                collection=collection,
                ttl_seconds=ttl_seconds,
                target_tokens=target_tokens,
                enable_ocr=enable_ocr,
            )
        if norm_mode == "vault":
            return self.vault.ingest(
                raw_bytes,
                filename,
                collection=collection,
                target_tokens=target_tokens,
                enable_ocr=enable_ocr,
            )
        raise ValueError(f"Unsupported RAG mode '{mode}': expected 'ghost' or 'vault'.")

    def query(
        self,
        query: str,
        *,
        mode: str = "ghost",
        collection: str = "default",
        top_k: int = 5,
    ) -> list[RagQueryHit]:
        norm_mode = (mode or "ghost").strip().lower()
        if norm_mode == "ghost":
            return self.ghost.query(query, collection=collection, top_k=top_k)
        if norm_mode == "vault":
            return self.vault.query(query, collection=collection, top_k=top_k)
        raise ValueError(f"Unsupported RAG mode '{mode}': expected 'ghost' or 'vault'.")

    def clear(self, *, mode: str = "ghost", collection: str | None = None) -> dict[str, Any]:
        norm_mode = (mode or "ghost").strip().lower()
        if norm_mode == "ghost":
            wiped = self.ghost.clear(collection=collection)
            return {"mode": "ghost", "cleared_chunks": wiped}
        if norm_mode == "vault":
            wiped = self.vault.clear(collection=collection)
            return {"mode": "vault", "cleared_chunks": wiped}
        if norm_mode == "all":
            g = self.ghost.clear(collection=collection)
            v = self.vault.clear(collection=collection)
            return {"mode": "all", "cleared_chunks": g + v}
        raise ValueError(f"Unsupported RAG mode '{mode}': expected 'ghost', 'vault', or 'all'.")

    def status(self) -> dict[str, Any]:
        return {
            "ghost": self.ghost.status(),
            "vault": self.vault.status(),
        }


_DEFAULT_RAG_MANAGER: RagStoreManager | None = None
_RAG_MGR_LOCK = threading.Lock()


def get_rag_manager() -> RagStoreManager:
    """Return the process-wide singleton `RagStoreManager`."""
    global _DEFAULT_RAG_MANAGER
    with _RAG_MGR_LOCK:
        if _DEFAULT_RAG_MANAGER is None:
            _DEFAULT_RAG_MANAGER = RagStoreManager()
        return _DEFAULT_RAG_MANAGER
