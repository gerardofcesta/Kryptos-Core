"""
Deterministic Orchestrator `code_audit` analyzer:
1. Anti-Slopsquatting: parses Python AST imports (`import` / `from ... import`), `requirements*.txt`,
   `pyproject.toml`, and `uv.lock`; resolves against `sys.stdlib_module_names`, local workspace
   modules, locked dependencies, and verified allowlist; flags unknown/hallucinated and
   typosquatted packages deterministically.
2. Deterministic Secret Scan: invokes `gitleaks` if available on PATH and always runs strict
   deterministic regex rules (AWS AKIA/ASIA, OpenAI/Anthropic/Stripe `sk-...`, GitHub `ghp_...`,
   private keys, JWTs, credential DSNs, hardcoded secret assignments) with safe redaction.

Pure Python stdlib only.
"""

from __future__ import annotations

import ast
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tomllib
from typing import Any

from .schemas import validate_target_path

EXCLUDED_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        "env",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".eggs",
        "dist",
        "build",
        "engines",
    }
)

SCANNABLE_TEXT_EXTS = frozenset(
    {
        ".py",
        ".js",
        ".ts",
        ".tsx",
        ".jsx",
        ".json",
        ".yaml",
        ".yml",
        ".toml",
        ".env",
        ".ini",
        ".cfg",
        ".conf",
        ".sh",
        ".ps1",
        ".md",
        ".txt",
        ".sql",
    }
)

MAX_SCAN_FILE_BYTES = 1024 * 1024  # 1 MiB per file cap

# Normalized canonical package/import names (lowercase, '-' and '.' mapped to '_')
VERIFIED_PYPI_ALLOWLIST: frozenset[str] = frozenset(
    {
        "aiofiles",
        "aiohttp",
        "alembic",
        "annotated_types",
        "anyio",
        "argon2",
        "argon2_cffi",
        "attrs",
        "beautifulsoup4",
        "bs4",
        "certifi",
        "cffi",
        "charset_normalizer",
        "click",
        "colorama",
        "cryptography",
        "dateutil",
        "defusedxml",
        "dotenv",
        "fastapi",
        "fitz",
        "flask",
        "h11",
        "httpcore",
        "httptools",
        "httpx",
        "idna",
        "iniconfig",
        "jinja2",
        "jsonschema",
        "jwt",
        "lxml",
        "markdown",
        "markdown_it",
        "markupsafe",
        "msgpack",
        "networkx",
        "numpy",
        "onnxruntime",
        "openai",
        "openpyxl",
        "orjson",
        "packaging",
        "pandas",
        "passlib",
        "pil",
        "pillow",
        "pluggy",
        "psutil",
        "psycopg",
        "psycopg2",
        "pycryptodome",
        "pydantic",
        "pydantic_core",
        "pydantic_settings",
        "pygments",
        "pyjwt",
        "pymupdf",
        "pymupdf4llm",
        "pytest",
        "python_dateutil",
        "python_docx",
        "python_dotenv",
        "python_multipart",
        "python_pptx",
        "pytz",
        "pyyaml",
        "rapidocr_onnxruntime",
        "redis",
        "regex",
        "requests",
        "rich",
        "ruff",
        "scikit_learn",
        "scipy",
        "setuptools",
        "shapely",
        "six",
        "sklearn",
        "sniffio",
        "sqlalchemy",
        "starlette",
        "sympy",
        "tabulate",
        "tenacity",
        "tiktoken",
        "toml",
        "tomli",
        "tqdm",
        "typer",
        "typing_extensions",
        "urllib3",
        "uvicorn",
        "websockets",
        "wheel",
        "yaml",
        "zod",
    }
)

PYPI_TO_IMPORT_ALIASES: dict[str, str] = {
    "beautifulsoup4": "bs4",
    "opencv_python": "cv2",
    "pillow": "pil",
    "pycryptodome": "crypto",
    "pyjwt": "jwt",
    "pymupdf": "fitz",
    "python_dateutil": "dateutil",
    "python_docx": "docx",
    "python_dotenv": "dotenv",
    "python_pptx": "pptx",
    "pyyaml": "yaml",
    "scikit_learn": "sklearn",
}

