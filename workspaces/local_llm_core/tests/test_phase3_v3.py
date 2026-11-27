"""Comprehensive Phase 3 tests: Ghost/Vault RAG, Air-Gapped ModelManager, Orchestrator code_audit, Protected /mcp, and Hardware Setup Wizard."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import pathlib
import subprocess
import tempfile
import time
import unittest
from unittest import mock

import pymupdf
from fastapi.testclient import TestClient
from workspaces.local_llm_core.app import create_app
from workspaces.local_llm_core.config import PROJECT_ROOT
from workspaces.local_llm_core.rag.store import RagStoreManager, get_rag_manager
from workspaces.local_llm_core.routes.mcp import get_mcp_audit_logger
from workspaces.local_llm_core.security import rotate_gateway_token
from workspaces.local_llm_core.services.hardware_wizard import (
    recommend_hardware_profile,
    validate_models_dir_path,
)
from workspaces.local_llm_core.services.model_manager import (
    get_airgapped_inventory,
    verify_model_sha256,
)
from workspaces.orchestrator.engine.code_audit import (
    analyze_slopsquatting,
    run_code_audit,
    scan_secrets,
)
from workspaces.orchestrator.engine.schemas import RunRequest
from workspaces.orchestrator.engine.state_machine import OrchestratorManager
from workspaces.orchestrator.engine.tier_switcher import TierSwitcher


class TestPhase3V3(unittest.TestCase):
    """End-to-end verification of WU-16 Phase 3 modules and endpoints."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_root = pathlib.Path(self._tmpdir.name)
        self._token_file = self.tmp_root / "LocalLLM" / "gateway.token"
        self._models_dir = self.tmp_root / "external_models"
        self._models_dir.mkdir(parents=True, exist_ok=True)
        self._vault_rag_dir = self.tmp_root / "rag_vault"

        self._env_patch = mock.patch.dict(
            os.environ,
            {
                "LOCAL_LLM_GATEWAY_TOKEN_FILE": str(self._token_file),
                "LOCAL_LLM_MODELS_DIR": str(self._models_dir),
                "LOCAL_LLM_RAG_VAULT_DIR": str(self._vault_rag_dir),
            },
            clear=False,
        )
        self._env_patch.start()
        get_rag_manager().ghost.clear()
        get_mcp_audit_logger().clear()
        self.app = create_app()
        self.token = rotate_gateway_token()
        self.client = TestClient(self.app, base_url="http://127.0.0.1:8787")

    def tearDown(self) -> None:
        get_rag_manager().ghost.clear()
        get_mcp_audit_logger().clear()
        self.client.close()
        self._env_patch.stop()
        self._tmpdir.cleanup()

    def _auth_headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        hdrs = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        if extra:
            hdrs.update(extra)
        return hdrs

    def _vault_files_in_repo(self) -> list[pathlib.Path]:
        vault_dir = PROJECT_ROOT / "workspaces" / "vault"
        if not vault_dir.exists():
            return []
        return [
            p
            for p in vault_dir.rglob("*")
            if p.is_file() and p.name != ".gitkeep"
        ]

    # ------------------------------------------------------------------
    # 1. Ghost RAG (RAM-only, <2s, 0 disk writes) & Isolated Vault RAG
    # ------------------------------------------------------------------
    def test_ghost_rag_ingest_query_under_2s_zero_disk_and_clear(self) -> None:
        # Build an in-memory multi-page PDF to test full doc-ingest-core PDF pipeline
        pdf_doc = pymupdf.open()
        p1 = pdf_doc.new_page()
        p1.insert_text((72, 72), "Section 1: Local LLM Gateway\nLoopback security token policy.")
        p2 = pdf_doc.new_page()
        p2.insert_text(
            (72, 72),
            "Section 2: Quantum Cryptography\nPost-quantum lattice key exchange protocol ML-KEM.",
        )
        pdf_bytes = pdf_doc.tobytes()
        pdf_doc.close()

        t0 = time.perf_counter()
        r_ingest = self.client.post(
            "/api/rag/ingest",
            headers=self._auth_headers(),
            json={
                "filename": "architecture_spec.pdf",
                "content_base64": base64.b64encode(pdf_bytes).decode("ascii"),
                "mode": "ghost",
                "collection": "ghost-session",
                "enable_ocr": False,
            },
        )
        self.assertEqual(r_ingest.status_code, 200, r_ingest.text)
        ing_data = r_ingest.json()
        self.assertTrue(ing_data["ok"])
        self.assertEqual(ing_data["mode"], "ghost")
        self.assertEqual(ing_data["disk_writes"], 0)
        self.assertGreaterEqual(ing_data["chunk_count"], 1)

        r_query = self.client.post(
            "/api/rag/query",
            headers=self._auth_headers(),
            json={
                "query": "post-quantum lattice ML-KEM",
                "mode": "ghost",
                "collection": "ghost-session",
                "top_k": 3,
            },
        )
        elapsed = time.perf_counter() - t0
        self.assertEqual(r_query.status_code, 200, r_query.text)
        self.assertLess(elapsed, 2.0, f"Ghost RAG ingest+query took {elapsed:.3f}s (>= 2.0s)")

        q_data = r_query.json()
        self.assertGreaterEqual(q_data["count"], 1)
        top_hit = q_data["results"][0]
        self.assertIn("ML-KEM", top_hit["text"])
        self.assertIn("architecture_spec.pdf", top_hit["citation"])

        # ZERO files written to repo workspaces/vault or rag_vault in Ghost mode
        self.assertEqual(self._vault_files_in_repo(), [])
        self.assertFalse(self._vault_rag_dir.exists())

        # Clear wipes RAM cleanly
        r_clear = self.client.post(
            "/api/rag/clear",
            headers=self._auth_headers(),
            json={"mode": "ghost", "collection": "ghost-session"},
        )
        self.assertEqual(r_clear.status_code, 200)
        self.assertGreaterEqual(r_clear.json()["cleared_chunks"], 1)

        r_after = self.client.post(
            "/api/rag/query",
            headers=self._auth_headers(),
            json={"query": "ML-KEM", "mode": "ghost", "collection": "ghost-session"},
        )
        self.assertEqual(r_after.status_code, 200)
        self.assertEqual(r_after.json()["count"], 0)

    def test_ghost_rag_ttl_expiry_and_vault_isolation_in_tmp_path(self) -> None:
        mgr = RagStoreManager(vault_root=self._vault_rag_dir, default_ttl_seconds=0.1)
        mgr.ingest(
            "# Ephemeral Secret\nTemporary Ghost memory payload alpha99.",
            "secret.md",
            mode="ghost",
            collection="ephem",
            ttl_seconds=0.05,
        )
        self.assertEqual(len(mgr.query("alpha99", mode="ghost", collection="ephem")), 1)
        time.sleep(0.08)
        self.assertEqual(len(mgr.query("alpha99", mode="ghost", collection="ephem")), 0)

        # Vault mode persists under isolated tmp_path and survives manager re-instantiation
        mgr.ingest(
            "# Vault Knowledge\nDeterministic persistent citation test beta42.",
            "vault_notes.md",
            mode="vault",
            collection="project-alpha",
        )
        col_dir = self._vault_rag_dir / "project-alpha"
        self.assertTrue((col_dir / "chunks.json").is_file())
        self.assertTrue((col_dir / "documents.json").is_file())

        mgr_reopened = RagStoreManager(vault_root=self._vault_rag_dir)
        hits = mgr_reopened.query("citation beta42", mode="vault", collection="project-alpha")
        self.assertEqual(len(hits), 1)
        self.assertIn("[vault_notes.md · p.1]", hits[0].citation)

        # Path traversal & ORCHESTRATOR_ALLOWED_ROOTS enforcement
        with self.assertRaises(ValueError):
            mgr_reopened.query("beta42", mode="vault", collection="../escape")

        other_root = self.tmp_root / "only_here"
        other_root.mkdir()
        with mock.patch.dict(os.environ, {"ORCHESTRATOR_ALLOWED_ROOTS": str(other_root)}):
            with self.assertRaises(PermissionError):
                mgr_reopened.ingest("blocked", "doc.txt", mode="vault", collection="blocked")

        # Clean up vault collection via clear
        cleared = mgr_reopened.clear(mode="vault", collection="project-alpha")
        self.assertGreaterEqual(cleared["cleared_chunks"], 1)
        self.assertFalse(col_dir.exists())

    # ------------------------------------------------------------------
    # 2. Air-Gapped ModelManager & Offline SHA-256 Verification
    # ------------------------------------------------------------------
    def test_airgapped_model_manager_inventory_and_sha256_verification(self) -> None:
        model_file = self._models_dir / "qwen3-14b-q5.gguf"
        payload_bytes = b"GGUF_MOCK_WEIGHTS_V3_" * 128
        model_file.write_bytes(payload_bytes)
        expected_hash = hashlib.sha256(payload_bytes).hexdigest()

        manifest = {"files": {"qwen3-14b-q5.gguf": expected_hash}}
        (self._models_dir / "manifest.sha256.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        (self._models_dir / "custom.Modelfile").write_text(
            "FROM qwen3-14b-q5.gguf\nPARAMETER num_ctx 32768\n", encoding="utf-8"
        )

        r_inv = self.client.get(
            "/api/models/inventory", headers={"Authorization": f"Bearer {self.token}"}
        )
        self.assertEqual(r_inv.status_code, 200, r_inv.text)
        inv = r_inv.json()
        self.assertTrue(inv["air_gapped"])
        self.assertEqual(inv["models_dir_source"], "LOCAL_LLM_MODELS_DIR")
        self.assertEqual(inv["count"], 1)
        self.assertEqual(inv["models"][0]["manifest_sha256"], expected_hash)
        self.assertTrue(any(mf["num_ctx"] == 32768 for mf in inv["modelfiles"]))

        # Valid verification (using manifest SHA-256)
        r_ok = self.client.post(
            "/api/models/verify",
            headers=self._auth_headers(),
            json={"path": "qwen3-14b-q5.gguf"},
        )
        self.assertEqual(r_ok.status_code, 200)
        self.assertTrue(r_ok.json()["verified"])
        self.assertEqual(r_ok.json()["status"], "VERIFIED")

        # Tampered model file fails verification deterministically
        model_file.write_bytes(b"TAMPERED_WEIGHTS_" * 128)
        r_bad = self.client.post(
            "/api/models/verify",
            headers=self._auth_headers(),
            json={"path": "qwen3-14b-q5.gguf"},
        )
        self.assertEqual(r_bad.status_code, 200)
        self.assertFalse(r_bad.json()["verified"])
        self.assertEqual(r_bad.json()["status"], "MISMATCH")

        # Path traversal rejected with 403
        r_trav = self.client.post(
            "/api/models/verify",
            headers=self._auth_headers(),
            json={"path": "../LocalLLM/gateway.token", "expected_sha256": expected_hash},
        )
        self.assertEqual(r_trav.status_code, 403)

        # Direct unit helper check
        inv_direct = get_airgapped_inventory(self._models_dir)
        self.assertEqual(inv_direct["count"], 1)
        with self.assertRaises(PermissionError):
            verify_model_sha256("../../etc/passwd", expected_hash, models_dir=self._models_dir)

    # ------------------------------------------------------------------
    # 3. Orchestrator code_audit (Anti-Slopsquatting & Secret Scan)
    # ------------------------------------------------------------------
    def test_code_audit_catches_slopsquatting_and_secrets_while_passing_clean_repo(self) -> None:
        clean_repo = self.tmp_root / "clean_repo"
        clean_repo.mkdir()
        (clean_repo / "helper.py").write_text("def add(a: int, b: int) -> int:\n    return a + b\n", encoding="utf-8")
        (clean_repo / "app.py").write_text(
            "import json\nimport pathlib\nfrom fastapi import FastAPI\nimport helper\n",
            encoding="utf-8",
        )
        (clean_repo / "requirements.txt").write_text("fastapi>=0.115\npydantic>=2.0\n", encoding="utf-8")

        clean_res = run_code_audit(clean_repo, use_gitleaks=False)
        self.assertTrue(clean_res["passed"], clean_res)
        self.assertFalse(clean_res["blocked"])
        self.assertEqual(clean_res["slopsquatting"]["findings"], [])
        self.assertEqual(clean_res["secrets"]["findings"], [])

        # Dirty repo with hallucinated slopsquatting, typosquatting, and embedded secrets
        dirty_repo = self.tmp_root / "dirty_repo"
        dirty_repo.mkdir()
        aws_key = "AKIA" + "IOSFODNN7SYNTH99"
        sk_key = "sk-" + "proj-" + ("a1B2c3D4" * 4)
        gh_tok = "ghp_" + ("Z9y8X7w6V5" * 4)
        pem_hdr = "-----BEGIN " + "RSA " + "PRIVATE KEY-----"
        (dirty_repo / "bad.py").write_text(
            "\n".join(
                [
                    "import json",
                    "import totally_fake_ai_helper_xyz",
                    "import reqeusts",
                    f'AWS_KEY = "{aws_key}"',
                    f'OPENAI_KEY = "{sk_key}"',
                    f'GH_TOKEN = "{gh_tok}"',
                    f'PEM = "{pem_hdr}"',
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        (dirty_repo / "requirements.txt").write_text(
            "totally-fake-ai-helper-xyz==1.0.0\nreqeusts==2.31.0\n", encoding="utf-8"
        )

        slop = analyze_slopsquatting(dirty_repo)
        self.assertFalse(slop["passed"])
        found_pkgs = {f["package"] for f in slop["findings"]}
        self.assertIn("totally_fake_ai_helper_xyz", found_pkgs)
        self.assertIn("totally-fake-ai-helper-xyz", found_pkgs)
        self.assertTrue(
            any(f["type"] == "TYPOSQUATTING_RISK" and f["closest_known"] == "requests" for f in slop["findings"])
        )

        sec = scan_secrets(dirty_repo, use_gitleaks=False)
        self.assertFalse(sec["passed"])
        found_rules = {f["rule"] for f in sec["findings"]}
        self.assertIn("AWS_ACCESS_KEY", found_rules)
        self.assertIn("API_SECRET_SK", found_rules)
        self.assertIn("GITHUB_TOKEN", found_rules)
        self.assertIn("PRIVATE_KEY_HEADER", found_rules)
        # Ensure secrets are redacted in findings
        for f in sec["findings"]:
            self.assertNotIn(aws_key, f["preview"])
            self.assertNotIn(sk_key, f["preview"])
            self.assertNotIn(gh_tok, f["preview"])

        # Orchestrator RunRequest accepts mission_profile="code_audit" & attaches codeAudit
        req = RunRequest.from_dict(
            {
                "operator": "orchestrator",
                "retention_mode": "ephemeral",
                "mission": "Audit target repo for slopsquatting and secrets",
                "mission_profile": "code_audit",
                "target_path": str(dirty_repo),
            }
        )
        self.assertEqual(req.mission_profile, "code_audit")

        def _mock_scout(**kwargs):
            return {"mode": "mock", "fileCount": 2, "entrypoints": ["bad.py"]}

        class _NoopTierSwitcher(TierSwitcher):
            def ensure_tier(self, target_tier: str, allow_kv_save: bool = False) -> dict:
                return {"ok": True, "active_engine": "mock"}

            def call_chat(self, *args, **kwargs):
                return json.dumps(
                    {
                        "status": "IMPOSSIBLE_PROVEN",
                        "impossible_category": "LOGICAL_CONTRADICTION",
                        "impossible_proof": "Mocked immediate halt after deterministic scout code_audit.",
                        "summary": "Halted after scout code_audit.",
                    }
                )

        mgr = OrchestratorManager(
            vault_root=self.tmp_root / "orch_vault",
            tier_switcher=_NoopTierSwitcher(),
            scout_fn=_mock_scout,
        )
        with mock.patch("workspaces.orchestrator.engine.code_audit.shutil.which", return_value=None):
            code, start_res = mgr.start_run(
                {
                    "operator": "orchestrator",
                    "retention_mode": "ephemeral",
                    "mission": "Run code_audit profile on dirty repo",
                    "mission_profile": "code_audit",
                    "target_path": str(dirty_repo),
                }
            )
            self.assertEqual(code, 200, start_res)
            self.assertTrue(mgr.wait_for_completion(timeout=25.0))
        ctrl = mgr._current
        self.assertIsNotNone(ctrl)
        # Ephemeral finishes cleanly and purges working state
        mgr.shutdown_cleanup()

    # ------------------------------------------------------------------
    # 4. Protected Stateless /mcp (Auth, OWASP MCP03/SSRF, PII Blocklist, JSONL Audit)
    # ------------------------------------------------------------------
    def test_mcp_auth_ssrf_sensitive_blocklist_and_jsonl_audit(self) -> None:
        init_payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-03-26"},
        }
        # 1. Missing Bearer token -> 401
        r_unauth = self.client.post(
            "/mcp",
            headers={"Content-Type": "application/json"},
            json=init_payload,
        )
        self.assertEqual(r_unauth.status_code, 401)
        self.assertEqual(r_unauth.headers.get("www-authenticate"), "Bearer")

        # 2. Bad Host -> 403
        r_host = self.client.post(
            "/mcp",
            headers=self._auth_headers({"Host": "evil.attacker.com"}),
            json=init_payload,
        )
        self.assertEqual(r_host.status_code, 403)

        # 3. Valid initialize + tools/list
        r_init = self.client.post("/mcp", headers=self._auth_headers(), json=init_payload)
        self.assertEqual(r_init.status_code, 200, r_init.text)
        self.assertEqual(r_init.json()["result"]["serverInfo"]["name"], "kryptos-core-mcp-gateway")

        r_list = self.client.post(
            "/mcp",
            headers=self._auth_headers(),
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        )
        self.assertEqual(r_list.status_code, 200)
        tool_names = {t["name"] for t in r_list.json()["result"]["tools"]}
        self.assertTrue({"rag_query", "code_audit", "local_chat", "model_inventory"} <= tool_names)

        # 4. OWASP MCP03 / Anti-SSRF blocks external URLs and metadata IPs (403 + -32003)
        ext_http_url = "https://" + "evil.example.com/exfil"
        meta_http_url = "http://" + "169.254.169.254/latest/meta-data/"
        for ssrf_args in (
            {"url": ext_http_url},
            {"query": f"please fetch {ext_http_url}"},
            {"target_path": meta_http_url},
            {"endpoint": "http://10.0.0.5:8080/internal"},
        ):
            r_ssrf = self.client.post(
                "/mcp",
                headers=self._auth_headers(),
                json={
                    "jsonrpc": "2.0",
                    "id": 10,
                    "method": "tools/call",
                    "params": {"name": "rag_query", "arguments": ssrf_args},
                },
            )
            self.assertEqual(r_ssrf.status_code, 403, f"SSRF not blocked for {ssrf_args}")
            self.assertEqual(r_ssrf.json()["error"]["code"], -32003)

        # 5. Sensitive file / PII blocklist blocks .env, .ssh, id_rsa, *.pem, gateway.token, .. traversal
        for bad_target in (
            str(self.tmp_root / ".env"),
            str(self.tmp_root / ".ssh" / "id_rsa"),
            str(self.tmp_root / "server.pem"),
            str(self._token_file),
            "../../etc/passwd",
        ):
            r_pii = self.client.post(
                "/mcp",
                headers=self._auth_headers(),
                json={
                    "jsonrpc": "2.0",
                    "id": 11,
                    "method": "tools/call",
                    "params": {"name": "code_audit", "arguments": {"target_path": bad_target}},
                },
            )
            self.assertEqual(r_pii.status_code, 403, f"Sensitive path not blocked: {bad_target}")
            self.assertEqual(r_pii.json()["error"]["code"], -32003)

        # 6. Valid code_audit tool call via /mcp succeeds
        clean_dir = self.tmp_root / "mcp_clean"
        clean_dir.mkdir()
        (clean_dir / "ok.py").write_text("import math\nprint(math.pi)\n", encoding="utf-8")
        r_ok = self.client.post(
            "/mcp",
            headers=self._auth_headers({"X-App-Id": "antigravity-test"}),
            json={
                "jsonrpc": "2.0",
                "id": 12,
                "method": "tools/call",
                "params": {"name": "code_audit", "arguments": {"target_path": str(clean_dir)}},
            },
        )
        self.assertEqual(r_ok.status_code, 200, r_ok.text)
        self.assertTrue(r_ok.json()["result"]["structuredContent"]["passed"])

        # 7. Structured JSONL audit log recorded in RAM-only ring buffer
        r_audit = self.client.get(
            "/api/mcp/audit", headers={"Authorization": f"Bearer {self.token}"}
        )
        self.assertEqual(r_audit.status_code, 200)
        audit_data = r_audit.json()
        self.assertEqual(audit_data["retention_mode"], "ghost")
        self.assertGreaterEqual(audit_data["count"], 10)
        decisions = {e["decision"] for e in audit_data["entries"]}
        self.assertEqual(decisions, {"allow", "deny"})
        jsonl_lines = [json.loads(line) for line in audit_data["jsonl"].splitlines() if line.strip()]
        self.assertEqual(len(jsonl_lines), audit_data["count"])

    # ------------------------------------------------------------------
    # 5. Hardware Setup Wizard (>=2 Hardware Profiles + PowerShell AST)
    # ------------------------------------------------------------------
    def test_hardware_wizard_profiles_and_setup_wizard_ps1_ast(self) -> None:
        ext_models = str(self.tmp_root / "CustomModels")
        p_desktop = recommend_hardware_profile(
            vram_gb=16.0,
            ram_gb=32.0,
            gpu_name="Reference 16GB Workstation GPU",
            free_disk_gb=500.0,
            models_dir=ext_models,
        )
        self.assertEqual(p_desktop["profile_id"], "workstation_16gb_vram")
        self.assertEqual(p_desktop["recommended_tier"], "dual_tier")
        self.assertEqual(p_desktop["num_ctx"], 32768)
        self.assertTrue(p_desktop["heavy_moe_enabled"])
        self.assertEqual(p_desktop["local_llm_models_dir"], str(pathlib.Path(ext_models).resolve()))

        p_laptop_8gb = recommend_hardware_profile(
            vram_gb=8.0,
            ram_gb=16.0,
            gpu_name="NVIDIA GeForce RTX 4060 Laptop",
            free_disk_gb=250.0,
            models_dir=ext_models,
        )
        self.assertEqual(p_laptop_8gb["profile_id"], "laptop_8gb_vram")
        self.assertEqual(p_laptop_8gb["recommended_tier"], "fast_only")
        self.assertEqual(p_laptop_8gb["num_ctx"], 16384)
        self.assertFalse(p_laptop_8gb["heavy_moe_enabled"])

        p_cpu = recommend_hardware_profile(
            vram_gb=0.0,
            ram_gb=16.0,
            gpu_name="CPU / Integrated Graphics",
            free_disk_gb=120.0,
            models_dir=ext_models,
        )
        self.assertEqual(p_cpu["profile_id"], "cpu_only_16gb")
        self.assertEqual(p_cpu["recommended_tier"], "cpu_only")
        self.assertEqual(p_cpu["num_ctx"], 8192)
        self.assertFalse(p_cpu["heavy_moe_enabled"])

        # Reject AppData for LOCAL_LLM_MODELS_DIR
        appdata = os.environ.get("APPDATA")
        if appdata:
            with self.assertRaises(PermissionError):
                validate_models_dir_path(pathlib.Path(appdata) / "LocalLLM" / "weights")

        # HTTP endpoint check
        r_wiz = self.client.get(
            "/api/system/hardware-wizard?vram_gb=8&ram_gb=16",
            headers={"Authorization": f"Bearer {self.token}"},
        )
        self.assertEqual(r_wiz.status_code, 200)
        self.assertEqual(r_wiz.json()["profile_id"], "laptop_8gb_vram")

        # PowerShell AST parse check on scripts/setup-wizard.ps1 (zero syntax errors)
        import shutil

        wizard_ps1 = PROJECT_ROOT / "scripts" / "setup-wizard.ps1"
        self.assertTrue(wizard_ps1.is_file())
        ps_exe = shutil.which("pwsh") or shutil.which("powershell")
        if ps_exe:
            ps_cmd = (
                "$errors = $null; $tokens = $null; "
                f"[void][System.Management.Automation.Language.Parser]::ParseFile('{wizard_ps1}', [ref]$tokens, [ref]$errors); "
                "if ($errors.Count -gt 0) { $errors | ForEach-Object { Write-Error $_.Message }; exit 1 }"
            )
            proc = subprocess.run(
                [ps_exe, "-NoProfile", "-Command", ps_cmd],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)
