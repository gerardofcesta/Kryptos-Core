"""
RAM-Only Ephemeral Storage with Rolling Purge, Dead-Man's Switch, and Crypto-Shredding.

Invariants:
- ZERO disk writes for mission, surface_map, hypotheses, evidence_ledger, trace, or final_report.
- All sensitive payloads are held inside mutable `bytearray` buffers wiped in-place with 0x00
  via ctypes.memset (with safe fallback) + gc.collect() upon purge.
- Upon finish, `purge_working_state_on_finish()` destroys all working buffers leaving ONLY
  `_report_buffer` + minimal metadata (`verdict`, `retention_mode`, `run_id`).
- Dead-Man's Switch daemon `threading.Timer` automatically calls `purge_report()` after
  `report_auto_purge_minutes` (default 45, range 1..240).
- `EphemeralEncryptedBlob` provides optional streaming authenticated encryption for extreme
  >512 MB intermediate edge cases with a 32-byte RAM-only key destroyed via `crypto_shred()`.
"""

from __future__ import annotations

import ctypes
import gc
import hashlib
import hmac
import json
import math
import os
import pathlib
import secrets
import struct
import tempfile
import threading
import time
from typing import Any, Callable, Iterable, Iterator

from .base import RunStore

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM as _OPTIONAL_AESGCM
except Exception:
    _OPTIONAL_AESGCM = None


def wipe_bytearray(buf: bytearray | None) -> None:
    """Overwrite every byte of a mutable bytearray in-place with 0x00 and clear it."""
    if not isinstance(buf, bytearray):
        return
    length = len(buf)
    if length > 0:
        try:
            raw_view = (ctypes.c_char * length).from_buffer(buf)
            ctypes.memset(raw_view, 0, length)
            del raw_view
        except Exception:
            buf[:] = bytes(length)
    buf.clear()


def _wipe_nested(obj: Any, _seen: set[int] | None = None) -> None:
    """Recursively wipe mutable bytearray objects inside containers."""
    if obj is None:
        return
    if _seen is None:
        _seen = set()
    obj_id = id(obj)
    if obj_id in _seen:
        return
    _seen.add(obj_id)

    if isinstance(obj, bytearray):
        wipe_bytearray(obj)
    elif isinstance(obj, list):
        for item in obj:
            _wipe_nested(item, _seen)
        obj.clear()
    elif isinstance(obj, tuple):
        for item in obj:
            _wipe_nested(item, _seen)
    elif isinstance(obj, dict):
        for val in list(obj.values()):
            _wipe_nested(val, _seen)
        obj.clear()


def _hkdf_sha256(ikm: bytes, salt: bytes, info: bytes, length: int) -> bytes:
    """Pure-stdlib RFC 5869 HKDF-SHA256."""
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    okm = bytearray()
    t = b""
    counter = 1
    while len(okm) < length:
        t = hmac.new(prk, t + info + bytes([counter]), hashlib.sha256).digest()
        okm.extend(t)
        counter += 1
    return bytes(okm[:length])


def _xor_keystream_sha256(enc_key: bytes, nonce: bytes, chunk_index: int, data: bytes) -> bytes:
    """Pure-stdlib CTR-mode keystream XOR using HMAC-SHA256 PRF blocks."""
    out = bytearray(len(data))
    block_idx = 0
    pos = 0
    total = len(data)
    prefix = nonce + struct.pack(">Q", chunk_index)
    while pos < total:
        ks = hmac.new(enc_key, prefix + struct.pack(">Q", block_idx), hashlib.sha256).digest()
        take = min(32, total - pos)
        for i in range(take):
            out[pos + i] = data[pos + i] ^ ks[i]
        pos += take
        block_idx += 1
    return bytes(out)


def _derive_gcm_chunk_nonce(nonce12: bytes, chunk_index: int) -> bytes:
    """Derive a unique 12-byte GCM nonce per chunk via XOR with 64-bit big-endian counter."""
    base_int = int.from_bytes(nonce12, "big")
    derived = base_int ^ (chunk_index & 0xFFFFFFFFFFFFFFFF)
    return derived.to_bytes(12, "big")


