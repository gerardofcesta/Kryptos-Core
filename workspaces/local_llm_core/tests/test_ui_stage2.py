"""Comprehensive Stage 2 UI, Desktop Shell (pywebview private_mode=True), Mutex & Inno Setup tests."""

from __future__ import annotations

import os
import pathlib
import re
import shutil
import subprocess
import tempfile
from typing import Any
from unittest import mock

import pytest
from fastapi.testclient import TestClient
from workspaces.local_llm_core.desktop_shell import compile_inno_script, find_iscc
from workspaces.local_llm_core.app import create_app
from workspaces.local_llm_core.config import PROJECT_ROOT, WEB_DIST_DIR
from workspaces.local_llm_core.desktop import (
    APP_NAME,
    APP_PATHS_SCHEMA_VERSION,
    MUTEX_NAME,
    build_desktop_config,
    get_app_paths,
    get_models_dir,
    launch,
)
from workspaces.local_llm_core.security import get_gateway_token

_URL_RE = re.compile(r"https?://([^\s\"'`<>)]+)")
_ALLOWED_XML_NAMESPACES = frozenset(
    {
        "http://www.w3.org/1998/Math/MathML",
        "http://www.w3.org/1999/xhtml",
        "http://www.w3.org/1999/xlink",
        "http://www.w3.org/2000/svg",
        "http://www.w3.org/XML/1998/namespace",
    }
)


def test_stage2_web_dist_exists_and_contains_zero_external_urls() -> None:
    """Verify compiled React+TS+Vite+Tailwind SPA exists in web/dist/ with zero external URLs."""
    dist_index = WEB_DIST_DIR / "index.html"
    assert dist_index.is_file(), f"Missing compiled SPA entry: {dist_index}"
    assert (WEB_DIST_DIR / "static" / "app.js").is_file()
    assert (WEB_DIST_DIR / "static" / "styles.css").is_file()

    scanned_files = [
        p
        for p in WEB_DIST_DIR.rglob("*")
        if p.is_file() and p.suffix.lower() in {".html", ".js", ".mjs", ".css", ".json"}
    ]
    assert len(scanned_files) >= 3

    external_hits: list[str] = []
    for fpath in scanned_files:
        text = fpath.read_text(encoding="utf-8", errors="replace")
        for match in _URL_RE.finditer(text):
            full_url = match.group(0)
            if full_url in _ALLOWED_XML_NAMESPACES:
                continue
            host_part = match.group(1).split("/")[0].split("?")[0].split("#")[0].split(":")[0]
            if host_part not in {"127.0.0.1", "localhost", "[::1]"}:
                external_hits.append(f"{fpath.name}: {full_url}")

    assert external_hits == [], f"External URLs detected in web/dist/: {external_hits}"


def test_stage2_spa_served_at_root_with_no_store_and_strict_csp(tmp_path: pathlib.Path) -> None:
    """Verify `/` serves compiled `web/dist/index.html` with no-store and zero `'unsafe-inline'`."""
    token_file = tmp_path / "LocalLLM" / "gateway.token"
    with mock.patch.dict(
        os.environ,
        {"LOCAL_LLM_GATEWAY_TOKEN_FILE": str(token_file)},
        clear=False,
    ):
        app = create_app()
        with TestClient(app, base_url="http://127.0.0.1:8787") as client:
            token = get_gateway_token()
            r_root = client.get("/")
            assert r_root.status_code == 200
            assert "no-store" in r_root.headers.get("cache-control", "").lower()

            csp = r_root.headers.get("content-security-policy", "")
            assert "default-src 'self'" in csp
            assert "script-src 'self'" in csp
            assert "style-src 'self'" in csp
            assert "'unsafe-inline'" not in csp
            assert "object-src 'none'" in csp
            assert "frame-ancestors 'none'" in csp

            expected_html = (WEB_DIST_DIR / "index.html").read_bytes().decode("utf-8")
            assert r_root.text == expected_html
            assert '<div id="root"></div>' in r_root.text
            assert "/static/app.js" in r_root.text
            assert "/static/styles.css" in r_root.text

            r_js = client.get("/static/app.js")
            assert r_js.status_code == 200
            assert "no-store" in r_js.headers.get("cache-control", "").lower()
            assert "/v1/chat/completions" in r_js.text
            assert "/api/rag/ingest" in r_js.text

            r_css = client.get("/static/styles.css")
            assert r_css.status_code == 200
            assert "no-store" in r_css.headers.get("cache-control", "").lower()

            # Verify direct /api/orchestrator/code-audit endpoint works cleanly
            dirty_dir = tmp_path / "dirty_repo"
            dirty_dir.mkdir()
            (dirty_dir / "app.py").write_text("import totally_fake_slopsquat_pkg_xyz\n", encoding="utf-8")
            r_audit = client.post(
                "/api/orchestrator/code-audit",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"target_path": str(dirty_dir), "use_gitleaks": False},
            )
            assert r_audit.status_code == 200
            audit_json = r_audit.json()
            assert audit_json["ok"] is True
            assert audit_json["passed"] is False
            assert audit_json["slopsquatting"]["findings"]


