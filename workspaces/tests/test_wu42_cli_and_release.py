"""WU-42 — Headless CLI, Multi-OS Release Manifest, Workflows & Public Release Gate Tests."""

from __future__ import annotations

import getpass
import json
import pathlib
import sys
import zipfile
from unittest import mock

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from workspaces.local_llm_core import cli


def test_cli_doctor_json_and_support_bundle_redacts_pii(tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli.main(["doctor", "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["ok"] is True
    assert data["tier_plan"]["tier_id"] in ("tier_a", "tier_b", "tier_c", "tier_d", "tier_e")

    zip_path = tmp_path / "support_bundle.zip"
    rc_zip = cli.main(["doctor", "--support-bundle", str(zip_path)])
    assert rc_zip == 0
    assert zip_path.is_file()

    home_str = str(pathlib.Path.home())
    username = getpass.getuser()
    with zipfile.ZipFile(zip_path, "r") as zf:
        names = set(zf.namelist())
        assert {"doctor_report.json", "system_summary.txt"} <= names
        for fname in ("doctor_report.json", "system_summary.txt"):
            txt = zf.read(fname).decode("utf-8")
            assert home_str not in txt
            if len(username) >= 2:
                assert username.lower() not in txt.lower()


def test_cli_models_list_and_discover(capsys: pytest.CaptureFixture[str]) -> None:
    with mock.patch(
        "workspaces.local_llm_core.services.model_manager._http_json",
        return_value=(False, 0, {}, "offline"),
    ):
        rc_list = cli.main(["models", "list"])
    assert rc_list == 0
    list_out = capsys.readouterr().out
    assert "SIZE_GB" in list_out

    with mock.patch(
        "workspaces.local_llm_core.services.model_manager.discover_local_models",
        return_value={"models": [{"source": "ollama", "name": "qwen2.5:7b", "size_gb": 4.7, "hardware_fit": {"emoji": "🟢", "label": "GPU"}}]},
    ):
        rc_disc = cli.main(["models", "discover"])
    assert rc_disc == 0
    disc_out = capsys.readouterr().out
    assert "Zero-Copy Local Discovery" in disc_out


def test_cli_serve_rejects_non_loopback_host() -> None:
    rc = cli.main(["serve", "--host", "0.0.0.0"])
    assert rc != 0


def test_cli_chat_discovers_runtime_and_sends_request(tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]) -> None:
    token_file = tmp_path / "gateway.token"
    token_file.write_text("cli-test-token", encoding="utf-8")
    captured: dict[str, object] = {}

    class _FakeResp:
        status_code = 200
        text = "{}"

        def json(self) -> dict[str, object]:
            return {"choices": [{"message": {"content": "Hello from headless CLI!"}}]}

    class _FakeClient:
        def __init__(self, *, base_url: str, timeout: float) -> None:
            captured["base_url"] = base_url

        def __enter__(self) -> _FakeClient:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def post(self, path: str, *, headers: dict[str, str], json: dict[str, object]) -> _FakeResp:
            captured["path"] = path
            captured["headers"] = headers
            captured["json"] = json
            return _FakeResp()

    with (
        mock.patch.dict("os.environ", {"LOCAL_LLM_GATEWAY_TOKEN_FILE": str(token_file)}),
        mock.patch(
            "workspaces.local_llm_core.runtime_state.read_active_runtime",
            return_value={"base_url": "http://127.0.0.1:49123", "port": 49123},
        ),
        mock.patch("httpx.Client", _FakeClient),
    ):
        rc = cli.main(["chat", "--firewall", "hello"])
    assert rc == 0
    assert captured["base_url"] == "http://127.0.0.1:49123"
    assert captured["path"] == "/v1/chat/completions"
    assert captured["headers"]["X-Prompt-Firewall"] == "true"  # type: ignore[index]
    assert "Hello from headless CLI!" in capsys.readouterr().out


def test_release_manifest_sbom_workflows_and_sanitizer(tmp_path: pathlib.Path) -> None:
    from scripts import generate_release_manifest as grm
    from scripts import prepare_public_release as ppr

    art_dir = tmp_path / "out"
    art_dir.mkdir()
    sample = art_dir / "KryptosCore-v3.0.0-linux-x86_64.tar.gz"
    sample.write_bytes(b"offline-release-artifact")

    rc = grm.main(["--artifacts-dir", str(art_dir), "--repo-root", str(REPO_ROOT), "--version", "3.0.0"])
    assert rc == 0
    sums_text = (art_dir / "SHA256SUMS.txt").read_text(encoding="utf-8")
    assert "KryptosCore-v3.0.0-linux-x86_64.tar.gz" in sums_text
    assert len(sums_text.split()[0]) == 64

    sbom = json.loads((art_dir / "sbom.cyclonedx.json").read_text(encoding="utf-8"))
    assert sbom["bomFormat"] == "CycloneDX"
    assert sbom["specVersion"] == "1.5"
    assert sbom["metadata"]["component"]["name"] == "kryptos-core"
    assert len(sbom["components"]) >= 10

    for rel in (
        "scripts/build_linux.sh",
        "scripts/install.sh",
        "scripts/build_macos.sh",
        "scripts/build_dist.ps1",
        "scripts/generate_release_manifest.py",
        "scripts/prepare_public_release.py",
        ".github/workflows/ci.yml",
        ".github/workflows/release.yml",
    ):
        fpath = REPO_ROOT / rel
        assert fpath.is_file() and fpath.stat().st_size > 100, f"Missing or empty release file: {rel}"

    # Verify prepare_public_release secret detector catches synthetic token without storing literal in source
    fake_tok = "sk-" + "live_" + "Z9y8X7w6V5u4T3s2R1q0"
    assert ppr.scan_text_for_secrets(fake_tok)
    assert ppr.check_file_sizes_and_binaries(REPO_ROOT) == []

