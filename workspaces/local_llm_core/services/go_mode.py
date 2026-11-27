"""Permission-gated Go Mode service with 4 configurable guardrails, Level 5 Hard System Shield & Break-Glass ritual."""

from __future__ import annotations

import datetime
import difflib
import json
import os
import pathlib
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
from dataclasses import asdict, dataclass, field
from typing import Any

from workspaces.orchestrator.engine.schemas import SchemaValidationError, validate_target_path

VALID_SHELL_PROFILES = ("read_only", "dev_allowlist", "workspace_shell")
VALID_UNDO_STORAGE_MODES = ("disk", "ram")
VALID_ACTIONS = (
    "write_file",
    "create_file",
    "overwrite_file",
    "delete_file",
    "run_command",
    "read_external_path",
    "install_package",
)

DEV_ALLOWLIST_EXES = frozenset({"pytest", "ruff", "python", "python3", "py", "node", "npm", "uv"})
DEV_ALLOWLIST_GIT_SUBCOMMANDS = frozenset({"status", "diff", "log", "show", "branch"})
_SHELL_METACHAR_TOKENS = frozenset({"&&", "||", ";", "|", ">", ">>", "<", "<<", "`"})

BREAK_GLASS_PHRASE = "I-ACKNOWLEDGE-CRITICAL-SYSTEM-RISK-AND-UNLOCK-PROTECTED-BOUNDARY"
BREAK_GLASS_REQUIRED_CHECKBOXES: tuple[dict[str, str], ...] = (
    {
        "id": "ack_os_damage",
        "label": "I understand this bypasses Level 5 OS/Kernel protection and can permanently damage the operating system.",
    },
    {
        "id": "ack_no_rollback",
        "label": "I understand automatic Atomic Rollback cannot restore system files, boot records, or Windows Registry keys.",
    },
    {
        "id": "ack_full_responsibility",
        "label": "I assume full responsibility for this single-session break-glass override and will re-lock immediately after.",
    },
)

# Level 5 Hard System Shield patterns
_L5_CREDENTIAL_MARKERS = (
    ".ssh",
    ".gnupg",
    ".aws",
    ".kube",
    ".azure",
    ".docker",
    "google\\chrome\\user data",
    "google/chrome/user data",
    "microsoft\\edge\\user data",
    "microsoft/edge/user data",
    "mozilla\\firefox\\profiles",
    "mozilla/firefox/profiles",
    "login data",
    "credential",
)

_L5_WINDOWS_SYSTEM_PATHS = (
    "c:\\windows",
    "c:/windows",
    "\\windows\\system32",
    "/windows/system32",
    "\\windows\\syswow64",
    "/windows/syswow64",
    "c:\\program files",
    "c:/program files",
    "c:\\program files (x86)",
    "c:/program files (x86)",
    "c:\\programdata",
    "c:/programdata",
    "c:\\recovery",
    "c:/recovery",
    "c:\\boot",
    "c:/boot",
    "\\efi\\",
    "/efi/",
)

_L5_POSIX_SYSTEM_PREFIXES = (
    "/etc",
    "/usr",
    "/bin",
    "/sbin",
    "/boot",
    "/lib",
    "/lib64",
    "/var",
    "/proc",
    "/sys",
    "/dev",
    "/root",
)

_L5_REGISTRY_RE = re.compile(
    r"(^|[\s\"'\\/])(HKLM|HKCU|HKCR|HKU|HKCC|HKEY_LOCAL_MACHINE|HKEY_CURRENT_USER|HKEY_CLASSES_ROOT|HKEY_USERS)([\\/:]|$)",
    re.IGNORECASE,
)

_L5_PRIV_ESC_RE = re.compile(
    r"\b(runas|gsudo|sudo|su)\b|-verb\s+runas",
    re.IGNORECASE,
)

_L5_DESTRUCTIVE_CMD_RES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("diskpart/format/mkfs", re.compile(r"\b(diskpart|format|mkfs(?:\.[a-z0-9]+)?|fdisk|parted)\b", re.IGNORECASE)),
    ("boot/vss/sfc/dism", re.compile(r"\b(bcdedit|vssadmin|wbadmin|cipher\s+/w|sfc\s+/scannow|dism)\b", re.IGNORECASE)),
    ("registry mutation", re.compile(r"\breg(\.exe)?\s+(delete|add|import|restore|load|unload)\b", re.IGNORECASE)),
    ("service mutation", re.compile(r"\bsc(\.exe)?\s+(delete|config|stop|failure|sdset)\b", re.IGNORECASE)),
    ("shutdown/reboot", re.compile(r"\b(shutdown|reboot|halt|poweroff|stop-computer|restart-computer)\b", re.IGNORECASE)),
    ("credential manager", re.compile(r"\b(cmdkey|vaultcmd)\b", re.IGNORECASE)),
    ("destructive root wipe", re.compile(r"\brm\s+-[a-z]*r[a-z]*f[a-z]*\s+(/($|\s|\*)|[a-z]:[\\/]\s*$)", re.IGNORECASE)),
    ("raw block device write", re.compile(r"\bdd\s+if=.*\bof=/dev/", re.IGNORECASE)),
)

_L5_ACL_CMD_RE = re.compile(r"\b(takeown|icacls|chmod\s+-R\s+777|chown\s+-R)\b", re.IGNORECASE)
_DRIVE_ROOT_RE = re.compile(r"^[a-zA-Z]:[\\/]?$")


def _utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()


def _normalize_cmp(raw: str) -> str:
    return raw.replace("/", "\\").strip().lower()


def _decode_path_for_security(raw_path: str, max_passes: int = 3) -> str:
    cur = raw_path
    for _ in range(max_passes):
        nxt = urllib.parse.unquote(cur)
        if nxt == cur:
            break
        cur = nxt
    return cur


