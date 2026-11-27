"""Windows Desktop wrapper for Local LLM (pywebview private_mode=True).

Provides portable ``app_paths.v1`` layout with ``ephemeral_cache=True``,
single-instance Win32 mutex ``Local\\LocalLLM_SingleInstance_v1``, embedded
Uvicorn server on ``127.0.0.1:8787``, and ``pywebview`` ``private_mode=True``,
respecting ``LOCAL_LLM_MODELS_DIR`` for external model weights, plus
deterministic Inno Setup 6 script generation.
"""

from __future__ import annotations

import argparse
import atexit
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .app import create_app
from .config import PROJECT_ROOT, SETTINGS
from .desktop_shell import (
    APP_PATHS_SCHEMA_VERSION,
    AppPaths,
    DesktopAppConfig,
    EmbeddedServer,
    InnoInstallerSpec,
    SingleInstanceMutex,
    ensure_directories,
    launch_desktop_app,
    launch_gui_surface,
    mutex_name_for_app,
    render_inno_script,
    resolve_app_paths,
    resolve_models_dir,
)
from .runtime_state import (
    DEVELOPER_DEFAULT_PORT,
    DEVELOPER_PORT_RANGE,
    load_desktop_settings,
    remove_runtime_file,
    write_runtime_file,
)
from .security import get_gateway_token_path

APP_NAME = "KryptosCore"
APP_DISPLAY_NAME = "Kryptos Core"
APP_VERSION = "3.0.0"
ENV_PREFIX = "KRYPTOS"
SERVICE_KEY = "kryptos_core"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = SETTINGS.port
MUTEX_NAME = mutex_name_for_app(APP_NAME)
EXE_NAME = "KryptosCore.exe"


def _resolve_bundle_root() -> Path:
    """Return runtime root for frozen (PyInstaller) or source checkouts."""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass).resolve()
    return PROJECT_ROOT


def get_app_paths(
    *,
    portable_dir: Path | str | None = None,
    ensure: bool = True,
) -> AppPaths:
    """Resolve portable ``app_paths.v1`` directories with ``ephemeral_cache=True``."""
    paths = resolve_app_paths(
        APP_NAME,
        env_prefix=ENV_PREFIX,
        portable_dir=portable_dir,
        ephemeral_cache=True,
    )
    if ensure:
        ensure_directories(paths)
    return paths


def get_models_dir(default_dir: Path | str | None = None) -> Path:
    """Resolve external weights directory respecting ``LOCAL_LLM_MODELS_DIR``."""
    fallback = default_dir if default_dir is not None else (PROJECT_ROOT / "models")
    return resolve_models_dir(default_dir=fallback)


def build_desktop_config(
    *,
    paths: AppPaths | None = None,
    preferred_port: int | None = None,
    debug: bool = False,
    private_mode: bool = True,
    developer_gateway_enabled: bool | None = None,
) -> DesktopAppConfig:
    """Create ``DesktopAppConfig`` with dual-binding (ephemeral ``:0`` default, pinned ``:8787`` opt-in)."""
    resolved_paths = paths or get_app_paths(ensure=True)
    bundle_root = _resolve_bundle_root()
    icon_candidate = bundle_root / "launcher" / "local-llm.ico"
    if not icon_candidate.is_file():
        icon_candidate = PROJECT_ROOT / "launcher" / "local-llm.ico"

    if developer_gateway_enabled is None:
        pinned = bool(load_desktop_settings(resolved_paths.data_dir).get("developer_gateway_enabled", False))
    else:
        pinned = bool(developer_gateway_enabled)

    if preferred_port is not None:
        pref_port = int(preferred_port)
        range_start = pref_port
        range_end = DEVELOPER_PORT_RANGE[1] if pref_port == DEVELOPER_DEFAULT_PORT else pref_port
    elif pinned:
        pref_port = DEVELOPER_DEFAULT_PORT
        range_start, range_end = DEVELOPER_PORT_RANGE
    else:
        pref_port = 0
        range_start = 0
        range_end = 0

    overrides: dict[str, Any] = {
        "title": APP_DISPLAY_NAME,
        "host": DEFAULT_HOST,
        "preferred_port": pref_port,
        "port_range_start": range_start,
        "port_range_end": range_end,
        "health_path": "/",
        "width": 1440,
        "height": 920,
        "min_size": (1024, 680),
        "background_color": "#0b1120",
        "private_mode": private_mode,
        "storage_path": None,
        "icon_path": icon_candidate if icon_candidate.is_file() else None,
        "debug": debug,
    }
    return DesktopAppConfig(app_name=APP_NAME, **overrides)