class EphemeralEncryptedBlob:
    """
    Streaming authenticated encrypted blob for extreme >512 MB intermediate edge cases.

    - Key: 32 bytes from `secrets.token_bytes(32)`, stored strictly in RAM in a mutable `bytearray`.
    - Supports chunked streaming AES-256-GCM when `cryptography` is installed, or pure-stdlib
      HKDF-SHA256 + HMAC-SHA256-CTR Encrypt-then-MAC (always available, zero pip deps).
    - Every 64 KiB chunk is independently authenticated with chunk index and final-chunk flag
      to prevent truncation, reordering, or tampering without buffering huge blobs in RAM.
    - `crypto_shred()` zeroes the RAM key in-place and unlinks the encrypted file immediately.
    """

    MAGIC_STDLIB = b"LLEB01"  # Local LLM Ephemeral Blob v1 (pure stdlib)
    MAGIC_AESGCM = b"LLEB02"  # Local LLM Ephemeral Blob v2 (AES-256-GCM)
    CHUNK_SIZE = 64 * 1024

    def __init__(
        self,
        directory: pathlib.Path | str | None = None,
        prefix: str = "ll_ephemeral_",
        force_stdlib: bool = False,
    ) -> None:
        self._lock = threading.RLock()
        self._key = bytearray(secrets.token_bytes(32))
        self._shredded = False
        self._use_aesgcm = (_OPTIONAL_AESGCM is not None) and (not force_stdlib)
        self._dir = pathlib.Path(directory) if directory is not None else pathlib.Path(tempfile.gettempdir())
        self._prefix = prefix
        self._path: pathlib.Path | None = None

    @property
    def cipher_name(self) -> str:
        return "aes-256-gcm" if self._use_aesgcm else "hkdf-hmac-sha256-ctr-etm"

    @property
    def is_shredded(self) -> bool:
        return self._shredded

    @property
    def path(self) -> pathlib.Path | None:
        return self._path

    def _require_key(self) -> bytes:
        if self._shredded or len(self._key) != 32:
            raise RuntimeError("EphemeralEncryptedBlob has been crypto-shredded; key destroyed.")
        return bytes(self._key)

    def write_stream(self, chunks: Iterable[bytes]) -> pathlib.Path:
        """Stream plaintext chunks into an authenticated encrypted file on disk."""
        with self._lock:
            master_key = self._require_key()
            self._dir.mkdir(parents=True, exist_ok=True)
            if self._path is not None and self._path.exists():
                try:
                    self._path.unlink()
                except OSError:
                    pass

            filename = f"{self._prefix}{secrets.token_hex(12)}.eblob"
            target = self._dir / filename
            self._path = target

            if self._use_aesgcm:
                nonce12 = secrets.token_bytes(12)
                header = self.MAGIC_AESGCM + nonce12
                aesgcm = _OPTIONAL_AESGCM(master_key)
                self._write_chunks_buffered(
                    target,
                    header,
                    chunks,
                    encrypt_chunk=lambda idx, is_final, pt: aesgcm.encrypt(
                        _derive_gcm_chunk_nonce(nonce12, idx),
                        pt,
                        header + struct.pack(">QB", idx, 1 if is_final else 0),
                    ),
                )
            else:
                salt = secrets.token_bytes(16)
                nonce16 = secrets.token_bytes(16)
                header = self.MAGIC_STDLIB + salt + nonce16
                derived = _hkdf_sha256(master_key, salt, b"local-llm-ephemeral-blob-v1", 64)
                enc_key = derived[:32]
                mac_key = derived[32:64]

                def _enc_stdlib(idx: int, is_final: bool, pt: bytes) -> bytes:
                    ct = _xor_keystream_sha256(enc_key, nonce16, idx, pt)
                    aad = header + struct.pack(">QBI", idx, 1 if is_final else 0, len(ct))
                    tag = hmac.new(mac_key, aad + ct, hashlib.sha256).digest()
                    return ct + tag

                self._write_chunks_buffered(target, header, chunks, encrypt_chunk=_enc_stdlib)

            return target

    def _write_chunks_buffered(
        self,
        target: pathlib.Path,
        header: bytes,
        chunks: Iterable[bytes],
        encrypt_chunk: Callable[[int, bool, bytes], bytes],
    ) -> None:
        buf = bytearray()
        chunk_index = 0
        with open(target, "wb") as fh:
            fh.write(header)
            for raw in chunks:
                if not raw:
                    continue
                buf.extend(raw)
                while len(buf) > self.CHUNK_SIZE:
                    piece = bytes(buf[: self.CHUNK_SIZE])
                    del buf[: self.CHUNK_SIZE]
                    frame = encrypt_chunk(chunk_index, False, piece)
                    fh.write(struct.pack(">BI", 0, len(frame)))
                    fh.write(frame)
                    chunk_index += 1
            # Always emit a final authenticated frame (even for empty plaintext)
            final_pt = bytes(buf)
            wipe_bytearray(buf)
            frame = encrypt_chunk(chunk_index, True, final_pt)
            fh.write(struct.pack(">BI", 1, len(frame)))
            fh.write(frame)
            fh.flush()

    def write_bytes(self, data: bytes) -> pathlib.Path:
        """Encrypt `data` into the backing ephemeral blob file."""
        return self.write_stream([data])

    def read_stream(self) -> Iterator[bytes]:
        """Verify and yield authenticated plaintext chunks."""
        with self._lock:
            master_key = self._require_key()
            if self._path is None or not self._path.is_file():
                raise FileNotFoundError("Encrypted blob file does not exist.")
            path = self._path

        with open(path, "rb") as fh:
            magic = fh.read(6)
            if magic == self.MAGIC_AESGCM:
                if _OPTIONAL_AESGCM is None:
                    raise RuntimeError("AES-GCM backend unavailable for reading LLEB02 blob.")
                nonce12 = fh.read(12)
                if len(nonce12) != 12:
                    raise ValueError("Truncated AES-GCM blob header.")
                header = magic + nonce12
                aesgcm = _OPTIONAL_AESGCM(master_key)
                chunk_index = 0
                saw_final = False
                while True:
                    prefix = fh.read(5)
                    if not prefix:
                        break
                    if len(prefix) != 5:
                        raise ValueError("Truncated frame header.")
                    is_final_byte, frame_len = struct.unpack(">BI", prefix)
                    frame = fh.read(frame_len)
                    if len(frame) != frame_len:
                        raise ValueError("Truncated encrypted frame.")
                    aad = header + struct.pack(">QB", chunk_index, is_final_byte)
                    try:
                        pt = aesgcm.decrypt(_derive_gcm_chunk_nonce(nonce12, chunk_index), frame, aad)
                    except Exception as exc:
                        raise ValueError("Encrypted blob authentication failed.") from exc
                    if pt:
                        yield pt
                    chunk_index += 1
                    if is_final_byte == 1:
                        saw_final = True
                        if fh.read(1):
                            raise ValueError("Trailing unauthenticated bytes after final frame.")
                        break
                if not saw_final:
                    raise ValueError("Encrypted blob truncated before final authenticated frame.")
            elif magic == self.MAGIC_STDLIB:
                salt = fh.read(16)
                nonce16 = fh.read(16)
                if len(salt) != 16 or len(nonce16) != 16:
                    raise ValueError("Truncated stdlib encrypted blob header.")
                header = magic + salt + nonce16
                derived = _hkdf_sha256(master_key, salt, b"local-llm-ephemeral-blob-v1", 64)
                enc_key = derived[:32]
                mac_key = derived[32:64]
                chunk_index = 0
                saw_final = False
                while True:
                    prefix = fh.read(5)
                    if not prefix:
                        break
                    if len(prefix) != 5:
                        raise ValueError("Truncated frame header.")
                    is_final_byte, frame_len = struct.unpack(">BI", prefix)
                    if frame_len < 32:
                        raise ValueError("Malformed encrypted frame (missing MAC tag).")
                    frame = fh.read(frame_len)
                    if len(frame) != frame_len:
                        raise ValueError("Truncated encrypted frame.")
                    ct = frame[:-32]
                    tag = frame[-32:]
                    aad = header + struct.pack(">QBI", chunk_index, is_final_byte, len(ct))
                    expected_tag = hmac.new(mac_key, aad + ct, hashlib.sha256).digest()
                    if not hmac.compare_digest(tag, expected_tag):
                        raise ValueError("Encrypted blob HMAC verification failed (tampered or wrong key).")
                    pt = _xor_keystream_sha256(enc_key, nonce16, chunk_index, ct)
                    if pt:
                        yield pt
                    chunk_index += 1
                    if is_final_byte == 1:
                        saw_final = True
                        if fh.read(1):
                            raise ValueError("Trailing unauthenticated bytes after final frame.")
                        break
                if not saw_final:
                    raise ValueError("Encrypted blob truncated before final authenticated frame.")
            else:
                raise ValueError(f"Unrecognized encrypted blob magic: {magic!r}")

    def read_bytes(self) -> bytes:
        """Decrypt and return the entire blob payload in memory."""
        return b"".join(self.read_stream())

    def crypto_shred(self) -> None:
        """Zero out the 32-byte key in RAM, unlink the encrypted file, and invoke gc.collect()."""
        with self._lock:
            wipe_bytearray(self._key)
            self._shredded = True
            path = self._path
            self._path = None
        if path is not None:
            try:
                if path.exists():
                    path.unlink()
            except OSError:
                pass
        gc.collect()

    def __enter__(self) -> "EphemeralEncryptedBlob":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.crypto_shred()