_IMPORT_TO_PYPI_ALIASES: dict[str, str] = {v: k for k, v in PYPI_TO_IMPORT_ALIASES.items()}

_STDLIB_NAMES: frozenset[str] = frozenset(
    getattr(sys, "stdlib_module_names", frozenset())
    | set(sys.builtin_module_names)
    | {"__future__", "builtins"}
)

_REQ_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")

_SECRET_RULES: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    ("AWS_ACCESS_KEY", "critical", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    (
        "API_SECRET_SK",
        "critical",
        re.compile(r"\bsk-(?:proj-|ant-api\d*-|live_|test_)?[A-Za-z0-9_-]{20,}\b"),
    ),
    (
        "GITHUB_TOKEN",
        "critical",
        re.compile(r"\b(?:(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{22,})\b"),
    ),
    (
        "PRIVATE_KEY_HEADER",
        "critical",
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----"),
    ),
    (
        "JWT_TOKEN",
        "high",
        re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    ),
    (
        "CREDENTIAL_DSN",
        "critical",
        re.compile(
            r"\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)://[^:\s/'\"`]+:[^@\s/'\"`]+@[^\s/'\"`]+"
        ),
    ),
    (
        "HARDCODED_SECRET_ASSIGN",
        "high",
        re.compile(
            r"(?i)\b(?:api_key|secret_key|client_secret|access_token|auth_token|private_key|db_password)\b"
            r"\s*[:=]\s*[\"']([A-Za-z0-9/+=_.-]{16,})[\"']"
        ),
    ),
)

_PLACEHOLDER_MARKERS = ("example", "changeme", "placeholder", "your_", "dummy", "xxxx", "00000000")


def _canon_name(name: str) -> str:
    return re.sub(r"[-_.]+", "_", (name or "").strip().lower()).strip("_")


def _redact_secret(raw: str) -> str:
    s = (raw or "").strip()
    if len(s) <= 8:
        return "***"
    return f"{s[:4]}***{s[-4:]}"


def _levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if abs(len(a) - len(b)) > 2:
        return 3
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        curr = [i]
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            curr.append(min(curr[-1] + 1, prev[j] + 1, prev[j - 1] + cost))
        prev = curr
    return prev[-1]


def _closest_known_package(canon: str, known_set: frozenset[str] | set[str]) -> str | None:
    if len(canon) < 4:
        return None
    best_name: str | None = None
    best_dist = 3
    for candidate in known_set:
        if len(candidate) < 4 or candidate == canon:
            continue
        dist = _levenshtein(canon, candidate)
        if 1 <= dist <= 2 and dist < best_dist:
            best_dist = dist
            best_name = candidate
    return best_name


def _iter_project_files(target_dir: pathlib.Path) -> list[pathlib.Path]:
    collected: list[pathlib.Path] = []
    for walk_root, dirs, files in os.walk(target_dir):
        dirs[:] = [d for d in sorted(dirs) if d not in EXCLUDED_DIRS]
        for fname in sorted(files):
            collected.append(pathlib.Path(walk_root) / fname)
    return collected


def _discover_local_modules(target_dir: pathlib.Path, files: list[pathlib.Path]) -> set[str]:
    local_mods: set[str] = set()
    for fpath in files:
        if fpath.suffix.lower() != ".py":
            continue
        rel = fpath.relative_to(target_dir)
        parts = rel.parts
        if fpath.stem != "__init__":
            local_mods.add(_canon_name(fpath.stem))
        if len(parts) >= 2:
            local_mods.add(_canon_name(parts[0]))
            if parts[0] == "src" and len(parts) >= 2:
                local_mods.add(_canon_name(pathlib.Path(parts[1]).stem))
    for entry in target_dir.iterdir():
        if entry.is_dir() and entry.name not in EXCLUDED_DIRS and not entry.name.startswith("."):
            if any(entry.glob("*.py")):
                local_mods.add(_canon_name(entry.name))
    src_dir = target_dir / "src"
    if src_dir.is_dir():
        for entry in src_dir.iterdir():
            if entry.is_dir() and not entry.name.startswith("."):
                local_mods.add(_canon_name(entry.name))
    return {m for m in local_mods if m}


