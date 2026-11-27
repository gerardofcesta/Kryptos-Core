"""Configuration loader for Local LLM Core (.env, endpoints.json, hardware-profile.json)."""

from __future__ import annotations

import json
import os
import pathlib
import re
import sys
import urllib.parse
from typing import Any

from pydantic import BaseModel, Field

CORE_DIR = pathlib.Path(__file__).resolve().parent
WORKSPACES_DIR = CORE_DIR.parent
PROJECT_ROOT = WORKSPACES_DIR.parent
OFFLINE_CHAT_DIR = WORKSPACES_DIR / "offline-chat"
STATIC_DIR = OFFLINE_CHAT_DIR / "static"
WEB_DIST_DIR = OFFLINE_CHAT_DIR / "web" / "dist"


def resolve_web_dist_dir() -> pathlib.Path:
    """Resolve compiled Stage 2 `web/dist` directory (supports PyInstaller `_MEIPASS` and overrides)."""
    override = os.environ.get("LOCAL_LLM_WEB_DIST", "").strip()
    if override:
        return pathlib.Path(override).expanduser().resolve()
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        bundled_candidates = (
            pathlib.Path(meipass) / "web" / "dist",
            pathlib.Path(meipass) / "workspaces" / "offline-chat" / "web" / "dist",
        )
        for cand in bundled_candidates:
            if cand.is_dir():
                return cand.resolve()
    return WEB_DIST_DIR
PROMPTS_DIR = PROJECT_ROOT / "prompts"
MODELS_DIR = PROJECT_ROOT / "models"
HEAVY_MOE_DIR = MODELS_DIR / "heavy-moe"
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
BENCHMARKS_RESULTS_FILE = PROJECT_ROOT / "benchmarks" / "RESULTS.md"
REGISTRY_MD_FILE = MODELS_DIR / "REGISTRY.md"
ENDPOINTS_JSON_FILE = PROJECT_ROOT / "config" / "endpoints.json"
HARDWARE_PROFILE_FILE = PROJECT_ROOT / "config" / "hardware-profile.json"
HARDWARE_PROFILE_EXAMPLE_FILE = PROJECT_ROOT / "config" / "hardware-profile.example.json"

PROMPT_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{2,64}$")
MODEL_NAME_RE = re.compile(r"^[a-zA-Z0-9._:/@-]{1,160}$")
PROTECTED_SAVE_IDS = frozenset({"readme"})
PROTECTED_DELETE_IDS = frozenset({"readme", "uncensored-direct"})
ALLOWED_ENGINE_ACTIONS = frozenset(
    {"start_fast", "start_heavy", "stop_all", "unload_vram", "shutdown_app"}
)
MAX_REQUEST_BODY_BYTES = 20 * 1024 * 1024

_PROJECT_ROOT_STR = str(PROJECT_ROOT)
if _PROJECT_ROOT_STR not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT_STR)


def load_dotenv(env_path: pathlib.Path) -> None:
    """Load variables from project .env without overriding explicit environment values."""
    if not env_path.is_file():
        return
    try:
        for raw_line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].strip()
            if "=" not in line:
                continue
            key, val = line.split("=", 1)
            key = key.strip()
            val = val.strip()
            if not key:
                continue
            if len(val) >= 2 and ((val[0] == val[-1] == '"') or (val[0] == val[-1] == "'")):
                val = val[1:-1]
            os.environ.setdefault(key, val)
    except OSError:
        pass


load_dotenv(PROJECT_ROOT / ".env")


def read_endpoints_config() -> dict[str, Any]:
    """Read `endpoints` section from `config/endpoints.json`."""
    if not ENDPOINTS_JSON_FILE.is_file():
        return {}
    try:
        data = json.loads(ENDPOINTS_JSON_FILE.read_text(encoding="utf-8", errors="replace"))
        return data.get("endpoints", {}) if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def read_hardware_profile() -> dict[str, Any]:
    """Read `config/hardware-profile.json` or fall back to `hardware-profile.example.json`."""
    for candidate in (HARDWARE_PROFILE_FILE, HARDWARE_PROFILE_EXAMPLE_FILE):
        if not candidate.is_file():
            continue
        try:
            data = json.loads(candidate.read_text(encoding="utf-8", errors="replace"))
            if isinstance(data, dict):
                return data
        except (OSError, ValueError):
            continue
    return {}


def normalize_loopback_endpoint(
    raw_url: str | None, default_host: str, default_port: int
) -> tuple[str, int, str]:
    """Ensure every configured backend endpoint is strictly bound to loopback."""
    candidate = (raw_url or "").strip()
    if not candidate:
        return default_host, default_port, f"http://{default_host}:{default_port}"
    if "://" not in candidate:
        candidate = f"http://{candidate}"
    parsed = urllib.parse.urlsplit(candidate)
    host = parsed.hostname or default_host
    if host not in ("127.0.0.1", "localhost", "::1"):
        host = default_host
    if host == "::1":
        host = "127.0.0.1"
    port = parsed.port or default_port
    return host, port, f"http://{host}:{port}"


