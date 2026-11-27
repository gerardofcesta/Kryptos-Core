"""Unit tests for Dual Storage Layer: MemoryEphemeralStore, EphemeralEncryptedBlob, DiskVaultStore."""

from __future__ import annotations

import pathlib
import tempfile
import threading
import time
import unittest

from workspaces.orchestrator.storage.disk_vault import DiskVaultStore
from workspaces.orchestrator.storage.memory_store import (
    EphemeralEncryptedBlob,
    MemoryEphemeralStore,
    wipe_bytearray,
)


class TestMemoryEphemeralStore(unittest.TestCase):
    def test_zero_disk_writes_and_full_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            vault_probe = pathlib.Path(tmpdir) / "vault"
            vault_probe.mkdir()

            purged_callback_fired = threading.Event()
            store = MemoryEphemeralStore(
                run_id="eph_test_001",
                report_auto_purge_minutes=45,
                on_report_purged=purged_callback_fired.set,
            )

            self.assertEqual(store.retention_mode, "ephemeral")
            self.assertIsNone(store.get_artifacts_dir())
            self.assertIsNone(store.get_kv_cache_dir())
            self.assertIsNone(store.auto_purge_remaining_sec())

            store.save_mission({"mission": "Find race condition", "secret": "TOP_SECRET_123"})
            store.save_surface_map({"fileCount": 3, "files": [{"path": "app.py"}]})
            store.save_hypotheses([{"id": "H1", "status": "OPEN", "title": "Race"}])
            store.save_ledger("# Ledger\n- Step 1 inspected app.py")
            store.record_step_trace({"step": 1, "raw": "sensitive stdout"})

            # Checkpoints are no-op in ephemeral mode
            store.save_checkpoint(1, {"phase": "INVESTIGATOR"})
            self.assertIsNone(store.load_checkpoint())

            # Rolling purge wipes raw bytearrays and volatile trace buffer
            raw_buf_1 = bytearray(b"SUPER_SENSITIVE_STDOUT_BUFFER")
            raw_buf_2 = bytearray(b"RAW_FILE_SLICE_SECRET")
            raw_list = [raw_buf_1, {"nested": raw_buf_2}]
            store.rolling_purge_step(1, raw_list)

            self.assertEqual(len(raw_buf_1), 0)
            self.assertEqual(len(raw_buf_2), 0)
            self.assertEqual(len(raw_list), 0)
            self.assertEqual(store.purged_steps, 1)
            self.assertNotIn("trace_last", store._working_buffers)

            # Working state still readable before finish
            self.assertIsNotNone(store.get_mission())
            self.assertEqual(len(store.get_hypotheses()), 1)

            # Save final report and immediately purge working state
            report_text = "# Final Report\nVerified vulnerability H1."
            store.save_final_report(report_text, verdict={"code": "OBJECTIVE_ACHIEVED"})
            store.purge_working_state_on_finish()

            # Working state MUST be completely wiped
            self.assertTrue(store.working_state_purged)
            self.assertIsNone(store.get_mission())
            self.assertIsNone(store.get_surface_map())
            self.assertEqual(store.get_hypotheses(), [])
            self.assertEqual(store.get_ledger(), "")
            self.assertEqual(len(store._working_buffers), 0)

            # ONLY final report + minimal metadata remain in RAM
            self.assertEqual(store.get_final_report(), report_text)
            self.assertEqual(store.get_verdict(), {"code": "OBJECTIVE_ACHIEVED"})
            remaining = store.auto_purge_remaining_sec()
            self.assertIsNotNone(remaining)
            self.assertGreater(remaining, 0)
            self.assertLessEqual(remaining, 45 * 60)

            # Explicit purge_report() wipes report buffer and cancels timer
            report_ref = store._report_buffer
            self.assertIsNotNone(report_ref)
            store.purge_report()

            self.assertTrue(store.report_purged)
            self.assertIsNone(store.get_final_report())
            self.assertIsNone(store.get_verdict())
            self.assertIsNone(store.auto_purge_remaining_sec())
            self.assertEqual(len(report_ref), 0)
            self.assertTrue(purged_callback_fired.is_set())

            # Zero files written to vault probe
            self.assertEqual(list(vault_probe.rglob("*")), [])

    def test_dead_mans_switch_auto_purge(self) -> None:
        fired = threading.Event()
        store = MemoryEphemeralStore(
            run_id="eph_timer_test",
            report_auto_purge_minutes=1,
            on_report_purged=fired.set,
            auto_purge_seconds=0.15,
        )
        store.save_final_report("Ephemeral report content", verdict={"code": "OBJECTIVE_ACHIEVED"})
        self.assertEqual(store.get_final_report(), "Ephemeral report content")

        triggered = fired.wait(timeout=2.0)
        self.assertTrue(triggered, "Dead-Man's Switch timer did not fire within 2s")
        self.assertIsNone(store.get_final_report())
        self.assertIsNone(store.get_verdict())
        self.assertIsNone(store.auto_purge_remaining_sec())
        self.assertTrue(store.report_purged)

    def test_wipe_bytearray_zeroes_memory(self) -> None:
        buf = bytearray(b"sensitive-key-material-1234567890")
        wipe_bytearray(buf)
        self.assertEqual(len(buf), 0)

    def test_ephemeral_encrypted_blob_stdlib_roundtrip_tamper_and_shred(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            blob = EphemeralEncryptedBlob(directory=tmpdir, force_stdlib=True)
            self.assertEqual(blob.cipher_name, "hkdf-hmac-sha256-ctr-etm")

            # Multi-chunk payload exceeding CHUNK_SIZE (64 KiB) to test streaming authentication
            plaintext = b"SENSITIVE_RECORD_LINE_" * 5000  # ~110 KiB
            chunks = [plaintext[i : i + 10000] for i in range(0, len(plaintext), 10000)]
            enc_path = blob.write_stream(chunks)

            self.assertTrue(enc_path.is_file())
            raw_on_disk = enc_path.read_bytes()
            self.assertNotIn(b"SENSITIVE_RECORD_LINE_", raw_on_disk)

            decrypted = blob.read_bytes()
            self.assertEqual(decrypted, plaintext)

            # Tamper a byte in ciphertext -> MAC verification MUST fail
            tampered = bytearray(raw_on_disk)
            tampered[-10] ^= 0xFF
            enc_path.write_bytes(bytes(tampered))
            with self.assertRaises(ValueError):
                blob.read_bytes()

            # Restore valid ciphertext, then crypto_shred()
            enc_path.write_bytes(raw_on_disk)
            self.assertEqual(blob.read_bytes(), plaintext)

            blob.crypto_shred()
            self.assertTrue(blob.is_shredded)
            self.assertEqual(len(blob._key), 0)
            self.assertFalse(enc_path.exists())
            with self.assertRaises(RuntimeError):
                blob.read_bytes()


class TestDiskVaultStore(unittest.TestCase):
    def test_atomic_writes_checkpoints_list_detail_and_delete(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            vault_root = pathlib.Path(tmpdir) / "vault"
            store = DiskVaultStore(slug="Sample App!", vault_root=vault_root)

            self.assertEqual(store.retention_mode, "persistent")
            self.assertIsNone(store.auto_purge_remaining_sec())
            self.assertTrue(store.vault_dir.is_dir())
            self.assertTrue(store.get_artifacts_dir().is_dir())
            self.assertTrue(store.get_kv_cache_dir().is_dir())

            store.save_mission({"operator": "orchestrator", "mission": "Audit app", "target_path": tmpdir})
            store.save_surface_map({"fileCount": 1, "files": [{"path": "app.py"}]})
            store.save_hypotheses([{"id": "H1", "status": "CONFIRMED", "title": "Race"}])
            store.save_ledger("# Evidence Ledger\n- Confirmed H1")
            store.record_step_trace({"step": 1, "phase": "SCOUT"})
            store.record_step_trace({"step": 2, "phase": "INVESTIGATOR"})

            # Check numeric ordering of checkpoints (step_10 > step_2)
            store.save_checkpoint(2, {"phase": "INVESTIGATOR", "step": 2})
            store.save_checkpoint(10, {"phase": "VALIDATOR", "step": 10})
            latest_cp = store.load_checkpoint()
            self.assertIsNotNone(latest_cp)
            self.assertEqual(latest_cp["step"], 10)
            self.assertEqual(latest_cp["state"]["phase"], "VALIDATOR")

            store.save_final_report("# Final Report\nDone.", verdict={"code": "OBJECTIVE_ACHIEVED"})

            # Verify expected files exist and zero .tmp leftovers remain
            expected_files = [
                "00_MANIFEST.json",
                "01_SURFACE_MAP.json",
                "02_HYPOTHESES.json",
                "03_EVIDENCE_LEDGER.md",
                "03_TRACE.jsonl",
                "FINAL_REPORT.md",
                "checkpoints/step_2.json",
                "checkpoints/step_10.json",
            ]
            for rel in expected_files:
                self.assertTrue((store.vault_dir / rel).is_file(), f"Missing vault file {rel}")
            leftover_tmps = list(store.vault_dir.rglob("*.tmp"))
            self.assertEqual(leftover_tmps, [])

            # Reopen existing vault (simulates pause/resume across process restart)
            reopened = DiskVaultStore.open_existing(vault_root, store.run_id)
            self.assertEqual(reopened.get_final_report(), "# Final Report\nDone.")
            self.assertEqual(reopened.load_checkpoint()["step"], 10)
            self.assertEqual(len(reopened.get_trace()), 2)

            # list_vaults & load_vault_detail
            vaults = DiskVaultStore.list_vaults(vault_root)
            self.assertEqual(len(vaults), 1)
            self.assertEqual(vaults[0]["run_id"], store.run_id)
            self.assertTrue(vaults[0]["has_report"])

            detail = DiskVaultStore.load_vault_detail(vault_root, store.run_id)
            self.assertIsNotNone(detail)
            self.assertEqual(detail["latest_checkpoint_step"], 10)
            self.assertEqual(detail["trace_count"], 2)

            # delete_vault
            deleted = DiskVaultStore.delete_vault(vault_root, store.run_id)
            self.assertTrue(deleted)
            self.assertFalse(store.vault_dir.exists())
            self.assertEqual(DiskVaultStore.list_vaults(vault_root), [])
            self.assertFalse(DiskVaultStore.delete_vault(vault_root, store.run_id))

    def test_path_traversal_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            vault_root = pathlib.Path(tmpdir) / "vault"
            vault_root.mkdir()
            bad_ids = [
                "..",
                "../outside",
                "..\\outside",
                "a/b",
                "a\\b",
                ".hidden",
                "abc",  # too short (< 4)
                "bad id with spaces",
                "run..traversal",
            ]
            for bad in bad_ids:
                with self.subTest(bad_id=bad):
                    with self.assertRaises(ValueError):
                        DiskVaultStore.validate_run_id(bad)
                    with self.assertRaises(ValueError):
                        DiskVaultStore.load_vault_detail(vault_root, bad)
                    with self.assertRaises(ValueError):
                        DiskVaultStore.delete_vault(vault_root, bad)
                    with self.assertRaises(ValueError):
                        DiskVaultStore.open_existing(vault_root, bad)


if __name__ == "__main__":
    unittest.main()