def _parse_requirements_file(req_path: pathlib.Path) -> list[tuple[int, str]]:
    deps: list[tuple[int, str]] = []
    try:
        lines = req_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return deps
    for idx, raw_line in enumerate(lines, 1):
        line = raw_line.split("#", 1)[0].strip()
        if not line or line.startswith(("-", "--", "git+", "http:", "https:")):
            continue
        match = _REQ_NAME_RE.match(line)
        if match:
            deps.append((idx, match.group(1)))
    return deps


def _parse_pyproject_deps(pyproject_path: pathlib.Path) -> list[str]:
    deps: list[str] = []
    try:
        data = tomllib.loads(pyproject_path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, tomllib.TOMLDecodeError):
        return deps

    proj = data.get("project") if isinstance(data.get("project"), dict) else {}
    raw_lists: list[Any] = []
    if isinstance(proj.get("dependencies"), list):
        raw_lists.append(proj["dependencies"])
    opt = proj.get("optional-dependencies")
    if isinstance(opt, dict):
        raw_lists.extend(v for v in opt.values() if isinstance(v, list))
    dep_groups = data.get("dependency-groups")
    if isinstance(dep_groups, dict):
        raw_lists.extend(v for v in dep_groups.values() if isinstance(v, list))

    for lst in raw_lists:
        for spec in lst:
            if isinstance(spec, str):
                m = _REQ_NAME_RE.match(spec)
                if m:
                    deps.append(m.group(1))

    poetry = (data.get("tool") or {}).get("poetry") if isinstance(data.get("tool"), dict) else None
    if isinstance(poetry, dict) and isinstance(poetry.get("dependencies"), dict):
        for pkg_name in poetry["dependencies"]:
            if str(pkg_name).lower() != "python":
                deps.append(str(pkg_name))
    return deps


def _parse_uv_lock_packages(lock_path: pathlib.Path) -> set[str]:
    locked: set[str] = set()
    if not lock_path.is_file():
        return locked
    try:
        data = tomllib.loads(lock_path.read_text(encoding="utf-8", errors="replace"))
        for pkg in data.get("package", []) or []:
            if isinstance(pkg, dict) and pkg.get("name"):
                canon = _canon_name(str(pkg["name"]))
                locked.add(canon)
                if canon in PYPI_TO_IMPORT_ALIASES:
                    locked.add(PYPI_TO_IMPORT_ALIASES[canon])
    except (OSError, tomllib.TOMLDecodeError):
        pass
    return locked


def _extract_python_imports(py_path: pathlib.Path) -> list[tuple[int, str]]:
    imports: list[tuple[int, str]] = []
    try:
        src = py_path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(src, filename=str(py_path))
    except (OSError, SyntaxError):
        return imports

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = (alias.name or "").split(".", 1)[0].strip()
                if top:
                    imports.append((node.lineno, top))
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                continue
            if node.module:
                top = node.module.split(".", 1)[0].strip()
                if top:
                    imports.append((node.lineno, top))
    return imports


