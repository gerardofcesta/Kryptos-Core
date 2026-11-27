"""
End-to-end headless FSM unit tests for OrchestratorManager using mock LLM & sandbox runner.

Covers:
(a) OBJECTIVE_ACHIEVED in `ephemeral` mode:
    - Strict DI proof: DiskVaultStore is NEVER instantiated
    - Zero files written to vault directory
    - Tier swaps Heavy -> Fast -> Heavy with KVSAVE=0 forced
    - Verified PoC embedded in FINAL_REPORT.md
    - Working state purged immediately on finish, leaving ONLY report in RAM
    - purge_report() zeroes report and resets state to IDLE
(b) IMPOSSIBLE_PROVEN (MISSING_EXTERNAL_DEPENDENCY) in `persistent` mode:
    - Full vault artifacts written atomically on disk
    - list_vaults, get_vault_detail, delete_vault clean removal
(c) pause_run & resume_run rejected with HTTP 409 in `ephemeral` mode
(d) fast_ghost rejected with HTTP 400 by start_run
(e) Circuit Breaker stagnation -> ESCALATE -> HALTED_CIRCUIT_BREAKER when escalation budget is 0
(f) Persistent Pause -> Checkpoint -> Resume across a brand-new OrchestratorManager instance
(g) Validator downgrades unverified CONFIRMED claim (without sandbox execution) to ESCALATE
"""

from __future__ import annotations

import json
import pathlib
import tempfile
import threading
import unittest
from typing import Any
from unittest import mock

from workspaces.orchestrator.engine.state_machine import OrchestratorManager
from workspaces.orchestrator.storage.disk_vault import DiskVaultStore

FIXTURE_APP_DIR = pathlib.Path(__file__).resolve().parent / "fixtures" / "sample_vulnerable_app"


class _MockTierSwitcher:
    """Deterministic local TierSwitcher mock recording tier swaps & KVSAVE policy."""

    def __init__(
        self,
        heavy_responses: list[str],
        fast_responses: list[str],
    ) -> None:
        self._heavy_responses = list(heavy_responses)
        self._fast_responses = list(fast_responses)
        self.ensure_calls: list[dict[str, Any]] = []
        self.heavy_calls: list[list[dict[str, str]]] = []
        self.fast_calls: list[list[dict[str, str]]] = []
        self.cleanup_count: int = 0

    def ensure_tier(
        self,
        target_tier: str,
        retention_mode: str,
        allow_kv_save: bool = False,
        kv_cache_dir: pathlib.Path | None = None,
        ram_limit_gb: int = 12,
        ctx: int = 16384,
    ) -> tuple[bool, str]:
        self.ensure_calls.append(
            {
                "target_tier": target_tier,
                "retention_mode": retention_mode,
                "allow_kv_save": allow_kv_save,
                "kv_cache_dir": kv_cache_dir,
            }
        )
        return True, f"mock switched to {target_tier}"

    def chat_heavy(self, messages: list[dict[str, str]], **kwargs: Any) -> dict[str, Any]:
        self.heavy_calls.append(list(messages))
        content = self._heavy_responses.pop(0) if self._heavy_responses else "{}"
        return {"ok": True, "tier": "heavy", "content": content}

    def chat_ollama(self, messages: list[dict[str, str]], **kwargs: Any) -> dict[str, Any]:
        self.fast_calls.append(list(messages))
        content = self._fast_responses.pop(0) if self._fast_responses else ""
        return {"ok": True, "tier": "fast", "content": content}

    def cleanup_orchestrator_containers(self) -> tuple[bool, str]:
        self.cleanup_count += 1
        return True, "0 containers"


class _MockSandboxRunner:
    def __init__(self) -> None:
        self.cleaned_run_ids: list[str] = []

    def cleanup_run_containers(self, run_id: str | None = None) -> int:
        self.cleaned_run_ids.append(str(run_id or ""))
        return 0


