"""
Formal Data Schemas, Policy Validators, Evidence Ledger Compaction, and Mandatory 3-Outcome Verdicts.

Pure Python 3.14 stdlib only.
"""

from __future__ import annotations

import os
import pathlib
import re
import tempfile
from dataclasses import dataclass, field
from typing import Any

VALID_OPERATORS = ("fast_ghost", "orchestrator")
VALID_RETENTION_MODES = ("ephemeral", "persistent")
VALID_EXECUTION_MODES = ("dual_tier", "fast_only")
VALID_SANDBOX_MODES = ("auto", "host_readonly", "docker")
VALID_MISSION_PROFILES = (
    "general",
    "code_audit",
    "document_research",
    "document_synthesis",
    "data_analysis",
)
HYPOTHESIS_STATUSES = ("OPEN", "TESTING", "CONFIRMED", "REFUTED", "BLOCKED", "ESCALATE")
TERMINAL_HYPOTHESIS_STATUSES = ("CONFIRMED", "REFUTED", "BLOCKED")
VERDICT_CODES = ("OBJECTIVE_ACHIEVED", "IMPOSSIBLE_PROVEN", "HALTED_CIRCUIT_BREAKER")
IMPOSSIBLE_CATEGORIES = (
    "MISSING_EXTERNAL_DEPENDENCY",
    "LOGICAL_CONTRADICTION",
    "SEARCH_SPACE_EXHAUSTED",
)
MAX_LEDGER_CHARS = 8000

DEFAULT_FAST_MODEL = "local-worker:latest"
DEFAULT_HEAVY_MODEL = "heavy-moe-uncensored"
_SAFE_MODEL_RE = re.compile(r"^[a-zA-Z0-9._:/@-]{1,160}$")

_SENSITIVE_DIR_NAMES = frozenset({".ssh", ".gnupg", ".aws", ".kube", ".azure", ".docker"})


def _norm_path_cmp(p: pathlib.Path) -> str:
    s = str(p.resolve())
    return os.path.normcase(os.path.normpath(s))


def _is_same_or_child(candidate: pathlib.Path, parent: pathlib.Path) -> bool:
    try:
        cand_norm = pathlib.Path(_norm_path_cmp(candidate))
        par_norm = pathlib.Path(_norm_path_cmp(parent))
        return cand_norm == par_norm or cand_norm.is_relative_to(par_norm)
    except Exception:
        return False