def _symlink_escapes_workspace(
    raw_path: str,
    ws_root: pathlib.Path,
) -> tuple[bool, str]:
    """
    Detect symlinks along a workspace-relative (or lexically contained) path
    whose `resolve()` escapes `ws_root`.
    """
    cand = pathlib.Path(raw_path).expanduser()
    resolved_ws = ws_root.resolve()
    ws_lexical = pathlib.Path(os.path.abspath(str(ws_root)))

    if not cand.is_absolute():
        lex_target = pathlib.Path(os.path.abspath(str(ws_lexical / cand)))
        try:
            lex_target.relative_to(ws_lexical)
        except ValueError:
            return False, ""
    else:
        lex_target = pathlib.Path(os.path.abspath(str(cand)))
        try:
            lex_target.relative_to(ws_lexical)
        except ValueError:
            return False, ""

    rel_parts = lex_target.relative_to(ws_lexical).parts
    probe = ws_lexical
    for part in rel_parts:
        probe = probe / part
        if probe.is_symlink() and not GoModeManager._is_within_root(probe, resolved_ws):
            return (
                True,
                f"Symlink escape outside workspace rejected: '{probe}' -> '{probe.resolve()}'",
            )
    if lex_target.is_symlink() and not GoModeManager._is_within_root(lex_target, resolved_ws):
        return (
            True,
            f"Symlink target outside workspace rejected: '{lex_target}' -> '{lex_target.resolve()}'",
        )
    return False, ""


def _is_system_or_protected_path(raw_path: str) -> tuple[bool, str]:
    """Check whether a path string targets OS system roots, drive roots, registry, or credential vaults."""
    if not raw_path or not isinstance(raw_path, str):
        return False, ""
    stripped = raw_path.strip()
    norm_win = stripped.replace("/", "\\").lower()
    norm_posix = stripped.replace("\\", "/").lower()

    if _L5_REGISTRY_RE.search(stripped):
        return True, f"Registry hive access blocked by Level 5 Hard Shield: {stripped}"

    if _DRIVE_ROOT_RE.match(stripped) or stripped in ("/", "\\"):
        return True, f"Filesystem/drive root blocked by Level 5 Hard Shield: {stripped}"

    for marker in _L5_CREDENTIAL_MARKERS:
        if marker in norm_win or marker in norm_posix:
            return True, f"Credential vault or browser profile blocked by Level 5 Hard Shield: {stripped}"

    for sys_path in _L5_WINDOWS_SYSTEM_PATHS:
        if norm_win == sys_path or norm_win.startswith(sys_path.rstrip("\\") + "\\") or sys_path in norm_win:
            return True, f"OS system directory blocked by Level 5 Hard Shield: {stripped}"

    for p_prefix in _L5_POSIX_SYSTEM_PREFIXES:
        if norm_posix == p_prefix or norm_posix.startswith(p_prefix + "/"):
            if p_prefix == "/var" and (
                norm_posix.startswith("/var/folders/") or norm_posix.startswith("/var/tmp/")
            ):
                continue
            return True, f"POSIX system directory blocked by Level 5 Hard Shield: {stripped}"

    # Resolve real path when possible to catch symlink/relative escapes to system roots
    try:
        resolved = pathlib.Path(stripped).expanduser().resolve()
        res_str = str(resolved)
        res_win = res_str.replace("/", "\\").lower()
        res_posix = res_str.replace("\\", "/").lower()
        if resolved == resolved.parent:
            return True, f"Resolved drive/filesystem root blocked by Level 5 Hard Shield: {resolved}"
        for marker in _L5_CREDENTIAL_MARKERS:
            if marker in res_win or marker in res_posix:
                return True, f"Resolved credential vault blocked by Level 5 Hard Shield: {resolved}"
        if os.name == "nt":
            sys_drive = os.environ.get("SystemDrive", "C:").lower()
            for env_key, suffix in (
                ("WINDIR", "\\windows"),
                ("SystemRoot", "\\windows"),
                ("ProgramFiles", "\\program files"),
                ("ProgramFiles(x86)", "\\program files (x86)"),
                ("ProgramData", "\\programdata"),
            ):
                base = (os.environ.get(env_key) or (sys_drive + suffix)).replace("/", "\\").lower()
                if res_win == base or res_win.startswith(base.rstrip("\\") + "\\"):
                    return True, f"Resolved OS protected tree blocked by Level 5 Hard Shield: {resolved}"
    except OSError:
        pass

    return False, ""


def check_level5_hard_shield(
    action: str,
    *,
    path: str | None = None,
    command: str | None = None,
    workspace_root: pathlib.Path | None = None,
) -> tuple[bool, str]:
    """
    Evaluate Level 5 Hard System Protection Shield (always enforced server-side).

    Returns `(allowed, reason)`. If `allowed` is False, the operation MUST be blocked
    unless a valid Break-Glass override token is presented.
    """
    if path:
        blocked, reason = _is_system_or_protected_path(path)
        if blocked:
            return False, reason
        if workspace_root is not None and not pathlib.Path(path).is_absolute():
            candidate = (workspace_root / path).resolve()
            blocked_cand, reason_cand = _is_system_or_protected_path(str(candidate))
            if blocked_cand:
                return False, reason_cand

    if command:
        cmd_str = command.strip()
        if _L5_PRIV_ESC_RE.search(cmd_str):
            return False, f"Privilege escalation blocked by Level 5 Hard Shield: {cmd_str[:120]}"
        if _L5_REGISTRY_RE.search(cmd_str):
            return False, f"Windows Registry command blocked by Level 5 Hard Shield: {cmd_str[:120]}"
        for label, rx in _L5_DESTRUCTIVE_CMD_RES:
            if rx.search(cmd_str):
                return False, f"Destructive/low-level command ({label}) blocked by Level 5 Hard Shield."

        # Check takeown/icacls/chmod/chown when targeting OS/system roots or drive roots
        if _L5_ACL_CMD_RE.search(cmd_str):
            try:
                tokens = shlex.split(cmd_str, posix=(os.name != "nt"))
            except ValueError:
                tokens = cmd_str.split()
            for tok in tokens:
                blocked_tok, reason_tok = _is_system_or_protected_path(tok)
                if blocked_tok:
                    return False, f"ACL/ownership modification on protected path blocked by Level 5: {reason_tok}"

        # Also inspect every token/quoted path inside command for OS roots or credential vaults
        try:
            tokens = shlex.split(cmd_str, posix=(os.name != "nt"))
        except ValueError:
            tokens = cmd_str.split()
        for tok in tokens:
            clean_tok = tok.strip("\"'")
            if not clean_tok or clean_tok.startswith("-"):
                continue
            blocked_tok, reason_tok = _is_system_or_protected_path(clean_tok)
            if blocked_tok:
                return False, reason_tok

    _ = action
    return True, ""