class _MockToolkit:
    """In-memory read-only mock of the 5 atomic sandbox tools (zero Docker daemon needed)."""

    def __init__(
        self,
        runner: Any,
        target_path: str | pathlib.Path,
        retention_mode: str,
        run_id: str,
        artifacts_path: str | pathlib.Path | None = None,
        on_execute: Any = None,
    ) -> None:
        self.runner = runner
        self.target_path = pathlib.Path(target_path)
        self.retention_mode = retention_mode
        self.run_id = run_id
        self.artifacts_path = artifacts_path
        self._on_execute = on_execute

    def describe_tools_for_prompt(self) -> str:
        return "Mock tools: list_files, read_file_slice, search_code, run_python_sandbox, update_hypothesis"

    def execute(
        self,
        tool_name: str,
        args: dict[str, Any] | None = None,
        hypothesis_callback: Any = None,
    ) -> dict[str, Any]:
        args = args or {}
        if callable(self._on_execute):
            self._on_execute(tool_name, args)

        if tool_name == "read_file_slice":
            rel = str(args.get("rel_path") or "app.py")
            start = int(args.get("start_line") or 1)
            end = int(args.get("end_line") or 80)
            target_file = self.target_path / rel
            if not target_file.is_file():
                return {"ok": False, "tool": tool_name, "error": f"File not found: {rel}"}
            lines = target_file.read_text(encoding="utf-8").splitlines()
            sliced = [f"{i}: {lines[i - 1]}" for i in range(start, min(end, len(lines)) + 1)]
            return {
                "ok": True,
                "tool": tool_name,
                "rel_path": rel,
                "start_line": start,
                "end_line": min(end, len(lines)),
                "content": "\n".join(sliced),
            }

        if tool_name == "run_python_sandbox":
            code = str(args.get("code") or "")
            return {
                "ok": True,
                "tool": tool_name,
                "exit_code": 0,
                "stdout": f"SANDBOX_VERIFIED: ran {len(code)} chars -> JSONDecodeError reproduced (4/10)",
                "stderr": "",
                "timed_out": False,
            }

        if tool_name == "update_hypothesis":
            payload = {
                "id": str(args.get("id") or ""),
                "status": str(args.get("status") or "").upper(),
                "evidence": str(args.get("evidence") or ""),
                "poc_snippet": str(args.get("poc_snippet") or ""),
            }
            if callable(hypothesis_callback):
                hypothesis_callback(payload)
            return {"ok": True, "tool": tool_name, "hypothesis": payload}

        if tool_name == "list_files":
            return {"ok": True, "tool": tool_name, "entries": [{"path": "app.py"}]}

        if tool_name == "search_code":
            return {"ok": True, "tool": tool_name, "matches": [{"path": "app.py", "line": 42, "text": "open("}]}

        return {"ok": False, "tool": tool_name, "error": f"Unknown tool {tool_name}"}


def _mock_scout(
    runner: Any,
    target_path: str | pathlib.Path,
    retention_mode: str,
    run_id: str,
    artifacts_path: str | pathlib.Path | None = None,
) -> dict[str, Any]:
    return {
        "mode": "mock_sandbox",
        "targetName": pathlib.Path(target_path).name,
        "fileCount": 1,
        "totalBytes": 2400,
        "languages": {".py": 1},
        "files": [{"path": "app.py", "size": 2400, "lines": 95}],
        "entrypoints": ["app.py"],
        "pythonAst": [
            {
                "path": "app.py",
                "lines": 95,
                "classes": ["LedgerStore"],
                "functions": ["run_health_check", "calculate_compound_balance"],
                "riskySinks": ["open", "subprocess.run"],
            }
        ],
        "staticFindings": {"ruff": [], "bandit": []},
        "compactSummary": "# Surface Map: sample_vulnerable_app\n- `app.py` sinks=open,subprocess.run",
    }


