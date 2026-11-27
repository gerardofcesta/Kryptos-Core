"""Unit tests for Tool Protocol, Self-Correction, Circuit Breaker, and Formal Schemas."""

from __future__ import annotations

import pathlib
import tempfile
import unittest
from typing import Any

from workspaces.orchestrator.engine.circuit_breaker import NoProgressCircuitBreaker
from workspaces.orchestrator.engine.schemas import (
    MAX_LEDGER_CHARS,
    Hypothesis,
    RunRequest,
    SchemaValidationError,
    Verdict,
    compact_ledger_text,
    validate_verdict,
)
from workspaces.orchestrator.engine.tier_switcher import TierSwitcher
from workspaces.orchestrator.engine.tool_protocol import (
    ProtocolParseError,
    call_with_self_correction,
    parse_json_object,
    parse_tool_calls,
    strip_think_blocks,
)


class TestToolProtocol(unittest.TestCase):
    def test_strip_think_blocks(self) -> None:
        raw = "<think>internal chain of thought\nline 2</think>\nFinal output"
        self.assertEqual(strip_think_blocks(raw), "Final output")

        orphan = "reasoning before close</think>\n{\"ok\": true}"
        self.assertEqual(strip_think_blocks(orphan), '{"ok": true}')

    def test_parse_single_and_multiple_tool_calls(self) -> None:
        text = (
            "<think>Let's inspect app.py and search for subprocess.</think>\n"
            '<tool_call name="read_file_slice">{"rel_path": "app.py", "start_line": 1, "end_line": 50}</tool_call>\n'
            '<tool_call name="search_code">{"pattern": "subprocess", "glob": "*.py"}</tool_call>'
        )
        calls = parse_tool_calls(text, allowed_tools=("read_file_slice", "search_code"))
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0].name, "read_file_slice")
        self.assertEqual(calls[0].arguments["rel_path"], "app.py")
        self.assertEqual(calls[1].name, "search_code")
        self.assertEqual(calls[1].arguments["pattern"], "subprocess")

    def test_parse_run_python_sandbox_fenced_python_fallback(self) -> None:
        text = (
            "<think>Write a quick PoC</think>\n"
            '<tool_call name="run_python_sandbox">\n'
            "```python\n"
            "import json\n"
            "print('POC_OK')\n"
            "```\n"
            "</tool_call>"
        )
        calls = parse_tool_calls(text, allowed_tools=("run_python_sandbox",))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].name, "run_python_sandbox")
        self.assertIn("print('POC_OK')", calls[0].arguments["code"])

    def test_parse_tool_call_errors(self) -> None:
        with self.assertRaises(ProtocolParseError):
            parse_tool_calls('<tool_call name="read_file_slice">{"rel_path": "app.py"')

        with self.assertRaises(ProtocolParseError):
            parse_tool_calls('<tool_call name="read_file_slice">{bad json}</tool_call>')

        with self.assertRaises(ProtocolParseError):
            parse_tool_calls(
                '<tool_call name="delete_everything">{}</tool_call>',
                allowed_tools=("list_files", "read_file_slice"),
            )

    def test_parse_json_object_variants(self) -> None:
        self.assertEqual(
            parse_json_object('<think>hmm</think>\n```json\n{"status": "OK", "n": 2}\n```'),
            {"status": "OK", "n": 2},
        )
        self.assertEqual(
            parse_json_object('Here is the result:\n{"verdict": "OBJECTIVE_ACHIEVED"}\nThanks!'),
            {"verdict": "OBJECTIVE_ACHIEVED"},
        )
        with self.assertRaises(ProtocolParseError):
            parse_json_object("No JSON object anywhere here.")

    def test_self_correction_3_retry_success_and_exhaustion(self) -> None:
        # Succeeds on 3rd attempt after 2 syntax errors, without mutating original messages
        original_messages = [{"role": "user", "content": "Emit tool call"}]
        responses = [
            "No tags here at all",
            '<tool_call name="read_file_slice">{malformed json</tool_call>',
            '<tool_call name="read_file_slice">{"rel_path": "app.py", "start_line": 1, "end_line": 40}</tool_call>',
        ]
        seen_calls: list[list[dict[str, str]]] = []

        def fake_llm(msgs: list[dict[str, str]]) -> str:
            seen_calls.append(list(msgs))
            return responses[len(seen_calls) - 1]

        parsed, raw_text, attempts = call_with_self_correction(
            llm_fn=fake_llm,
            messages=original_messages,
            parser_fn=lambda txt: parse_tool_calls(txt, require_at_least_one=True),
            max_retries=3,
        )
        self.assertEqual(attempts, 3)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0].name, "read_file_slice")
        self.assertIn("app.py", raw_text)
        # Original caller messages must remain completely untouched
        self.assertEqual(len(original_messages), 1)
        # Retry prompts stay bounded (1 base + 1 assistant snippet + 1 concise error = 3)
        self.assertEqual(len(seen_calls[2]), 3)
        self.assertIn("FORMAT ERROR", seen_calls[2][-1]["content"])

        # Exhausting all 3 attempts raises ProtocolParseError
        bad_calls = 0

        def always_bad(msgs: list[dict[str, str]]) -> str:
            nonlocal bad_calls
            bad_calls += 1
            return "still broken"

        with self.assertRaises(ProtocolParseError):
            call_with_self_correction(
                llm_fn=always_bad,
                messages=original_messages,
                parser_fn=parse_json_object,
                max_retries=3,
            )
        self.assertEqual(bad_calls, 3)