def validate_target_path(raw_target: Any) -> pathlib.Path:
    """
    Validate and sanitize Orchestrator `target_path` against traversal, UNC paths,
    filesystem/drive roots, sensitive system/user directories, and `ORCHESTRATOR_ALLOWED_ROOTS`.
    """
    if not isinstance(raw_target, str) or not raw_target.strip():
        raise SchemaValidationError(
            "Missing required field 'target_path'.",
            field_name="target_path",
        )
    raw_str = raw_target.strip()

    if raw_str.startswith("\\\\") or raw_str.startswith("//"):
        raise SchemaValidationError(
            "UNC or network paths are not allowed in 'target_path'.",
            field_name="target_path",
            http_status=403,
        )

    raw_parts = [part for part in re.split(r"[\\/]+", raw_str) if part]
    if ".." in raw_parts:
        raise SchemaValidationError(
            "Path traversal ('..') is not allowed in 'target_path'.",
            field_name="target_path",
            http_status=403,
        )

    try:
        target_dir = pathlib.Path(raw_str).expanduser().resolve()
    except Exception as exc:
        raise SchemaValidationError(
            f"Invalid 'target_path': {exc}",
            field_name="target_path",
        ) from exc

    resolved_str = str(target_dir)
    if resolved_str.startswith("\\\\") or resolved_str.startswith("//"):
        raise SchemaValidationError(
            "UNC or network paths are not allowed in 'target_path'.",
            field_name="target_path",
            http_status=403,
        )

    if target_dir == target_dir.parent:
        raise SchemaValidationError(
            f"Filesystem or drive root is not allowed as 'target_path': {target_dir}",
            field_name="target_path",
            http_status=403,
        )

    lowered_parts = {part.lower() for part in target_dir.parts}
    if lowered_parts & _SENSITIVE_DIR_NAMES:
        raise SchemaValidationError(
            "Sensitive credential/configuration directory is denied in 'target_path'.",
            field_name="target_path",
            http_status=403,
        )

    # Fixed system & user profile denylist
    denied_trees: list[pathlib.Path] = []
    if os.name == "nt":
        sys_drive = os.environ.get("SystemDrive", "C:") + "\\"
        for env_key, fallback in (
            ("WINDIR", os.path.join(sys_drive, "Windows")),
            ("SystemRoot", os.path.join(sys_drive, "Windows")),
            ("ProgramFiles", os.path.join(sys_drive, "Program Files")),
            ("ProgramFiles(x86)", os.path.join(sys_drive, "Program Files (x86)")),
            ("ProgramData", os.path.join(sys_drive, "ProgramData")),
            ("APPDATA", ""),
        ):
            val = os.environ.get(env_key) or fallback
            if val:
                denied_trees.append(pathlib.Path(val))
        denied_exact = [pathlib.Path(os.path.join(sys_drive, "Users"))]
    else:
        denied_trees.extend(
            pathlib.Path(p)
            for p in ("/etc", "/usr", "/bin", "/sbin", "/var", "/proc", "/sys", "/root", "/dev")
        )
        denied_exact = [pathlib.Path("/home"), pathlib.Path("/Users")]

    user_profile = os.environ.get("USERPROFILE") or str(pathlib.Path.home())
    if user_profile:
        denied_exact.append(pathlib.Path(user_profile))

    for exact_p in denied_exact:
        if _norm_path_cmp(target_dir) == _norm_path_cmp(exact_p):
            raise SchemaValidationError(
                f"Top-level profile/users directory is denied as 'target_path': {target_dir}",
                field_name="target_path",
                http_status=403,
            )

    for denied_p in denied_trees:
        if _is_same_or_child(target_dir, denied_p):
            raise SchemaValidationError(
                f"System or application-data directory is denied in 'target_path': {target_dir}",
                field_name="target_path",
                http_status=403,
            )

    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        local_app_path = pathlib.Path(local_appdata)
        if _is_same_or_child(target_dir, local_app_path):
            temp_root = pathlib.Path(tempfile.gettempdir()).resolve()
            # Permit isolated temp subdirectories (e.g. unit tests) when not explicitly blocked by allowlist,
            # but deny %LOCALAPPDATA% itself, %LOCALAPPDATA%\Temp root, and any non-Temp app/browser folder.
            is_temp_subdir = (
                _norm_path_cmp(target_dir) != _norm_path_cmp(temp_root)
                and _is_same_or_child(target_dir, temp_root)
            )
            if not is_temp_subdir:
                raise SchemaValidationError(
                    f"%LOCALAPPDATA% directory is denied in 'target_path': {target_dir}",
                    field_name="target_path",
                    http_status=403,
                )

    raw_allowed = os.environ.get("ORCHESTRATOR_ALLOWED_ROOTS", "").strip()
    if raw_allowed:
        allowed_roots = [
            pathlib.Path(item.strip()).expanduser().resolve()
            for item in raw_allowed.split(os.pathsep)
            if item.strip()
        ]
        if allowed_roots and not any(_is_same_or_child(target_dir, root) for root in allowed_roots):
            raise SchemaValidationError(
                f"'target_path' is outside ORCHESTRATOR_ALLOWED_ROOTS: {target_dir}",
                field_name="target_path",
                http_status=403,
            )

    if not target_dir.exists() or not target_dir.is_dir():
        raise SchemaValidationError(
            f"'target_path' does not exist or is not a directory: {raw_target}",
            field_name="target_path",
        )

    return target_dir


