"""Self-contained cross-platform desktop shell, app paths, instance lock, and installer helpers."""

from __future__ import annotations

import http.client
import importlib.util
import os
import re
import shutil
import socket
import subprocess  # nosec B404
import sys
import threading
import time
import uuid
import webbrowser
from collections.abc import Callable
from pathlib import Path
from types import TracebackType
from typing import Any, Self

import uvicorn
from jinja2 import Environment, StrictUndefined
from pydantic import BaseModel, Field, field_validator

from .instance_lock import (
    ERROR_ALREADY_EXISTS,
    CrossPlatformInstanceLock,
    SingleInstanceError,
    SingleInstanceMutex,
    mutex_name_for_app,
)
from .paths import (
    APP_PATHS_SCHEMA_VERSION,
    MODELS_DIR_ENV_VAR,
    AppPaths,
    ensure_directories,
    resolve_app_paths,
    resolve_models_dir,
)

_CONTROL_CHAR_RE = re.compile(r"[\r\n\x00-\x1f]")
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_APP_NAMESPACE = uuid.UUID("8f4e2a1d-7c65-4e3a-9d42-6a1f9c2c8e10")

PORT_MAP: dict[str, tuple[int, int, int]] = {
    "local_llm": (8787, 8787, 8787),
}


class DesktopAppConfig(BaseModel):
    """Configuration for launching a FastAPI + pywebview desktop shell."""

    app_name: str
    title: str | None = None
    host: str = "127.0.0.1"
    preferred_port: int = 8787
    port_range_start: int = 8787
    port_range_end: int = 8787
    health_path: str = "/"
    startup_timeout_s: float = 15.0
    shutdown_timeout_s: float = 5.0
    width: int = 1440
    height: int = 920
    min_size: tuple[int, int] = (1024, 680)
    resizable: bool = True
    confirm_close: bool = False
    background_color: str = "#0b1120"
    private_mode: bool = True
    storage_path: Path | None = None
    icon_path: Path | None = None
    debug: bool = False

    @field_validator("host")
    @classmethod
    def _validate_loopback_host(cls, value: str) -> str:
        cleaned = value.strip()
        if cleaned not in _LOOPBACK_HOSTS:
            raise ValueError(
                f"Desktop shell host must be loopback ({sorted(_LOOPBACK_HOSTS)}), got {value!r}"
            )
        return cleaned

    @field_validator("health_path")
    @classmethod
    def _validate_health_path(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned.startswith("/"):
            return "/" + cleaned
        return cleaned

    @classmethod
    def for_service(
        cls,
        service_key: str,
        app_name: str,
        **overrides: Any,
    ) -> DesktopAppConfig:
        """Create a ``DesktopAppConfig`` pre-populated from ``PORT_MAP``."""
        if service_key not in PORT_MAP:
            raise KeyError(f"Unknown desktop service key: {service_key!r}")
        preferred, start, end = PORT_MAP[service_key]
        return cls(
            app_name=app_name,
            preferred_port=preferred,
            port_range_start=start,
            port_range_end=end,
            **overrides,
        )


def _is_port_available(host: str, port: int) -> bool:
    family = socket.AF_INET6 if host == "::1" else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.15)
        if probe.connect_ex((host, port)) == 0:
            return False

    with socket.socket(family, socket.SOCK_STREAM) as sock:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def find_available_port(
    preferred: int,
    range_start: int | None = None,
    range_end: int | None = None,
    host: str = "127.0.0.1",
) -> int:
    """Return an available loopback TCP port, preferring ``preferred`` then range."""
    if host not in _LOOPBACK_HOSTS:
        raise ValueError(f"Host must be loopback, got {host!r}")

    if preferred == 0:
        family = socket.AF_INET6 if host == "::1" else socket.AF_INET
        with socket.socket(family, socket.SOCK_STREAM) as sock:
            sock.bind((host, 0))
            return int(sock.getsockname()[1])

    if _is_port_available(host, preferred):
        return preferred

    if range_start is not None and range_end is not None:
        for candidate in range(range_start, range_end + 1):
            if candidate == preferred:
                continue
            if _is_port_available(host, candidate):
                return candidate

    raise RuntimeError(
        f"No available port found (preferred={preferred}, range={range_start}-{range_end}) on {host}"
    )


