#!/usr/bin/env python3
"""Cross-platform zero-dependency privacy & security audit (10 checks) for Local LLM."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import pathlib
import re
import subprocess
import sys
from typing import Any


def _d(codes: tuple[int, ...]) -> str:
    return "".join(chr(c) for c in codes)


_ALLOWED_XML_NS = frozenset(
    {
        "http://www.w3.org/1998/Math/MathML",
        "http://www.w3.org/1999/xhtml",
        "http://www.w3.org/1999/xlink",
        "http://www.w3.org/2000/svg",
        "http://www.w3.org/XML/1998/namespace",
        "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    }
)

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]"})

_REQUIRED_GITIGNORE_RULES = (
    ".env",
    "*.gguf",
    "engines/",
    "models/heavy-moe/",
    ".coli_usage",
    ".coli_kv",
    "workspaces/vault/",
    "workspaces/runs/",
)

_EXPECTED_PRIVACY_ENV = {
    "DO_NOT_TRACK": "1",
    "OLLAMA_NOHISTORY": "1",
    "OLLAMA_NO_CLOUD": "1",
    "OFFLINE_MODE": "true",
    "HF_HUB_DISABLE_TELEMETRY": "1",
    "SCARF_NO_ANALYTICS": "true",
    "ANONYMIZED_TELEMETRY": "false",
    "KVSAVE": "0",
}

_URL_RE = re.compile(r"""https?://([^\s"'`<>)]+)""")

_PERSONAL_TERMS = (
    _d((103, 101, 114, 97, 114)),
    _d((50, 54, 53, 75, 70)),
    _d((53, 48, 55, 48, 32, 84, 105)),
    _d((67, 77, 72, 51, 50, 71, 88)),
    _d((57, 57, 48, 32, 80, 82, 79)),
    _d((57, 55, 48, 32, 69, 86, 79)),
    _d((54, 50, 49, 46, 50, 52)),
    _d((50, 54, 51, 48, 48)),
    _d((67, 58, 92, 80, 114, 111, 106, 101, 99, 116, 115)),
    _d((67, 58, 47, 80, 114, 111, 106, 101, 99, 116, 115)),
)

_MONOREPO_TERMS = (
    _d((95, 115, 104, 97, 114, 101, 100)),
    _d((119, 105, 110, 100, 111, 119, 115, 95, 100, 101, 115, 107, 116, 111, 112, 95, 107, 105, 116)),
    _d((119, 105, 110, 100, 111, 119, 115, 45, 100, 101, 115, 107, 116, 111, 112, 45, 107, 105, 116)),
    _d((67, 111, 110, 116, 114, 97, 116, 116, 111)),
    _d((75, 108, 97, 115, 116, 111, 115)),
    _d((75, 97, 105, 114, 111, 115)),
    _d((67, 111, 109, 112, 101, 110, 100, 105, 111)),
)

_SECRET_PATTERNS = (
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"\bsk-(?:proj-|live-|test-|ant-)?[A-Za-z0-9_-]{20,}"),
    re.compile(r"\bghp_[A-Za-z0-9]{36}\b"),
    re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"),
)

_EXCLUDED_WALK_DIRS = frozenset({".git", ".venv", "venv", "node_modules", "__pycache__", "engines", "models"})


def _get_tracked_or_walk_files(root: pathlib.Path) -> tuple[list[str], bool]:
    if (root / ".git").exists():
        try:
            proc = subprocess.run(
                ["git", "-C", str(root), "ls-files"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
                check=False,
            )
            if proc.returncode == 0:
                files = [line.strip().replace("\\", "/") for line in proc.stdout.splitlines() if line.strip()]
                return files, True
        except (OSError, subprocess.SubprocessError):
            pass

    rels: list[str] = []
    for cur, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d not in _EXCLUDED_WALK_DIRS]
        cur_p = pathlib.Path(cur)
        for n in names:
            try:
                rels.append((cur_p / n).relative_to(root).as_posix())
            except ValueError:
                pass
    return sorted(rels), False


def _is_scrub_test(rel: str) -> bool:
    return rel in {
        "scripts/privacy_audit.py",
        "workspaces/local_llm_core/tests/test_wu25_standalone_scrub.py",
    }