class TestOrchestratorStateMachine(unittest.TestCase):
    def test_a_objective_achieved_ephemeral_zero_disk_and_purge(self) -> None:
        poc_code = (
            "import threading, tempfile\n"
            "from app import LedgerStore\n"
            "# Concurrent save_state() triggers JSONDecodeError\n"
        )
        strategist_json = json.dumps(
            {
                "status": "HYPOTHESES_READY",
                "hypotheses": [
                    {
                        "id": "H1",
                        "title": "Non-atomic JSON write race in LedgerStore.save_state",
                        "target_files": ["app.py"],
                        "risk_area": "concurrency",
                    },
                    {
                        "id": "H2",
                        "title": "Command injection via subprocess.run in run_health_check",
                        "target_files": ["app.py"],
                        "risk_area": "injection",
                    },
                ],
            }
        )
        adjudicator_json = json.dumps(
            {
                "resolved_hypotheses": [],
                "verdict": {
                    "code": "OBJECTIVE_ACHIEVED",
                    "summary": "Confirmed non-atomic JSON write race (H1); refuted subprocess injection (H2).",
                    "impossible_category": None,
                    "impossible_proof": None,
                    "confirmed_ids": ["H1"],
                    "refuted_ids": ["H2"],
                },
                "report_markdown": "# Security Audit Report\n\nConfirmed H1 race condition in `app.py`.",
            }
        )
        fast_turns = [
            # H1 turn 1: read app.py
            '<tool_call name="read_file_slice">{"rel_path": "app.py", "start_line": 1, "end_line": 55}</tool_call>',
            # H1 turn 2: run sandbox PoC
            f'<tool_call name="run_python_sandbox">{json.dumps({"code": poc_code})}</tool_call>',
            # H1 turn 3: confirm H1
            f'<tool_call name="update_hypothesis">{json.dumps({"id": "H1", "status": "CONFIRMED", "evidence": "Concurrent threads corrupted JSON in 4/10 runs.", "poc_snippet": poc_code})}</tool_call>',
            # H2 turn 1: read subprocess section
            '<tool_call name="read_file_slice">{"rel_path": "app.py", "start_line": 55, "end_line": 85}</tool_call>',
            # H2 turn 2: refute H2 (shell=False + allowlist)
            '<tool_call name="update_hypothesis">{"id": "H2", "status": "REFUTED", "evidence": "Uses shell=False with strict allowlist and regex validation."}</tool_call>',
        ]

        switcher = _MockTierSwitcher([strategist_json, adjudicator_json], fast_turns)
        runner = _MockSandboxRunner()

        with tempfile.TemporaryDirectory() as tmpdir:
            vault_root = pathlib.Path(tmpdir) / "vault"
            vault_root.mkdir()

            mgr = OrchestratorManager(
                vault_root=vault_root,
                tier_switcher=switcher,
                sandbox_runner=runner,
                scout_fn=_mock_scout,
                toolkit_factory=_MockToolkit,
            )

            # STRICT DI PROOF: patching DiskVaultStore.__init__ to explode proves it is NEVER instantiated!
            with mock.patch.object(
                DiskVaultStore,
                "__init__",
                side_effect=AssertionError("DiskVaultStore MUST NOT be instantiated in ephemeral mode!"),
            ):
                code, resp = mgr.start_run(
                    {
                        "operator": "orchestrator",
                        "retention_mode": "ephemeral",
                        "mission": "Find file race conditions and command injection flaws.",
                        "target_path": str(FIXTURE_APP_DIR),
                        "target_access": "read_only",
                        "execution_mode": "dual_tier",
                        "ephemeral_rules": {
                            "rolling_purge_per_step": True,
                            "embed_verified_poc_in_report": True,
                            "report_auto_purge_minutes": 45,
                            "allow_kv_save": True,  # must be forced False!
                        },
                        "budget": {"max_steps": 30, "max_heavy_escalations": 2, "no_progress_limit": 3},
                    }
                )
                self.assertEqual(code, 200, resp)
                self.assertTrue(resp["ok"])
                self.assertEqual(resp["retention_mode"], "ephemeral")

                finished = mgr.wait_for_completion(timeout=10.0)
                self.assertTrue(finished)

            # Verify zero files written to vault directory
            self.assertEqual(list(vault_root.rglob("*")), [])

            # Verify tier sequence: heavy -> fast -> heavy, ALWAYS allow_kv_save=False & kv_cache_dir=None
            self.assertEqual([c["target_tier"] for c in switcher.ensure_calls], ["heavy", "fast", "heavy"])
            for call in switcher.ensure_calls:
                self.assertEqual(call["retention_mode"], "ephemeral")
                self.assertFalse(call["allow_kv_save"])
                self.assertIsNone(call["kv_cache_dir"])

            # Verify finished state & working-state purge
            st = mgr.get_status()
            self.assertEqual(st["state"], "DONE")
            self.assertFalse(st["active"])
            self.assertTrue(st["has_report"])
            self.assertEqual(st["verdict"]["code"], "OBJECTIVE_ACHIEVED")
            self.assertEqual(st["hypotheses"], [])  # working state purged!
            self.assertEqual(st["ledger"], "")  # working state purged!
            self.assertIsNotNone(st["auto_purge_remaining_sec"])

            # Verify report held in RAM includes embedded verified PoC snippet
            rep_code, rep = mgr.get_report()
            self.assertEqual(rep_code, 200)
            self.assertIn("Security Audit Report", rep["report"])
            self.assertIn("Verified PoC Snippets", rep["report"])
            self.assertIn("LedgerStore", rep["report"])

            # Verify purge_report() wipes report and returns to IDLE
            p_code, p_resp = mgr.purge_report()
            self.assertEqual(p_code, 200)
            self.assertTrue(p_resp["purged"])
            self.assertEqual(mgr.get_status()["state"], "IDLE")
            self.assertFalse(mgr.get_status()["has_report"])
            rep_code_after, _ = mgr.get_report()
            self.assertEqual(rep_code_after, 404)

    def test_b_impossible_proven_persistent_vault_and_delete(self) -> None:
        strategist_impossible = json.dumps(
            {
                "status": "IMPOSSIBLE_PROVEN",
                "impossible_category": "MISSING_EXTERNAL_DEPENDENCY",
                "impossible_proof": (
                    "The mission asks to verify remote bank OAuth token expiration on api.bank.example, "
                    "which is absent from target files and unreachable inside --network none sandbox."
                ),
                "summary": "Cannot test remote banking API in offline local-only sandbox.",
            }
        )
        switcher = _MockTierSwitcher([strategist_impossible], [])
        runner = _MockSandboxRunner()

        with tempfile.TemporaryDirectory() as tmpdir:
            vault_root = pathlib.Path(tmpdir) / "vault"
            mgr = OrchestratorManager(
                vault_root=vault_root,
                tier_switcher=switcher,
                sandbox_runner=runner,
                scout_fn=_mock_scout,
                toolkit_factory=_MockToolkit,
            )

            code, resp = mgr.start_run(
                {
                    "operator": "orchestrator",
                    "retention_mode": "persistent",
                    "mission": "Verify if remote external banking API accepts expired OAuth tokens.",
                    "target_path": str(FIXTURE_APP_DIR),
                    "target_access": "read_only",
                    "execution_mode": "dual_tier",
                }
            )
            self.assertEqual(code, 200, resp)
            run_id = resp["run_id"]
            self.assertTrue(mgr.wait_for_completion(timeout=10.0))

            st = mgr.get_status()
            self.assertEqual(st["state"], "IMPOSSIBLE")
            self.assertEqual(st["verdict"]["code"], "IMPOSSIBLE_PROVEN")
            self.assertEqual(st["verdict"]["impossible_category"], "MISSING_EXTERNAL_DEPENDENCY")

            # Verify persistent vault directory & files exist on disk
            vault_dir = vault_root / run_id
            self.assertTrue((vault_dir / "00_MANIFEST.json").is_file())
            self.assertTrue((vault_dir / "01_SURFACE_MAP.json").is_file())
            self.assertTrue((vault_dir / "03_TRACE.jsonl").is_file())
            self.assertTrue((vault_dir / "FINAL_REPORT.md").is_file())

            # list_vaults & get_vault_detail
            l_code, l_resp = mgr.list_vaults()
            self.assertEqual(l_code, 200)
            self.assertEqual(len(l_resp["vaults"]), 1)
            self.assertEqual(l_resp["vaults"][0]["run_id"], run_id)

            d_code, d_resp = mgr.get_vault_detail(run_id)
            self.assertEqual(d_code, 200)
            self.assertEqual(d_resp["vault"]["verdict"]["code"], "IMPOSSIBLE_PROVEN")

            # delete_vault cleanly removes all vault files
            del_code, del_resp = mgr.delete_vault(run_id)
            self.assertEqual(del_code, 200)
            self.assertTrue(del_resp["deleted"])
            self.assertFalse(vault_dir.exists())
            self.assertEqual(mgr.list_vaults()[1]["vaults"], [])

    def test_c_pause_and_resume_rejected_409_in_ephemeral_mode(self) -> None:
        gate = threading.Event()

        def slow_scout(**kwargs: Any) -> dict[str, Any]:
            gate.wait(timeout=3.0)
            return _mock_scout(**kwargs)

        strategist_impossible = json.dumps(
            {
                "status": "IMPOSSIBLE_PROVEN",
                "impossible_category": "LOGICAL_CONTRADICTION",
                "impossible_proof": "Contradictory mission requirements proven mathematically.",
                "summary": "Logical contradiction detected.",
            }
        )
        switcher = _MockTierSwitcher([strategist_impossible], [])
        mgr = OrchestratorManager(
            tier_switcher=switcher,
            sandbox_runner=_MockSandboxRunner(),
            scout_fn=slow_scout,
            toolkit_factory=_MockToolkit,
        )

        code, _ = mgr.start_run(
            {
                "operator": "orchestrator",
                "retention_mode": "ephemeral",
                "mission": "Test pause rejection in ephemeral mode.",
                "target_path": str(FIXTURE_APP_DIR),
            }
        )
        self.assertEqual(code, 200)

        p_code, p_resp = mgr.pause_run()
        self.assertEqual(p_code, 409)
        self.assertFalse(p_resp["ok"])

        r_code, r_resp = mgr.resume_run()
        self.assertEqual(r_code, 409)
        self.assertFalse(r_resp["ok"])

        gate.set()
        self.assertTrue(mgr.wait_for_completion(timeout=5.0))
        mgr.purge_report()

    def test_d_fast_ghost_request_rejected(self) -> None:
        mgr = OrchestratorManager(
            tier_switcher=_MockTierSwitcher([], []),
            sandbox_runner=_MockSandboxRunner(),
            scout_fn=_mock_scout,
            toolkit_factory=_MockToolkit,
        )
        code, resp = mgr.start_run(
            {
                "operator": "fast_ghost",
                "retention_mode": "ephemeral",
                "mission": "Should be rejected by orchestrator",
                "target_path": str(FIXTURE_APP_DIR),
            }
        )
        self.assertEqual(code, 400)
        self.assertFalse(resp["ok"])
        self.assertIn("fast_ghost", resp["error"])

    def test_e_circuit_breaker_stagnation_halts_when_escalation_budget_zero(self) -> None:
        strategist_json = json.dumps(
            {
                "status": "HYPOTHESES_READY",
                "hypotheses": [
                    {
                        "id": "H1",
                        "title": "Float precision drift",
                        "target_files": ["app.py"],
                        "risk_area": "precision",
                    }
                ],
            }
        )
        # Same exact tool call repeated 4 times -> 1 baseline + 3 stagnant -> Circuit Breaker trips!
        stagnant_call = '<tool_call name="read_file_slice">{"rel_path": "app.py", "start_line": 70, "end_line": 95}</tool_call>'
        switcher = _MockTierSwitcher([strategist_json], [stagnant_call] * 5)

        mgr = OrchestratorManager(
            tier_switcher=switcher,
            sandbox_runner=_MockSandboxRunner(),
            scout_fn=_mock_scout,
            toolkit_factory=_MockToolkit,
        )
        code, _ = mgr.start_run(
            {
                "operator": "orchestrator",
                "retention_mode": "ephemeral",
                "mission": "Check float precision drift.",
                "target_path": str(FIXTURE_APP_DIR),
                "execution_mode": "dual_tier",
                "budget": {
                    "max_steps": 20,
                    "max_heavy_escalations": 0,  # forces HALTED_CIRCUIT_BREAKER directly
                    "no_progress_limit": 3,
                    "max_turns_per_hypothesis": 6,
                },
            }
        )
        self.assertEqual(code, 200)
        self.assertTrue(mgr.wait_for_completion(timeout=10.0))

        st = mgr.get_status()
        self.assertEqual(st["state"], "HALTED")
        self.assertEqual(st["verdict"]["code"], "HALTED_CIRCUIT_BREAKER")
        self.assertEqual(st["escalations_used"], 1)
        mgr.purge_report()

    def test_f_persistent_pause_and_resume_across_manager_restart(self) -> None:
        first_tool_started = threading.Event()
        release_first_tool = threading.Event()

        strategist_json = json.dumps(
            {
                "status": "HYPOTHESES_READY",
                "hypotheses": [
                    {"id": "H1", "title": "Check H1", "target_files": ["app.py"], "risk_area": "concurrency"},
                    {"id": "H2", "title": "Check H2", "target_files": ["app.py"], "risk_area": "injection"},
                ],
            }
        )

        def on_exec_run1(tool_name: str, args: dict[str, Any]) -> None:
            if tool_name == "read_file_slice":
                first_tool_started.set()
                release_first_tool.wait(timeout=3.0)

        fast_run1 = [
            strategist_json,
            '<tool_call name="read_file_slice">{"rel_path": "app.py", "start_line": 1, "end_line": 30}</tool_call>',
            '<tool_call name="update_hypothesis">{"id": "H1", "status": "REFUTED", "evidence": "Checked."}</tool_call>',
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            vault_root = pathlib.Path(tmpdir) / "vault"
            mgr1 = OrchestratorManager(
                vault_root=vault_root,
                tier_switcher=_MockTierSwitcher([], fast_run1),
                sandbox_runner=_MockSandboxRunner(),
                scout_fn=_mock_scout,
                toolkit_factory=lambda **kw: _MockToolkit(**kw, on_execute=on_exec_run1),
            )
            code, resp = mgr1.start_run(
                {
                    "operator": "orchestrator",
                    "retention_mode": "persistent",
                    "mission": "Pause/resume checkpoint test",
                    "target_path": str(FIXTURE_APP_DIR),
                    "execution_mode": "fast_only",
                }
            )
            self.assertEqual(code, 200)
            run_id = resp["run_id"]

            # Wait until Investigator is inside step 1, request pause, then release tool
            self.assertTrue(first_tool_started.wait(timeout=5.0))
            p_code, _ = mgr1.pause_run()
            self.assertEqual(p_code, 200)
            release_first_tool.set()

            # Wait briefly for checkpoint save and PAUSED state
            for _ in range(50):
                if mgr1.get_status()["state"] == "PAUSED":
                    break
                threading.Event().wait(0.05)
            self.assertEqual(mgr1.get_status()["state"], "PAUSED")

            # Stop mgr1 cleanly (simulates process shutdown while paused)
            mgr1.stop_run()

            # Brand-new OrchestratorManager resumes exact persistent vault from disk checkpoint!
            fast_run2 = [
                '<tool_call name="update_hypothesis">{"id": "H1", "status": "REFUTED", "evidence": "No flaw in H1."}</tool_call>',
                '<tool_call name="update_hypothesis">{"id": "H2", "status": "REFUTED", "evidence": "No flaw in H2."}</tool_call>',
                json.dumps(
                    {
                        "resolved_hypotheses": [],
                        "verdict": {
                            "code": "OBJECTIVE_ACHIEVED",
                            "summary": "Resumed from checkpoint and audited all hypotheses cleanly.",
                            "confirmed_ids": [],
                            "refuted_ids": ["H1", "H2"],
                        },
                        "report_markdown": "# Resumed Audit Report\nAll hypotheses verified.",
                    }
                ),
            ]
            mgr2 = OrchestratorManager(
                vault_root=vault_root,
                tier_switcher=_MockTierSwitcher([], fast_run2),
                sandbox_runner=_MockSandboxRunner(),
                scout_fn=_mock_scout,
                toolkit_factory=_MockToolkit,
            )
            res_code, res_resp = mgr2.resume_run(run_id)
            self.assertEqual(res_code, 200, res_resp)
            self.assertTrue(res_resp["resumed"])
            self.assertGreaterEqual(res_resp["from_step"], 1)

            self.assertTrue(mgr2.wait_for_completion(timeout=10.0))
            st2 = mgr2.get_status()
            self.assertEqual(st2["state"], "DONE")
            self.assertEqual(st2["verdict"]["code"], "OBJECTIVE_ACHIEVED")
            self.assertIn("Resumed Audit Report", mgr2.get_report()[1]["report"])

    def test_g_validator_downgrades_unverified_confirmed_claim(self) -> None:
        strategist_json = json.dumps(
            {
                "status": "HYPOTHESES_READY",
                "hypotheses": [
                    {"id": "H1", "title": "Claimed bug without PoC", "target_files": ["app.py"], "risk_area": "logic"}
                ],
            }
        )
        # Investigator tries to mark CONFIRMED without EVER calling run_python_sandbox!
        fast_turns = [
            '<tool_call name="update_hypothesis">{"id": "H1", "status": "CONFIRMED", "evidence": "Looks buggy by eyeballing."}</tool_call>'
        ]
        # Adjudicator sees Validator's downgrade note and resolves H1 as REFUTED
        adjudicator_json = json.dumps(
            {
                "resolved_hypotheses": [
                    {"id": "H1", "status": "REFUTED", "evidence": "Downgraded by Validator; no reproducible PoC."}
                ],
                "verdict": {
                    "code": "OBJECTIVE_ACHIEVED",
                    "summary": "Unverified claim refuted during adjudication.",
                    "confirmed_ids": [],
                    "refuted_ids": ["H1"],
                },
                "report_markdown": "# Adjudication Report\nNo verified vulnerabilities.",
            }
        )
        switcher = _MockTierSwitcher([strategist_json, adjudicator_json], fast_turns)
        mgr = OrchestratorManager(
            tier_switcher=switcher,
            sandbox_runner=_MockSandboxRunner(),
            scout_fn=_mock_scout,
            toolkit_factory=_MockToolkit,
        )
        code, _ = mgr.start_run(
            {
                "operator": "orchestrator",
                "retention_mode": "ephemeral",
                "mission": "Verify unverified CONFIRMED downgrade",
                "target_path": str(FIXTURE_APP_DIR),
                "execution_mode": "dual_tier",
            }
        )
        self.assertEqual(code, 200)
        self.assertTrue(mgr.wait_for_completion(timeout=10.0))

        # Check Adjudicator prompt actually received Validator's downgrade warning!
        self.assertEqual(len(switcher.heavy_calls), 2)
        adjudicator_prompt_text = switcher.heavy_calls[1][1]["content"]
        self.assertIn("Downgraded H1 CONFIRMED -> ESCALATE", adjudicator_prompt_text)
        self.assertEqual(mgr.get_status()["verdict"]["confirmed_ids"], [])
        self.assertEqual(mgr.get_status()["verdict"]["refuted_ids"], ["H1"])
        mgr.purge_report()

    def test_h_fixture_app_sanity_and_shutdown_cleanup(self) -> None:
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "sample_vulnerable_app_fixture", FIXTURE_APP_DIR / "app.py"
        )
        self.assertIsNotNone(spec)
        mod = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(mod)

        # Safe subprocess check works and rejects disallowed input
        res = mod.run_health_check("version", label="ok_1")
        self.assertEqual(res["returncode"], 0)
        with self.assertRaises(ValueError):
            mod.run_health_check("rm -rf /")

        # Float precision drift exists relative to exact Decimal math
        from decimal import Decimal

        f_bal = mod.calculate_compound_balance(0.0, 0.1, 0.0, 3)
        d_bal = Decimal("0.1") * 3
        self.assertNotEqual(str(f_bal), str(d_bal))

        # Verify shutdown_cleanup cleans up ephemeral report and resets to IDLE
        switcher = _MockTierSwitcher(
            [
                json.dumps(
                    {
                        "status": "IMPOSSIBLE_PROVEN",
                        "impossible_category": "MISSING_EXTERNAL_DEPENDENCY",
                        "impossible_proof": "External service absent in local offline environment.",
                        "summary": "Missing external service.",
                    }
                )
            ],
            [],
        )
        mgr = OrchestratorManager(
            tier_switcher=switcher,
            sandbox_runner=_MockSandboxRunner(),
            scout_fn=_mock_scout,
            toolkit_factory=_MockToolkit,
        )
        mgr.start_run(
            {
                "operator": "orchestrator",
                "retention_mode": "ephemeral",
                "mission": "Check external service.",
                "target_path": str(FIXTURE_APP_DIR),
            }
        )
        self.assertTrue(mgr.wait_for_completion(timeout=5.0))
        self.assertTrue(mgr.get_status()["has_report"])
        mgr.shutdown_cleanup()
        self.assertEqual(mgr.get_status()["state"], "IDLE")
        self.assertFalse(mgr.get_status()["has_report"])
        self.assertGreaterEqual(switcher.cleanup_count, 1)


if __name__ == "__main__":
    unittest.main()