@dataclass
class GoModeConfig:
    """User-configurable Go Mode guardrails (Levels 1-4) + immutable Level 5 shield."""

    workspace_root: str = ""
    session_id: str = "default"
    # Level 1: Workspace Sandbox & File Writes
    hitl_per_diff: bool = True
    undo_journal_enabled: bool = True
    undo_journal_mode: str = "disk"  # "disk" (.localllm_undo/<session_id>/) or "ram"
    # Level 2: File Deletions & Destructive Overwrites
    confirm_deletions: bool = True
    # Level 3: Shell & Tool Execution
    shell_profile: str = "dev_allowlist"  # "read_only" | "dev_allowlist" | "workspace_shell"
    confirm_shell: bool = True
    shell_timeout_sec: int = 30
    # Level 4: External Paths & Package Installs
    confirm_external_and_packages: bool = True
    # Level 5: Hard System Protection Shield (ALWAYS True on server)
    level5_shield: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace_root": self.workspace_root,
            "session_id": self.session_id,
            "hitl_per_diff": self.hitl_per_diff,
            "undo_journal_enabled": self.undo_journal_enabled,
            "undo_journal_mode": self.undo_journal_mode,
            "confirm_deletions": self.confirm_deletions,
            "shell_profile": self.shell_profile,
            "confirm_shell": self.confirm_shell,
            "shell_timeout_sec": self.shell_timeout_sec,
            "confirm_external_and_packages": self.confirm_external_and_packages,
            "level5_shield": True,
        }


@dataclass
class UndoJournalEntry:
    """Atomic pre-mutation snapshot allowing 100% deterministic rollback."""

    step_id: str
    session_id: str
    proposal_id: str
    action: str
    target_path: str
    existed_before: bool
    original_bytes: bytes | None
    backup_file: str | None
    created_at: str

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "session_id": self.session_id,
            "proposal_id": self.proposal_id,
            "action": self.action,
            "target_path": self.target_path,
            "existed_before": self.existed_before,
            "size_bytes": len(self.original_bytes) if self.original_bytes is not None else 0,
            "backup_file": self.backup_file,
            "created_at": self.created_at,
        }


@dataclass
class GoModeProposal:
    """Pending or executed Go Mode action proposal with unified diff & risk level."""

    proposal_id: str
    session_id: str
    action: str
    risk_level: int
    status: str  # "pending_approval" | "executed" | "rejected" | "failed"
    created_at: str
    target_path: str = ""
    rel_path: str = ""
    content: str = ""
    command: str = ""
    timeout_sec: int = 30
    diff: str = ""
    reason: str = ""
    requires_break_glass: bool = False
    executed_at: str | None = None
    execution_result: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "session_id": self.session_id,
            "action": self.action,
            "risk_level": self.risk_level,
            "status": self.status,
            "created_at": self.created_at,
            "target_path": self.target_path,
            "rel_path": self.rel_path,
            "command": self.command,
            "timeout_sec": self.timeout_sec,
            "diff": self.diff,
            "reason": self.reason,
            "requires_break_glass": self.requires_break_glass,
            "executed_at": self.executed_at,
            "execution_result": self.execution_result,
        }