def build_inno_spec(
    *,
    source_dir: Path | str,
    output_dir: Path | str,
    app_version: str = APP_VERSION,
    icon_file: Path | str | None = None,
) -> InnoInstallerSpec:
    """Build the ``InnoInstallerSpec`` for Local LLM."""
    return InnoInstallerSpec(
        app_name=APP_DISPLAY_NAME,
        app_version=app_version,
        exe_name=EXE_NAME,
        install_dir_name=APP_NAME,
        mutex_name=MUTEX_NAME,
        source_dir=Path(source_dir),
        output_dir=Path(output_dir),
        output_base_filename=f"{APP_NAME}-Setup-{app_version}",
        icon_file=Path(icon_file) if icon_file is not None else None,
        create_desktop_icon=True,
        launch_after_install=True,
    )


def render_local_llm_inno_script(
    *,
    source_dir: Path | str = Path("..") / "dist" / APP_NAME,
    output_dir: Path | str = Path("out"),
    app_version: str = APP_VERSION,
    icon_file: Path | str | None = None,
) -> str:
    """Render deterministic Inno Setup 6 ``.iss`` script for Local LLM."""
    spec = build_inno_spec(
        source_dir=source_dir,
        output_dir=output_dir,
        app_version=app_version,
        icon_file=icon_file,
    )
    return render_inno_script(spec)


def write_inno_script(
    destination: Path | str,
    *,
    source_dir: Path | str = Path("..") / "dist" / APP_NAME,
    output_dir: Path | str = Path("out"),
    app_version: str = APP_VERSION,
    icon_file: Path | str | None = None,
) -> Path:
    """Write ``installer/local_llm.iss`` using the built-in installer renderer."""
    dest = Path(destination)
    dest.parent.mkdir(parents=True, exist_ok=True)
    content = render_local_llm_inno_script(
        source_dir=source_dir,
        output_dir=output_dir,
        app_version=app_version,
        icon_file=icon_file,
    )
    dest.write_text(content, encoding="utf-8")
    return dest