class TestCircuitBreaker(unittest.TestCase):
    def test_trips_at_exactly_3_stagnant_steps_and_resets_on_progress(self) -> None:
        cb = NoProgressCircuitBreaker(limit=3)
        hyps = [{"id": "H1", "status": "TESTING", "evidence": "", "poc_snippet": "", "inspected_slices": []}]

        # Baseline progress step (new file read + new output)
        tripped, _ = cb.record_step(hyps, ["app.py:1-50"], "read_file_slice", "hash_A")
        self.assertFalse(tripped)
        self.assertEqual(cb.stagnant_streak, 0)

        # Stagnant step #1
        tripped, _ = cb.record_step(hyps, ["app.py:1-50"], "read_file_slice", "hash_A")
        self.assertFalse(tripped)
        self.assertEqual(cb.stagnant_streak, 1)

        # Stagnant step #2
        tripped, _ = cb.record_step(hyps, ["app.py:1-50"], "read_file_slice", "hash_A")
        self.assertFalse(tripped)
        self.assertEqual(cb.stagnant_streak, 2)

        # Progress resumes (new file slice read) -> resets streak to 0!
        tripped, _ = cb.record_step(hyps, ["app.py:51-100"], "read_file_slice", "hash_A")
        self.assertFalse(tripped)
        self.assertEqual(cb.stagnant_streak, 0)

        # Now 3 consecutive stagnant steps MUST trip at step 3
        tripped, _ = cb.record_step(hyps, ["app.py:51-100"], "read_file_slice", "hash_A")
        self.assertFalse(tripped)
        self.assertEqual(cb.stagnant_streak, 1)

        tripped, _ = cb.record_step(hyps, ["app.py:51-100"], "read_file_slice", "hash_A")
        self.assertFalse(tripped)
        self.assertEqual(cb.stagnant_streak, 2)

        tripped, reason = cb.record_step(hyps, ["app.py:51-100"], "read_file_slice", "hash_A")
        self.assertTrue(tripped)
        self.assertEqual(cb.stagnant_streak, 3)
        self.assertIn("Circuit breaker tripped", reason)

        # Checkpoint serialization round-trip
        restored = NoProgressCircuitBreaker.from_dict(cb.to_dict())
        self.assertEqual(restored.stagnant_streak, 3)
        self.assertEqual(restored.limit, 3)

        # Progress via hypothesis update resets streak
        hyps_updated = [
            {"id": "H1", "status": "CONFIRMED", "evidence": "PoC reproduced", "poc_snippet": "x=1", "inspected_slices": []}
        ]
        tripped, _ = restored.record_step(hyps_updated, [], "update_hypothesis", "hash_A")
        self.assertFalse(tripped)
        self.assertEqual(restored.stagnant_streak, 0)