def analyze_slopsquatting(
    target_path: str | pathlib.Path,
    *,
    extra_allowlist: set[str] | frozenset[str] | None = None,
) -> dict[str, Any]:
    """
    Deterministically detect hallucinated/slopsquatted or typosquatted Python
    packages across AST imports, `requirements*.txt`, and `pyproject.toml`.
    """
    target_dir = validate_target_path(str(target_path) if isinstance(target_path, pathlib.Path) else target_path)
    files = _iter_project_files(target_dir)
    local_mods = _discover_local_modules(target_dir, files)
    locked_pkgs = _parse_uv_lock_packages(target_dir / "uv.lock")

    allowlist: set[str] = set(VERIFIED_PYPI_ALLOWLIST)
    if extra_allowlist:
        allowlist.update(_canon_name(item) for item in extra_allowlist if item)
    for k, v in PYPI_TO_IMPORT_ALIASES.items():
        if k in allowlist or v in allowlist:
            allowlist.add(k)
            allowlist.add(v)
    trusted_set = allowlist | locked_pkgs

    findings: list[dict[str, Any]] = []
    seen_findings: set[tuple[str, str, str]] = set()
    checked_imports = 0
    checked_declared = 0

    def _evaluate_pkg(raw_name: str, source_file: str, line: int, kind: str) -> None:
        canon = _canon_name(raw_name)
        if not canon:
            return
        if raw_name in _STDLIB_NAMES or canon in _STDLIB_NAMES:
            return
        if canon in local_mods:
            return
        mapped_pypi = _IMPORT_TO_PYPI_ALIASES.get(canon, canon)
        mapped_imp = PYPI_TO_IMPORT_ALIASES.get(canon, canon)
        if canon in trusted_set or mapped_pypi in trusted_set or mapped_imp in trusted_set:
            return

        closest = _closest_known_package(canon, trusted_set)
        issue_type = "TYPOSQUATTING_RISK" if closest else "SLOPSQUATTING_RISK"
        key = (issue_type, canon, source_file)
        if key in seen_findings:
            return
        seen_findings.add(key)
        findings.append(
            {
                "type": issue_type,
                "reason": kind,
                "severity": "high" if closest else "medium",
                "package": raw_name,
                "canonical_name": canon,
                "closest_known": closest,
                "file": source_file,
                "line": line,
            }
        )

    for fpath in files:
        rel = fpath.relative_to(target_dir).as_posix()
        fname_lower = fpath.name.lower()
        if fpath.suffix.lower() == ".py":
            for lineno, mod_name in _extract_python_imports(fpath):
                checked_imports += 1
                _evaluate_pkg(mod_name, rel, lineno, "UNVERIFIED_IMPORT")
        elif fname_lower == "requirements.txt" or (
            fname_lower.startswith("requirements-") and fname_lower.endswith(".txt")
        ):
            for lineno, dep_name in _parse_requirements_file(fpath):
                checked_declared += 1
                _evaluate_pkg(dep_name, rel, lineno, "UNVERIFIED_DEPENDENCY")
        elif fname_lower == "pyproject.toml":
            for dep_name in _parse_pyproject_deps(fpath):
                checked_declared += 1
                _evaluate_pkg(dep_name, rel, 1, "UNVERIFIED_DEPENDENCY")

    return {
        "passed": len(findings) == 0,
        "checked_imports": checked_imports,
        "checked_declared": checked_declared,
        "local_modules_count": len(local_mods),
        "uv_lock_present": (target_dir / "uv.lock").is_file(),
        "findings": findings,
    }