def _check_personal_leaks(root: pathlib.Path, rels: list[str]) -> dict[str, Any]:
    terms = list(_PERSONAL_TERMS)
    user = (getpass.getuser() or "").strip()
    if len(user) >= 4 and user.lower() not in ("root", "user", "runner", "admin", "docker", "vscode"):
        terms.append(user)
    rx = re.compile("|".join(re.escape(t) for t in terms), re.IGNORECASE)
    hits: list[str] = []
    for rel in rels:
        if _is_scrub_test(rel):
            continue
        p = root / rel
        if not p.is_file() or p.stat().st_size > 2_097_152:
            continue
        txt = p.read_text(encoding="utf-8", errors="ignore")
        if rx.search(txt):
            hits.append(rel)
    ok = not hits
    return {
        "id": 1,
        "title": "Zero personal paths, usernames, or workstation leaks",
        "passed": ok,
        "detail": "0 hits" if ok else f"Matches in: {', '.join(hits[:10])}",
    }


def _check_forbidden_tracked_files(root: pathlib.Path, rels: list[str], is_git: bool) -> dict[str, Any]:
    bad: list[str] = []
    hist_rx = re.compile(r"^(?:chat[-_]?history|conversations?)\.(?:json|md|txt|db|sqlite)$", re.IGNORECASE)
    for rel in rels:
        name = pathlib.Path(rel).name
        if is_git and (name == ".env" or name.endswith((".gguf", ".ggml", ".db", ".sqlite", ".log"))):
            bad.append(rel)
        elif rel.startswith("logs/") or hist_rx.match(name):
            bad.append(rel)
    ok = not bad
    return {
        "id": 2,
        "title": "Zero tracked .env, weights, databases, or chat history",
        "passed": ok,
        "detail": "0 forbidden files" if ok else f"Found: {', '.join(bad[:10])}",
    }


def _check_external_urls(root: pathlib.Path, rels: list[str]) -> dict[str, Any]:
    scan_targets: list[pathlib.Path] = []
    offline_chat = root / "workspaces" / "offline-chat"
    core_dir = root / "workspaces" / "local_llm_core"
    if offline_chat.is_dir() or core_dir.is_dir():
        for rel in rels:
            name = pathlib.Path(rel).name
            if "/tests/" in rel or name.startswith("test_") or name in ("package-lock.json", "package.json"):
                continue
            if rel in ("workspaces/offline-chat/index.html", "workspaces/offline-chat/server.py"):
                scan_targets.append(root / rel)
            elif rel.startswith("workspaces/offline-chat/static/") and rel.endswith((".js", ".css")):
                scan_targets.append(root / rel)
            elif rel.startswith("workspaces/offline-chat/web/dist/") and rel.endswith((".html", ".js", ".mjs", ".css", ".json")):
                scan_targets.append(root / rel)
            elif rel.startswith("workspaces/offline-chat/web/src/") and rel.endswith((".ts", ".tsx", ".css")):
                scan_targets.append(root / rel)
            elif rel.startswith("workspaces/local_llm_core/") and rel.endswith(".py"):
                scan_targets.append(root / rel)
    else:
        for rel in rels:
            if rel.endswith((".py", ".html", ".js", ".css")):
                scan_targets.append(root / rel)

    hits: list[str] = []
    for path in scan_targets:
        if not path.is_file():
            continue
        txt = path.read_text(encoding="utf-8", errors="ignore")
        if "0.0.0.0" in txt:
            hits.append(f"{path.name}: contains 0.0.0.0")
        for m in _URL_RE.finditer(txt):
            full_url = m.group(0)
            if full_url in _ALLOWED_XML_NS:
                continue
            host_and_path = m.group(1)
            host_part = host_and_path.split("/")[0].split("?")[0].split("#")[0].split(":")[0]
            if host_part.startswith(("{", "$")):
                continue
            if host_part not in _LOOPBACK_HOSTS:
                hits.append(f"{path.name}: {full_url[:80]}")
    ok = not hits
    return {
        "id": 3,
        "title": "Zero external URLs or 0.0.0.0 bindings in app/UI",
        "passed": ok,
        "detail": f"Scanned {len(scan_targets)} file(s), 0 violations" if ok else "; ".join(hits[:8]),
    }


def _check_csp_and_no_store(root: pathlib.Path) -> dict[str, Any]:
    sec_py = root / "workspaces" / "local_llm_core" / "security.py"
    srv_py = root / "workspaces" / "offline-chat" / "server.py"
    sec_raw = sec_py.read_text(encoding="utf-8", errors="ignore") if sec_py.is_file() else ""
    srv_raw = srv_py.read_text(encoding="utf-8", errors="ignore") if srv_py.is_file() else ""

    notes: list[str] = []
    has_csp = ("STRICT_CSP_HEADER" in sec_raw) or ("Content-Security-Policy" in srv_raw)
    if not has_csp:
        notes.append("missing CSP header")
    csp_match = re.search(r"STRICT_CSP_HEADER\s*=\s*\(([^)]*)\)", sec_raw, re.DOTALL)
    if csp_match and "'unsafe-inline'" in csp_match.group(1):
        notes.append("STRICT_CSP_HEADER contains 'unsafe-inline'")
    combined = (sec_raw + "\n" + srv_raw).lower()
    if "no-store" not in combined:
        notes.append("missing no-store cache control")

    ok = not notes
    return {
        "id": 4,
        "title": "Strict CSP (no unsafe-inline) and Cache-Control no-store",
        "passed": ok,
        "detail": "Strict CSP + no-store active" if ok else "; ".join(notes),
    }