class SchemaValidationError(ValueError):
    """Raised when an API payload, hypothesis, or formal verdict violates schema invariants."""

    def __init__(
        self,
        message: str,
        field_name: str | None = None,
        http_status: int = 400,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.field_name = field_name
        self.http_status = http_status


def _clamp_int(value: Any, default: int, min_val: int, max_val: int) -> int:
    if value is None or isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(min_val, min(max_val, parsed))


def _coerce_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("true", "1", "yes", "on"):
            return True
        if low in ("false", "0", "no", "off"):
            return False
    return bool(value)


@dataclass
class EphemeralRules:
    rolling_purge_per_step: bool = True
    embed_verified_poc_in_report: bool = True
    report_auto_purge_minutes: int = 45
    allow_kv_save: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "rolling_purge_per_step": self.rolling_purge_per_step,
            "embed_verified_poc_in_report": self.embed_verified_poc_in_report,
            "report_auto_purge_minutes": self.report_auto_purge_minutes,
            "allow_kv_save": self.allow_kv_save,
        }


@dataclass
class BudgetConfig:
    max_steps: int = 40
    max_heavy_escalations: int = 3
    no_progress_limit: int = 3
    max_turns_per_hypothesis: int = 4
    sandbox_timeout_sec: int = 60

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_steps": self.max_steps,
            "max_heavy_escalations": self.max_heavy_escalations,
            "no_progress_limit": self.no_progress_limit,
            "max_turns_per_hypothesis": self.max_turns_per_hypothesis,
            "sandbox_timeout_sec": self.sandbox_timeout_sec,
        }