class _BackgroundUvicornServer(uvicorn.Server):
    """Uvicorn server subclass that skips signal handler registration on worker threads."""

    def install_signal_handlers(self) -> None:
        return None


class EmbeddedServer:
    """Runs an ASGI application in a daemon Uvicorn thread on ``127.0.0.1``."""

    def __init__(
        self,
        app: Any,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        range_start: int | None = None,
        range_end: int | None = None,
        health_path: str = "/health",
        log_level: str = "warning",
    ) -> None:
        if host not in _LOOPBACK_HOSTS:
            raise ValueError(f"EmbeddedServer only binds loopback hosts, got {host!r}")
        self.app = app
        self.host = host
        self.port = find_available_port(port, range_start, range_end, host=host)
        self.health_path = health_path if health_path.startswith("/") else f"/{health_path}"
        self.log_level = log_level
        self._server: _BackgroundUvicornServer | None = None
        self._thread: threading.Thread | None = None
        self._startup_error: BaseException | None = None

    @property
    def base_url(self) -> str:
        """Return the loopback HTTP base URL for the running server."""
        formatted_host = f"[{self.host}]" if ":" in self.host else self.host
        return f"http://{formatted_host}:{self.port}"

    @property
    def is_running(self) -> bool:
        """Return ``True`` while the background Uvicorn server thread is alive."""
        return self._thread is not None and self._thread.is_alive()

    def _poll_health(self) -> bool:
        conn: http.client.HTTPConnection | None = None
        try:
            conn = http.client.HTTPConnection(self.host, self.port, timeout=1.0)
            conn.request("GET", self.health_path)
            resp = conn.getresponse()
            resp.read()
            return 200 <= resp.status < 500
        except OSError:
            return False
        finally:
            if conn is not None:
                conn.close()

    def start(self, timeout: float = 15.0) -> str:
        """Start the embedded server and block until the health endpoint responds."""
        if self._thread is not None and self._thread.is_alive():
            return self.base_url

        self._startup_error = None
        config = uvicorn.Config(
            app=self.app,
            host=self.host,
            port=self.port,
            log_level=self.log_level,
            lifespan="on",
            access_log=False,
        )
        server = _BackgroundUvicornServer(config)
        self._server = server

        def _runner() -> None:
            try:
                server.run()
            except (Exception, SystemExit) as exc:  # noqa: BLE001  # pragma: no cover
                self._startup_error = exc

        thread = threading.Thread(
            target=_runner,
            name=f"EmbeddedServer-{self.port}",
            daemon=True,
        )
        self._thread = thread
        thread.start()

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._startup_error is not None:
                raise RuntimeError(f"EmbeddedServer failed to start: {self._startup_error}") from self._startup_error
            if not thread.is_alive():
                raise RuntimeError("EmbeddedServer thread exited before becoming healthy.")
            if server.started and self._poll_health():
                return self.base_url
            time.sleep(0.05)

        self.stop(timeout=2.0)
        raise TimeoutError(
            f"EmbeddedServer did not become healthy at {self.base_url}{self.health_path} within {timeout}s."
        )

    def stop(self, timeout: float = 5.0) -> None:
        """Signal Uvicorn to exit and wait for the background thread to join."""
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        self._server = None
        self._thread = None

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.stop()


_APP_MODE_BROWSERS = (
    "chromium",
    "chromium-browser",
    "brave-browser",
    "google-chrome",
    "google-chrome-stable",
    "microsoft-edge",
    "microsoft-edge-stable",
)


def is_linux_webview_available() -> bool:
    """Return ``True`` on non-Linux, or on Linux when WebKit2GTK/PyGObject or QtPy is importable."""
    if not sys.platform.startswith("linux"):
        return True
    try:
        if importlib.util.find_spec("gi") is not None:
            import gi  # type: ignore[import-untyped]

            for version in ("4.1", "4.0"):
                try:
                    gi.require_version("WebKit2", version)
                    from gi.repository import WebKit2  # type: ignore[import-untyped]  # noqa: F401

                    return True
                except Exception:  # noqa: BLE001
                    continue
            try:
                gi.require_version("WebKit", "6.0")
                from gi.repository import WebKit  # type: ignore[import-untyped]  # noqa: F401

                return True
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass
    return importlib.util.find_spec("qtpy") is not None