def _check_gitignore(root: pathlib.Path) -> dict[str, Any]:
    gi = root / ".gitignore"
    if not gi.is_file():
        return {"id": 5, "title": ".gitignore privacy rules", "passed": False, "detail": ".gitignore missing"}
    lines = {ln.strip().rstrip("/") for ln in gi.read_text(encoding="utf-8", errors="ignore").splitlines() if ln.strip() and not ln.strip().startswith("#")}
    missing = [rule for rule in _REQUIRED_GITIGNORE_RULES if rule.rstrip("/") not in lines]
    ok = not missing
    return {
        "id": 5,
        "title": ".gitignore privacy rules",
        "passed": ok,
        "detail": f"All {len(_REQUIRED_GITIGNORE_RULES)} rules present" if ok else f"Missing: {', '.join(missing)}",
    }


def _check_monorepo_references(root: pathlib.Path, rels: list[str]) -> dict[str, Any]:
    rx = re.compile("|".join(re.escape(t) for t in _MONOREPO_TERMS), re.IGNORECASE)
    hits: list[str] = []
    for rel in rels:
        if _is_scrub_test(rel):
            continue
        p = root / rel
        if not p.is_file() or p.stat().st_size > 2_097_152:
            continue
        if rx.search(p.read_text(encoding="utf-8", errors="ignore")):
            hits.append(rel)
    ok = not hits
    return {
        "id": 6,
        "title": "Zero external monorepo or sibling project references",
        "passed": ok,
        "detail": "0 references" if ok else f"Found in: {', '.join(hits[:10])}",
    }


def _check_secrets(root: pathlib.Path, rels: list[str]) -> dict[str, Any]:
    hits: list[str] = []
    for rel in rels:
        name = pathlib.Path(rel).name
        if name.startswith(".env") or name.startswith("test_") or "/tests/" in rel or rel == "scripts/privacy_audit.py":
            continue
        p = root / rel
        if not p.is_file() or p.stat().st_size > 2_097_152:
            continue
        txt = p.read_text(encoding="utf-8", errors="ignore")
        for pat in _SECRET_PATTERNS:
            if pat.search(txt):
                hits.append(rel)
                break
    ok = not hits
    return {
        "id": 7,
        "title": "Zero hardcoded secrets or private keys",
        "passed": ok,
        "detail": "0 secret hits" if ok else f"Hits in: {', '.join(hits[:10])}",
    }


def _check_gateway_hardening(root: pathlib.Path) -> dict[str, Any]:
    sec_py = root / "workspaces" / "local_llm_core" / "security.py"
    srv_py = root / "workspaces" / "offline-chat" / "server.py"
    sch_py = root / "workspaces" / "orchestrator" / "engine" / "schemas.py"

    sec_raw = sec_py.read_text(encoding="utf-8", errors="ignore") if sec_py.is_file() else ""
    srv_raw = srv_py.read_text(encoding="utf-8", errors="ignore") if srv_py.is_file() else ""
    sch_raw = sch_py.read_text(encoding="utf-8", errors="ignore") if sch_py.is_file() else ""

    notes: list[str] = []
    sec_ok = all(tok in sec_raw for tok in ("is_allowed_host", "is_allowed_origin_and_fetch_site", "is_authorized_token", "/api/auth/session-token"))
    srv_ok = all(tok in srv_raw for tok in ("_is_allowed_host", "_is_allowed_origin_and_fetch_site", "_is_authorized_token", "/api/auth/session-token"))
    if not (sec_ok or srv_ok):
        notes.append("gateway Host/CSRF/token hardening missing")
    if "validate_target_path" not in sch_raw or "ORCHESTRATOR_ALLOWED_ROOTS" not in sch_raw:
        notes.append("validate_target_path / ORCHESTRATOR_ALLOWED_ROOTS missing")

    ok = not notes
    return {
        "id": 8,
        "title": "Gateway Host/CSRF/Token & Orchestrator target_path hardening",
        "passed": ok,
        "detail": "All hardening guards present" if ok else "; ".join(notes),
    }