class GatewaySettings(BaseModel):
    """Runtime settings for Local LLM FastAPI Core & Unified Gateway."""

    host: str = "127.0.0.1"
    port: int = Field(default=8787, ge=1, le=65535)

    ollama_host: str = "127.0.0.1"
    ollama_port: int = 11434
    ollama_base: str = "http://127.0.0.1:11434"

    heavy_host: str = "127.0.0.1"
    heavy_port: int = 11435
    heavy_base: str = "http://127.0.0.1:11435"

    lms_host: str = "127.0.0.1"
    lms_port: int = 1234
    lms_base: str = "http://127.0.0.1:1234"

    llamacpp_host: str = "127.0.0.1"
    llamacpp_port: int = 8080
    llamacpp_base: str = "http://127.0.0.1:8080"

    fast_model: str = "local-worker:latest"
    balanced_model: str = "local-uncensored:latest"
    heavy_model: str = "heavy-moe-uncensored"
    embedding_model: str = "nomic-embed-text"

    default_max_tokens_per_workflow: int = Field(default=500_000, ge=1)
    default_max_cost_usd_per_workflow: float = Field(default=5.0, ge=0.0)


def load_settings() -> GatewaySettings:
    """Build `GatewaySettings` from environment variables and config JSON files."""
    endpoints = read_endpoints_config()

    try:
        port = int(os.environ.get("OFFLINE_CHAT_PORT", "8787"))
    except ValueError:
        port = 8787

    ollama_raw = os.environ.get("OLLAMA_HOST") or (
        endpoints.get("ollama", {}).get("baseUrl")
        if isinstance(endpoints.get("ollama"), dict)
        else None
    )
    ol_h, ol_p, ol_base = normalize_loopback_endpoint(ollama_raw, "127.0.0.1", 11434)

    heavy_cfg = (
        endpoints.get("colibriHeavyMoe")
        or endpoints.get("heavyMoe")
        or endpoints.get("colibri")
        or {}
    )
    heavy_env_host = os.environ.get("HEAVY_MOE_HOST") or os.environ.get("COLIBRI_HOST") or "127.0.0.1"
    heavy_env_port = os.environ.get("HEAVY_MOE_PORT") or os.environ.get("COLIBRI_PORT")
    heavy_raw = (
        os.environ.get("HEAVY_MOE_BASE_URL")
        or os.environ.get("COLIBRI_BASE_URL")
        or (f"http://{heavy_env_host}:{heavy_env_port}" if heavy_env_port else None)
        or (heavy_cfg.get("baseUrl") if isinstance(heavy_cfg, dict) else None)
    )
    hv_h, hv_p, hv_base = normalize_loopback_endpoint(heavy_raw, "127.0.0.1", 11435)

    lms_cfg = endpoints.get("lmstudio") or {}
    lms_raw = os.environ.get("LMSTUDIO_BASE_URL") or (
        lms_cfg.get("baseUrl") if isinstance(lms_cfg, dict) else None
    )
    lms_h, lms_p, lms_base = normalize_loopback_endpoint(lms_raw, "127.0.0.1", 1234)

    lcpp_cfg = endpoints.get("llamacpp") or {}
    lcpp_raw = os.environ.get("LLAMACPP_BASE_URL") or (
        lcpp_cfg.get("baseUrl") if isinstance(lcpp_cfg, dict) else None
    )
    lcpp_h, lcpp_p, lcpp_base = normalize_loopback_endpoint(lcpp_raw, "127.0.0.1", 8080)

    try:
        max_tok_cap = int(os.environ.get("GATEWAY_MAX_TOKENS_PER_WORKFLOW", "500000"))
    except ValueError:
        max_tok_cap = 500_000

    try:
        max_cost_cap = float(os.environ.get("GATEWAY_MAX_COST_USD_PER_WORKFLOW", "5.0"))
    except ValueError:
        max_cost_cap = 5.0

    return GatewaySettings(
        host="127.0.0.1",
        port=port,
        ollama_host=ol_h,
        ollama_port=ol_p,
        ollama_base=ol_base,
        heavy_host=hv_h,
        heavy_port=hv_p,
        heavy_base=hv_base,
        lms_host=lms_h,
        lms_port=lms_p,
        lms_base=lms_base,
        llamacpp_host=lcpp_h,
        llamacpp_port=lcpp_p,
        llamacpp_base=lcpp_base,
        fast_model=os.environ.get("LOCAL_FAST_MODEL", "local-worker:latest"),
        balanced_model=os.environ.get("LOCAL_BALANCED_MODEL", "local-uncensored:latest"),
        heavy_model=os.environ.get("HEAVY_MOE_MODEL_ID", "heavy-moe-uncensored"),
        embedding_model=os.environ.get("LOCAL_EMBEDDING_MODEL", "nomic-embed-text"),
        default_max_tokens_per_workflow=max(1, max_tok_cap),
        default_max_cost_usd_per_workflow=max(0.0, max_cost_cap),
    )


SETTINGS = load_settings()
HOST = SETTINGS.host
PORT = SETTINGS.port
OLLAMA_HOST, OLLAMA_PORT, OLLAMA_BASE = SETTINGS.ollama_host, SETTINGS.ollama_port, SETTINGS.ollama_base
HEAVY_HOST, HEAVY_PORT, HEAVY_BASE = SETTINGS.heavy_host, SETTINGS.heavy_port, SETTINGS.heavy_base
LMS_HOST, LMS_PORT, LMS_BASE = SETTINGS.lms_host, SETTINGS.lms_port, SETTINGS.lms_base
LLAMACPP_HOST, LLAMACPP_PORT, LLAMACPP_BASE = (
    SETTINGS.llamacpp_host,
    SETTINGS.llamacpp_port,
    SETTINGS.llamacpp_base,
)