def find_app_mode_browser() -> str | None:
    """Locate an installed Chromium-family browser supporting ``--app=<url>``."""
    for candidate in _APP_MODE_BROWSERS:
        found = shutil.which(candidate)
        if found:
            return found
    return None


def launch_gui_surface(
    url: str,
    config: DesktopAppConfig,
    *,
    window_factory: Callable[..., Any] | None = None,
    start_hook: Callable[..., Any] | None = None,
    browser_launcher: Callable[[list[str]], Any] | None = None,
) -> str:
    """Launch desktop GUI via hooks, pywebview, or Linux ``--app=`` browser fallback."""
    storage_path_str = (
        str(config.storage_path)
        if (config.storage_path is not None and not config.private_mode)
        else None
    )
    icon_str = str(config.icon_path) if config.icon_path is not None else None

    if window_factory is not None or start_hook is not None:
        if window_factory is None or start_hook is None:
            raise ValueError(
                "Both window_factory and start_hook must be provided together for headless testing."
            )
        window = window_factory(
            title=config.title or config.app_name,
            url=url,
            width=config.width,
            height=config.height,
            min_size=config.min_size,
            resizable=config.resizable,
            confirm_close=config.confirm_close,
            background_color=config.background_color,
            private_mode=config.private_mode,
        )
        start_hook(
            window,
            private_mode=config.private_mode,
            storage_path=storage_path_str,
            icon=icon_str,
            debug=config.debug,
        )
        return "hooks"

    if sys.platform.startswith("linux") and not is_linux_webview_available():
        browser = find_app_mode_browser()
        if browser:
            cmd = [browser, f"--app={url}", "--new-window"]
            if browser_launcher is not None:
                browser_launcher(cmd)
            else:
                proc = subprocess.Popen(cmd)  # nosec B603
                proc.wait()
            return "browser-app"
        webbrowser.open(url)
        return "webbrowser"

    import webview

    webview.create_window(
        title=config.title or config.app_name,
        url=url,
        width=config.width,
        height=config.height,
        min_size=config.min_size,
        resizable=config.resizable,
        confirm_close=config.confirm_close,
        background_color=config.background_color,
    )
    webview.start(
        private_mode=config.private_mode,
        storage_path=storage_path_str,
        icon=icon_str,
        debug=config.debug,
    )
    return "pywebview"


def launch_desktop_app(
    app: Any,
    config: DesktopAppConfig,
    *,
    window_factory: Callable[..., Any] | None = None,
    start_hook: Callable[..., Any] | None = None,
    browser_launcher: Callable[[list[str]], Any] | None = None,
    use_mutex: bool = True,
    on_server_started: Callable[[int], None] | None = None,
    on_server_stopped: Callable[[], None] | None = None,
) -> int:
    """Start the embedded backend, open a desktop GUI surface, and clean up on close."""
    mutex = SingleInstanceMutex(config.app_name) if use_mutex else None
    if mutex is not None and not mutex.acquire():
        return 1

    try:
        server = EmbeddedServer(
            app,
            host=config.host,
            port=config.preferred_port,
            range_start=config.port_range_start,
            range_end=config.port_range_end,
            health_path=config.health_path,
        )
        base_url = server.start(timeout=config.startup_timeout_s)
        if on_server_started is not None:
            on_server_started(server.port)
        try:
            launch_gui_surface(
                base_url,
                config,
                window_factory=window_factory,
                start_hook=start_hook,
                browser_launcher=browser_launcher,
            )
        finally:
            try:
                if on_server_stopped is not None:
                    on_server_stopped()
            finally:
                server.stop(timeout=config.shutdown_timeout_s)
    finally:
        if mutex is not None:
            mutex.release()

    return 0