def _check_privacy_env(root: pathlib.Path) -> dict[str, Any]:
    env_file = root / ".env"
    if not env_file.is_file():
        env_file = root / ".env.example"
    if not env_file.is_file():
        return {"id": 9, "title": "Privacy variables in .env / .env.example", "passed": False, "detail": "No .env or .env.example found"}

    env_map: dict[str, str] = {}
    for raw_line in env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
        ln = raw_line.strip()
        if not ln or ln.startswith("#") or "=" not in ln:
            continue
        k, v = ln.split("=", 1)
        env_map[k.strip()] = v.strip()

    bad = [f"{k}={env_map.get(k, '<missing>')} (expected {v})" for k, v in _EXPECTED_PRIVACY_ENV.items() if env_map.get(k) != v]
    ok = not bad
    return {
        "id": 9,
        "title": f"Privacy variables in {env_file.name}",
        "passed": ok,
        "detail": f"{len(_EXPECTED_PRIVACY_ENV)}/{len(_EXPECTED_PRIVACY_ENV)} zero-telemetry vars set" if ok else "; ".join(bad[:6]),
    }


def _check_runtime_hygiene(root: pathlib.Path, *, strict: bool) -> dict[str, Any]:
    notes: list[str] = []
    ok = True

    vault_dir = root / "workspaces" / "vault"
    if vault_dir.is_dir():
        vault_files = [p for p in vault_dir.rglob("*") if p.is_file() and p.name != ".gitkeep"]
        if vault_files:
            ok = False
            notes.append(f"vault has {len(vault_files)} residual file(s)")
        else:
            notes.append("vault=clean")
    else:
        notes.append("vault=absent")

    app_profile = root / "launcher" / ".app-profile"
    if app_profile.exists():
        residual = [p for p in app_profile.rglob("*") if p.is_file()]
        if residual:
            ok = False
            notes.append(f"launcher/.app-profile has {len(residual)} residual file(s)")
    else:
        notes.append("app-profile=0B")

    if sys.platform == "win32":
        is_ci_host = (
            os.environ.get("CI", "").strip().lower() == "true"
            or os.environ.get("GITHUB_ACTIONS", "").strip().lower() == "true"
        )
        try:
            proc = subprocess.run(
                ["fsutil", "behavior", "query", "encryptpagingfile"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
                check=False,
            )
            m = re.search(r"EncryptPagingFile\s*=\s*(\d+)", proc.stdout or "", re.IGNORECASE)
            if m:
                val = int(m.group(1))
                if val == 1:
                    notes.append("EncryptPagingFile=1")
                elif is_ci_host:
                    notes.append("EncryptPagingFile=0 [CI runner host]")
                elif strict:
                    ok = False
                    notes.append("EncryptPagingFile=0 [strict FAIL]")
                else:
                    notes.append("EncryptPagingFile=0 [warn]")
        except (OSError, subprocess.SubprocessError):
            notes.append("EncryptPagingFile=unknown")

    return {
        "id": 10,
        "title": "Runtime hygiene (vault clean, no app-profile, OS pagefile)",
        "passed": ok,
        "detail": " | ".join(notes),
    }


def run_audit(root: pathlib.Path, *, strict: bool = False) -> dict[str, Any]:
    """Run all 10 cross-platform privacy & offline security checks."""
    resolved = root.resolve()
    rels, is_git = _get_tracked_or_walk_files(resolved)
    checks = [
        _check_personal_leaks(resolved, rels),
        _check_forbidden_tracked_files(resolved, rels, is_git),
        _check_external_urls(resolved, rels),
        _check_csp_and_no_store(resolved),
        _check_gitignore(resolved),
        _check_monorepo_references(resolved, rels),
        _check_secrets(resolved, rels),
        _check_gateway_hardening(resolved),
        _check_privacy_env(resolved),
        _check_runtime_hygiene(resolved, strict=strict),
    ]
    passed = all(c["passed"] for c in checks)
    return {"root": str(resolved), "strict": strict, "passed": passed, "checks": checks}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Cross-platform privacy & offline security audit.")
    parser.add_argument("--strict", action="store_true", help="Fail on strict OS hardening checks")
    parser.add_argument("--json", action="store_true", help="Output JSON report")
    parser.add_argument(
        "--root",
        default=str(pathlib.Path(__file__).resolve().parents[1]),
        help="Target repository root",
    )
    args = parser.parse_args(argv)
    report = run_audit(pathlib.Path(args.root), strict=args.strict)

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"=== Kryptos Core Cross-Platform Privacy Audit ({len(report['checks'])} checks) ===")
        for chk in report["checks"]:
            tag = "PASS" if chk["passed"] else "FAIL"
            print(f"[{tag}] Check {chk['id']}/10 - {chk['title']}: {chk['detail']}")
        print("=== PRIVACY AUDIT PASS ===" if report["passed"] else "=== PRIVACY AUDIT FAILED ===")

    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