class TestSchemasAndVerdicts(unittest.TestCase):
    def test_run_request_validation_and_clamping(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            # Rejects fast_ghost
            with self.assertRaises(SchemaValidationError):
                RunRequest.from_dict(
                    {
                        "operator": "fast_ghost",
                        "retention_mode": "ephemeral",
                        "mission": "Inspect code",
                        "target_path": tmpdir,
                    }
                )

            # Rejects missing retention_mode
            with self.assertRaises(SchemaValidationError):
                RunRequest.from_dict(
                    {
                        "operator": "orchestrator",
                        "mission": "Inspect code",
                        "target_path": tmpdir,
                    }
                )

            # Rejects read_write target_access
            with self.assertRaises(SchemaValidationError):
                RunRequest.from_dict(
                    {
                        "operator": "orchestrator",
                        "retention_mode": "ephemeral",
                        "mission": "Inspect code",
                        "target_path": tmpdir,
                        "target_access": "read_write",
                    }
                )

            # Valid ephemeral request clamps budget & forces allow_kv_save=False
            req = RunRequest.from_dict(
                {
                    "operator": "orchestrator",
                    "retention_mode": "ephemeral",
                    "mission": "Audit target",
                    "target_path": tmpdir,
                    "execution_mode": "fast_only",
                    "ephemeral_rules": {
                        "allow_kv_save": True,  # must be overridden to False!
                        "rolling_purge_per_step": False,  # must be overridden to True!
                        "report_auto_purge_minutes": 999,  # clamped to 240
                    },
                    "budget": {
                        "max_steps": 999,  # clamped to 120
                        "max_heavy_escalations": 50,  # clamped to 10
                        "no_progress_limit": 1,  # clamped to 2
                    },
                }
            )
            self.assertFalse(req.ephemeral_rules.allow_kv_save)
            self.assertTrue(req.ephemeral_rules.rolling_purge_per_step)
            self.assertEqual(req.ephemeral_rules.report_auto_purge_minutes, 240)
            self.assertEqual(req.budget.max_steps, 120)
            self.assertEqual(req.budget.max_heavy_escalations, 10)
            self.assertEqual(req.budget.no_progress_limit, 2)

    def test_verdict_validation_rules(self) -> None:
        h_conf = Hypothesis(id="H1", title="Race", status="CONFIRMED", evidence="PoC", sandbox_verified=True)
        h_open = Hypothesis(id="H2", title="Subprocess", status="OPEN")
        h_ref = Hypothesis(id="H2", title="Subprocess", status="REFUTED", evidence="shell=False")

        # OBJECTIVE_ACHIEVED valid with >=1 CONFIRMED
        ok, errs = validate_verdict(
            Verdict(code="OBJECTIVE_ACHIEVED", summary="Found race.", confirmed_ids=["H1"]),
            [h_conf, h_open],
        )
        self.assertTrue(ok, errs)

        # OBJECTIVE_ACHIEVED invalid with 0 CONFIRMED and unresolved OPEN hypothesis
        ok, errs = validate_verdict(
            Verdict(code="OBJECTIVE_ACHIEVED", summary="All good?"),
            [h_open],
        )
        self.assertFalse(ok)

        # OBJECTIVE_ACHIEVED valid when ALL hypotheses resolved (REFUTED)
        ok, errs = validate_verdict(
            Verdict(code="OBJECTIVE_ACHIEVED", summary="Audited cleanly.", refuted_ids=["H2"]),
            [h_ref],
        )
        self.assertTrue(ok, errs)

        # IMPOSSIBLE_PROVEN invalid without category or proof
        ok, errs = validate_verdict(
            Verdict(code="IMPOSSIBLE_PROVEN", summary="Cannot do it"),
            [],
        )
        self.assertFalse(ok)

        # IMPOSSIBLE_PROVEN valid with valid category + structured proof
        ok, errs = validate_verdict(
            Verdict(
                code="IMPOSSIBLE_PROVEN",
                summary="Remote API unavailable offline.",
                impossible_category="MISSING_EXTERNAL_DEPENDENCY",
                impossible_proof="Target relies on external banking API unreachable under --network none.",
            ),
            [],
        )
        self.assertTrue(ok, errs)

    def test_compact_ledger_enforces_hard_cap(self) -> None:
        huge = "\n".join(f"- Step {i}: verified line {'X' * 120}" for i in range(200))
        compacted = compact_ledger_text(huge, max_chars=MAX_LEDGER_CHARS)
        self.assertLessEqual(len(compacted), MAX_LEDGER_CHARS)
        self.assertIn("LEDGER COMPACTED", compacted)


class TestTierSwitcherPolicy(unittest.TestCase):
    def test_loopback_enforced_and_ephemeral_forces_kvsave_zero(self) -> None:
        from unittest import mock

        ts = TierSwitcher(ollama_host="8.8.8.8", heavy_host="example.com")
        self.assertEqual(ts.ollama_host, "127.0.0.1")
        self.assertEqual(ts.heavy_host, "127.0.0.1")

        captured_env: dict[str, str] = {}
        captured_cmd: list[str] = []

        def fake_run(cmd: list[str], **kwargs: Any) -> Any:
            captured_cmd.extend(cmd)
            if isinstance(kwargs.get("env"), dict):
                captured_env.update(kwargs["env"])

            class _Proc:
                returncode = 0
                stdout = "OK"
                stderr = ""

            return _Proc()

        with tempfile.TemporaryDirectory() as tmpdir:
            kv_dir = pathlib.Path(tmpdir) / "kv_cache"
            with (
                mock.patch.object(ts, "check_health", return_value=False),
                mock.patch.object(ts, "cleanup_orchestrator_containers", return_value=(True, "0")),
                mock.patch("subprocess.run", side_effect=fake_run),
            ):
                ok, _ = ts.ensure_tier(
                    target_tier="heavy",
                    retention_mode="ephemeral",
                    allow_kv_save=True,  # MUST be overridden to KVSAVE=0 in ephemeral!
                    kv_cache_dir=kv_dir,
                    ram_limit_gb=18,  # MUST be capped to 12 in ephemeral!
                )
            self.assertTrue(ok)
            self.assertEqual(captured_env.get("KVSAVE"), "0")
            self.assertEqual(captured_env.get("HEAVY_MOE_RAM_GB"), "12")
            self.assertNotIn("COLI_KV_DIR", captured_env)
            self.assertIn("-RamLimitGB", captured_cmd)
            ram_idx = captured_cmd.index("-RamLimitGB")
            self.assertEqual(captured_cmd[ram_idx + 1], "12")


if __name__ == "__main__":
    unittest.main()
