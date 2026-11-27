"""Dual-binding runtime discovery (`runtime.json`) and desktop settings persistence."""

from __future__ import annotations

import json
import os
import secrets
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psutil

from .security import get_gateway_token_path, restrict_token_file_acl

RUNTIME_SCHEMA = "kryptos.runtime.v1"
DEVELOPER_DEFAULT_PORT = 8787
DEVELOPER_PORT_RANGE = (8787, 8795)

_RUNTIME_LOCK = threading.Lock()
_ACTIVE_RUNTIME_INFO: dict[str, Any] = {}


def _atomic_write_json(target: Path, payload: dict[str, Any], *, restrict_acl: bool = False) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.parent / f".{target.name}.{secrets.token_hex(4)}.tmp"
    raw = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    try:
        tmp.write_text(raw, encoding="utf-8")
        if restrict_acl:
            restrict_token_file_acl(tmp)
        os.replace(tmp, target)
        if restrict_acl:
            restrict_token_file_acl(target)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    return target


def load_desktop_settings(data_dir: Path) -> dict[str, Any]:
    """Load `<data_dir>/desktop_settings.json` with safe defaults."""
    defaults: dict[str, Any] = {"developer_gateway_enabled": False}
    settings_path = data_dir / "desktop_settings.json"
    if not settings_path.is_file():
        return defaults
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            defaults["developer_gateway_enabled"] = bool(data.get("developer_gateway_enabled", False))
    except (OSError, ValueError):
        pass
    return defaults


def save_desktop_settings(data_dir: Path, settings: dict[str, Any]) -> Path:
    """Persist `<data_dir>/desktop_settings.json` atomically."""
    clean = {"developer_gateway_enabled": bool(settings.get("developer_gateway_enabled", False))}
    return _atomic_write_json(data_dir / "desktop_settings.json", clean, restrict_acl=False)


def set_active_runtime_info(
    *,
    host: str,
    port: int,
    developer_gateway_pinned: bool,
    runtime_file: Path | None = None,
) -> dict[str, Any]:
    """Record active runtime binding details in memory for security/telemetry routes."""
    info = {
        "schema": RUNTIME_SCHEMA,
        "pid": os.getpid(),
        "host": host,
        "port": int(port),
        "base_url": f"http://{host}:{int(port)}",
        "developer_gateway_pinned": bool(developer_gateway_pinned),
        "runtime_file": str(runtime_file) if runtime_file is not None else None,
    }
    with _RUNTIME_LOCK:
        _ACTIVE_RUNTIME_INFO.clear()
        _ACTIVE_RUNTIME_INFO.update(info)
    return dict(info)


def clear_active_runtime_info() -> None:
    """Clear in-memory active runtime binding details."""
    with _RUNTIME_LOCK:
        _ACTIVE_RUNTIME_INFO.clear()


def get_active_runtime_info() -> dict[str, Any]:
    """Return a copy of the active in-memory runtime binding details."""
    with _RUNTIME_LOCK:
        return dict(_ACTIVE_RUNTIME_INFO)


def write_runtime_file(
    data_dir: Path,
    *,
    host: str,
    port: int,
    developer_gateway_pinned: bool,
    token_file: Path | None = None,
) -> Path:
    """Atomically write `<data_dir>/runtime.json` with user-only ACL (`0600` on POSIX)."""
    resolved_token_file = token_file or get_gateway_token_path()
    payload = {
        "schema": RUNTIME_SCHEMA,
        "pid": os.getpid(),
        "host": host,
        "port": int(port),
        "base_url": f"http://{host}:{int(port)}",
        "developer_gateway_pinned": bool(developer_gateway_pinned),
        "token_file": str(resolved_token_file),
        "started_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }
    runtime_path = data_dir / "runtime.json"
    _atomic_write_json(runtime_path, payload, restrict_acl=True)
    set_active_runtime_info(
        host=host,
        port=port,
        developer_gateway_pinned=developer_gateway_pinned,
        runtime_file=runtime_path,
    )
    return runtime_path


def remove_runtime_file(runtime_path: Path | None) -> None:
    """Cleanly remove `<data_dir>/runtime.json` and clear active runtime state."""
    clear_active_runtime_info()
    if runtime_path is None:
        return
    try:
        runtime_path.unlink(missing_ok=True)
    except OSError:
        pass


def read_active_runtime(data_dir: Path | None = None) -> dict[str, Any] | None:
    """Read `<data_dir>/runtime.json` and verify the recorded PID is still alive."""
    if data_dir is None:
        from .paths import resolve_app_paths

        data_dir = resolve_app_paths("KryptosCore", env_prefix="KRYPTOS", ephemeral_cache=True).data_dir
    runtime_path = data_dir / "runtime.json"
    if not runtime_path.is_file():
        return None
    try:
        data = json.loads(runtime_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        pid = int(data.get("pid") or 0)
        if pid <= 0 or not psutil.pid_exists(pid):
            return None
        return data
    except (OSError, ValueError, TypeError):
        return None


__all__ = [
    "DEVELOPER_DEFAULT_PORT",
    "DEVELOPER_PORT_RANGE",
    "RUNTIME_SCHEMA",
    "clear_active_runtime_info",
    "get_active_runtime_info",
    "load_desktop_settings",
    "read_active_runtime",
    "remove_runtime_file",
    "save_desktop_settings",
    "set_active_runtime_info",
    "write_runtime_file",
]