class MemoryEphemeralStore(RunStore):
    """
    Strictly RAM-only RunStore implementing Rolling Purge, Working-State Purge on Finish,
    and Dead-Man's Switch automatic report destruction.

    ZERO disk writes ever occur through this store.
    """

    def __init__(
        self,
        run_id: str,
        report_auto_purge_minutes: int = 45,
        on_report_purged: Callable[[], None] | None = None,
        auto_purge_seconds: float | None = None,
    ) -> None:
        self._run_id = str(run_id)
        self._retention_mode = "ephemeral"
        self._lock = threading.RLock()

        clamped_min = max(1, min(240, int(report_auto_purge_minutes)))
        self._report_auto_purge_minutes = clamped_min
        self._auto_purge_timeout_sec = (
            float(auto_purge_seconds)
            if auto_purge_seconds is not None and auto_purge_seconds > 0
            else float(clamped_min * 60)
        )

        self._working_buffers: dict[str, bytearray] = {}
        self._report_buffer: bytearray | None = None
        self._verdict: dict[str, Any] | None = None
        self._report_deadline_monotonic: float | None = None
        self._purge_timer: threading.Timer | None = None
        self._on_report_purged = on_report_purged

        self._purged_steps: int = 0
        self._working_state_purged: bool = False
        self._report_purged: bool = False
        self._encrypted_blobs: list[EphemeralEncryptedBlob] = []

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def retention_mode(self) -> str:
        return self._retention_mode

    @property
    def purged_steps(self) -> int:
        with self._lock:
            return self._purged_steps

    @property
    def working_state_purged(self) -> bool:
        with self._lock:
            return self._working_state_purged

    @property
    def report_purged(self) -> bool:
        with self._lock:
            return self._report_purged

    def register_encrypted_blob(self, blob: EphemeralEncryptedBlob) -> None:
        """Track an opt-in EphemeralEncryptedBlob so step/finish purge crypto-shreds it."""
        with self._lock:
            self._encrypted_blobs.append(blob)

    def _set_working_bytes(self, key: str, raw: bytes) -> None:
        with self._lock:
            old = self._working_buffers.pop(key, None)
            if old is not None:
                wipe_bytearray(old)
            self._working_buffers[key] = bytearray(raw)
            self._working_state_purged = False

    def _get_working_bytes(self, key: str) -> bytes | None:
        with self._lock:
            buf = self._working_buffers.get(key)
            if buf is None:
                return None
            return bytes(buf)

    def save_mission(self, mission: dict[str, Any]) -> None:
        raw = json.dumps(mission, ensure_ascii=False).encode("utf-8")
        self._set_working_bytes("mission", raw)

    def get_mission(self) -> dict[str, Any] | None:
        raw = self._get_working_bytes("mission")
        if raw is None:
            return None
        return json.loads(raw.decode("utf-8"))

    def save_surface_map(self, surface_map: dict[str, Any]) -> None:
        raw = json.dumps(surface_map, ensure_ascii=False).encode("utf-8")
        self._set_working_bytes("surface_map", raw)

    def get_surface_map(self) -> dict[str, Any] | None:
        raw = self._get_working_bytes("surface_map")
        if raw is None:
            return None
        return json.loads(raw.decode("utf-8"))

    def save_hypotheses(self, hypotheses: list[dict[str, Any]]) -> None:
        raw = json.dumps(hypotheses, ensure_ascii=False).encode("utf-8")
        self._set_working_bytes("hypotheses", raw)

    def get_hypotheses(self) -> list[dict[str, Any]]:
        raw = self._get_working_bytes("hypotheses")
        if raw is None:
            return []
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, list) else []

    def save_ledger(self, ledger_md: str) -> None:
        raw = str(ledger_md or "").encode("utf-8")
        self._set_working_bytes("ledger", raw)

    def get_ledger(self) -> str:
        raw = self._get_working_bytes("ledger")
        if raw is None:
            return ""
        return raw.decode("utf-8")

    def record_step_trace(self, entry: dict[str, Any]) -> None:
        # Never accumulates trace history; holds only latest volatile step until rolling_purge_step.
        raw = json.dumps(entry, ensure_ascii=False).encode("utf-8")
        self._set_working_bytes("trace_last", raw)

    def save_checkpoint(self, step_num: int, state: dict[str, Any]) -> None:
        # Ephemeral mode intentionally disables checkpoints / disk persistence.
        return None

    def load_checkpoint(self) -> dict[str, Any] | None:
        return None

    def rolling_purge_step(
        self, step_num: int, raw_buffers: list[Any] | None = None
    ) -> None:
        """Zero out transient raw step outputs and volatile trace buffer, then run gc.collect()."""
        blobs_to_shred: list[EphemeralEncryptedBlob] = []
        with self._lock:
            if raw_buffers is not None:
                _wipe_nested(raw_buffers)
            trace_buf = self._working_buffers.pop("trace_last", None)
            if trace_buf is not None:
                wipe_bytearray(trace_buf)
            if self._encrypted_blobs:
                blobs_to_shred = list(self._encrypted_blobs)
                self._encrypted_blobs.clear()
            self._purged_steps += 1

        for blob in blobs_to_shred:
            try:
                blob.crypto_shred()
            except Exception:
                pass
        gc.collect()

    def _cancel_timer_locked(self) -> None:
        if self._purge_timer is not None:
            try:
                self._purge_timer.cancel()
            except Exception:
                pass
            self._purge_timer = None
        self._report_deadline_monotonic = None

    def _dead_mans_switch_fire(self) -> None:
        self.purge_report()

    def save_final_report(
        self, report_md: str, verdict: dict[str, Any] | None = None
    ) -> None:
        with self._lock:
            self._cancel_timer_locked()
            if self._report_buffer is not None:
                wipe_bytearray(self._report_buffer)
            self._report_buffer = bytearray(str(report_md or "").encode("utf-8"))
            self._verdict = dict(verdict) if isinstance(verdict, dict) else None
            self._report_purged = False

            timeout_sec = self._auto_purge_timeout_sec
            self._report_deadline_monotonic = time.monotonic() + timeout_sec
            timer = threading.Timer(timeout_sec, self._dead_mans_switch_fire)
            timer.daemon = True
            timer.name = f"dead-mans-switch-{self._run_id[:20]}"
            self._purge_timer = timer
            timer.start()

    def get_final_report(self) -> str | None:
        with self._lock:
            if self._report_buffer is None:
                return None
            return bytes(self._report_buffer).decode("utf-8")

    def get_verdict(self) -> dict[str, Any] | None:
        with self._lock:
            return dict(self._verdict) if isinstance(self._verdict, dict) else None

    def purge_working_state_on_finish(self) -> None:
        """
        Immediately wipe and remove mission, surface_map, hypotheses, evidence_ledger,
        and transient trace buffers — leaving ONLY `_report_buffer` + minimal verdict metadata.
        """
        blobs_to_shred: list[EphemeralEncryptedBlob] = []
        with self._lock:
            for key in list(self._working_buffers.keys()):
                buf = self._working_buffers.pop(key, None)
                if buf is not None:
                    wipe_bytearray(buf)
            self._working_buffers.clear()
            if self._encrypted_blobs:
                blobs_to_shred = list(self._encrypted_blobs)
                self._encrypted_blobs.clear()
            self._working_state_purged = True

        for blob in blobs_to_shred:
            try:
                blob.crypto_shred()
            except Exception:
                pass
        gc.collect()

    def purge_report(self) -> None:
        """Cancel Dead-Man's Switch, wipe `_report_buffer` and any residual buffers, run gc.collect()."""
        callback: Callable[[], None] | None = None
        blobs_to_shred: list[EphemeralEncryptedBlob] = []
        with self._lock:
            self._cancel_timer_locked()
            if self._report_buffer is not None:
                wipe_bytearray(self._report_buffer)
                self._report_buffer = None
            for key in list(self._working_buffers.keys()):
                buf = self._working_buffers.pop(key, None)
                if buf is not None:
                    wipe_bytearray(buf)
            self._working_buffers.clear()
            self._verdict = None
            self._working_state_purged = True
            self._report_purged = True
            if self._encrypted_blobs:
                blobs_to_shred = list(self._encrypted_blobs)
                self._encrypted_blobs.clear()
            callback = self._on_report_purged

        for blob in blobs_to_shred:
            try:
                blob.crypto_shred()
            except Exception:
                pass
        gc.collect()

        if callback is not None:
            try:
                callback()
            except Exception:
                pass

    def get_artifacts_dir(self) -> pathlib.Path | None:
        return None

    def get_kv_cache_dir(self) -> pathlib.Path | None:
        return None

    def auto_purge_remaining_sec(self) -> int | None:
        with self._lock:
            if self._report_buffer is None or self._report_deadline_monotonic is None:
                return None
            remaining = self._report_deadline_monotonic - time.monotonic()
            return max(0, int(math.ceil(remaining)))
