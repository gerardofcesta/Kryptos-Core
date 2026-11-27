"""WU-25 verification tests: standalone portability, total scrub, IDE configs, and native shell."""

from __future__ import annotations

import io
import os
import pathlib
import re
import subprocess
import zipfile
from typing import Any
from unittest import mock

import pytest
from fastapi.testclient import TestClient
from workspaces.local_llm_core.app import create_app
from workspaces.local_llm_core.config import PROJECT_ROOT
from workspaces.local_llm_core.desktop import build_desktop_config, launch
from workspaces.local_llm_core.rag.store import RagStoreManager
from workspaces.local_llm_core.security import get_gateway_token


def _decode(codes: tuple[int, ...]) -> str:
    return "".join(chr(c) for c in codes)


def _git_tracked_files() -> list[str]:
    out = subprocess.check_output(
        ["git", "-C", str(PROJECT_ROOT), "ls-files"],
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return [line.strip() for line in out.splitlines() if line.strip()]


def test_zero_external_shared_references_across_repo() -> None:
    """Ensure zero `_shared` or `windows-desktop-kit` monorepo references exist in tracked files."""
    tracked = _git_tracked_files()
    shared_token = _decode((95, 115, 104, 97, 114, 101, 100))
    wdk_py = _decode((119, 105, 110, 100, 111, 119, 115, 95, 100, 101, 115, 107, 116, 111, 112, 95, 107, 105, 116))
    wdk_js = _decode((119, 105, 110, 100, 111, 119, 115, 45, 100, 101, 115, 107, 116, 111, 112, 45, 107, 105, 116))
    rx = re.compile(
        "|".join(re.escape(tok) for tok in (shared_token, wdk_py, wdk_js)),
        re.IGNORECASE,
    )

    hits: list[str] = []
    for rel in tracked:
        if rel == "workspaces/local_llm_core/tests/test_wu25_standalone_scrub.py":
            continue
        p = PROJECT_ROOT / rel
        if not p.is_file():
            continue
        text = p.read_text(encoding="utf-8", errors="ignore")
        for idx, line in enumerate(text.splitlines(), 1):
            if rx.search(line):
                hits.append(f"{rel}:{idx}: {line.strip()[:120]}")

    assert hits == [], f"External monorepo references detected: {hits}"

    # Verify .venv has no editable .pth pointing outside repo
    site_pkgs = PROJECT_ROOT / ".venv" / "Lib" / "site-packages"
    if site_pkgs.is_dir():
        pth_hits: list[str] = []
        for pth in site_pkgs.glob("*.pth"):
            content = pth.read_text(encoding="utf-8", errors="ignore")
            if shared_token in content.lower():
                pth_hits.append(f"{pth.name}: {content.strip()}")
        assert pth_hits == [], f"External .pth references found in .venv: {pth_hits}"


def test_zero_personal_hardware_or_multirepo_leaks_in_all_tracked_files() -> None:
    """Verify 0 personal username, private workstation, or internal multi-project leaks in tracked files."""
    tracked = _git_tracked_files()
    assert "config/hardware-profile.json" not in tracked
    assert "config/hardware-profile.example.json" in tracked
    assert "pyproject.toml" in tracked

    forbidden_terms = [
        _decode((103, 101, 114, 97, 114)),  # personal username
        _decode((50, 54, 53, 75, 70)),  # private CPU SKU
        _decode((53, 48, 55, 48, 32, 84, 105)),  # private GPU SKU
        _decode((67, 77, 72, 51, 50, 71, 88)),  # private RAM SKU
        _decode((57, 57, 48, 32, 80, 82, 79)),  # private SSD
        _decode((57, 55, 48, 32, 69, 86, 79)),  # secondary SSD
        _decode((54, 50, 49, 46, 50, 52)),  # private GPU driver
        _decode((50, 54, 51, 48, 48)),  # private Windows build
        _decode((67, 58, 92, 80, 114, 111, 106, 101, 99, 116, 115)),  # Windows Projects path
        _decode((67, 58, 47, 80, 114, 111, 106, 101, 99, 116, 115)),  # POSIX Projects path
        _decode((67, 111, 110, 116, 114, 97, 116, 116, 111)),  # internal contract term
        _decode((75, 108, 97, 115, 116, 111, 115)),  # internal sibling project 1
        _decode((75, 97, 105, 114, 111, 115)),  # internal sibling project 2
        _decode((67, 111, 109, 112, 101, 110, 100, 105, 111)),  # internal sibling project 3
        _decode((50, 55, 66, 32, 79, 108, 108, 97, 109, 97)),  # hardcoded 27B splash
        _decode((49, 55, 55, 66, 32, 67, 111, 108, 105, 98, 114, 105)),  # hardcoded 177B splash
    ]
    rx = re.compile("|".join(re.escape(t) for t in forbidden_terms), re.IGNORECASE)

    leaks: list[str] = []
    for rel in tracked:
        if rel == "workspaces/local_llm_core/tests/test_wu25_standalone_scrub.py":
            continue
        p = PROJECT_ROOT / rel
        if not p.is_file():
            continue
        text = p.read_text(encoding="utf-8", errors="ignore")
        for idx, line in enumerate(text.splitlines(), 1):
            if rx.search(line):
                leaks.append(f"{rel}:{idx}: {line.strip()[:120]}")

    assert leaks == [], f"Sensitive/personal/internal leaks found: {leaks}"


def test_ide_configs_endpoint_generates_safe_snippets(tmp_path: pathlib.Path) -> None:
    """Verify GET /api/system/ide-configs covers all 5 tools and never exposes real gateway.token."""
    token_file = tmp_path / "LocalLLM" / "gateway.token"
    with mock.patch.dict(
        os.environ,
        {"LOCAL_LLM_GATEWAY_TOKEN_FILE": str(token_file)},
        clear=False,
    ):
        app = create_app()
        with TestClient(app, base_url="http://127.0.0.1:8787") as client:
            token = get_gateway_token()
            r_unauth = client.get("/api/system/ide-configs")
            assert r_unauth.status_code == 401

            r_ok = client.get(
                "/api/system/ide-configs",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert r_ok.status_code == 200
            payload = r_ok.json()
            assert payload["ok"] is True
            assert payload["openaiBaseUrl"] == "http://127.0.0.1:8787/v1"
            assert payload["mcpUrl"] == "http://127.0.0.1:8787/mcp"
            assert payload["tokenPlaceholder"] == "<YOUR_GATEWAY_TOKEN>"

            configs = {item["id"]: item for item in payload["configs"]}
            assert {"continue", "cline", "cursor", "openai_sdk", "mcp"} <= set(configs.keys())
            for cid, item in configs.items():
                assert token not in item["snippet"], f"Real token leaked in {cid} snippet!"
                assert "<YOUR_GATEWAY_TOKEN>" in item["snippet"]
            assert "/mcp" in configs["mcp"]["snippet"]
            assert "/v1" in configs["openai_sdk"]["snippet"]


def test_standalone_rag_ingestion_for_text_and_docx(tmp_path: pathlib.Path) -> None:
    """Verify standalone RAG ingestion handles txt/md/docx purely in memory without external packages."""
    mgr = RagStoreManager(vault_root=tmp_path / "vault")

    res_md = mgr.ingest("# Local Architecture\nStandalone Ghost RAG verification token.", "notes.md", mode="ghost")
    assert res_md["disk_writes"] == 0
    assert res_md["chunk_count"] >= 1
    hits_md = mgr.query("Standalone Ghost verification", mode="ghost")
    assert len(hits_md) == 1
    assert "[notes.md" in hits_md[0].citation

    # Build a minimal valid .docx archive in memory
    docx_buf = io.BytesIO()
    with zipfile.ZipFile(docx_buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "word/document.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:body><w:p><w:r><w:t>Offline DOCX compliance checkpoint alpha77.</w:t></w:r></w:p></w:body>"
            "</w:document>",
        )
    res_docx = mgr.ingest(docx_buf.getvalue(), "policy.docx", mode="ghost", collection="docx-col")
    assert res_docx["chunk_count"] >= 1
    hits_docx = mgr.query("alpha77", mode="ghost", collection="docx-col")
    assert len(hits_docx) == 1
    assert "alpha77" in hits_docx[0].text


def test_desktop_attach_url_enforces_loopback_and_private_mode(tmp_path: pathlib.Path) -> None:
    """Verify desktop.launch --attach-url strictly enforces loopback and private_mode=True."""
    cfg = build_desktop_config(preferred_port=0, private_mode=True)
    with pytest.raises(ValueError, match="loopback"):
        launch(config=cfg, attach_url="https://example.com:8787", use_mutex=False)

    seen: dict[str, Any] = {}

    def fake_win(**kwargs: Any) -> dict[str, Any]:
        seen["win"] = kwargs
        return {"ok": True}

    def fake_start(win: Any, **kwargs: Any) -> None:
        seen["start"] = kwargs

    rc = launch(
        config=cfg,
        attach_url="http://127.0.0.1:8787",
        window_factory=fake_win,
        start_hook=fake_start,
        use_mutex=False,
    )
    assert rc == 0
    assert seen["win"]["private_mode"] is True
    assert seen["start"]["private_mode"] is True
    assert seen["win"]["url"] == "http://127.0.0.1:8787"
