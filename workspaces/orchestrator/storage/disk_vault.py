"""
Persistent Disk Vault Storage with Atomic Writes (tmp + os.replace), Checkpoints,
JSONL Step Trace, and Strict Path-Traversal Protection.

ONLY instantiated when retention_mode == "persistent".
"""

from __future__ import annotations

import datetime
import json
import os
import pathlib
import re
import secrets
import shutil
import threading
from typing import Any

from .base import RunStore
from .memory_store import _wipe_nested

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[3]
DEFAULT_VAULT_ROOT = PROJECT_ROOT / "workspaces" / "vault"
SAFE_RUN_ID_RE = re.compile(r"^[a-zA-Z0-9._-]{4,96}$")
CHECKPOINT_FILE_RE = re.compile(r"^step_(\d+)\.json$")


def _utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()


def _sanitize_slug(raw: str, max_len: int = 28) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "-", (raw or "").strip().lower()).strip("-_")
    if not cleaned:
        cleaned = "audit"
    return cleaned[:max_len].strip("-_") or "audit"


class DiskVaultStore(RunStore):
    """
    Full Audit persistent RunStore writing exclusively inside:
        `<vault_root>/<YYYY-MM-DD_HHMMSS>_<slug>_<shortid>/`

    All file writes are atomic (`write .tmp` + `fsync` + `os.replace`) under a lock.
    """

    def __init__(
        self,
        run_id: str | None = None,
        slug: str = "audit",
        vault_root: pathlib.Path | str | None = None,
        create_dirs: bool = True,
    ) -> None:
        self._vault_root = (
            pathlib.Path(vault_root).resolve()
            if vault_root is not None
            else DEFAULT_VAULT_ROOT.resolve()
        )
        chosen_id = run_id if run_id is not None else self.build_run_id(slug)
        self._run_id = self.validate_run_id(chosen_id)
        self._retention_mode = "persistent"
        self._lock = threading.RLock()
        self._vault_dir = self._resolve_safe_vault_dir(self._vault_root, self._run_id)

        if create_dirs:
            self._vault_dir.mkdir(parents=True, exist_ok=True)
            (self._vault_dir / "checkpoints").mkdir(parents=True, exist_ok=True)
            (self._vault_dir / "artifacts").mkdir(parents=True, exist_ok=True)
            (self._vault_dir / "kv_cache").mkdir(parents=True, exist_ok=True)

    @staticmethod
    def build_run_id(slug: str = "audit") -> str:
        ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d_%H%M%S")
        short_id = secrets.token_hex(3)
        safe_slug = _sanitize_slug(slug, max_len=28)
        candidate = f"{ts}_{safe_slug}_{short_id}"
        return DiskVaultStore.validate_run_id(candidate)

    @staticmethod
    def validate_run_id(run_id: str) -> str:
        if not isinstance(run_id, str):
            raise ValueError("run_id must be a string.")
        rid = run_id.strip()
        if not rid or not SAFE_RUN_ID_RE.match(rid):
            raise ValueError(
                f"Invalid run_id '{rid}': must match {SAFE_RUN_ID_RE.pattern}"
            )
        if ".." in rid or rid.startswith(".") or rid.endswith(".") or "/" in rid or "\\" in rid:
            raise ValueError(f"Unsafe run_id rejected (path traversal risk): '{rid}'")
        return rid

    @classmethod
    def _resolve_safe_vault_dir(
        cls, vault_root: pathlib.Path, run_id: str
    ) -> pathlib.Path:
        safe_id = cls.validate_run_id(run_id)
        root_resolved = vault_root.resolve()
        target = (root_resolved / safe_id).resolve()
        if target.parent != root_resolved or target == root_resolved:
            raise ValueError(f"Path traversal blocked for run_id '{safe_id}'.")
        return target

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def retention_mode(self) -> str:
        return self._retention_mode

    @property
    def vault_dir(self) -> pathlib.Path:
        return self._vault_dir

    @property
    def vault_root(self) -> pathlib.Path:
        return self._vault_root

    def _atomic_write_text(self, target: pathlib.Path, content: str) -> None:
        with self._lock:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp_name = f".{target.name}.{os.getpid()}.{threading.get_ident()}.{secrets.token_hex(4)}.tmp"
            tmp_path = target.with_name(tmp_name)
            try:
                with open(tmp_path, "w", encoding="utf-8", newline="\n") as fh:
                    fh.write(content)
                    fh.flush()
                    try:
                        os.fsync(fh.fileno())
                    except OSError:
                        pass
                os.replace(tmp_path, target)
            except Exception:
                try:
                    if tmp_path.exists():
                        tmp_path.unlink()
                except OSError:
                    pass
                raise

    def _atomic_write_json(self, target: pathlib.Path, data: Any) -> None:
        serialized = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
        self._atomic_write_text(target, serialized)

    def _read_json(self, target: pathlib.Path) -> Any | None:
        with self._lock:
            if not target.is_file():
                return None
            try:
                return json.loads(target.read_text(encoding="utf-8", errors="replace"))
            except (OSError, ValueError):
                return None

    def _update_manifest_fields(self, updates: dict[str, Any]) -> None:
        with self._lock:
            manifest_path = self._vault_dir / "00_MANIFEST.json"
            existing = self._read_json(manifest_path)
            if not isinstance(existing, dict):
                existing = {
                    "run_id": self._run_id,
                    "retention_mode": self._retention_mode,
                    "created_at": _utc_now_iso(),
                }
            existing.update(updates)
            existing["updated_at"] = _utc_now_iso()
            self._atomic_write_json(manifest_path, existing)

    def save_mission(self, mission: dict[str, Any]) -> None:
        with self._lock:
            manifest_path = self._vault_dir / "00_MANIFEST.json"
            existing = self._read_json(manifest_path)
            base: dict[str, Any] = existing if isinstance(existing, dict) else {}
            created_at = base.get("created_at") or _utc_now_iso()
            merged = {
                **base,
                **mission,
                "run_id": self._run_id,
                "retention_mode": self._retention_mode,
                "created_at": created_at,
                "updated_at": _utc_now_iso(),
            }
            self._atomic_write_json(manifest_path, merged)

    def get_mission(self) -> dict[str, Any] | None:
        data = self._read_json(self._vault_dir / "00_MANIFEST.json")
        return data if isinstance(data, dict) else None

    def save_surface_map(self, surface_map: dict[str, Any]) -> None:
        self._atomic_write_json(self._vault_dir / "01_SURFACE_MAP.json", surface_map)

    def get_surface_map(self) -> dict[str, Any] | None:
        data = self._read_json(self._vault_dir / "01_SURFACE_MAP.json")
        return data if isinstance(data, dict) else None

    def save_hypotheses(self, hypotheses: list[dict[str, Any]]) -> None:
        self._atomic_write_json(self._vault_dir / "02_HYPOTHESES.json", list(hypotheses))

    def get_hypotheses(self) -> list[dict[str, Any]]:
        data = self._read_json(self._vault_dir / "02_HYPOTHESES.json")
        return data if isinstance(data, list) else []

    def save_ledger(self, ledger_md: str) -> None:
        self._atomic_write_text(self._vault_dir / "03_EVIDENCE_LEDGER.md", str(ledger_md or ""))

    def get_ledger(self) -> str:
        with self._lock:
            path = self._vault_dir / "03_EVIDENCE_LEDGER.md"
            if not path.is_file():
                return ""
            try:
                return path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return ""

    def record_step_trace(self, entry: dict[str, Any]) -> None:
        with self._lock:
            trace_path = self._vault_dir / "03_TRACE.jsonl"
            existing = ""
            if trace_path.is_file():
                try:
                    existing = trace_path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    existing = ""
            record = dict(entry)
            record.setdefault("ts", _utc_now_iso())
            line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
            if existing and not existing.endswith("\n"):
                existing += "\n"
            self._atomic_write_text(trace_path, existing + line + "\n")

    def get_trace(self) -> list[dict[str, Any]]:
        with self._lock:
            trace_path = self._vault_dir / "03_TRACE.jsonl"
            if not trace_path.is_file():
                return []
            entries: list[dict[str, Any]] = []
            try:
                for line in trace_path.read_text(encoding="utf-8", errors="replace").splitlines():
                    s = line.strip()
                    if not s:
                        continue
                    try:
                        item = json.loads(s)
                        if isinstance(item, dict):
                            entries.append(item)
                    except ValueError:
                        continue
            except OSError:
                return []
            return entries

    def save_checkpoint(self, step_num: int, state: dict[str, Any]) -> None:
        step_int = max(0, int(step_num))
        with self._lock:
            cp_dir = self._vault_dir / "checkpoints"
            cp_dir.mkdir(parents=True, exist_ok=True)
            payload = {
                "run_id": self._run_id,
                "step": step_int,
                "saved_at": _utc_now_iso(),
                "state": state,
            }
            self._atomic_write_json(cp_dir / f"step_{step_int}.json", payload)
            manifest_updates: dict[str, Any] = {"latest_step": step_int}
            if isinstance(state, dict) and state.get("phase"):
                manifest_updates["status"] = str(state["phase"])
            self._update_manifest_fields(manifest_updates)

    def load_checkpoint(self) -> dict[str, Any] | None:
        with self._lock:
            cp_dir = self._vault_dir / "checkpoints"
            if not cp_dir.is_dir():
                return None
            best_step = -1
            best_path: pathlib.Path | None = None
            for item in cp_dir.iterdir():
                if not item.is_file():
                    continue
                m = CHECKPOINT_FILE_RE.match(item.name)
                if m:
                    step_n = int(m.group(1))
                    if step_n >= best_step:
                        best_step = step_n
                        best_path = item
            if best_path is None:
                return None
            data = self._read_json(best_path)
            return data if isinstance(data, dict) else None

    def save_final_report(
        self, report_md: str, verdict: dict[str, Any] | None = None
    ) -> None:
        with self._lock:
            self._atomic_write_text(self._vault_dir / "FINAL_REPORT.md", str(report_md or ""))
            updates: dict[str, Any] = {
                "has_report": True,
                "finished_at": _utc_now_iso(),
            }
            if isinstance(verdict, dict):
                updates["verdict"] = verdict
                if verdict.get("code"):
                    updates["status"] = str(verdict["code"])
            self._update_manifest_fields(updates)

    def get_final_report(self) -> str | None:
        with self._lock:
            path = self._vault_dir / "FINAL_REPORT.md"
            if not path.is_file():
                return None
            try:
                return path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return None

    def get_verdict(self) -> dict[str, Any] | None:
        manifest = self.get_mission()
        if isinstance(manifest, dict) and isinstance(manifest.get("verdict"), dict):
            return dict(manifest["verdict"])
        return None

    def rolling_purge_step(
        self, step_num: int, raw_buffers: list[Any] | None = None
    ) -> None:
        # Persistent vault keeps full trace on disk; only clear optional caller transient buffers.
        if raw_buffers is not None:
            _wipe_nested(raw_buffers)

    def purge_working_state_on_finish(self) -> None:
        # Persistent vault retains all audit files until explicit delete_vault().
        return None

    def purge_report(self) -> None:
        # Persistent vault report is retained on disk; removal is explicit via delete_vault().
        return None

    def get_artifacts_dir(self) -> pathlib.Path | None:
        path = self._vault_dir / "artifacts"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def get_kv_cache_dir(self) -> pathlib.Path | None:
        path = self._vault_dir / "kv_cache"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def auto_purge_remaining_sec(self) -> int | None:
        return None

    @classmethod
    def open_existing(
        cls, vault_root: pathlib.Path | str | None, run_id: str
    ) -> "DiskVaultStore":
        root = (
            pathlib.Path(vault_root).resolve()
            if vault_root is not None
            else DEFAULT_VAULT_ROOT.resolve()
        )
        target = cls._resolve_safe_vault_dir(root, run_id)
        if not target.is_dir():
            raise FileNotFoundError(f"Persistent vault not found: '{run_id}'")
        return cls(run_id=run_id, vault_root=root, create_dirs=False)

    @classmethod
    def list_vaults(
        cls, vault_root: pathlib.Path | str | None = None
    ) -> list[dict[str, Any]]:
        root = (
            pathlib.Path(vault_root).resolve()
            if vault_root is not None
            else DEFAULT_VAULT_ROOT.resolve()
        )
        if not root.is_dir():
            return []

        vaults: list[dict[str, Any]] = []
        for entry in sorted(root.iterdir(), key=lambda p: p.name, reverse=True):
            if not entry.is_dir():
                continue
            try:
                safe_id = cls.validate_run_id(entry.name)
            except ValueError:
                continue

            manifest_path = entry / "00_MANIFEST.json"
            manifest: dict[str, Any] = {}
            if manifest_path.is_file():
                try:
                    loaded = json.loads(manifest_path.read_text(encoding="utf-8", errors="replace"))
                    if isinstance(loaded, dict):
                        manifest = loaded
                except (OSError, ValueError):
                    pass

            report_path = entry / "FINAL_REPORT.md"
            trace_path = entry / "03_TRACE.jsonl"
            trace_count = 0
            if trace_path.is_file():
                try:
                    trace_count = sum(
                        1
                        for line in trace_path.read_text(encoding="utf-8", errors="replace").splitlines()
                        if line.strip()
                    )
                except OSError:
                    trace_count = 0

            size_bytes = 0
            try:
                for f in entry.rglob("*"):
                    if f.is_file():
                        size_bytes += f.stat().st_size
            except OSError:
                pass

            mission_text = str(manifest.get("mission") or "")
            vaults.append(
                {
                    "run_id": safe_id,
                    "retention_mode": "persistent",
                    "created_at": manifest.get("created_at"),
                    "updated_at": manifest.get("updated_at"),
                    "finished_at": manifest.get("finished_at"),
                    "status": manifest.get("status") or ("DONE" if report_path.is_file() else "UNKNOWN"),
                    "verdict": manifest.get("verdict"),
                    "mission_preview": (mission_text[:160] + "...") if len(mission_text) > 160 else mission_text,
                    "target_path": manifest.get("target_path"),
                    "has_report": report_path.is_file(),
                    "latest_step": int(manifest.get("latest_step") or 0),
                    "trace_steps": trace_count,
                    "size_bytes": size_bytes,
                }
            )
        return vaults

    @classmethod
    def load_vault_detail(
        cls, vault_root: pathlib.Path | str | None, run_id: str
    ) -> dict[str, Any] | None:
        root = (
            pathlib.Path(vault_root).resolve()
            if vault_root is not None
            else DEFAULT_VAULT_ROOT.resolve()
        )
        target = cls._resolve_safe_vault_dir(root, run_id)
        if not target.is_dir():
            return None

        store = cls(run_id=run_id, vault_root=root, create_dirs=False)
        artifacts_dir = target / "artifacts"
        artifacts: list[str] = []
        if artifacts_dir.is_dir():
            for p in sorted(artifacts_dir.rglob("*")):
                if p.is_file():
                    artifacts.append(str(p.relative_to(artifacts_dir)).replace("\\", "/"))

        cp = store.load_checkpoint()
        trace = store.get_trace()
        return {
            "run_id": store.run_id,
            "retention_mode": "persistent",
            "vault_dir": str(target),
            "manifest": store.get_mission(),
            "surface_map": store.get_surface_map(),
            "hypotheses": store.get_hypotheses(),
            "ledger": store.get_ledger(),
            "trace_count": len(trace),
            "trace_tail": trace[-20:],
            "latest_checkpoint_step": cp.get("step") if isinstance(cp, dict) else None,
            "artifacts": artifacts,
            "final_report": store.get_final_report(),
            "verdict": store.get_verdict(),
        }

    @classmethod
    def delete_vault(
        cls, vault_root: pathlib.Path | str | None, run_id: str
    ) -> bool:
        root = (
            pathlib.Path(vault_root).resolve()
            if vault_root is not None
            else DEFAULT_VAULT_ROOT.resolve()
        )
        target = cls._resolve_safe_vault_dir(root, run_id)
        if not target.exists():
            return False
        shutil.rmtree(target)
        return True