class GoModeManager:
    """Thread-safe manager for Permission-Gated Go Mode, Undo Journal, and Level 5 Break-Glass."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._config = GoModeConfig()
        self._proposals: dict[str, GoModeProposal] = {}
        self._approved_folders: dict[str, set[str]] = {}
        self._undo_stacks: dict[str, list[UndoJournalEntry]] = {}
        self._audit_log: list[dict[str, Any]] = []
        self._break_glass_token: str | None = None
        self._break_glass_expires_monotonic: float = 0.0
        self._break_glass_expires_iso: str | None = None
        self._break_glass_session_id: str | None = None

    def _append_audit(self, event: str, detail: str, extra: dict[str, Any] | None = None) -> None:
        entry: dict[str, Any] = {
            "ts": _utc_now_iso(),
            "event": event,
            "detail": detail,
        }
        if extra:
            entry.update(extra)
        self._audit_log.append(entry)
        if len(self._audit_log) > 200:
            self._audit_log = self._audit_log[-200:]

    def _is_break_glass_active_locked(self) -> bool:
        if self._break_glass_token is None:
            return False
        if time.monotonic() >= self._break_glass_expires_monotonic:
            self._append_audit("BREAK_GLASS_EXPIRED", "Level 5 Break-Glass token expired; Shield auto-relocked.")
            self._break_glass_token = None
            self._break_glass_expires_monotonic = 0.0
            self._break_glass_expires_iso = None
            self._break_glass_session_id = None
            return False
        return True

    def _validate_break_glass_token_locked(self, supplied_token: str | None) -> bool:
        if not self._is_break_glass_active_locked():
            return False
        if not supplied_token or not isinstance(supplied_token, str):
            return False
        return secrets.compare_digest(supplied_token.strip(), self._break_glass_token or "")

    def get_break_glass_status(self) -> dict[str, Any]:
        with self._lock:
            active = self._is_break_glass_active_locked()
            rem = max(0, int(round(self._break_glass_expires_monotonic - time.monotonic()))) if active else 0
            return {
                "ok": True,
                "unlocked": active,
                "level5_shield_active": not active,
                "expires_at": self._break_glass_expires_iso if active else None,
                "remaining_sec": rem,
                "session_id": self._break_glass_session_id if active else None,
                "required_phrase": BREAK_GLASS_PHRASE,
                "required_checkboxes": list(BREAK_GLASS_REQUIRED_CHECKBOXES),
            }

    def unlock_break_glass(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """Validate >= 3 distinct checkboxes + exact manual phrase to issue a short-lived token."""
        if not isinstance(payload, dict):
            return 400, {"ok": False, "error": "Payload must be a JSON object."}

        raw_checks = payload.get("checkboxes")
        if raw_checks is None:
            raw_checks = payload.get("acknowledgements")
        checks_map: dict[str, bool] = {}
        if isinstance(raw_checks, dict):
            for k, v in raw_checks.items():
                checks_map[str(k)] = bool(v)
        elif isinstance(raw_checks, list):
            for item in raw_checks:
                if isinstance(item, str) and item.strip():
                    checks_map[item.strip()] = True

        # Also support top-level checkbox booleans
        for item in BREAK_GLASS_REQUIRED_CHECKBOXES:
            cid = item["id"]
            if cid in payload:
                checks_map[cid] = bool(payload[cid])

        required_ids = [c["id"] for c in BREAK_GLASS_REQUIRED_CHECKBOXES]
        missing_checks = [cid for cid in required_ids if not checks_map.get(cid, False)]
        true_count = sum(1 for v in checks_map.values() if v)
        if missing_checks or true_count < 3:
            with self._lock:
                self._append_audit(
                    "BREAK_GLASS_DENIED",
                    f"Missing required risk acknowledgements: {missing_checks}",
                )
            return 403, {
                "ok": False,
                "error": (
                    "Break-Glass requires at least 3 distinct explicit risk checkboxes "
                    f"(missing: {', '.join(missing_checks) or 'required checkboxes'})."
                ),
                "missing_checkboxes": missing_checks,
            }

        raw_phrase = payload.get("confirmation_phrase")
        if raw_phrase is None:
            raw_phrase = payload.get("phrase")
        if not isinstance(raw_phrase, str) or raw_phrase.strip() != BREAK_GLASS_PHRASE:
            with self._lock:
                self._append_audit("BREAK_GLASS_DENIED", "Confirmation phrase mismatch.")
            return 403, {
                "ok": False,
                "error": "Manual confirmation phrase does not match the exact required server phrase.",
            }

        ttl_raw = payload.get("ttl_seconds", payload.get("ttl_sec", 600))
        try:
            ttl_sec = max(30, min(900, int(ttl_raw)))
        except (TypeError, ValueError):
            ttl_sec = 600

        session_id = str(payload.get("session_id") or self._config.session_id or "default").strip()
        token = secrets.token_urlsafe(24)
        expires_dt = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=ttl_sec)
        expires_iso = expires_dt.replace(microsecond=0).isoformat()

        with self._lock:
            self._break_glass_token = token
            self._break_glass_expires_monotonic = time.monotonic() + ttl_sec
            self._break_glass_expires_iso = expires_iso
            self._break_glass_session_id = session_id
            self._append_audit(
                "BREAK_GLASS_UNLOCKED",
                f"Level 5 Break-Glass unlocked for session '{session_id}' (TTL={ttl_sec}s).",
                {"session_id": session_id, "ttl_sec": ttl_sec, "expires_at": expires_iso},
            )

        return 200, {
            "ok": True,
            "unlocked": True,
            "token": token,
            "session_id": session_id,
            "ttl_seconds": ttl_sec,
            "expires_at": expires_iso,
            "warning": "CRITICAL: Level 5 System Protection Shield temporarily unlocked. Re-lock immediately after action.",
        }

    def relock_break_glass(self) -> dict[str, Any]:
        with self._lock:
            was_unlocked = self._is_break_glass_active_locked()
            self._break_glass_token = None
            self._break_glass_expires_monotonic = 0.0
            self._break_glass_expires_iso = None
            self._break_glass_session_id = None
            if was_unlocked:
                self._append_audit("BREAK_GLASS_RELOCKED", "Level 5 Hard Shield manually re-locked.")
        return {"ok": True, "unlocked": False, "level5_shield_active": True}

    def get_config(self) -> dict[str, Any]:
        with self._lock:
            bg = self.get_break_glass_status()
            sid = self._config.session_id
            approved = sorted(self._approved_folders.get(sid, set()))
            return {
                "ok": True,
                "config": self._config.to_dict(),
                "approved_folders": approved,
                "break_glass": bg,
            }

    def update_config(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        if not isinstance(payload, dict):
            return 400, {"ok": False, "error": "Payload must be a JSON object."}

        # Level 5 CANNOT be disabled via config!
        if payload.get("level5_shield") is False or payload.get("disable_level5") is True:
            return 403, {
                "ok": False,
                "blocked_by": "level5_hard_shield",
                "error": "Level 5 Hard System Protection Shield is always enforced server-side and cannot be disabled via config. Use the Break-Glass ritual.",
            }

        with self._lock:
            if "workspace_root" in payload:
                raw_ws = str(payload.get("workspace_root") or "").strip()
                if raw_ws:
                    try:
                        validated = validate_target_path(raw_ws)
                    except SchemaValidationError as exc:
                        return exc.http_status, {"ok": False, "error": exc.message}
                    self._config.workspace_root = str(validated)
                else:
                    self._config.workspace_root = ""

            if "session_id" in payload and str(payload["session_id"]).strip():
                self._config.session_id = re.sub(r"[^a-zA-Z0-9._-]", "_", str(payload["session_id"]).strip())[:64]

            if "hitl_per_diff" in payload:
                self._config.hitl_per_diff = bool(payload["hitl_per_diff"])
            if "undo_journal_enabled" in payload:
                self._config.undo_journal_enabled = bool(payload["undo_journal_enabled"])
            if "undo_journal_mode" in payload:
                mode = str(payload["undo_journal_mode"] or "disk").strip().lower()
                if mode not in VALID_UNDO_STORAGE_MODES:
                    return 400, {"ok": False, "error": f"Invalid undo_journal_mode: {mode}"}
                self._config.undo_journal_mode = mode
            if "confirm_deletions" in payload:
                self._config.confirm_deletions = bool(payload["confirm_deletions"])
            if "shell_profile" in payload:
                prof = str(payload["shell_profile"] or "").strip().lower()
                if prof not in VALID_SHELL_PROFILES:
                    return 400, {"ok": False, "error": f"Invalid shell_profile: {prof}"}
                self._config.shell_profile = prof
            if "confirm_shell" in payload:
                self._config.confirm_shell = bool(payload["confirm_shell"])
            if "shell_timeout_sec" in payload:
                try:
                    self._config.shell_timeout_sec = max(1, min(120, int(payload["shell_timeout_sec"])))
                except (TypeError, ValueError):
                    return 400, {"ok": False, "error": "Invalid shell_timeout_sec"}
            if "confirm_external_and_packages" in payload:
                self._config.confirm_external_and_packages = bool(payload["confirm_external_and_packages"])

            self._append_audit("CONFIG_UPDATED", f"Updated Go Mode config (shell_profile={self._config.shell_profile}).")
            return 200, self.get_config()

    def _resolve_workspace_root_locked(self) -> pathlib.Path | None:
        if not self._config.workspace_root:
            return None
        return pathlib.Path(self._config.workspace_root).resolve()

    @staticmethod
    def _is_within_root(target: pathlib.Path, root: pathlib.Path) -> bool:
        try:
            target.resolve().relative_to(root.resolve())
            return True
        except ValueError:
            return False

    @staticmethod
    def _split_command_args(command: str) -> list[str]:
        tokens = shlex.split(command, posix=(os.name != "nt"))
        if os.name == "nt":
            cleaned: list[str] = []
            for tok in tokens:
                if len(tok) >= 2 and ((tok[0] == '"' and tok[-1] == '"') or (tok[0] == "'" and tok[-1] == "'")):
                    tok = tok[1:-1]
                cleaned.append(tok)
            return cleaned
        return tokens

    @staticmethod
    def _validate_dev_allowlist_command(command: str) -> tuple[bool, list[str], str]:
        """Validate command against strict dev_allowlist with shell=False and zero chaining."""
        try:
            argv = GoModeManager._split_command_args(command)
        except ValueError as exc:
            return False, [], f"Invalid command syntax: {exc}"
        if not argv:
            return False, [], "Empty command."
        for tok in argv:
            if tok in _SHELL_METACHAR_TOKENS or any(op in tok for op in ("&&", "||", ";", "|", "`", "$(")):
                return False, [], f"Shell chaining/redirection operator rejected in dev_allowlist: {tok!r}"

        exe = pathlib.Path(argv[0]).name.lower()
        if exe.endswith(".exe"):
            exe = exe[:-4]
        if exe == "git":
            sub = argv[1].lower() if len(argv) > 1 else ""
            if sub not in DEV_ALLOWLIST_GIT_SUBCOMMANDS:
                return False, [], f"Git subcommand '{sub}' is not in dev_allowlist ({sorted(DEV_ALLOWLIST_GIT_SUBCOMMANDS)})."
            return True, argv, ""
        if exe not in DEV_ALLOWLIST_EXES:
            return False, [], (
                f"Executable '{exe}' is not permitted in 'dev_allowlist'. "
                f"Allowed: {sorted(DEV_ALLOWLIST_EXES | {'git status/diff/log'})}."
            )
        return True, argv, ""

    @staticmethod
    def _detect_package_install_command(command: str) -> bool:
        low = command.strip().lower()
        return bool(
            re.search(
                r"\b(pip\s+install|pip3\s+install|python\s+-m\s+pip\s+install|npm\s+(install|i|add)|uv\s+(pip\s+install|add|sync))\b",
                low,
            )
        )

    @staticmethod
    def _compute_unified_diff(rel_label: str, old_text: str, new_text: str) -> str:
        lines = list(
            difflib.unified_diff(
                old_text.splitlines(keepends=True),
                new_text.splitlines(keepends=True),
                fromfile=f"a/{rel_label}",
                tofile=f"b/{rel_label}",
                n=3,
            )
        )
        return "".join(lines) or f"(no textual diff for {rel_label})"

    def _record_undo_entry_locked(
        self,
        *,
        session_id: str,
        proposal_id: str,
        action: str,
        target: pathlib.Path,
        ws_root: pathlib.Path | None,
    ) -> UndoJournalEntry:
        existed = target.exists() and target.is_file()
        orig_bytes = target.read_bytes() if existed else None
        step_id = f"undo_{len(self._undo_stacks.get(session_id, [])) + 1}_{secrets.token_hex(3)}"
        backup_file_str: str | None = None

        if (
            self._config.undo_journal_enabled
            and self._config.undo_journal_mode == "disk"
            and ws_root is not None
            and self._is_within_root(target, ws_root)
        ):
            undo_dir = ws_root / ".localllm_undo" / session_id
            undo_dir.mkdir(parents=True, exist_ok=True)
            manifest_path = undo_dir / "journal.json"
            if existed and orig_bytes is not None:
                bak_path = undo_dir / f"{step_id}.bak"
                bak_path.write_bytes(orig_bytes)
                backup_file_str = str(bak_path)
            entry_meta = {
                "step_id": step_id,
                "proposal_id": proposal_id,
                "action": action,
                "target_path": str(target),
                "existed_before": existed,
                "backup_file": backup_file_str,
                "created_at": _utc_now_iso(),
            }
            existing_manifest: list[dict[str, Any]] = []
            if manifest_path.is_file():
                try:
                    loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
                    if isinstance(loaded, list):
                        existing_manifest = loaded
                except (OSError, ValueError):
                    existing_manifest = []
            existing_manifest.append(entry_meta)
            tmp_m = undo_dir / f".journal.{secrets.token_hex(4)}.tmp"
            tmp_m.write_text(json.dumps(existing_manifest, indent=2), encoding="utf-8")
            tmp_m.replace(manifest_path)

        entry = UndoJournalEntry(
            step_id=step_id,
            session_id=session_id,
            proposal_id=proposal_id,
            action=action,
            target_path=str(target),
            existed_before=existed,
            original_bytes=orig_bytes,
            backup_file=backup_file_str,
            created_at=_utc_now_iso(),
        )
        self._undo_stacks.setdefault(session_id, []).append(entry)
        return entry

    def _execute_proposal_locked(
        self,
        prop: GoModeProposal,
        *,
        break_glass_token: str | None = None,
    ) -> tuple[int, dict[str, Any]]:
        ws_root = self._resolve_workspace_root_locked()
        bg_ok = self._validate_break_glass_token_locked(break_glass_token)

        # Defense-in-depth Level 5 check right before execution
        l5_ok, l5_reason = check_level5_hard_shield(
            prop.action,
            path=prop.target_path or None,
            command=prop.command or None,
            workspace_root=ws_root,
        )
        if not l5_ok and not bg_ok:
            prop.status = "failed"
            prop.reason = l5_reason
            self._append_audit("LEVEL5_BLOCKED", l5_reason, {"proposal_id": prop.proposal_id})
            return 403, {
                "ok": False,
                "blocked_by": "level5_hard_shield",
                "requires_break_glass": True,
                "error": l5_reason,
            }

        if not l5_ok and bg_ok:
            self._append_audit(
                "LEVEL5_BREAK_GLASS_EXEC",
                f"Executed Level 5 override action '{prop.action}' with valid Break-Glass token.",
                {"proposal_id": prop.proposal_id, "target_path": prop.target_path},
            )

        undo_entry: UndoJournalEntry | None = None
        result: dict[str, Any] = {}

        if prop.action in ("write_file", "create_file", "overwrite_file"):
            target = pathlib.Path(prop.target_path).resolve()
            if self._config.undo_journal_enabled:
                undo_entry = self._record_undo_entry_locked(
                    session_id=prop.session_id,
                    proposal_id=prop.proposal_id,
                    action=prop.action,
                    target=target,
                    ws_root=ws_root,
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp_file = target.parent / f".{target.name}.{secrets.token_hex(4)}.tmp"
            try:
                tmp_file.write_text(prop.content, encoding="utf-8")
                tmp_file.replace(target)
            except OSError as exc:
                if tmp_file.exists():
                    tmp_file.unlink(missing_ok=True)
                prop.status = "failed"
                return 500, {"ok": False, "error": f"File write failed: {exc}"}
            result = {
                "written": True,
                "path": str(target),
                "bytes": len(prop.content.encode("utf-8")),
            }

        elif prop.action == "delete_file":
            target = pathlib.Path(prop.target_path).resolve()
            if not target.exists():
                prop.status = "failed"
                return 404, {"ok": False, "error": f"File not found: {target}"}
            if target.is_dir():
                prop.status = "failed"
                return 400, {"ok": False, "error": "Directory deletion is not supported; specify a file."}
            # Level 2 mandatory pre-delete backup snapshot when rollback journal or confirm_deletions enabled
            if self._config.undo_journal_enabled or self._config.confirm_deletions:
                undo_entry = self._record_undo_entry_locked(
                    session_id=prop.session_id,
                    proposal_id=prop.proposal_id,
                    action=prop.action,
                    target=target,
                    ws_root=ws_root,
                )
            target.unlink()
            result = {"deleted": True, "path": str(target)}

        elif prop.action == "read_external_path":
            target = pathlib.Path(prop.target_path).resolve()
            if not target.exists() or not target.is_file():
                prop.status = "failed"
                return 404, {"ok": False, "error": f"External file not found: {target}"}
            text = target.read_text(encoding="utf-8", errors="replace")[:32768]
            result = {"read": True, "path": str(target), "preview": text}

        elif prop.action in ("run_command", "install_package"):
            if self._config.shell_profile == "read_only":
                prop.status = "failed"
                return 403, {"ok": False, "error": "Shell execution disabled in 'read_only' profile."}
            argv: list[str] = []
            use_shell = False
            if self._config.shell_profile == "dev_allowlist" and prop.action == "run_command":
                ok_cmd, argv, err_cmd = self._validate_dev_allowlist_command(prop.command)
                if not ok_cmd:
                    prop.status = "failed"
                    return 403, {"ok": False, "error": err_cmd}
            else:
                try:
                    argv = self._split_command_args(prop.command)
                except ValueError as exc:
                    prop.status = "failed"
                    return 400, {"ok": False, "error": f"Invalid command syntax: {exc}"}
                if not argv:
                    prop.status = "failed"
                    return 400, {"ok": False, "error": "Empty command."}
                if os.name == "nt" and not shutil.which(argv[0]):
                    argv = ["cmd.exe", "/c", prop.command]
                    use_shell = False

            if argv and pathlib.Path(argv[0]).stem.lower() in ("python", "python3", "py"):
                resolved_py = shutil.which(argv[0]) or ""
                if not resolved_py or "windowsapps" in resolved_py.lower():
                    argv[0] = sys.executable

            sub_kwargs: dict[str, Any] = {
                "cwd": str(ws_root) if ws_root and ws_root.is_dir() else None,
                "stdin": subprocess.DEVNULL,
                "capture_output": True,
                "text": True,
                "encoding": "utf-8",
                "errors": "replace",
                "timeout": prop.timeout_sec,
                "shell": use_shell,
            }
            if sys.platform == "win32" and hasattr(subprocess, "CREATE_NO_WINDOW"):
                sub_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            try:
                proc = subprocess.run(argv, **sub_kwargs)
                result = {
                    "exit_code": proc.returncode,
                    "stdout": (proc.stdout or "")[:16384],
                    "stderr": (proc.stderr or "")[:8192],
                }
            except subprocess.TimeoutExpired:
                prop.status = "failed"
                return 408, {"ok": False, "error": f"Command timed out after {prop.timeout_sec}s."}
            except OSError as exc:
                prop.status = "failed"
                return 500, {"ok": False, "error": f"Command execution failed: {exc}"}

        prop.status = "executed"
        prop.executed_at = _utc_now_iso()
        prop.execution_result = result
        self._append_audit(
            "ACTION_EXECUTED",
            f"Executed {prop.action} ({prop.target_path or prop.command[:80]})",
            {"proposal_id": prop.proposal_id, "risk_level": prop.risk_level},
        )
        return 200, {
            "ok": True,
            "executed": True,
            "proposal": prop.to_dict(),
            "result": result,
            "undo_step": undo_entry.to_public_dict() if undo_entry is not None else None,
        }

    def propose_action(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        if not isinstance(payload, dict):
            return 400, {"ok": False, "error": "Payload must be a JSON object."}

        action = str(payload.get("action") or "").strip().lower()
        if action not in VALID_ACTIONS:
            return 400, {"ok": False, "error": f"Invalid action '{action}'. Must be one of {VALID_ACTIONS}."}

        with self._lock:
            session_id = str(payload.get("session_id") or self._config.session_id or "default").strip()
            ws_root = self._resolve_workspace_root_locked()
            raw_path = str(payload.get("path") or payload.get("rel_path") or "").strip()
            command = str(payload.get("command") or "").strip()
            content = str(payload.get("content") or "")
            bg_token = payload.get("break_glass_token")
            if bg_token is not None:
                bg_token = str(bg_token)

            timeout_sec = self._config.shell_timeout_sec
            if "timeout_sec" in payload:
                try:
                    timeout_sec = max(1, min(120, int(payload["timeout_sec"])))
                except (TypeError, ValueError):
                    pass

            resolved_target_str = ""
            rel_label = raw_path
            outside_workspace = False

            if action in ("write_file", "create_file", "overwrite_file", "delete_file", "read_external_path"):
                if not raw_path:
                    return 400, {"ok": False, "error": "Field 'path' (or 'rel_path') is required."}

                decoded_path = _decode_path_for_security(raw_path)
                if "\x00" in raw_path or "\x00" in decoded_path:
                    self._append_audit("TRAVERSAL_BLOCKED", "Null byte rejected in Go Mode path.", {"action": action})
                    return 403, {
                        "ok": False,
                        "blocked_by": "path_traversal_guard",
                        "error": "Null byte in path argument is forbidden.",
                    }
                if "%" in raw_path and decoded_path != raw_path:
                    decoded_parts = [p for p in re.split(r"[\\/]+", decoded_path) if p]
                    if ".." in decoded_parts:
                        self._append_audit(
                            "TRAVERSAL_BLOCKED",
                            "URL-encoded path traversal rejected.",
                            {"action": action, "path": raw_path[:120]},
                        )
                        return 403, {
                            "ok": False,
                            "blocked_by": "path_traversal_guard",
                            "error": "URL-encoded path traversal ('..') is forbidden.",
                        }

                bg_ok = self._validate_break_glass_token_locked(bg_token)
                # Check Level 5 on raw_path and URL-decoded path BEFORE path resolution
                for path_variant in (raw_path, decoded_path):
                    l5_raw_ok, l5_raw_reason = check_level5_hard_shield(
                        action, path=path_variant, workspace_root=ws_root
                    )
                    if not l5_raw_ok and not bg_ok:
                        self._append_audit("LEVEL5_BLOCKED", l5_raw_reason, {"action": action, "path": path_variant})
                        return 403, {
                            "ok": False,
                            "blocked_by": "level5_hard_shield",
                            "requires_break_glass": True,
                            "error": l5_raw_reason,
                        }

                if ws_root is not None:
                    sym_escape, sym_reason = _symlink_escapes_workspace(decoded_path, ws_root)
                    if sym_escape:
                        self._append_audit(
                            "SYMLINK_ESCAPE_BLOCKED",
                            sym_reason,
                            {"action": action, "path": raw_path[:160]},
                        )
                        return 403, {
                            "ok": False,
                            "blocked_by": "symlink_workspace_escape",
                            "error": sym_reason,
                        }

                cand = pathlib.Path(raw_path).expanduser()
                if not cand.is_absolute():
                    if ws_root is None:
                        return 400, {"ok": False, "error": "Configure 'workspace_root' before proposing relative file actions."}
                    resolved_target = (ws_root / cand).resolve()
                else:
                    resolved_target = cand.resolve()
                resolved_target_str = str(resolved_target)

                if ws_root is not None and self._is_within_root(resolved_target, ws_root):
                    rel_label = resolved_target.relative_to(ws_root).as_posix()
                    outside_workspace = False
                else:
                    outside_workspace = True
                    if ws_root is None and action != "read_external_path":
                        return 400, {"ok": False, "error": "Configure 'workspace_root' before file mutations."}

                # Level 5 check on resolved absolute path
                l5_res_ok, l5_res_reason = check_level5_hard_shield(
                    action, path=resolved_target_str, workspace_root=ws_root
                )
                if not l5_res_ok and not bg_ok:
                    self._append_audit("LEVEL5_BLOCKED", l5_res_reason, {"action": action, "path": resolved_target_str})
                    return 403, {
                        "ok": False,
                        "blocked_by": "level5_hard_shield",
                        "requires_break_glass": True,
                        "error": l5_res_reason,
                    }

            elif action in ("run_command", "install_package"):
                if not command:
                    return 400, {"ok": False, "error": "Field 'command' is required."}
                l5_cmd_ok, l5_cmd_reason = check_level5_hard_shield(
                    action, command=command, workspace_root=ws_root
                )
                bg_ok = self._validate_break_glass_token_locked(bg_token)
                if not l5_cmd_ok and not bg_ok:
                    self._append_audit("LEVEL5_BLOCKED", l5_cmd_reason, {"action": action, "command": command[:120]})
                    return 403, {
                        "ok": False,
                        "blocked_by": "level5_hard_shield",
                        "requires_break_glass": True,
                        "error": l5_cmd_reason,
                    }

                if self._config.shell_profile == "read_only":
                    return 403, {"ok": False, "error": "Level 3 policy 'read_only' blocks all command execution."}
                if self._config.shell_profile == "dev_allowlist" and action == "run_command":
                    ok_cmd, _, err_cmd = self._validate_dev_allowlist_command(command)
                    if not ok_cmd:
                        return 403, {"ok": False, "error": err_cmd}

            # Classify risk level & determine whether HITL approval is required
            risk_level = 1
            requires_approval = self._config.hitl_per_diff
            diff_text = ""

            if action in ("write_file", "create_file", "overwrite_file"):
                target_p = pathlib.Path(resolved_target_str)
                old_text = ""
                existed = target_p.exists() and target_p.is_file()
                if existed:
                    try:
                        old_text = target_p.read_text(encoding="utf-8", errors="replace")
                    except OSError:
                        old_text = ""
                diff_text = self._compute_unified_diff(rel_label, old_text, content)

                is_destructive_overwrite = action == "overwrite_file" and existed and bool(old_text)
                if outside_workspace:
                    risk_level = 4
                    requires_approval = self._config.confirm_external_and_packages or self._config.hitl_per_diff
                elif is_destructive_overwrite:
                    risk_level = 2
                    requires_approval = self._config.confirm_deletions
                else:
                    risk_level = 1
                    folder_key = str(target_p.parent.resolve())
                    folder_approved = folder_key in self._approved_folders.get(session_id, set())
                    requires_approval = self._config.hitl_per_diff and not folder_approved

            elif action == "delete_file":
                target_p = pathlib.Path(resolved_target_str)
                old_text = ""
                if target_p.exists() and target_p.is_file():
                    try:
                        old_text = target_p.read_text(encoding="utf-8", errors="replace")
                    except OSError:
                        old_text = ""
                diff_text = self._compute_unified_diff(rel_label, old_text, "")
                risk_level = 4 if outside_workspace else 2
                requires_approval = (
                    self._config.confirm_external_and_packages if outside_workspace else self._config.confirm_deletions
                )

            elif action == "read_external_path":
                risk_level = 4
                requires_approval = self._config.confirm_external_and_packages

            elif action == "install_package" or (
                action == "run_command" and self._detect_package_install_command(command)
            ):
                risk_level = 4
                requires_approval = self._config.confirm_external_and_packages or self._config.confirm_shell

            elif action == "run_command":
                risk_level = 3
                requires_approval = self._config.confirm_shell

            prop_id = f"prop_{secrets.token_hex(6)}"
            prop = GoModeProposal(
                proposal_id=prop_id,
                session_id=session_id,
                action=action,
                risk_level=risk_level,
                status="pending_approval",
                created_at=_utc_now_iso(),
                target_path=resolved_target_str,
                rel_path=rel_label,
                content=content,
                command=command,
                timeout_sec=timeout_sec,
                diff=diff_text,
            )
            self._proposals[prop_id] = prop

            if not requires_approval:
                return self._execute_proposal_locked(prop, break_glass_token=bg_token)

            self._append_audit(
                "PROPOSAL_QUEUED",
                f"Queued L{risk_level} proposal '{action}' for HITL review.",
                {"proposal_id": prop_id, "risk_level": risk_level},
            )
            return 200, {
                "ok": True,
                "executed": False,
                "requires_approval": True,
                "proposal": prop.to_dict(),
            }

    def approve_proposal(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        if not isinstance(payload, dict):
            return 400, {"ok": False, "error": "Payload must be a JSON object."}
        prop_id = str(payload.get("proposal_id") or "").strip()
        if not prop_id:
            return 400, {"ok": False, "error": "Missing 'proposal_id'."}
        approve_mode = str(payload.get("approve_mode") or "once").strip().lower()
        bg_token = payload.get("break_glass_token")
        if bg_token is not None:
            bg_token = str(bg_token)

        with self._lock:
            prop = self._proposals.get(prop_id)
            if prop is None:
                return 404, {"ok": False, "error": f"Proposal '{prop_id}' not found."}
            if prop.status != "pending_approval":
                return 409, {"ok": False, "error": f"Proposal '{prop_id}' is already '{prop.status}'."}

            if approve_mode == "folder" and prop.risk_level == 1 and prop.target_path:
                folder_key = str(pathlib.Path(prop.target_path).parent.resolve())
                self._approved_folders.setdefault(prop.session_id, set()).add(folder_key)

            return self._execute_proposal_locked(prop, break_glass_token=bg_token)

    def reject_proposal(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        if not isinstance(payload, dict):
            return 400, {"ok": False, "error": "Payload must be a JSON object."}
        prop_id = str(payload.get("proposal_id") or "").strip()
        if not prop_id:
            return 400, {"ok": False, "error": "Missing 'proposal_id'."}
        reason = str(payload.get("reason") or "Rejected by user").strip()

        with self._lock:
            prop = self._proposals.get(prop_id)
            if prop is None:
                return 404, {"ok": False, "error": f"Proposal '{prop_id}' not found."}
            if prop.status != "pending_approval":
                return 409, {"ok": False, "error": f"Proposal '{prop_id}' is already '{prop.status}'."}
            prop.status = "rejected"
            prop.reason = reason
            self._append_audit("PROPOSAL_REJECTED", f"Rejected {prop.action}: {reason}", {"proposal_id": prop_id})
            return 200, {"ok": True, "rejected": True, "proposal": prop.to_dict()}

    def rollback(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        if not isinstance(payload, dict):
            return 400, {"ok": False, "error": "Payload must be a JSON object."}
        session_id = str(payload.get("session_id") or self._config.session_id or "default").strip()
        mode = str(payload.get("mode") or payload.get("scope") or "last").strip().lower()
        if mode not in ("last", "all"):
            return 400, {"ok": False, "error": "Rollback 'mode' must be 'last' or 'all'."}

        with self._lock:
            stack = self._undo_stacks.get(session_id, [])
            if not stack:
                return 200, {
                    "ok": True,
                    "restored_count": 0,
                    "restored_files": [],
                    "remaining_steps": 0,
                    "message": "Undo journal is empty.",
                }

            to_restore = [stack.pop()] if mode == "last" else list(reversed(stack))
            if mode == "all":
                stack.clear()

            restored_files: list[str] = []
            for entry in to_restore:
                target = pathlib.Path(entry.target_path)
                if entry.existed_before:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    raw_bytes = entry.original_bytes
                    if raw_bytes is None and entry.backup_file and pathlib.Path(entry.backup_file).is_file():
                        raw_bytes = pathlib.Path(entry.backup_file).read_bytes()
                    if raw_bytes is not None:
                        tmp = target.parent / f".restore.{secrets.token_hex(4)}.tmp"
                        tmp.write_bytes(raw_bytes)
                        tmp.replace(target)
                else:
                    if target.exists() and target.is_file():
                        target.unlink()
                restored_files.append(entry.target_path)

            self._append_audit(
                "ROLLBACK_EXECUTED",
                f"Rolled back {len(restored_files)} step(s) (mode={mode}) for session '{session_id}'.",
                {"session_id": session_id, "mode": mode},
            )
            return 200, {
                "ok": True,
                "mode": mode,
                "session_id": session_id,
                "restored_count": len(restored_files),
                "restored_files": restored_files,
                "remaining_steps": len(stack),
            }

    def get_sessions(self) -> dict[str, Any]:
        with self._lock:
            sid = self._config.session_id
            pending = [p.to_dict() for p in self._proposals.values() if p.status == "pending_approval"]
            history = [p.to_dict() for p in list(self._proposals.values())[-50:] if p.status != "pending_approval"]
            undo_entries = {
                s_id: [e.to_public_dict() for e in entries]
                for s_id, entries in self._undo_stacks.items()
            }
            return {
                "ok": True,
                "active_session_id": sid,
                "pending_proposals": pending,
                "history": list(reversed(history)),
                "undo_journal": undo_entries,
                "approved_folders": sorted(self._approved_folders.get(sid, set())),
                "break_glass": self.get_break_glass_status(),
                "audit_log": list(reversed(self._audit_log[-40:])),
            }


_GO_MODE_MANAGER: GoModeManager | None = None
_GO_MODE_LOCK = threading.Lock()


def get_go_mode_manager() -> GoModeManager:
    """Return singleton GoModeManager instance."""
    global _GO_MODE_MANAGER
    with _GO_MODE_LOCK:
        if _GO_MODE_MANAGER is None:
            _GO_MODE_MANAGER = GoModeManager()
        return _GO_MODE_MANAGER
