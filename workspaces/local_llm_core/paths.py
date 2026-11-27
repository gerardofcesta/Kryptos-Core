"""Cross-platform application paths for Kryptos Core (Windows, Linux XDG, macOS)."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

APP_PATHS_SCHEMA_VERSION = "app_paths.v1"
MODELS_DIR_ENV_VAR = "KRYPTOS_MODELS_DIR"
LEGACY_MODELS_DIR_ENV_VAR = "LOCAL_LLM_MODELS_DIR"

_INVALID_APP_NAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _normalize_app_name(app_name: str) -> str:
    cleaned = app_name.strip()
    if not cleaned or _INVALID_APP_NAME_RE.search(cleaned):
        raise ValueError(f"Invalid application name: {app_name!r}")
    return cleaned


def _normalize_env_prefix(app_name: str, env_prefix: str | None) -> str:
    raw = env_prefix.strip() if env_prefix is not None else app_name
    normalized = re.sub(r"[^A-Za-z0-9]+", "_", raw).strip("_").upper()
    if not normalized:
        raise ValueError(f"Cannot derive env_prefix from {app_name!r}")
    return normalized


def _linux_slug(app_name: str) -> str:
    slug = re.sub(r"[^a-z0-9._-]+", "", app_name.strip().lower())
    return slug or "kryptoscore"


def _get_local_appdata() -> Path:
    env_val = os.environ.get("LOCALAPPDATA", "").strip()
    if env_val:
        return Path(env_val).expanduser()
    return Path.home() / "AppData" / "Local"


def _get_xdg_dir(env_var: str, fallback: Path) -> Path:
    env_val = os.environ.get(env_var, "").strip()
    if env_val:
        return Path(env_val).expanduser()
    return fallback


def resolve_models_dir(default_dir: Path | str | None = None) -> Path:
    """Resolve the external LLM weights directory via ``KRYPTOS_MODELS_DIR`` (or legacy ``LOCAL_LLM_MODELS_DIR``)."""
    env_val = (
        os.environ.get(MODELS_DIR_ENV_VAR, "").strip()
        or os.environ.get(LEGACY_MODELS_DIR_ENV_VAR, "").strip()
    )
    if env_val:
        return Path(env_val).expanduser()
    if default_dir is not None:
        return Path(default_dir).expanduser()
    return Path.home() / "Kryptos_Models"


class AppPaths(BaseModel):
    """Canonical directory layout for desktop application storage (``app_paths.v1``)."""

    model_config = ConfigDict(frozen=True)

    schema_version: str = Field(default=APP_PATHS_SCHEMA_VERSION)
    app_name: str
    env_prefix: str
    binaries_dir: Path
    data_dir: Path
    cache_dir: Path | None = None
    runtime_dir: Path | None = None
    models_dir: Path | None = None
    is_portable: bool = False
    ephemeral_cache: bool = False

    def ensure_directories(self, *, include_binaries: bool = False) -> AppPaths:
        """Create writable application directories (data, optional cache, optional runtime)."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if self.cache_dir is not None:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        if self.runtime_dir is not None:
            self.runtime_dir.mkdir(parents=True, exist_ok=True)
            if sys.platform != "win32":
                try:
                    os.chmod(self.runtime_dir, 0o700)
                except OSError:
                    pass
        if include_binaries:
            self.binaries_dir.mkdir(parents=True, exist_ok=True)
        return self


def ensure_directories(paths: AppPaths, *, include_binaries: bool = False) -> AppPaths:
    """Ensure writable directories exist for the given ``AppPaths`` instance."""
    return paths.ensure_directories(include_binaries=include_binaries)


def resolve_app_paths(
    app_name: str = "KryptosCore",
    env_prefix: str | None = None,
    portable_dir: Path | str | None = None,
    ephemeral_cache: bool = False,
    *,
    platform: str | None = None,
) -> AppPaths:
    """Resolve portable or OS-native directories (Win32, Linux XDG, macOS)."""
    clean_app = _normalize_app_name(app_name)
    prefix = _normalize_env_prefix(clean_app, env_prefix)
    target_platform = (platform or sys.platform).lower()

    if target_platform.startswith("win"):
        local_appdata = _get_local_appdata()
        binaries_dir = local_appdata / "Programs" / clean_app
        default_data_dir = local_appdata / clean_app / "data"
        default_cache_dir = local_appdata / clean_app / "cache"
        runtime_dir: Path | None = None
    elif target_platform == "darwin":
        home = Path.home()
        binaries_dir = home / "Applications" / clean_app
        default_data_dir = home / "Library" / "Application Support" / clean_app
        default_cache_dir = home / "Library" / "Caches" / clean_app
        runtime_dir = None
    else:
        home = Path.home()
        slug = _linux_slug(clean_app)
        xdg_data = _get_xdg_dir("XDG_DATA_HOME", home / ".local" / "share")
        xdg_cache = _get_xdg_dir("XDG_CACHE_HOME", home / ".cache")
        xdg_runtime = os.environ.get("XDG_RUNTIME_DIR", "").strip()
        binaries_dir = home / ".local" / "bin"
        default_data_dir = xdg_data / slug
        default_cache_dir = xdg_cache / slug
        runtime_dir = Path(xdg_runtime).expanduser() / slug if xdg_runtime else (default_data_dir / "run")

    portable_base = Path(portable_dir).expanduser() if portable_dir is not None else binaries_dir
    portable_active = (portable_base / "portable.flag").is_file()

    data_override = os.environ.get(f"{prefix}_DATA_DIR", "").strip()
    if not data_override and prefix in ("KRYPTOS", "KRYPTOSCORE"):
        data_override = os.environ.get("LOCAL_LLM_DATA_DIR", "").strip()
    if data_override:
        data_dir = Path(data_override).expanduser()
        is_portable = False
    elif portable_active:
        data_dir = portable_base / "data"
        is_portable = True
    else:
        data_dir = default_data_dir
        is_portable = False

    if ephemeral_cache:
        cache_dir: Path | None = None
    elif is_portable:
        cache_dir = portable_base / "cache"
    else:
        cache_dir = default_cache_dir

    models_env = (
        os.environ.get(MODELS_DIR_ENV_VAR, "").strip()
        or os.environ.get(LEGACY_MODELS_DIR_ENV_VAR, "").strip()
    )
    models_dir = Path(models_env).expanduser() if models_env else None

    return AppPaths(
        schema_version=APP_PATHS_SCHEMA_VERSION,
        app_name=clean_app,
        env_prefix=prefix,
        binaries_dir=binaries_dir,
        data_dir=data_dir,
        cache_dir=cache_dir,
        runtime_dir=runtime_dir,
        models_dir=models_dir,
        is_portable=is_portable,
        ephemeral_cache=ephemeral_cache,
    )


__all__ = [
    "APP_PATHS_SCHEMA_VERSION",
    "MODELS_DIR_ENV_VAR",
    "AppPaths",
    "ensure_directories",
    "resolve_app_paths",
    "resolve_models_dir",
]
