"""WU-24 verification tests: Orchestrator host_readonly mode, Go Mode 5-tier + Break-Glass + Rollback, Batch Runner + Thermal/RAM Watchdog."""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import tempfile
import unittest
from typing import Any
from unittest import mock

from fastapi.testclient import TestClient

from workspaces.local_llm_core.app import create_app
from workspaces.local_llm_core.security import rotate_gateway_token
from workspaces.local_llm_core.services.batch_runner import (
    BatchJobRunner,
    ThermalRamWatchdog,
    WatchdogConfig,
    compute_safe_chunk_chars,
    get_batch_runner,
    split_text_into_chunks,
)
from workspaces.local_llm_core.services.go_mode import (
    BREAK_GLASS_PHRASE,
    GoModeManager,
    get_go_mode_manager,
)
from workspaces.orchestrator.engine.state_machine import OrchestratorManager
from workspaces.orchestrator.sandbox.sandbox_runner import DockerSandboxRunner
from workspaces.orchestrator.sandbox.scout import run_deterministic_scout
from workspaces.orchestrator.sandbox.tools import HostReadOnlyToolkit

FIXTURE_APP_DIR = (
    pathlib.Path(__file__).resolve().parents[2]
    / "orchestrator"
    / "tests"
    / "fixtures"
    / "sample_vulnerable_app"
)


class _MockNoDockerRunner(DockerSandboxRunner):
    def is_docker_available(self) -> tuple[bool, str]:
        return False, "Docker daemon not running"

    def is_image_available(self) -> bool:
        return False


class _MockHostModeSwitcher:
    def __init__(self, heavy_responses: list[str], fast_responses: list[str]) -> None:
        self._heavy = list(heavy_responses)
        self._fast = list(fast_responses)

    def ensure_tier(self, target_tier: str, retention_mode: str, **kwargs: Any) -> tuple[bool, str]:
        _ = (retention_mode, kwargs)
        return True, f"mock-{target_tier}"

    def chat_heavy(self, messages: list[dict[str, str]], **kwargs: Any) -> dict[str, Any]:
        _ = (messages, kwargs)
        return {"ok": True, "tier": "heavy", "content": self._heavy.pop(0) if self._heavy else "{}"}

    def chat_ollama(self, messages: list[dict[str, str]], **kwargs: Any) -> dict[str, Any]:
        _ = (messages, kwargs)
        return {"ok": True, "tier": "fast", "content": self._fast.pop(0) if self._fast else ""}

    def cleanup_orchestrator_containers(self) -> tuple[bool, str]:
        return True, "0"


