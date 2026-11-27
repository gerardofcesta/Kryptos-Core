"""Pre-publication Git history secret sanitizer, size/binary guard, and clean-orphan builder."""

from __future__ import annotations

import argparse
import os
import pathlib
import re
import shutil
import subprocess
import sys
from typing import Any

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
MAX_TRACKED_FILE_BYTES = 5 * 1024 * 1024  # 5 MB

_FORBIDDEN_EXTENSIONS = frozenset(
    {".gguf", ".ggml", ".bin", ".safetensors", ".pt", ".pth", ".ckpt", ".db", ".sqlite", ".sqlite3"}
)

_SECRET_RES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("OpenAI/Stripe key", re.compile(r"\bsk-(?:live|test|proj|svcacct)[-_][A-Za-z0-9_-]{16,}\b")),
    ("GitHub PAT", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")),
    ("AWS Access Key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Private Key Header", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----")),
)


def _run_git(args: list[str], cwd: pathlib.Path = REPO_ROOT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def get_tracked_files(repo_root: pathlib.Path = REPO_ROOT) -> list[str]:
    proc = _run_git(["ls-files"], cwd=repo_root)
    if proc.returncode != 0:
        return []
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def check_file_sizes_and_binaries(
    repo_root: pathlib.Path = REPO_ROOT,
    max_bytes: int = MAX_TRACKED_FILE_BYTES,
) -> list[str]:
    issues: list[str] = []
    for rel in get_tracked_files(repo_root):
        fpath = repo_root / rel
        if not fpath.is_file():
            continue
        name = fpath.name
        if name == ".env" or name.startswith(".env.") and name != ".env.example":
            issues.append(f"Forbidden env file tracked: {rel}")
        if fpath.suffix.lower() in _FORBIDDEN_EXTENSIONS:
            issues.append(f"Forbidden model/database binary tracked: {rel}")
        size = fpath.stat().st_size
        if size > max_bytes:
            issues.append(f"Tracked file exceeds 5 MB limit ({size} bytes): {rel}")
    return issues


def scan_text_for_secrets(text: str) -> list[str]:
    hits: list[str] = []
    for label, rx in _SECRET_RES:
        if rx.search(text):
            hits.append(label)
    return hits


def check_secrets_full_history(repo_root: pathlib.Path = REPO_ROOT) -> dict[str, Any]:
    gitleaks = shutil.which("gitleaks")
    if gitleaks:
        proc = subprocess.run(
            [gitleaks, "detect", "--source", str(repo_root), "--redact", "--no-banner"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        return {
            "engine": "gitleaks",
            "ok": proc.returncode == 0,
            "findings": [] if proc.returncode == 0 else [proc.stdout.strip() or proc.stderr.strip()],
        }

    findings: list[str] = []
    for rel in get_tracked_files(repo_root):
        if rel == "scripts/prepare_public_release.py":
            continue
        fpath = repo_root / rel
        if not fpath.is_file() or fpath.stat().st_size > 2_000_000:
            continue
        txt = fpath.read_text(encoding="utf-8", errors="ignore")
        for hit in scan_text_for_secrets(txt):
            findings.append(f"working-tree:{rel}: {hit}")

    hist_proc = _run_git(["log", "--all", "-p", "--unified=0"], cwd=repo_root)
    cur_file = "<history>"
    for line in hist_proc.stdout.splitlines():
        if line.startswith("+++ b/"):
            cur_file = line[6:].strip()
            continue
        if cur_file == "scripts/prepare_public_release.py":
            continue
        if line.startswith("+") and not line.startswith("+++"):
            for hit in scan_text_for_secrets(line[1:]):
                findings.append(f"git-history:{cur_file}: {hit}")
                if len(findings) >= 20:
                    break
        if len(findings) >= 20:
            break

    return {
        "engine": "regex+git-history",
        "ok": len(findings) == 0,
        "findings": findings,
    }


def run_release_checks(repo_root: pathlib.Path = REPO_ROOT) -> tuple[bool, list[str]]:
    errors: list[str] = []

    privacy_script = repo_root / "scripts" / "privacy_audit.py"
    if privacy_script.is_file():
        priv_proc = subprocess.run(
            [sys.executable, str(privacy_script), "--strict"],
            cwd=str(repo_root),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if priv_proc.returncode != 0:
            errors.append(f"privacy_audit --strict failed:\n{priv_proc.stdout}\n{priv_proc.stderr}")

    size_issues = check_file_sizes_and_binaries(repo_root)
    errors.extend(size_issues)

    sec = check_secrets_full_history(repo_root)
    if not sec["ok"]:
        errors.extend([f"Secret scan hit ({sec['engine']}): {item}" for item in sec["findings"]])

    return len(errors) == 0, errors


def create_clean_orphan_branch(branch_name: str, repo_root: pathlib.Path = REPO_ROOT) -> int:
    clean_name = branch_name.strip()
    if not clean_name or clean_name == "main" or clean_name.startswith("-"):
        print("Error: Provide a safe non-main orphan branch name.", file=sys.stderr)
        return 2

    status_proc = _run_git(["status", "--porcelain"], cwd=repo_root)
    if any(not line.startswith("?? ") for line in status_proc.stdout.splitlines() if line.strip()):
        print("Error: Uncommitted tracked changes present; commit before generating clean orphan.", file=sys.stderr)
        return 1

    ok, errors = run_release_checks(repo_root)
    if not ok:
        for err in errors:
            print(f"[FAIL] {err}", file=sys.stderr)
        return 1

    orig_branch = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_root).stdout.strip()
    try:
        subprocess.run(["git", "-C", str(repo_root), "checkout", "--orphan", clean_name], check=True)
        subprocess.run(["git", "-C", str(repo_root), "add", "-u"], check=False)
        tracked = get_tracked_files(repo_root)
        if tracked:
            subprocess.run(["git", "-C", str(repo_root), "add", "--", *tracked], check=True)
        subprocess.run(
            ["git", "-C", str(repo_root), "commit", "-m", "chore: clean squashed public release v3.0.0"],
            check=True,
        )
    finally:
        if orig_branch and orig_branch != "HEAD":
            _run_git(["checkout", orig_branch], cwd=repo_root)

    print(f"Created verified clean orphan branch '{clean_name}' (local only, never pushed).")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pre-publication sanitizer & clean orphan generator.")
    parser.add_argument("--check", action="store_true", help="Run full privacy, size, binary & history secret checks")
    parser.add_argument("--create-clean-orphan", default="", help="Create squashed orphan branch after passing checks")
    args = parser.parse_args(argv)

    if args.create_clean_orphan:
        return create_clean_orphan_branch(args.create_clean_orphan)

    ok, errors = run_release_checks(REPO_ROOT)
    if not ok:
        for err in errors:
            print(f"[FAIL] {err}", file=sys.stderr)
        return 1
    print("=== prepare_public_release --check PASS (0 secrets, 0 leaks, all tracked files < 5 MB) ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