_INNO_TEMPLATE = """\
; Generated by Local LLM standalone desktop packager
[Setup]
AppId={{ app_id_literal }}
AppName={{ app_name }}
AppVersion={{ app_version }}
AppPublisher={{ publisher }}
DefaultDirName={localappdata}\\Programs\\{{ install_dir_name }}
DefaultGroupName={{ app_name }}
PrivilegesRequired=lowest
AppMutex={{ mutex_name }}
CloseApplications=force
RestartApplications=no
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\\{{ exe_name }}
OutputDir={{ output_dir }}
OutputBaseFilename={{ output_base_filename }}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
{% if icon_file -%}
SetupIconFile={{ icon_file }}
{% endif -%}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

{% if create_desktop_icon -%}
[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

{% endif -%}
[Dirs]
; Persistent user data lives outside {app} and is never wiped on upgrade/uninstall
Name: "{localappdata}\\{{ install_dir_name }}\\data"; Flags: uninsneveruninstall

[Files]
Source: "{{ source_dir }}\\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
{% for src, dest in extra_files -%}
Source: "{{ src }}"; DestDir: "{{ dest }}"; Flags: ignoreversion
{% endfor -%}

[Icons]
Name: "{autoprograms}\\{{ app_name }}"; Filename: "{app}\\{{ exe_name }}"
{% if create_desktop_icon -%}
Name: "{autodesktop}\\{{ app_name }}"; Filename: "{app}\\{{ exe_name }}"; Tasks: desktopicon
{% endif -%}

{% if launch_after_install -%}
[Run]
Filename: "{app}\\{{ exe_name }}"; Description: "Launch {{ app_name }}"; Flags: nowait postinstall skipifsilent
{% endif -%}
"""


def _reject_control_chars(value: str, field_name: str) -> str:
    if _CONTROL_CHAR_RE.search(value):
        raise ValueError(f"{field_name} must not contain newlines or control characters.")
    return value


def _format_inno_app_id(app_name: str, app_id: str | None) -> str:
    if app_id:
        cleaned = _reject_control_chars(app_id.strip(), "app_id")
        if cleaned.startswith("{") and not cleaned.startswith("{{"):
            return "{" + cleaned
        return cleaned
    generated = str(uuid.uuid5(_APP_NAMESPACE, app_name)).upper()
    return "{{" + generated + "}}"


class InnoInstallerSpec(BaseModel):
    """Specification for rendering a standalone Inno Setup 6 ``.iss`` script."""

    app_name: str
    exe_name: str
    source_dir: Path
    output_dir: Path
    app_version: str = "0.1.0"
    publisher: str = "Local LLM Contributors"
    app_id: str | None = None
    install_dir_name: str | None = None
    output_base_filename: str | None = None
    icon_file: Path | None = None
    mutex_name: str | None = None
    create_desktop_icon: bool = True
    launch_after_install: bool = True
    extra_files: list[tuple[str, str]] = Field(default_factory=list)

    @field_validator("app_name", "exe_name", "app_version", "publisher")
    @classmethod
    def _validate_single_line(cls, value: str, info: Any) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError(f"{info.field_name} must not be empty.")
        return _reject_control_chars(cleaned, info.field_name)


def render_inno_script(spec: InnoInstallerSpec) -> str:
    """Render a deterministic Inno Setup 6 ``.iss`` script from ``InnoInstallerSpec``."""
    install_dir_name = _reject_control_chars(
        (spec.install_dir_name or spec.app_name).strip(), "install_dir_name"
    )
    mutex_name = _reject_control_chars(
        (spec.mutex_name or mutex_name_for_app(spec.app_name)).strip(), "mutex_name"
    )
    safe_base = re.sub(r"[^A-Za-z0-9._-]+", "-", spec.app_name).strip("-") or "App"
    output_base_filename = _reject_control_chars(
        (spec.output_base_filename or f"{safe_base}-Setup-{spec.app_version}").strip(),
        "output_base_filename",
    )

    env = Environment(autoescape=False, undefined=StrictUndefined, keep_trailing_newline=True)  # nosec B701
    template = env.from_string(_INNO_TEMPLATE)

    validated_extra: list[tuple[str, str]] = [
        (
            _reject_control_chars(src.strip(), "extra_files.source"),
            _reject_control_chars(dest.strip(), "extra_files.dest"),
        )
        for src, dest in spec.extra_files
    ]

    return template.render(
        app_id_literal=_format_inno_app_id(spec.app_name, spec.app_id),
        app_name=spec.app_name,
        app_version=spec.app_version,
        publisher=spec.publisher,
        install_dir_name=install_dir_name,
        exe_name=spec.exe_name,
        mutex_name=mutex_name,
        source_dir=str(spec.source_dir),
        output_dir=str(spec.output_dir),
        output_base_filename=output_base_filename,
        icon_file=str(spec.icon_file) if spec.icon_file else "",
        create_desktop_icon=spec.create_desktop_icon,
        launch_after_install=spec.launch_after_install,
        extra_files=validated_extra,
    )