def test_desktop_shell_private_mode_ephemeral_cache_and_mutex(tmp_path: pathlib.Path) -> None:
    """Verify desktop.py enforces private_mode=True, ephemeral_cache=True, and unified mutex."""
    assert APP_NAME == "KryptosCore"
    assert MUTEX_NAME == r"Local\KryptosCore_SingleInstance_v1"

    ext_models = tmp_path / "ExternalModels87G"
    ext_models.mkdir()
    local_app = tmp_path / "LocalAppData"
    token_file = local_app / "LocalLLM" / "gateway.token"

    with mock.patch.dict(
        os.environ,
        {
            "LOCALAPPDATA": str(local_app),
            "LOCAL_LLM_MODELS_DIR": str(ext_models),
            "LOCAL_LLM_GATEWAY_TOKEN_FILE": str(token_file),
        },
        clear=False,
    ):
        paths = get_app_paths(ensure=True)
        assert paths.schema_version == APP_PATHS_SCHEMA_VERSION
        assert paths.ephemeral_cache is True
        assert paths.cache_dir is None
        assert paths.models_dir == ext_models
        assert get_models_dir() == ext_models

        cfg = build_desktop_config(paths=paths, preferred_port=0)
        assert cfg.private_mode is True
        assert cfg.storage_path is None
        assert cfg.host == "127.0.0.1"
        assert cfg.health_path == "/"

        # Reject private_mode=False
        unsafe_cfg = build_desktop_config(paths=paths, preferred_port=0, private_mode=False)
        with pytest.raises(ValueError, match="private_mode=True"):
            launch(config=unsafe_cfg, use_mutex=False)

        # Headless launch with window_factory & start_hook verifies embedded server + private_mode
        captured: dict[str, Any] = {}

        def fake_window_factory(**kwargs: Any) -> dict[str, Any]:
            captured["window_kwargs"] = kwargs
            return {"window": True}

        def fake_start_hook(window: Any, **kwargs: Any) -> None:
            captured["window"] = window
            captured["start_kwargs"] = kwargs

        rc = launch(
            config=cfg,
            window_factory=fake_window_factory,
            start_hook=fake_start_hook,
            use_mutex=True,
        )
        assert rc == 0
        assert captured["window_kwargs"]["private_mode"] is True
        assert captured["start_kwargs"]["private_mode"] is True
        assert captured["start_kwargs"]["storage_path"] is None
        assert str(captured["window_kwargs"]["url"]).startswith("http://127.0.0.1:")


def test_tray_app_uses_unified_mutex_and_parses_cleanly() -> None:
    """Verify launcher/tray-app.ps1 uses Local\\KryptosCore_SingleInstance_v1 with zero AST errors."""
    tray_path = PROJECT_ROOT / "launcher" / "tray-app.ps1"
    assert tray_path.is_file()
    tray_raw = tray_path.read_text(encoding="utf-8", errors="replace")
    assert r"Local\KryptosCore_SingleInstance_v1" in tray_raw
    assert "LocalLLM_TrayController_v1" not in tray_raw

    ps_exe = shutil.which("pwsh") or shutil.which("powershell")
    if not ps_exe:
        return

    ps_cmd = (
        "$errors = $null; $tokens = $null; "
        f"[void][System.Management.Automation.Language.Parser]::ParseFile('{tray_path}', [ref]$tokens, [ref]$errors); "
        "if ($errors.Count -gt 0) { $errors | ForEach-Object { Write-Error $_.Message }; exit 1 }"
    )
    proc = subprocess.run(
        [ps_exe, "-NoProfile", "-Command", ps_cmd],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout


def test_inno_setup_real_iscc_compile_smoke(tmp_path: pathlib.Path) -> None:
    """Run real ISCC.exe compile smoke test against installer/kryptos_core.iss in tmp_path."""
    repo_iss = PROJECT_ROOT / "installer" / "kryptos_core.iss"
    assert repo_iss.is_file(), f"Missing Inno Setup script: {repo_iss}"
    iss_text = repo_iss.read_text(encoding="utf-8")

    assert "PrivilegesRequired=lowest" in iss_text
    assert r"AppMutex=Local\KryptosCore_SingleInstance_v1" in iss_text
    assert "CloseApplications=force" in iss_text

    if os.name != "nt":
        pytest.skip("Inno Setup ISCC.exe compilation runs on Windows hosts")
    iscc_path = find_iscc()
    if iscc_path is None or not iscc_path.is_file():
        pytest.skip("ISCC.exe not installed on this Windows host")

    # Mirror installer/ + dist/KryptosCore/ inside isolated tmp_path so repo remains clean
    fake_dist = tmp_path / "dist" / "KryptosCore"
    fake_dist.mkdir(parents=True)
    (fake_dist / "KryptosCore.exe").write_bytes(b"MZ_SMOKE_STUB")
    (fake_dist / "web" / "dist").mkdir(parents=True)
    shutil.copy2(WEB_DIST_DIR / "index.html", fake_dist / "web" / "dist" / "index.html")

    iss_copy = tmp_path / "installer" / "kryptos_core.iss"
    iss_copy.parent.mkdir(parents=True)
    iss_copy.write_text(iss_text, encoding="utf-8")

    out_dir = iss_copy.parent / "out"
    compiled_exe = compile_inno_script(iss_copy, iscc_path=iscc_path, output_dir=out_dir)
    assert compiled_exe.is_file() and compiled_exe.stat().st_size > 0
    assert compiled_exe.name.startswith("KryptosCore-Setup-")
