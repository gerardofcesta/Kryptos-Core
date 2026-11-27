"""Tests for KRYPTOS_PROMPT_FIREWALL env alias and offline M&A Financial Due Diligence example."""

from __future__ import annotations

import importlib.util
import pathlib

import pytest

from workspaces.local_llm_core.prompt_firewall import is_prompt_firewall_enabled

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "examples" / "financial-due-diligence" / "run_nda_due_diligence.py"


def test_kryptos_prompt_firewall_env_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LOCAL_LLM_PROMPT_FIREWALL", raising=False)
    monkeypatch.setenv("KRYPTOS_PROMPT_FIREWALL", "1")
    assert is_prompt_firewall_enabled(None) is True
    assert is_prompt_firewall_enabled("false") is False


def test_nda_financial_due_diligence_end_to_end(tmp_path: pathlib.Path) -> None:
    spec = importlib.util.spec_from_file_location("run_nda_due_diligence", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    memo_out = tmp_path / "audit_memo.md"
    report = mod.run_audit(write_memo_path=memo_out)

    assert report["ok"] is True
    assert report["residual_hits_after_wipe"] == 0
    assert report["filing_redactions"]["iban"] >= 1
    assert report["filing_redactions"]["codice_fiscale"] >= 1
    assert report["filing_redactions"]["email"] >= 2
    assert report["prompt_redactions"]["iban"] == 1
    assert memo_out.is_file()
    memo_txt = memo_out.read_text(encoding="utf-8")
    assert "EUR 28.2M" in memo_txt
    assert "EUR 59.2M" in memo_txt
    assert "[REDACTED:IBAN]" in memo_txt
    assert "IT60X0542811101000000123456" not in memo_txt.split("Sanitized Prompt Dispatched")[1]