class TestWu24ModesAndBatch(unittest.TestCase):
    """End-to-end tests for WU-24 pillars."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_root = pathlib.Path(self._tmpdir.name)
        self._token_file = self.tmp_root / "LocalLLM" / "gateway.token"
        self._env_patch = mock.patch.dict(
            os.environ,
            {"LOCAL_LLM_GATEWAY_TOKEN_FILE": str(self._token_file)},
            clear=False,
        )
        self._env_patch.start()
        self.app = create_app()
        self.token = rotate_gateway_token()
        self.client = TestClient(self.app, base_url="http://127.0.0.1:8787")

        # Reset Go Mode singleton state cleanly for each test
        gm = get_go_mode_manager()
        gm.__init__()

    def tearDown(self) -> None:
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

    # ------------------------------------------------------------------
    # Pillar 1: Orchestrator Non-Docker Safe Mode (host_readonly)
    # ------------------------------------------------------------------
    def test_scout_auto_fallback_to_host_readonly_and_toolkit_safety(self) -> None:
        runner = _MockNoDockerRunner()
        report = run_deterministic_scout(
            runner=runner,
            target_path=FIXTURE_APP_DIR,
            retention_mode="ephemeral",
            run_id="wu24_scout_auto",
            execution_mode="auto",
        )
        self.assertEqual(report["mode"], "host_readonly")
        self.assertEqual(report["executionMode"], "host_readonly")
        self.assertGreaterEqual(report["fileCount"], 1)

        tk = HostReadOnlyToolkit(target_path=FIXTURE_APP_DIR, retention_mode="ephemeral", run_id="r1")
        listed = tk.execute("list_files", {"glob": "*.py"})
        self.assertTrue(listed["ok"])
        self.assertTrue(any(e["path"] == "app.py" for e in listed["entries"]))

        sliced = tk.execute("read_file_slice", {"rel_path": "app.py", "start_line": 1, "end_line": 15})
        self.assertTrue(sliced["ok"])
        self.assertIn("1:", sliced["content"])

        searched = tk.execute("search_code", {"pattern": "def ", "glob": "*.py"})
        self.assertTrue(searched["ok"])
        self.assertGreaterEqual(searched["count"], 1)

        audit = tk.execute("code_audit", {})
        self.assertTrue(audit["ok"])
        self.assertIn("slopsquatting", audit)
        self.assertIn("secrets", audit)

        # Arbitrary Python execution MUST be disabled outside Docker
        blocked_py = tk.execute("run_python_sandbox", {"code": "print(1)"})
        self.assertFalse(blocked_py["ok"])
        self.assertTrue(blocked_py.get("disabled"))

        # Traversal MUST be blocked
        bad_slice = tk.execute("read_file_slice", {"rel_path": "../secret.txt"})
        self.assertFalse(bad_slice["ok"])

    def test_orchestrator_runs_code_audit_without_docker_and_exposes_snapshot(self) -> None:
        strategist_json = json.dumps(
            {
                "status": "HYPOTHESES_READY",
                "hypotheses": [
                    {
                        "id": "H1",
                        "title": "Check sample_vulnerable_app for unsafe sinks",
                        "target_files": ["app.py"],
                        "risk_area": "security",
                    }
                ],
            }
        )
        adjudicator_json = json.dumps(
            {
                "resolved_hypotheses": [],
                "verdict": {
                    "code": "OBJECTIVE_ACHIEVED",
                    "summary": "Completed non-Docker host_readonly audit.",
                    "impossible_category": None,
                    "impossible_proof": None,
                    "confirmed_ids": ["H1"],
                    "refuted_ids": [],
                },
                "report_markdown": "# Host Read-Only Audit\n\nVerified in host_readonly mode.",
            }
        )
        fast_turns = [
            '<tool_call name="read_file_slice">{"rel_path": "app.py", "start_line": 1, "end_line": 40}</tool_call>',
            '<tool_call name="code_audit">{}</tool_call>',
            '<tool_call name="update_hypothesis">{"id": "H1", "status": "CONFIRMED", "evidence": "Verified app.py lines 1-40 and code_audit findings."}</tool_call>',
        ]

        vault_root = self.tmp_root / "vault"
        vault_root.mkdir()
        mgr = OrchestratorManager(
            vault_root=vault_root,
            tier_switcher=_MockHostModeSwitcher([strategist_json, adjudicator_json], fast_turns),
            sandbox_runner=_MockNoDockerRunner(),
        )
        code, resp = mgr.start_run(
            {
                "operator": "orchestrator",
                "retention_mode": "persistent",
                "mission_profile": "code_audit",
                "execution_mode": "host_readonly",
                "mission": "Audit sample app without Docker.",
                "target_path": str(FIXTURE_APP_DIR),
                "budget": {"max_steps": 20, "max_heavy_escalations": 2},
            }
        )
        self.assertEqual(code, 200, resp)
        self.assertTrue(mgr.wait_for_completion(timeout=10.0))

        snap = mgr.get_status()
        self.assertEqual(snap["state"], "DONE")
        self.assertIsNone(snap["error"])
        self.assertEqual(snap["resolved_sandbox_mode"], "host_readonly")
        self.assertEqual(snap["execution_mode"], "dual_tier")
        self.assertGreaterEqual(len(snap["events"]), 1)
        self.assertGreaterEqual(len(snap["hypotheses"]), 1)
        self.assertIn("Deterministic Code Audit", snap["ledger"])
        self.assertIsNotNone(snap["surface_map"])
        self.assertIn("code_audit", snap["surface_map"])
        self.assertIn("codeAudit", snap["surface_map"])
        self.assertIsNotNone(snap["code_audit"])

    # ------------------------------------------------------------------
    # Pillar 2: Go Mode Levels 1-4 + HITL + 100% Rollback + Level 5 Shield
    # ------------------------------------------------------------------
    def test_go_mode_levels_1_to_4_hitl_and_100_percent_rollback(self) -> None:
        ws = self.tmp_root / "ws"
        ws.mkdir()
        existing = ws / "app.txt"
        existing.write_text("line1\nline2\n", encoding="utf-8")

        # Configure workspace_root + Levels 1-4
        cfg_res = self.client.post(
            "/api/go-mode/config",
            headers=self._auth_headers(),
            json={
                "workspace_root": str(ws),
                "session_id": "s1",
                "hitl_per_diff": True,
                "undo_journal_enabled": True,
                "undo_journal_mode": "disk",
                "confirm_deletions": True,
                "shell_profile": "dev_allowlist",
                "confirm_shell": True,
                "confirm_external_and_packages": True,
            },
        )
        self.assertEqual(cfg_res.status_code, 200, cfg_res.text)

        # 1. Level 1 write_file creates HITL proposal with unified diff
        p1 = self.client.post(
            "/api/go-mode/propose",
            headers=self._auth_headers(),
            json={"action": "write_file", "path": "app.txt", "content": "line1\nmodified\n"},
        ).json()
        self.assertTrue(p1["requires_approval"])
        self.assertIn("-line2", p1["proposal"]["diff"])
        self.assertIn("+modified", p1["proposal"]["diff"])

        # Approve folder on Level 1
        a1 = self.client.post(
            "/api/go-mode/approve",
            headers=self._auth_headers(),
            json={"proposal_id": p1["proposal"]["proposal_id"], "approve_mode": "folder"},
        ).json()
        self.assertTrue(a1["executed"])
        self.assertEqual(existing.read_text(encoding="utf-8"), "line1\nmodified\n")

        # Second write in same folder auto-approves cleanly!
        p2 = self.client.post(
            "/api/go-mode/propose",
            headers=self._auth_headers(),
            json={"action": "create_file", "path": "new_file.txt", "content": "brand new\n"},
        ).json()
        self.assertTrue(p2["executed"])
        self.assertTrue((ws / "new_file.txt").is_file())

        # 2. Level 2 delete_file MUST STILL require separate confirmation even with folder auto-approved!
        p3 = self.client.post(
            "/api/go-mode/propose",
            headers=self._auth_headers(),
            json={"action": "delete_file", "path": "app.txt"},
        ).json()
        self.assertTrue(p3["requires_approval"])
        self.assertEqual(p3["proposal"]["risk_level"], 2)

        a3 = self.client.post(
            "/api/go-mode/approve",
            headers=self._auth_headers(),
            json={"proposal_id": p3["proposal"]["proposal_id"]},
        ).json()
        self.assertTrue(a3["executed"])
        self.assertFalse(existing.exists())

        # Undo Last Step restores deleted app.txt (with modified content before delete)
        rb_last = self.client.post(
            "/api/go-mode/rollback",
            headers=self._auth_headers(),
            json={"session_id": "s1", "mode": "last"},
        ).json()
        self.assertEqual(rb_last["restored_count"], 1)
        self.assertEqual(existing.read_text(encoding="utf-8"), "line1\nmodified\n")

        # Rollback All restores 100% original workspace state (unlinks new_file.txt & restores app.txt)
        rb_all = self.client.post(
            "/api/go-mode/rollback",
            headers=self._auth_headers(),
            json={"session_id": "s1", "mode": "all"},
        ).json()
        self.assertEqual(rb_all["restored_count"], 2)
        self.assertEqual(existing.read_text(encoding="utf-8"), "line1\nline2\n")
        self.assertFalse((ws / "new_file.txt").exists())

        # 3. Level 3 shell profile enforcement (dev_allowlist blocks chained or unknown commands)
        bad_cmd = self.client.post(
            "/api/go-mode/propose",
            headers=self._auth_headers(),
            json={"action": "run_command", "command": "pytest && echo pwned"},
        )
        self.assertEqual(bad_cmd.status_code, 403)

        ok_cmd = self.client.post(
            "/api/go-mode/propose",
            headers=self._auth_headers(),
            json={"action": "run_command", "command": "python -c \"print('go_ok')\""},
        ).json()
        self.assertTrue(ok_cmd["requires_approval"])
        exec_cmd = self.client.post(
            "/api/go-mode/approve",
            headers=self._auth_headers(),
            json={"proposal_id": ok_cmd["proposal"]["proposal_id"]},
        ).json()
        self.assertTrue(exec_cmd["executed"])
        self.assertIn("go_ok", exec_cmd["result"]["stdout"])

        # 4. Level 4 package install requires explicit consent
        pkg_prop = self.client.post(
            "/api/go-mode/propose",
            headers=self._auth_headers(),
            json={"action": "install_package", "command": "pip install httpx"},
        ).json()
        self.assertTrue(pkg_prop["requires_approval"])
        self.assertEqual(pkg_prop["proposal"]["risk_level"], 4)

    def test_level5_hard_shield_and_break_glass_ritual(self) -> None:
        ws = self.tmp_root / "ws_l5"
        ws.mkdir()
        # Disable Levels 1-4 — Level 5 MUST STILL block!
        self.client.post(
            "/api/go-mode/config",
            headers=self._auth_headers(),
            json={
                "workspace_root": str(ws),
                "hitl_per_diff": False,
                "confirm_deletions": False,
                "shell_profile": "workspace_shell",
                "confirm_shell": False,
                "confirm_external_and_packages": False,
            },
        )

        # Cannot disable Level 5 via config
        dis = self.client.post(
            "/api/go-mode/config",
            headers=self._auth_headers(),
            json={"level5_shield": False},
        )
        self.assertEqual(dis.status_code, 403)

        # Level 5 blocks OS paths, registry, credential vaults, and destructive commands
        for payload in (
            {"action": "write_file", "path": r"C:\Windows\System32\evil.dll", "content": "x"},
            {"action": "read_external_path", "path": "~/.ssh/id_rsa"},
            {"action": "run_command", "command": "reg delete HKLM\\Software\\Test /f"},
            {"action": "run_command", "command": "diskpart /s wipe.txt"},
            {"action": "run_command", "command": "runas /user:Administrator cmd.exe"},
            {"action": "run_command", "command": "shutdown /s /t 0"},
        ):
            r = self.client.post("/api/go-mode/propose", headers=self._auth_headers(), json=payload)
            self.assertEqual(r.status_code, 403, f"Should block {payload}: {r.text}")
            self.assertEqual(r.json().get("blocked_by"), "level5_hard_shield")

        # Break-Glass rejects < 3 checkboxes
        bg_fail_chk = self.client.post(
            "/api/go-mode/break-glass",
            headers=self._auth_headers(),
            json={
                "checkboxes": {"ack_os_damage": True, "ack_no_rollback": True, "ack_full_responsibility": False},
                "confirmation_phrase": BREAK_GLASS_PHRASE,
            },
        )
        self.assertEqual(bg_fail_chk.status_code, 403)

        # Break-Glass rejects wrong phrase
        bg_fail_phrase = self.client.post(
            "/api/go-mode/break-glass",
            headers=self._auth_headers(),
            json={
                "checkboxes": {"ack_os_damage": True, "ack_no_rollback": True, "ack_full_responsibility": True},
                "confirmation_phrase": "let me in",
            },
        )
        self.assertEqual(bg_fail_phrase.status_code, 403)

        # Break-Glass succeeds with all 3 checkboxes + exact phrase
        bg_ok = self.client.post(
            "/api/go-mode/break-glass",
            headers=self._auth_headers(),
            json={
                "checkboxes": {"ack_os_damage": True, "ack_no_rollback": True, "ack_full_responsibility": True},
                "confirmation_phrase": BREAK_GLASS_PHRASE,
                "ttl_seconds": 300,
            },
        )
        self.assertEqual(bg_ok.status_code, 200, bg_ok.text)
        token = bg_ok.json()["token"]
        self.assertTrue(token)

        # With valid token, Level 5 read check passes (targeting a harmless nonexistent file inside ~/.ssh or mocked path)
        ext_file = self.tmp_root / ".ssh" / "test_key.pub"
        ext_file.parent.mkdir(parents=True, exist_ok=True)
        ext_file.write_text("ssh-ed25519 TESTKEY", encoding="utf-8")

        # Without token: blocked
        no_tok = self.client.post(
            "/api/go-mode/propose",
            headers=self._auth_headers(),
            json={"action": "read_external_path", "path": str(ext_file)},
        )
        self.assertEqual(no_tok.status_code, 403)

        # With Break-Glass token: allowed & logged
        with_tok = self.client.post(
            "/api/go-mode/propose",
            headers=self._auth_headers(),
            json={"action": "read_external_path", "path": str(ext_file), "break_glass_token": token},
        )
        self.assertEqual(with_tok.status_code, 200, with_tok.text)
        self.assertIn("TESTKEY", with_tok.json()["result"]["preview"])

        # Re-lock immediately blocks again
        self.client.post("/api/go-mode/break-glass/lock", headers=self._auth_headers(), json={})
        after_lock = self.client.post(
            "/api/go-mode/propose",
            headers=self._auth_headers(),
            json={"action": "read_external_path", "path": str(ext_file), "break_glass_token": token},
        )
        self.assertEqual(after_lock.status_code, 403)

    # ------------------------------------------------------------------
    # Pillar 3: Overnight Batch Job Runner + Thermal & RAM Watchdog
    # ------------------------------------------------------------------
    def test_batch_runner_chunking_checkpoint_pause_resume_and_watchdog(self) -> None:
        sizing = compute_safe_chunk_chars("local-fast")
        self.assertGreater(sizing["max_safe_ctx"], 0)
        chunks = split_text_into_chunks(
            "\n\n".join(f"Paragraph {i}: " + ("word " * 80) for i in range(6)),
            max_chunk_chars=600,
        )
        self.assertGreaterEqual(len(chunks), 4)
        for c in chunks:
            self.assertLessEqual(len(c), 600)

        # Test async BatchJobRunner with Pause/Resume + Thermal Watchdog pause/auto-resume
        async def _run_scenario() -> None:
            telemetry_state = {"temp": 68.0, "ram": 16.0}
            seen_chunks: list[int] = []

            async def fake_llm(model: str, messages: list[dict[str, str]], num_ctx: int) -> str:
                _ = (model, num_ctx)
                user_msg = messages[1]["content"]
                # Extract chunk index from "[Chunk N/M]"
                header = user_msg.splitlines()[0]
                idx = int(header.split("/")[0].split()[-1])
                seen_chunks.append(idx)
                if idx == 1:
                    # Simulate GPU spike after chunk 1 completes
                    telemetry_state["temp"] = 86.0
                await asyncio.sleep(0.02)
                return f"DONE:{idx}"

            wd = ThermalRamWatchdog(
                config=WatchdogConfig(
                    enabled=True,
                    max_gpu_temp_c=82.0,
                    cooldown_gpu_temp_c=74.0,
                    min_free_ram_gb=6.0,
                    resume_free_ram_gb=7.5,
                    poll_interval_sec=0.03,
                ),
                telemetry_fn=lambda: (
                    {"tempC": telemetry_state["temp"]},
                    {"freeGb": telemetry_state["ram"]},
                ),
            )
            runner = BatchJobRunner(
                checkpoint_dir=self.tmp_root / "batch_cp",
                watchdog=wd,
                llm_fn=fake_llm,
            )

            code, created = await runner.create_and_start_job(
                {
                    "task_type": "translate_paragraphs",
                    "model": "local-fast",
                    "target_language": "Italian",
                    "items": ["Alpha text", "Beta text", "Gamma text", "Delta text"],
                    "persist_checkpoint": True,
                }
            )
            self.assertEqual(code, 200)
            job_id = created["job"]["job_id"]

            # Wait until Watchdog triggers paused_watchdog
            paused_by_wd = False
            for _ in range(40):
                _, snap = runner.get_job(job_id)
                if snap["job"]["status"] == "paused_watchdog":
                    paused_by_wd = True
                    break
                await asyncio.sleep(0.02)
            self.assertTrue(paused_by_wd, "Watchdog should auto-pause job when GPU >= 82°C")

            # Cool down GPU -> Watchdog MUST auto-resume and finish!
            telemetry_state["temp"] = 70.0
            completed = False
            for _ in range(60):
                _, snap = runner.get_job(job_id)
                if snap["job"]["status"] == "completed":
                    completed = True
                    break
                await asyncio.sleep(0.02)
            self.assertTrue(completed, "Watchdog should auto-resume job after cooldown")
            self.assertEqual(seen_chunks, [1, 2, 3, 4])

            # Verify disk checkpoint & exports (md, json, csv)
            cp_file = self.tmp_root / "batch_cp" / f"{job_id}.json"
            self.assertTrue(cp_file.is_file())
            for fmt in ("md", "json", "csv"):
                ec, content, _, _ = runner.export_job(job_id, fmt)
                self.assertEqual(ec, 200)
                self.assertIn("DONE:1", content)
                self.assertIn("DONE:4", content)

            # Test manual Pause / Resume skipping already completed chunks
            telemetry_state["temp"] = 65.0
            gate = asyncio.Event()
            manual_seen: list[int] = []

            async def gated_llm(model: str, messages: list[dict[str, str]], num_ctx: int) -> str:
                _ = (model, num_ctx)
                idx = int(messages[1]["content"].splitlines()[0].split("/")[0].split()[-1])
                manual_seen.append(idx)
                if idx == 1:
                    await gate.wait()
                await asyncio.sleep(0.01)
                return f"OK:{idx}"

            runner.set_llm_fn(gated_llm)
            _, c2 = await runner.create_and_start_job(
                {"task_type": "bulk_summarize", "items": ["One", "Two", "Three"]}
            )
            jid2 = c2["job"]["job_id"]
            await asyncio.sleep(0.02)
            p_code, p_res = runner.pause_job(jid2)
            self.assertEqual(p_code, 200)
            self.assertEqual(p_res["job"]["status"], "paused")
            gate.set()
            await asyncio.sleep(0.04)
            # Only chunk 1 ran before pause; chunks 2 & 3 stay pending
            self.assertEqual(manual_seen, [1])
            await runner.resume_job(jid2)
            for _ in range(40):
                _, s2 = runner.get_job(jid2)
                if s2["job"]["status"] == "completed":
                    break
                await asyncio.sleep(0.02)
            self.assertEqual(manual_seen, [1, 2, 3])

        asyncio.run(_run_scenario())

    def test_batch_routes_via_fastapi(self) -> None:
        sizing_res = self.client.get("/api/batch/sizing?model=local-fast", headers=self._auth_headers())
        self.assertEqual(sizing_res.status_code, 200)
        self.assertIn("max_safe_ctx", sizing_res.json())

        wd_res = self.client.post(
            "/api/batch/watchdog",
            headers=self._auth_headers(),
            json={"max_gpu_temp_c": 80, "min_free_ram_gb": 8},
        )
        self.assertEqual(wd_res.status_code, 200)
        self.assertEqual(wd_res.json()["watchdog"]["thresholds"]["max_gpu_temp_c"], 80.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