def find_iscc(explicit_path: Path | str | None = None) -> Path | None:
    """Locate Inno Setup 6 ``ISCC.exe`` on the host machine."""
    candidates: list[Path] = []
    if explicit_path is not None:
        candidates.append(Path(explicit_path).expanduser())

    env_path = os.environ.get("INNO_ISCC_PATH", "").strip()
    if env_path:
        candidates.append(Path(env_path).expanduser())

    which_iscc = shutil.which("ISCC.exe") or shutil.which("iscc")
    if which_iscc:
        candidates.append(Path(which_iscc))

    local_appdata = os.environ.get("LOCALAPPDATA", "").strip()
    if local_appdata:
        candidates.append(Path(local_appdata) / "Programs" / "Inno Setup 6" / "ISCC.exe")
    else:
        candidates.append(Path.home() / "AppData" / "Local" / "Programs" / "Inno Setup 6" / "ISCC.exe")

    candidates.extend(
        [
            Path(r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"),
            Path(r"C:\Program Files\Inno Setup 6\ISCC.exe"),
        ]
    )

    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    return None


def _parse_iss_setting(iss_text: str, key: str) -> str | None:
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
    match = pattern.search(iss_text)
    return match.group(1).strip() if match else None


def compile_inno_script(
    iss_path: Path | str,
    *,
    iscc_path: Path | str | None = None,
    output_dir: Path | str | None = None,
    quiet: bool = True,
    timeout: float = 120.0,
) -> Path:
    """Compile an Inno Setup ``.iss`` script using ``ISCC.exe`` and return the installer path."""
    iscc = find_iscc(iscc_path)
    if iscc is None:
        raise FileNotFoundError(
            "Inno Setup 6 compiler (ISCC.exe) not found. Install Inno Setup 6 or set INNO_ISCC_PATH."
        )

    resolved_iss = Path(iss_path).expanduser().resolve()
    if not resolved_iss.is_file():
        raise FileNotFoundError(f"Inno Setup script not found: {resolved_iss}")

    iss_text = resolved_iss.read_text(encoding="utf-8")
    resolved_out_dir = (
        Path(output_dir).expanduser().resolve()
        if output_dir is not None
        else Path(_parse_iss_setting(iss_text, "OutputDir") or (resolved_iss.parent / "Output"))
    )
    resolved_out_dir.mkdir(parents=True, exist_ok=True)
    base_filename = _parse_iss_setting(iss_text, "OutputBaseFilename") or "setup"

    cmd = [str(iscc)]
    if quiet:
        cmd.append("/Q")
    if output_dir is not None:
        cmd.append(f"/O{resolved_out_dir}")
    cmd.append(str(resolved_iss))

    result = subprocess.run(  # nosec B603
        cmd,
        cwd=str(resolved_iss.parent),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        details = (result.stdout or "") + "\n" + (result.stderr or "")
        raise RuntimeError(
            f"ISCC.exe failed with exit code {result.returncode}:\n{details.strip()}"
        )

    expected_exe = resolved_out_dir / f"{base_filename}.exe"
    if not expected_exe.is_file():
        raise FileNotFoundError(f"Expected compiled installer missing: {expected_exe}")
    return expected_exe


__all__ = [
    "APP_PATHS_SCHEMA_VERSION",
    "ERROR_ALREADY_EXISTS",
    "MODELS_DIR_ENV_VAR",
    "PORT_MAP",
    "AppPaths",
    "CrossPlatformInstanceLock",
    "DesktopAppConfig",
    "EmbeddedServer",
    "InnoInstallerSpec",
    "SingleInstanceError",
    "SingleInstanceMutex",
    "compile_inno_script",
    "ensure_directories",
    "find_app_mode_browser",
    "find_available_port",
    "find_iscc",
    "is_linux_webview_available",
    "launch_desktop_app",
    "launch_gui_surface",
    "mutex_name_for_app",
    "render_inno_script",
    "resolve_app_paths",
    "resolve_models_dir",
]