@dataclass
class RunRequest:
    operator: str
    retention_mode: str
    mission: str
    target_path: str
    target_access: str = "read_only"
    execution_mode: str = "dual_tier"
    sandbox_mode: str = "auto"
    mission_profile: str = "general"
    fast_model: str = DEFAULT_FAST_MODEL
    heavy_model: str = DEFAULT_HEAVY_MODEL
    ephemeral_rules: EphemeralRules = field(default_factory=EphemeralRules)
    budget: BudgetConfig = field(default_factory=BudgetConfig)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "RunRequest":
        if not isinstance(payload, dict):
            raise SchemaValidationError("Request payload must be a JSON object.")

        raw_operator = payload.get("operator")
        if not isinstance(raw_operator, str) or not raw_operator.strip():
            raise SchemaValidationError(
                "Missing required field 'operator'. Must explicitly specify 'orchestrator'.",
                field_name="operator",
            )
        operator = raw_operator.strip()
        if operator not in VALID_OPERATORS:
            raise SchemaValidationError(
                f"Invalid operator '{operator}'. Expected one of {VALID_OPERATORS}.",
                field_name="operator",
            )
        if operator == "fast_ghost":
            raise SchemaValidationError(
                "Operator 'fast_ghost' is strictly RAM-only chat with tools/filesystem disabled "
                "and cannot start an autonomous orchestrator run.",
                field_name="operator",
                http_status=400,
            )

        raw_retention = payload.get("retention_mode")
        if raw_retention is None:
            raw_retention = payload.get("retentionMode")
        if not isinstance(raw_retention, str) or not raw_retention.strip():
            raise SchemaValidationError(
                "Missing required field 'retention_mode'. Must explicitly choose 'ephemeral' or 'persistent'.",
                field_name="retention_mode",
            )
        retention_mode = raw_retention.strip().lower()
        if retention_mode not in VALID_RETENTION_MODES:
            raise SchemaValidationError(
                f"Ambiguous or invalid retention_mode '{retention_mode}'. "
                f"Must be one of {VALID_RETENTION_MODES}.",
                field_name="retention_mode",
            )

        raw_access = payload.get("target_access")
        if raw_access is None:
            raw_access = payload.get("targetAccess", "read_only")
        if not isinstance(raw_access, str) or raw_access.strip().lower() != "read_only":
            raise SchemaValidationError(
                "Invalid 'target_access': target directory MUST be mounted 'read_only'.",
                field_name="target_access",
            )
        target_access = "read_only"

        raw_mission = payload.get("mission")
        if not isinstance(raw_mission, str) or len(raw_mission.strip()) < 3:
            raise SchemaValidationError(
                "Missing or too-short 'mission' (at least 3 characters required).",
                field_name="mission",
            )
        mission = raw_mission.strip()
        if len(mission) > 16000:
            raise SchemaValidationError(
                "Mission exceeds maximum allowed length (16,000 characters).",
                field_name="mission",
            )

        raw_target = payload.get("target_path")
        if raw_target is None:
            raw_target = payload.get("targetPath")
        target_dir = validate_target_path(raw_target)

        raw_exec_mode = payload.get("execution_mode")
        if raw_exec_mode is None:
            raw_exec_mode = payload.get("executionMode", "dual_tier")
        raw_sandbox_mode = payload.get("sandbox_mode")
        if raw_sandbox_mode is None:
            raw_sandbox_mode = payload.get("sandboxMode")

        # Support callers passing execution_mode in ("auto", "host_readonly", "docker") directly
        # while preserving "dual_tier" | "fast_only" tier routing.
        if (
            isinstance(raw_exec_mode, str)
            and raw_exec_mode.strip().lower() in VALID_SANDBOX_MODES
        ):
            if raw_sandbox_mode is None:
                raw_sandbox_mode = raw_exec_mode.strip().lower()
            raw_tier_mode = payload.get("tier_mode") or payload.get("tierMode") or "dual_tier"
            raw_exec_mode = raw_tier_mode

        if not isinstance(raw_exec_mode, str) or raw_exec_mode.strip().lower() not in VALID_EXECUTION_MODES:
            raise SchemaValidationError(
                f"Invalid 'execution_mode': '{raw_exec_mode}'. Must be one of {VALID_EXECUTION_MODES}.",
                field_name="execution_mode",
            )
        execution_mode = raw_exec_mode.strip().lower()

        if raw_sandbox_mode is None:
            sandbox_mode = "auto"
        elif not isinstance(raw_sandbox_mode, str) or raw_sandbox_mode.strip().lower() not in VALID_SANDBOX_MODES:
            raise SchemaValidationError(
                f"Invalid 'sandbox_mode': '{raw_sandbox_mode}'. Must be one of {VALID_SANDBOX_MODES}.",
                field_name="sandbox_mode",
            )
        else:
            sandbox_mode = raw_sandbox_mode.strip().lower()

        raw_profile = payload.get("mission_profile")
        if raw_profile is None:
            raw_profile = payload.get("missionProfile", "general")
        if not isinstance(raw_profile, str) or raw_profile.strip().lower() not in VALID_MISSION_PROFILES:
            raise SchemaValidationError(
                f"Invalid 'mission_profile': '{raw_profile}'. Must be one of {VALID_MISSION_PROFILES}.",
                field_name="mission_profile",
            )
        mission_profile = raw_profile.strip().lower()

        raw_fast_model = payload.get("fast_model") or payload.get("fastModel") or DEFAULT_FAST_MODEL
        if not isinstance(raw_fast_model, str) or not _SAFE_MODEL_RE.match(raw_fast_model.strip()):
            raise SchemaValidationError(
                f"Invalid 'fast_model': '{raw_fast_model}'.",
                field_name="fast_model",
            )
        fast_model = raw_fast_model.strip()

        raw_heavy_model = payload.get("heavy_model") or payload.get("heavyModel") or DEFAULT_HEAVY_MODEL
        if not isinstance(raw_heavy_model, str) or not _SAFE_MODEL_RE.match(raw_heavy_model.strip()):
            raise SchemaValidationError(
                f"Invalid 'heavy_model': '{raw_heavy_model}'.",
                field_name="heavy_model",
            )
        heavy_model = raw_heavy_model.strip()

        raw_rules = payload.get("ephemeral_rules")
        if raw_rules is None:
            raw_rules = payload.get("ephemeralRules")
        rules_dict = raw_rules if isinstance(raw_rules, dict) else {}

        auto_purge_raw = rules_dict.get(
            "report_auto_purge_minutes",
            payload.get("report_auto_purge_minutes", payload.get("reportAutoPurgeMinutes", 45)),
        )
        embed_poc_raw = rules_dict.get(
            "embed_verified_poc_in_report",
            payload.get("embed_verified_poc_in_report", True),
        )
        rolling_raw = rules_dict.get("rolling_purge_per_step", True)
        kv_save_raw = rules_dict.get("allow_kv_save", payload.get("allow_kv_save", False))

        # Hard security invariant: in ephemeral mode, rolling purge is ALWAYS True and KVSAVE is ALWAYS False.
        if retention_mode == "ephemeral":
            rolling_purge = True
            allow_kv_save = False
        else:
            rolling_purge = _coerce_bool(rolling_raw, False)
            allow_kv_save = _coerce_bool(kv_save_raw, False)

        ephemeral_rules = EphemeralRules(
            rolling_purge_per_step=rolling_purge,
            embed_verified_poc_in_report=_coerce_bool(embed_poc_raw, True),
            report_auto_purge_minutes=_clamp_int(auto_purge_raw, 45, 1, 240),
            allow_kv_save=allow_kv_save,
        )

        raw_budget = payload.get("budget")
        budget_dict = raw_budget if isinstance(raw_budget, dict) else {}
        budget = BudgetConfig(
            max_steps=_clamp_int(
                budget_dict.get("max_steps", payload.get("max_steps")), 40, 1, 120
            ),
            max_heavy_escalations=_clamp_int(
                budget_dict.get("max_heavy_escalations", payload.get("max_heavy_escalations")),
                3,
                0,
                10,
            ),
            no_progress_limit=_clamp_int(
                budget_dict.get("no_progress_limit", payload.get("no_progress_limit")),
                3,
                2,
                8,
            ),
            max_turns_per_hypothesis=_clamp_int(
                budget_dict.get("max_turns_per_hypothesis"), 4, 1, 10
            ),
            sandbox_timeout_sec=_clamp_int(
                budget_dict.get("sandbox_timeout_sec"), 60, 5, 300
            ),
        )

        return cls(
            operator=operator,
            retention_mode=retention_mode,
            mission=mission,
            target_path=str(target_dir),
            target_access=target_access,
            execution_mode=execution_mode,
            sandbox_mode=sandbox_mode,
            mission_profile=mission_profile,
            fast_model=fast_model,
            heavy_model=heavy_model,
            ephemeral_rules=ephemeral_rules,
            budget=budget,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "operator": self.operator,
            "retention_mode": self.retention_mode,
            "mission": self.mission,
            "target_path": self.target_path,
            "target_access": self.target_access,
            "execution_mode": self.execution_mode,
            "sandbox_mode": self.sandbox_mode,
            "mission_profile": self.mission_profile,
            "fast_model": self.fast_model,
            "heavy_model": self.heavy_model,
            "ephemeral_rules": self.ephemeral_rules.to_dict(),
            "budget": self.budget.to_dict(),
        }