def _run_gitleaks_if_available(target_dir: pathlib.Path) -> tuple[bool, list[dict[str, Any]]]:
    gitleaks_exe = shutil.which("gitleaks")
    if not gitleaks_exe:
        return False, []
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "timeout": 12,
        "shell": False,
    }
    if sys.platform == "win32" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        proc = subprocess.run(
            [
                gitleaks_exe,
                "detect",
                "--no-git",
                "--source",
                str(target_dir),
                "--report-format",
                "json",
                "--report-path",
                "-",
                "--exit-code",
                "0",
            ],
            **kwargs,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return True, []
        parsed = json.loads(proc.stdout)
        if not isinstance(parsed, list):
            return True, []
        out: list[dict[str, Any]] = []
        for item in parsed[:50]:
            if not isinstance(item, dict):
                continue
            secret_val = str(item.get("Secret") or item.get("Match") or "")
            raw_file = str(item.get("File") or "")
            try:
                rel_file = pathlib.Path(raw_file).resolve().relative_to(target_dir).as_posix()
            except Exception:
                rel_file = raw_file
            out.append(
                {
                    "engine": "gitleaks",
                    "rule": str(item.get("RuleID") or "gitleaks"),
                    "severity": "critical",
                    "file": rel_file,
                    "line": int(item.get("StartLine") or 1),
                    "preview": _redact_secret(secret_val),
                }
            )
        return True, out
    except Exception:
        return False, []


def scan_secrets(
    target_path: str | pathlib.Path,
    *,
    use_gitleaks: bool = True,
) -> dict[str, Any]:
    """
    Scan target directory deterministically for hardcoded credentials, API keys,
    private keys, JWTs, and credential DSNs using `gitleaks` (if present) + strict regex rules.
    """
    target_dir = validate_target_path(str(target_path) if isinstance(target_path, pathlib.Path) else target_path)
    files = _iter_project_files(target_dir)

    findings: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, int, str]] = set()
    files_scanned = 0

    gitleaks_used = False
    if use_gitleaks:
        gitleaks_used, gl_findings = _run_gitleaks_if_available(target_dir)
        for item in gl_findings:
            k = (str(item["file"]), int(item["line"]), str(item["rule"]))
            if k not in seen_keys:
                seen_keys.add(k)
                findings.append(item)

    for fpath in files:
        if fpath.suffix.lower() not in SCANNABLE_TEXT_EXTS and not fpath.name.startswith(".env"):
            continue
        try:
            if fpath.stat().st_size > MAX_SCAN_FILE_BYTES:
                continue
            raw_text = fpath.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        files_scanned += 1
        rel = fpath.relative_to(target_dir).as_posix()
        for lineno, line in enumerate(raw_text.splitlines(), 1):
            for rule_id, severity, pattern in _SECRET_RULES:
                for match in pattern.finditer(line):
                    matched_val = match.group(1) if match.lastindex else match.group(0)
                    if rule_id == "HARDCODED_SECRET_ASSIGN":
                        low_val = matched_val.lower()
                        if any(marker in low_val for marker in _PLACEHOLDER_MARKERS):
                            continue
                    dedup_key = (rel, lineno, rule_id)
                    if dedup_key in seen_keys:
                        continue
                    seen_keys.add(dedup_key)
                    findings.append(
                        {
                            "engine": "regex",
                            "rule": rule_id,
                            "severity": severity,
                            "file": rel,
                            "line": lineno,
                            "preview": _redact_secret(matched_val),
                        }
                    )

    return {
        "passed": len(findings) == 0,
        "files_scanned": files_scanned,
        "gitleaks_used": gitleaks_used,
        "findings": findings,
    }


def run_code_audit(
    target_path: str | pathlib.Path,
    *,
    check_secrets: bool = True,
    check_slopsquatting: bool = True,
    extra_allowlist: set[str] | frozenset[str] | None = None,
    use_gitleaks: bool = True,
) -> dict[str, Any]:
    """
    Execute the complete deterministic Orchestrator `code_audit` pipeline
    (Anti-Slopsquatting + Secret Scan) on a validated local directory.
    """
    target_dir = validate_target_path(str(target_path) if isinstance(target_path, pathlib.Path) else target_path)

    slop_res = (
        analyze_slopsquatting(target_dir, extra_allowlist=extra_allowlist)
        if check_slopsquatting
        else {"passed": True, "checked_imports": 0, "checked_declared": 0, "findings": []}
    )
    secret_res = (
        scan_secrets(target_dir, use_gitleaks=use_gitleaks)
        if check_secrets
        else {"passed": True, "files_scanned": 0, "gitleaks_used": False, "findings": []}
    )

    blockers: list[str] = []
    for item in secret_res["findings"]:
        blockers.append(
            f"[SECRET:{item['rule']}] {item['file']}:{item['line']} ({item['preview']})"
        )
    for item in slop_res["findings"]:
        hint = f" (typosquatting close to '{item['closest_known']}')" if item.get("closest_known") else ""
        blockers.append(
            f"[{item['type']}] {item['package']} in {item['file']}:{item['line']}{hint}"
        )

    passed = bool(slop_res["passed"] and secret_res["passed"])
    return {
        "ok": True,
        "passed": passed,
        "blocked": not passed,
        "target_path": str(target_dir),
        "slopsquatting": slop_res,
        "secrets": secret_res,
        "blockers": blockers,
        "summary": (
            "code_audit PASS (0 secrets, 0 slopsquatting risks)"
            if passed
            else f"code_audit BLOCKED ({len(secret_res['findings'])} secret(s), {len(slop_res['findings'])} slopsquatting risk(s))"
        ),
    }