def launch(
    *,
    models_dir: Path | str | None = None,
    web_dist_dir: Path | str | None = None,
    config: DesktopAppConfig | None = None,
    window_factory: Callable[..., Any] | None = None,
    start_hook: Callable[..., Any] | None = None,
    browser_launcher: Callable[[list[str]], Any] | None = None,
    use_mutex: bool = True,
    serve_only: bool = False,
    attach_url: str | None = None,
) -> int:
    """Launch Local LLM desktop app (embedded FastAPI + pywebview private_mode=True)."""
    paths = get_app_paths(ensure=True)
    if models_dir is not None:
        os.environ["LOCAL_LLM_MODELS_DIR"] = str(Path(models_dir).expanduser().resolve())
    elif paths.models_dir is not None:
        os.environ["LOCAL_LLM_MODELS_DIR"] = str(paths.models_dir)
    if web_dist_dir is not None:
        os.environ["LOCAL_LLM_WEB_DIST"] = str(Path(web_dist_dir).expanduser().resolve())

    desktop_cfg = config or build_desktop_config(paths=paths, private_mode=True)
    if not desktop_cfg.private_mode:
        raise ValueError("Local LLM desktop shell strictly requires private_mode=True.")

    if attach_url is not None:
        from urllib.parse import urlsplit

        parsed = urlsplit(attach_url)
        if parsed.scheme not in ("http", "https") or parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError(f"attach_url must point to loopback, got {attach_url!r}")
        launch_gui_surface(
            attach_url,
            desktop_cfg,
            window_factory=window_factory,
            start_hook=start_hook,
            browser_launcher=browser_launcher,
        )
        return 0

    application = create_app()
    pinned_mode = desktop_cfg.preferred_port != 0
    runtime_holder: dict[str, Path | None] = {"path": None}

    def _on_started(bound_port: int) -> None:
        rt_path = write_runtime_file(
            paths.data_dir,
            host=desktop_cfg.host,
            port=bound_port,
            developer_gateway_pinned=pinned_mode,
            token_file=get_gateway_token_path(),
        )
        runtime_holder["path"] = rt_path
        atexit.register(remove_runtime_file, rt_path)

    def _on_stopped() -> None:
        remove_runtime_file(runtime_holder["path"])
        runtime_holder["path"] = None

    if serve_only:
        mutex = SingleInstanceMutex(desktop_cfg.app_name, mutex_name=MUTEX_NAME) if use_mutex else None
        if mutex is not None and not mutex.acquire():
            return 1
        try:
            with EmbeddedServer(
                application,
                host=desktop_cfg.host,
                port=desktop_cfg.preferred_port,
                range_start=desktop_cfg.port_range_start,
                range_end=desktop_cfg.port_range_end,
                health_path=desktop_cfg.health_path,
            ) as server:
                _on_started(server.port)
                print(f"Local LLM gateway running at {server.base_url}", flush=True)
                try:
                    import time

                    while server.is_running:
                        time.sleep(0.5)
                except KeyboardInterrupt:
                    pass
                finally:
                    _on_stopped()
        finally:
            if mutex is not None:
                mutex.release()
        return 0

    return launch_desktop_app(
        application,
        desktop_cfg,
        window_factory=window_factory,
        start_hook=start_hook,
        browser_launcher=browser_launcher,
        use_mutex=use_mutex,
        on_server_started=_on_started,
        on_server_stopped=_on_stopped,
    )


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="local-llm-desktop",
        description="Local LLM Windows Desktop Shell (127.0.0.1:8787 + pywebview private_mode=True).",
    )
    parser.add_argument("--port", type=int, default=None, help="Preferred loopback port (default: 8787)")
    parser.add_argument("--models-dir", type=str, default=None, help="Override LOCAL_LLM_MODELS_DIR")
    parser.add_argument("--web-dist", type=str, default=None, help="Override compiled web/dist directory")
    parser.add_argument("--serve-only", action="store_true", help="Run embedded FastAPI server without pywebview")
    parser.add_argument("--attach-url", type=str, default=None, help="Attach native window to already-running loopback gateway URL")
    parser.add_argument("--no-mutex", action="store_true", help="Disable single-instance mutex check")
    parser.add_argument("--debug", action="store_true", help="Enable pywebview DevTools")
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for ``local-llm-desktop``."""
    args = _build_arg_parser().parse_args(argv)
    paths = get_app_paths(ensure=True)
    cfg = build_desktop_config(
        paths=paths,
        preferred_port=args.port,
        debug=args.debug,
        private_mode=True,
    )
    return launch(
        models_dir=args.models_dir,
        web_dist_dir=args.web_dist,
        config=cfg,
        use_mutex=not args.no_mutex,
        serve_only=args.serve_only,
        attach_url=args.attach_url,
    )


__all__ = [
    "APP_DISPLAY_NAME",
    "APP_NAME",
    "APP_PATHS_SCHEMA_VERSION",
    "APP_VERSION",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "ENV_PREFIX",
    "EXE_NAME",
    "MUTEX_NAME",
    "SERVICE_KEY",
    "build_desktop_config",
    "build_inno_spec",
    "get_app_paths",
    "get_models_dir",
    "launch",
    "main",
    "render_local_llm_inno_script",
    "write_inno_script",
]


if __name__ == "__main__":
    raise SystemExit(main())