@dataclass
class Hypothesis:
    id: str
    title: str
    target_files: list[str] = field(default_factory=list)
    risk_area: str = "general"
    status: str = "OPEN"
    evidence: str = ""
    poc_snippet: str = ""
    attempts: int = 0
    inspected_slices: list[str] = field(default_factory=list)
    sandbox_verified: bool = False

    def __post_init__(self) -> None:
        self.id = str(self.id or "").strip()
        if not self.id:
            raise SchemaValidationError("Hypothesis 'id' cannot be empty.", field_name="id")
        self.title = str(self.title or "").strip()
        if not self.title:
            raise SchemaValidationError(
                f"Hypothesis '{self.id}' 'title' cannot be empty.", field_name="title"
            )
        self.status = str(self.status or "OPEN").strip().upper()
        if self.status not in HYPOTHESIS_STATUSES:
            raise SchemaValidationError(
                f"Invalid hypothesis status '{self.status}'. Expected one of {HYPOTHESIS_STATUSES}.",
                field_name="status",
            )
        self.risk_area = str(self.risk_area or "general").strip() or "general"
        self.target_files = [str(f).strip() for f in (self.target_files or []) if str(f).strip()]
        self.inspected_slices = [
            str(s).strip() for s in (self.inspected_slices or []) if str(s).strip()
        ]
        self.evidence = str(self.evidence or "").strip()
        self.poc_snippet = str(self.poc_snippet or "").strip()
        self.attempts = max(0, int(self.attempts or 0))
        self.sandbox_verified = bool(self.sandbox_verified)

    @classmethod
    def from_dict(cls, data: dict[str, Any], default_id: str | None = None) -> "Hypothesis":
        if not isinstance(data, dict):
            raise SchemaValidationError("Hypothesis entry must be an object.")
        hid = str(data.get("id") or default_id or "").strip()
        title = str(data.get("title") or data.get("description") or "").strip()
        raw_files = data.get("target_files") or data.get("files") or []
        if isinstance(raw_files, str):
            raw_files = [raw_files]
        raw_slices = data.get("inspected_slices") or []
        if isinstance(raw_slices, str):
            raw_slices = [raw_slices]
        return cls(
            id=hid,
            title=title,
            target_files=list(raw_files),
            risk_area=str(data.get("risk_area") or data.get("category") or "general"),
            status=str(data.get("status") or "OPEN"),
            evidence=str(data.get("evidence") or ""),
            poc_snippet=str(data.get("poc_snippet") or data.get("poc") or ""),
            attempts=int(data.get("attempts") or 0),
            inspected_slices=list(raw_slices),
            sandbox_verified=bool(data.get("sandbox_verified", False)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "target_files": list(self.target_files),
            "risk_area": self.risk_area,
            "status": self.status,
            "evidence": self.evidence,
            "poc_snippet": self.poc_snippet,
            "attempts": self.attempts,
            "inspected_slices": list(self.inspected_slices),
            "sandbox_verified": self.sandbox_verified,
        }


@dataclass
class Verdict:
    code: str
    summary: str
    impossible_category: str | None = None
    impossible_proof: str | None = None
    confirmed_ids: list[str] = field(default_factory=list)
    refuted_ids: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.code = str(self.code or "").strip().upper()
        self.summary = str(self.summary or "").strip()
        if self.impossible_category is not None:
            cat = str(self.impossible_category).strip().upper()
            self.impossible_category = cat if cat else None
        if self.impossible_proof is not None:
            proof = str(self.impossible_proof).strip()
            self.impossible_proof = proof if proof else None
        self.confirmed_ids = [str(x).strip() for x in (self.confirmed_ids or []) if str(x).strip()]
        self.refuted_ids = [str(x).strip() for x in (self.refuted_ids or []) if str(x).strip()]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Verdict":
        if not isinstance(data, dict):
            raise SchemaValidationError("Verdict must be a JSON object.")
        return cls(
            code=str(data.get("code") or data.get("verdict") or ""),
            summary=str(data.get("summary") or ""),
            impossible_category=data.get("impossible_category"),
            impossible_proof=data.get("impossible_proof"),
            confirmed_ids=list(data.get("confirmed_ids") or []),
            refuted_ids=list(data.get("refuted_ids") or []),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "summary": self.summary,
            "impossible_category": self.impossible_category,
            "impossible_proof": self.impossible_proof,
            "confirmed_ids": list(self.confirmed_ids),
            "refuted_ids": list(self.refuted_ids),
        }


def validate_verdict(
    verdict: Verdict,
    hypotheses: list[Hypothesis],
    surface_map: dict[str, Any] | None = None,
) -> tuple[bool, list[str]]:
    """
    Strictly enforce the mandatory 3-outcome verdict rules:
    1. `OBJECTIVE_ACHIEVED`: requires either >= 1 CONFIRMED hypothesis OR all hypotheses resolved
       (no OPEN / TESTING / ESCALATE remaining).
    2. `IMPOSSIBLE_PROVEN`: MUST include a valid `impossible_category` in IMPOSSIBLE_CATEGORIES
       and a non-empty `impossible_proof`. If `SEARCH_SPACE_EXHAUSTED`, no CONFIRMED/OPEN/TESTING
       hypotheses may remain.
    3. `HALTED_CIRCUIT_BREAKER`: requires a non-empty explanation summary.
    """
    errors: list[str] = []

    if verdict.code not in VERDICT_CODES:
        errors.append(
            f"Invalid verdict code '{verdict.code}'. Must be one of {VERDICT_CODES}."
        )
        return False, errors

    if not verdict.summary:
        errors.append("Verdict 'summary' must be non-empty.")

    by_id = {h.id: h for h in hypotheses}
    actual_confirmed = [h.id for h in hypotheses if h.status == "CONFIRMED"]
    unresolved = [h.id for h in hypotheses if h.status in ("OPEN", "TESTING", "ESCALATE")]

    for cid in verdict.confirmed_ids:
        h = by_id.get(cid)
        if h is None or h.status != "CONFIRMED":
            errors.append(
                f"Verdict lists '{cid}' in confirmed_ids, but hypothesis is not CONFIRMED."
            )

    if verdict.code == "IMPOSSIBLE_PROVEN":
        if verdict.impossible_category not in IMPOSSIBLE_CATEGORIES:
            errors.append(
                f"IMPOSSIBLE_PROVEN requires impossible_category in {IMPOSSIBLE_CATEGORIES}, "
                f"got '{verdict.impossible_category}'."
            )
        if not verdict.impossible_proof or len(verdict.impossible_proof.strip()) < 10:
            errors.append(
                "IMPOSSIBLE_PROVEN requires a non-empty structured 'impossible_proof' "
                "(at least 10 characters)."
            )
        if verdict.impossible_category == "SEARCH_SPACE_EXHAUSTED":
            if actual_confirmed:
                errors.append(
                    f"SEARCH_SPACE_EXHAUSTED is invalid when confirmed hypotheses exist: {actual_confirmed}."
                )
            if unresolved:
                errors.append(
                    f"SEARCH_SPACE_EXHAUSTED requires all hypotheses resolved; still open/testing/escalated: {unresolved}."
                )
            if surface_map is None:
                errors.append(
                    "SEARCH_SPACE_EXHAUSTED requires a completed surface_map."
                )

    elif verdict.code == "OBJECTIVE_ACHIEVED":
        if verdict.impossible_category is not None:
            errors.append("OBJECTIVE_ACHIEVED must not set impossible_category.")
        if not actual_confirmed:
            if not hypotheses:
                files = (surface_map or {}).get("files") if isinstance(surface_map, dict) else None
                if not (isinstance(files, list) and len(files) == 0):
                    errors.append(
                        "OBJECTIVE_ACHIEVED requires >= 1 CONFIRMED hypothesis or all generated "
                        "hypotheses resolved."
                    )
            elif unresolved:
                errors.append(
                    f"OBJECTIVE_ACHIEVED without CONFIRMED hypotheses requires all hypotheses "
                    f"resolved; remaining unresolved: {unresolved}."
                )

    elif verdict.code == "HALTED_CIRCUIT_BREAKER":
        if verdict.impossible_category is not None:
            errors.append("HALTED_CIRCUIT_BREAKER must not set impossible_category.")

    return len(errors) == 0, errors


def compact_ledger_text(text: str, max_chars: int = MAX_LEDGER_CHARS) -> str:
    """
    Deterministically compact evidence ledger text to stay strictly within `max_chars`
    (~2,000 tokens hard cap), preserving top header lines and newest verified findings.
    """
    cap = max(64, int(max_chars))
    normalized = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [ln.rstrip() for ln in normalized.split("\n")]

    # Collapse consecutive blank lines
    collapsed: list[str] = []
    prev_blank = False
    for ln in lines:
        is_blank = ln == ""
        if is_blank and prev_blank:
            continue
        collapsed.append(ln)
        prev_blank = is_blank

    cleaned = "\n".join(collapsed).strip()
    if len(cleaned) <= cap:
        return cleaned

    marker = "\n... [LEDGER COMPACTED TO STAY WITHIN 2K TOKEN CAP] ...\n"
    if cap <= len(marker) + 20:
        return cleaned[:cap]

    budget = cap - len(marker)
    # Keep header / confirmed summary prefix (~30%) and most recent entries (~70%)
    head_budget = min(int(budget * 0.30), 2000)
    tail_budget = budget - head_budget

    head = cleaned[:head_budget].rstrip()
    tail = cleaned[-tail_budget:].lstrip()

    # Align cuts on newline boundaries where possible without exceeding budget
    if "\n" in head:
        head_cut = head.rsplit("\n", 1)[0].rstrip()
        if len(head_cut) >= int(head_budget * 0.5):
            head = head_cut
    if "\n" in tail:
        tail_cut = tail.split("\n", 1)[1].lstrip()
        if len(tail_cut) >= int(tail_budget * 0.5):
            tail = tail_cut

    combined = f"{head}{marker}{tail}"
    if len(combined) > cap:
        combined = combined[:cap]
    return combined
