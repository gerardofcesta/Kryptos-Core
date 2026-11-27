"""Cross-platform single-instance lock (Win32 CreateMutexW + POSIX fcntl.flock)."""

from __future__ import annotations

import errno
import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from .paths import resolve_app_paths

ERROR_ALREADY_EXISTS = 183

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


class SingleInstanceError(RuntimeError):
    """Raised when another instance already holds the single-instance lock."""


def mutex_name_for_app(app_name: str) -> str:
    """Build the Windows single-instance mutex name ``Local\\<AppName>_SingleInstance_v1``."""
    cleaned = _SAFE_NAME_RE.sub("_", app_name.strip()).strip("_")
    if not cleaned:
        raise ValueError(f"Cannot build mutex name from {app_name!r}")
    return f"Local\\{cleaned}_SingleInstance_v1"


def _load_kernel32() -> Any:
    """Load kernel32 with typed Win32 mutex prototypes (Windows only)."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.ReleaseMutex.argtypes = [wintypes.HANDLE]
    kernel32.ReleaseMutex.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32


def _get_fcntl() -> Any:
    """Lazy-import ``fcntl`` on POSIX platforms (overridable in tests)."""
    import fcntl

    return fcntl


class CrossPlatformInstanceLock:
    """Cross-process single-instance lock for Windows, Linux, and macOS.

    - Windows (``sys.platform == "win32"``): uses ``CreateMutexW``.
    - POSIX (``linux``, ``darwin``): uses non-blocking ``fcntl.flock(LOCK_EX | LOCK_NB)``
      on ``<lock_dir>/localllm.lock`` created with ``0o600`` permissions.
    """

    def __init__(
        self,
        app_name: str = "LocalLLM",
        *,
        lock_dir: Path | str | None = None,
        mutex_name: str | None = None,
    ) -> None:
        self.app_name = app_name
        self.name = mutex_name or mutex_name_for_app(app_name)
        self.lock_dir = Path(lock_dir).expanduser() if lock_dir is not None else None
        self._handle: int | None = None
        self._lock_fd: int | None = None
        self._lock_path: Path | None = None
        self._acquired: bool = False
        self._kernel32 = _load_kernel32() if sys.platform == "win32" else None

    @property
    def is_acquired(self) -> bool:
        """Return ``True`` if this instance currently holds the lock."""
        return self._acquired

    @property
    def lock_path(self) -> Path | None:
        """Return the POSIX lock file path when acquired on POSIX."""
        return self._lock_path

    def _resolve_posix_lock_file(self) -> Path:
        if self.lock_dir is not None:
            base_dir = self.lock_dir
        else:
            paths = resolve_app_paths(self.app_name)
            base_dir = paths.runtime_dir if paths.runtime_dir is not None else paths.data_dir
        base_dir.mkdir(parents=True, exist_ok=True)
        return base_dir / "localllm.lock"

    def _acquire_posix(self) -> bool:
        fcntl_mod = _get_fcntl()
        lock_file = self._resolve_posix_lock_file()
        fd = os.open(str(lock_file), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                os.chmod(lock_file, 0o600)
            except OSError:
                pass

            try:
                fcntl_mod.flock(fd, fcntl_mod.LOCK_EX | fcntl_mod.LOCK_NB)
            except OSError as exc:
                if exc.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK) or isinstance(
                    exc, BlockingIOError
                ):
                    os.close(fd)
                    self._lock_fd = None
                    self._acquired = False
                    return False
                os.close(fd)
                raise

            payload = json.dumps(
                {
                    "app": self.app_name,
                    "pid": os.getpid(),
                    "started_at": datetime.now(UTC).isoformat(),
                }
            ).encode("utf-8")
            try:
                os.ftruncate(fd, 0)
                os.lseek(fd, 0, os.SEEK_SET)
                os.write(fd, payload)
            except OSError:
                pass

            self._lock_fd = fd
            self._lock_path = lock_file
            self._acquired = True
            return True
        except Exception:
            if self._lock_fd is None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            raise

    def acquire(self) -> bool:
        """Attempt to acquire the single-instance lock."""
        if self._acquired:
            return True

        if sys.platform != "win32":
            return self._acquire_posix()

        import ctypes

        if self._kernel32 is None:
            self._kernel32 = _load_kernel32()

        handle = self._kernel32.CreateMutexW(None, True, self.name)
        last_error = ctypes.get_last_error()
        if not handle:
            raise OSError(last_error, f"CreateMutexW failed for {self.name}")

        if last_error == ERROR_ALREADY_EXISTS:
            self._kernel32.CloseHandle(handle)
            self._handle = None
            self._acquired = False
            return False

        self._handle = int(handle)
        self._acquired = True
        return True

    def release(self) -> None:
        """Release lock ownership and close OS handles or descriptors."""
        if sys.platform == "win32":
            if self._kernel32 is not None and self._handle is not None:
                try:
                    self._kernel32.ReleaseMutex(self._handle)
                finally:
                    self._kernel32.CloseHandle(self._handle)
        else:
            if self._lock_fd is not None:
                try:
                    fcntl_mod = _get_fcntl()
                    fcntl_mod.flock(self._lock_fd, fcntl_mod.LOCK_UN)
                except OSError:
                    pass
                try:
                    os.close(self._lock_fd)
                except OSError:
                    pass
        self._handle = None
        self._lock_fd = None
        self._acquired = False

    def __enter__(self) -> Self:
        if not self.acquire():
            raise SingleInstanceError(
                f"Another instance of {self.app_name} is already running (lock: {self.name})."
            )
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.release()


SingleInstanceMutex = CrossPlatformInstanceLock

__all__ = [
    "CrossPlatformInstanceLock",
    "ERROR_ALREADY_EXISTS",
    "SingleInstanceError",
    "SingleInstanceMutex",
    "mutex_name_for_app",
]
