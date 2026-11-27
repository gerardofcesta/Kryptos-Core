"""
Deterministic pre-LLM repository/data scout for Local LLM Orchestrator.

Runs inside the offline Docker sandbox (`/target:ro`, `--network none`, `--read-only`)
before any LLM turn to generate a compact `01_SURFACE_MAP.json` structure and a
token-budgeted markdown summary (`<= 4800` chars / `<= 1200` tokens) for the
Heavy MoE 177B Strategist.

Pure Python stdlib on the host side. Optional read-only host fallback is ONLY
enabled when explicitly requested via `allow_host_readonly_fallback=True`.
"""

from __future__ import annotations

import ast
import csv
import json
import os
import pathlib
import sqlite3
from typing import Any
import urllib.parse

from .sandbox_runner import ALLOWED_RETENTION_MODES, DockerSandboxRunner

MAX_FILES_LIST = 250
MAX_PYTHON_AST_FILES = 60
MAX_DATA_SCHEMA_FILES = 20
MAX_STATIC_FINDINGS = 30
MAX_COMPACT_SUMMARY_CHARS = 4800
SCOUT_MAX_OUTPUT_BYTES = 512 * 1024

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
        ".idea",
        ".vscode",
        ".next",
        ".nuxt",
    }
)

SKIPPED_BINARY_EXTS = frozenset(
    {
        ".pyc",
        ".pyo",
        ".pyd",
        ".so",
        ".dll",
        ".exe",
        ".dylib",
        ".o",
        ".a",
        ".lib",
        ".obj",
        ".class",
        ".jar",
        ".war",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".bmp",
        ".ico",
        ".webp",
        ".tif",
        ".tiff",
        ".mp3",
        ".mp4",
        ".wav",
        ".avi",
        ".mov",
        ".mkv",
        ".flac",
        ".zip",
        ".tar",
        ".gz",
        ".bz2",
        ".xz",
        ".7z",
        ".rar",
        ".woff",
        ".woff2",
        ".ttf",
        ".otf",
        ".eot",
        ".gguf",
        ".bin",
        ".safetensors",
        ".pt",
        ".pth",
        ".onnx",
        ".iso",
        ".dmg",
    }
)

DATA_SCHEMA_EXTS = frozenset(
    {".csv", ".tsv", ".parquet", ".json", ".jsonl", ".db", ".sqlite", ".sqlite3", ".xlsx", ".pdf"}
)

ENTRYPOINT_EXACT_NAMES = frozenset(
    {
        "main.py",
        "server.py",
        "app.py",
        "cli.py",
        "manage.py",
        "wsgi.py",
        "asgi.py",
        "dockerfile",
        "docker-compose.yml",
        "docker-compose.yaml",
        "package.json",
        "pyproject.toml",
        "setup.py",
        "requirements.txt",
        "makefile",
        "cargo.toml",
        "go.mod",
    }
)

RISKY_SINK_MODULES = frozenset(
    {
        "subprocess",
        "sqlite3",
        "pickle",
        "marshal",
        "threading",
        "multiprocessing",
        "ctypes",
        "socket",
        "urllib",
        "requests",
        "shutil",
    }
)

RISKY_SINK_CALLS = frozenset(
    {
        "eval",
        "exec",
        "open",
        "compile",
        "__import__",
        "os.system",
        "os.popen",
        "os.remove",
        "os.unlink",
        "os.replace",
        "shutil.rmtree",
        "subprocess.run",
        "subprocess.Popen",
        "subprocess.call",
        "subprocess.check_call",
        "subprocess.check_output",
        "pickle.load",
        "pickle.loads",
        "yaml.load",
        "sqlite3.connect",
        "threading.Thread",
        "multiprocessing.Process",
    }
)


def _format_bytes(num_bytes: int) -> str:
    if num_bytes < 1024:
        return f"{num_bytes} B"
    if num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024:.1f} KB"
    return f"{num_bytes / (1024 * 1024):.2f} MB"


def build_compact_summary(report: dict[str, Any], max_chars: int = MAX_COMPACT_SUMMARY_CHARS) -> str:
    """Build a compact markdown summary strictly capped to <= max_chars (default 4800)."""
    target_name = str(report.get("targetName") or "target")
    mode = str(report.get("mode") or "docker_sandbox")
    file_count = int(report.get("fileCount") or 0)
    total_bytes = int(report.get("totalBytes") or 0)
    languages = report.get("languages") or {}
    entrypoints = report.get("entrypoints") or []
    python_ast = report.get("pythonAst") or []
    data_schemas = report.get("dataSchemas") or []
    static_findings = report.get("staticFindings") or {}
    ruff_items = static_findings.get("ruff") or []
    bandit_items = static_findings.get("bandit") or []

    lines: list[str] = [
        f"# Surface Map: {target_name}",
        f"- Mode: `{mode}` | Files: `{file_count}` | Size: `{_format_bytes(total_bytes)}`",
    ]

    if languages:
        top_langs = ", ".join(f"{ext}({cnt})" for ext, cnt in list(languages.items())[:8])
        lines.append(f"- Languages: {top_langs}")

    if entrypoints:
        ep_str = ", ".join(f"`{ep}`" for ep in entrypoints[:15])
        if len(entrypoints) > 15:
            ep_str += f" (+{len(entrypoints) - 15} more)"
        lines.append(f"\n## Entrypoints\n{ep_str}")

    if python_ast:
        lines.append(f"\n## Python Modules ({len(python_ast)} analyzed)")
        ranked = sorted(
            python_ast,
            key=lambda item: (
                -len(item.get("riskySinks") or []),
                -int(item.get("lines") or 0),
                str(item.get("path") or ""),
            ),
        )
        for mod in ranked[:15]:
            rel = mod.get("path", "")
            nlines = mod.get("lines", 0)
            classes = (mod.get("classes") or [])[:5]
            funcs = (mod.get("functions") or [])[:6]
            sinks = (mod.get("riskySinks") or [])[:6]
            parts = [f"`{rel}` ({nlines}L)"]
            if classes:
                parts.append("cls=" + ",".join(classes))
            if funcs:
                parts.append("fn=" + ",".join(funcs))
            if sinks:
                parts.append("sinks=" + ",".join(sinks))
            if mod.get("parseError"):
                parts.append(f"parseErr={mod['parseError']}")
            lines.append("- " + " | ".join(parts))

    if data_schemas:
        lines.append(f"\n## Data Files ({len(data_schemas)} inspected)")
        for ds in data_schemas[:10]:
            rel = ds.get("path", "")
            kind = ds.get("kind", "data")
            detail_parts: list[str] = [f"`{rel}` ({kind})"]
            if "rowCount" in ds and ds["rowCount"] is not None:
                detail_parts.append(f"rows={ds['rowCount']}")
            if "pages" in ds and ds["pages"] is not None:
                detail_parts.append(f"pages={ds['pages']}")
            if ds.get("columns"):
                cols = [str(c) for c in ds["columns"][:8]]
                col_str = ",".join(cols) + ("..." if len(ds["columns"]) > 8 else "")
                detail_parts.append(f"cols=[{col_str}]")
            if ds.get("keys"):
                keys = [str(k) for k in ds["keys"][:8]]
                key_str = ",".join(keys) + ("..." if len(ds["keys"]) > 8 else "")
                detail_parts.append(f"keys=[{key_str}]")
            if ds.get("tables"):
                tbls = [f"{t.get('name')}({t.get('rows', '?')})" for t in ds["tables"][:5]]
                detail_parts.append("tables=" + ",".join(tbls))
            if ds.get("error"):
                detail_parts.append(f"err={ds['error']}")
            lines.append("- " + " | ".join(detail_parts))

    lines.append(
        f"\n## Static Analysis\n- Ruff findings: `{len(ruff_items)}` | Bandit findings: `{len(bandit_items)}`"
    )
    for finding in bandit_items[:6]:
        lines.append(
            f"- [Bandit {finding.get('severity', '?')}/{finding.get('test_id', '?')}] "
            f"`{finding.get('file', '')}:{finding.get('line', 0)}` {str(finding.get('issue_text', ''))[:100]}"
        )
    for finding in ruff_items[:6]:
        lines.append(
            f"- [Ruff {finding.get('code', '?')}] "
            f"`{finding.get('file', '')}:{finding.get('line', 0)}` {str(finding.get('message', ''))[:100]}"
        )

    summary = "\n".join(lines).strip()
    if len(summary) <= max_chars:
        return summary

    marker = "\n...[SUMMARY TRUNCATED]"
    budget = max(0, max_chars - len(marker))
    clipped = summary[:budget].rstrip()
    last_nl = clipped.rfind("\n")
    if last_nl > budget // 2:
        clipped = clipped[:last_nl]
    return clipped + marker


def _is_entrypoint(rel_posix: str) -> bool:
    name = rel_posix.rsplit("/", 1)[-1]
    lower = name.lower()
    if lower in ENTRYPOINT_EXACT_NAMES:
        return True
    if lower.startswith("index.") or lower.startswith("dockerfile."):
        return True
    if lower.endswith(".ps1"):
        return True
    return False


def _extract_call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _extract_call_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return ""


def _analyze_python_ast_host(path: pathlib.Path, rel_posix: str) -> dict[str, Any]:
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {
            "path": rel_posix,
            "lines": 0,
            "classes": [],
            "functions": [],
            "imports": [],
            "riskySinks": [],
            "parseError": str(exc),
        }

    line_count = source.count("\n") + (1 if source else 0)
    try:
        tree = ast.parse(source, filename=rel_posix)
    except SyntaxError as exc:
        return {
            "path": rel_posix,
            "lines": line_count,
            "classes": [],
            "functions": [],
            "imports": [],
            "riskySinks": [],
            "parseError": f"SyntaxError line {exc.lineno}: {exc.msg}",
        }

    classes: list[str] = []
    functions: list[str] = []
    imports: set[str] = set()
    sinks: set[str] = set()

    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            classes.append(node.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.append(node.name)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root_mod = alias.name.split(".", 1)[0]
                imports.add(root_mod)
                if root_mod in RISKY_SINK_MODULES:
                    sinks.add(root_mod)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                root_mod = node.module.split(".", 1)[0]
                imports.add(root_mod)
                if root_mod in RISKY_SINK_MODULES:
                    sinks.add(root_mod)
        elif isinstance(node, ast.Call):
            cname = _extract_call_name(node.func)
            if not cname:
                continue
            root_call = cname.split(".", 1)[0]
            if cname in RISKY_SINK_CALLS:
                sinks.add(cname)
            elif root_call in RISKY_SINK_MODULES:
                sinks.add(cname)

    return {
        "path": rel_posix,
        "lines": line_count,
        "classes": classes[:40],
        "functions": functions[:60],
        "imports": sorted(imports)[:50],
        "riskySinks": sorted(sinks)[:30],
        "parseError": None,
    }


def _inspect_data_file_host(path: pathlib.Path, rel_posix: str, ext: str) -> dict[str, Any]:
    size = path.stat().st_size if path.exists() else 0
    if ext in (".csv", ".tsv"):
        delimiter = "\t" if ext == ".tsv" else ","
        try:
            with path.open("r", encoding="utf-8", errors="replace", newline="") as fh:
                reader = csv.reader(fh, delimiter=delimiter)
                header = next(reader, [])
                row_count = 0
                sampled = False
                for _ in reader:
                    row_count += 1
                    if row_count >= 5000:
                        sampled = True
                        break
            return {
                "path": rel_posix,
                "kind": ext.lstrip("."),
                "size": size,
                "columns": [c[:64] for c in header[:50]],
                "rowCount": f">={row_count}" if sampled else row_count,
            }
        except Exception as exc:
            return {"path": rel_posix, "kind": ext.lstrip("."), "size": size, "error": str(exc)}

    if ext == ".json":
        if size > 5 * 1024 * 1024:
            return {"path": rel_posix, "kind": "json", "size": size, "note": "file > 5MB skipped"}
        try:
            obj = json.loads(path.read_text(encoding="utf-8", errors="replace"))
            if isinstance(obj, dict):
                return {
                    "path": rel_posix,
                    "kind": "json",
                    "size": size,
                    "jsonType": "object",
                    "keys": [str(k)[:64] for k in list(obj.keys())[:40]],
                }
            if isinstance(obj, list):
                first_keys = (
                    [str(k)[:64] for k in list(obj[0].keys())[:40]]
                    if obj and isinstance(obj[0], dict)
                    else []
                )
                return {
                    "path": rel_posix,
                    "kind": "json",
                    "size": size,
                    "jsonType": "array",
                    "rowCount": len(obj),
                    "keys": first_keys,
                }
            return {"path": rel_posix, "kind": "json", "size": size, "jsonType": type(obj).__name__}
        except Exception as exc:
            return {"path": rel_posix, "kind": "json", "size": size, "error": str(exc)}

    if ext == ".jsonl":
        try:
            keys: list[str] = []
            row_count = 0
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    row_count += 1
                    if not keys:
                        try:
                            sample = json.loads(stripped)
                            if isinstance(sample, dict):
                                keys = [str(k)[:64] for k in list(sample.keys())[:40]]
                        except ValueError:
                            pass
                    if row_count >= 5000:
                        break
            return {
                "path": rel_posix,
                "kind": "jsonl",
                "size": size,
                "rowCount": row_count,
                "keys": keys,
            }
        except Exception as exc:
            return {"path": rel_posix, "kind": "jsonl", "size": size, "error": str(exc)}

    if ext in (".db", ".sqlite", ".sqlite3"):
        try:
            uri_path = urllib.parse.quote(path.resolve().as_posix(), safe="/:")
            uri = f"file:{uri_path}?mode=ro&immutable=1"
            conn = sqlite3.connect(uri, uri=True)
            try:
                cur = conn.cursor()
                cur.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name LIMIT 20"
                )
                tables = []
                for (tname,) in cur.fetchall():
                    safe_t = tname.replace('"', '""')
                    cur.execute(f'PRAGMA table_info("{safe_t}")')
                    cols = [row[1] for row in cur.fetchall()][:30]
                    cur.execute(f'SELECT COUNT(*) FROM "{safe_t}"')
                    cnt = cur.fetchone()[0]
                    tables.append({"name": tname, "columns": cols, "rows": cnt})
            finally:
                conn.close()
            return {"path": rel_posix, "kind": "sqlite", "size": size, "tables": tables}
        except Exception as exc:
            return {"path": rel_posix, "kind": "sqlite", "size": size, "error": str(exc)}

    return {
        "path": rel_posix,
        "kind": ext.lstrip("."),
        "size": size,
        "note": "requires Docker sandbox libraries for deep schema inspection",
    }


def _count_text_lines_safe(path: pathlib.Path, size: int) -> int | None:
    if size > 4 * 1024 * 1024:
        return None
    try:
        with path.open("rb") as fh:
            head = fh.read(2048)
            if b"\x00" in head:
                return None
            count = head.count(b"\n")
            while True:
                chunk = fh.read(65536)
                if not chunk:
                    break
                if b"\x00" in chunk:
                    return None
                count += chunk.count(b"\n")
            return count + (1 if size > 0 else 0)
    except OSError:
        return None


def _run_host_readonly_scout(target_path: pathlib.Path, fallback_reason: str = "") -> dict[str, Any]:
    """Pure-stdlib, strictly read-only host fallback scout (zero writes)."""
    resolved = target_path.resolve()
    languages: dict[str, int] = {}
    files_list: list[dict[str, Any]] = []
    entrypoints: list[str] = []
    python_ast: list[dict[str, Any]] = []
    data_schemas: list[dict[str, Any]] = []
    file_count = 0
    total_bytes = 0

    all_entries: list[tuple[str, pathlib.Path]] = []
    if resolved.is_file():
        all_entries.append((resolved.name, resolved))
    else:
        for dirpath, dirnames, filenames in os.walk(resolved, topdown=True, followlinks=False):
            dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDED_DIRS)
            for fname in sorted(filenames):
                full = pathlib.Path(dirpath) / fname
                rel = full.relative_to(resolved).as_posix()
                all_entries.append((rel, full))

    for rel, full in all_entries:
        ext = full.suffix.lower()
        if ext in SKIPPED_BINARY_EXTS:
            continue
        try:
            st = full.stat()
            size = int(st.st_size)
        except OSError:
            continue

        file_count += 1
        total_bytes += size
        lang_key = ext if ext else "<no_ext>"
        languages[lang_key] = languages.get(lang_key, 0) + 1

        if _is_entrypoint(rel):
            entrypoints.append(rel)

        if len(files_list) < MAX_FILES_LIST:
            line_cnt = None if ext in (".parquet", ".db", ".sqlite", ".sqlite3", ".xlsx", ".pdf") else _count_text_lines_safe(full, size)
            files_list.append({"path": rel, "size": size, "lines": line_cnt})

        if ext == ".py" and len(python_ast) < MAX_PYTHON_AST_FILES:
            python_ast.append(_analyze_python_ast_host(full, rel))

        if ext in DATA_SCHEMA_EXTS and len(data_schemas) < MAX_DATA_SCHEMA_FILES:
            data_schemas.append(_inspect_data_file_host(full, rel, ext))

    sorted_langs = dict(sorted(languages.items(), key=lambda kv: (-kv[1], kv[0])))

    report: dict[str, Any] = {
        "mode": "host_readonly_fallback",
        "executionMode": "host_readonly",
        "dockerAvailable": False,
        "fallbackReason": fallback_reason,
        "targetName": resolved.name or str(resolved),
        "fileCount": file_count,
        "totalBytes": total_bytes,
        "languages": sorted_langs,
        "files": files_list,
        "entrypoints": entrypoints[:50],
        "pythonAst": python_ast,
        "dataSchemas": data_schemas,
        "staticFindings": {"ruff": [], "bandit": []},
    }
    report["compactSummary"] = build_compact_summary(report, MAX_COMPACT_SUMMARY_CHARS)
    return report


_SANDBOX_SCOUT_SCRIPT = r"""
import ast
import csv
import json
import os
import pathlib
import sqlite3
import subprocess
import urllib.parse

ROOT = pathlib.Path("/target")
MAX_FILES_LIST = 250
MAX_PYTHON_AST_FILES = 60
MAX_DATA_SCHEMA_FILES = 20
MAX_STATIC_FINDINGS = 30

EXCLUDED_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", "env",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", ".eggs",
    "dist", "build", ".idea", ".vscode", ".next", ".nuxt",
}

SKIPPED_BINARY_EXTS = {
    ".pyc", ".pyo", ".pyd", ".so", ".dll", ".exe", ".dylib", ".o", ".a", ".lib",
    ".obj", ".class", ".jar", ".war", ".png", ".jpg", ".jpeg", ".gif", ".bmp",
    ".ico", ".webp", ".tif", ".tiff", ".mp3", ".mp4", ".wav", ".avi", ".mov",
    ".mkv", ".flac", ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar",
    ".woff", ".woff2", ".ttf", ".otf", ".eot", ".gguf", ".bin", ".safetensors",
    ".pt", ".pth", ".onnx", ".iso", ".dmg",
}

DATA_SCHEMA_EXTS = {
    ".csv", ".tsv", ".parquet", ".json", ".jsonl", ".db", ".sqlite", ".sqlite3", ".xlsx", ".pdf"
}

ENTRYPOINT_EXACT_NAMES = {
    "main.py", "server.py", "app.py", "cli.py", "manage.py", "wsgi.py", "asgi.py",
    "dockerfile", "docker-compose.yml", "docker-compose.yaml", "package.json",
    "pyproject.toml", "setup.py", "requirements.txt", "makefile", "cargo.toml", "go.mod",
}

RISKY_SINK_MODULES = {
    "subprocess", "sqlite3", "pickle", "marshal", "threading", "multiprocessing",
    "ctypes", "socket", "urllib", "requests", "shutil",
}

RISKY_SINK_CALLS = {
    "eval", "exec", "open", "compile", "__import__",
    "os.system", "os.popen", "os.remove", "os.unlink", "os.replace",
    "shutil.rmtree", "subprocess.run", "subprocess.Popen", "subprocess.call",
    "subprocess.check_call", "subprocess.check_output",
    "pickle.load", "pickle.loads", "yaml.load",
    "sqlite3.connect", "threading.Thread", "multiprocessing.Process",
}


def is_entrypoint(rel_posix):
    name = rel_posix.rsplit("/", 1)[-1]
    lower = name.lower()
    return (
        lower in ENTRYPOINT_EXACT_NAMES
        or lower.startswith("index.")
        or lower.startswith("dockerfile.")
        or lower.endswith(".ps1")
    )


def extract_call_name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = extract_call_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return ""


def analyze_python_ast(path, rel_posix):
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {
            "path": rel_posix, "lines": 0, "classes": [], "functions": [],
            "imports": [], "riskySinks": [], "parseError": str(exc),
        }

    line_count = source.count("\n") + (1 if source else 0)
    try:
        tree = ast.parse(source, filename=rel_posix)
    except SyntaxError as exc:
        return {
            "path": rel_posix, "lines": line_count, "classes": [], "functions": [],
            "imports": [], "riskySinks": [], "parseError": f"SyntaxError line {exc.lineno}: {exc.msg}",
        }

    classes = []
    functions = []
    imports = set()
    sinks = set()

    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            classes.append(node.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.append(node.name)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root_mod = alias.name.split(".", 1)[0]
                imports.add(root_mod)
                if root_mod in RISKY_SINK_MODULES:
                    sinks.add(root_mod)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                root_mod = node.module.split(".", 1)[0]
                imports.add(root_mod)
                if root_mod in RISKY_SINK_MODULES:
                    sinks.add(root_mod)
        elif isinstance(node, ast.Call):
            cname = extract_call_name(node.func)
            if not cname:
                continue
            root_call = cname.split(".", 1)[0]
            if cname in RISKY_SINK_CALLS or root_call in RISKY_SINK_MODULES:
                sinks.add(cname)

    return {
        "path": rel_posix,
        "lines": line_count,
        "classes": classes[:40],
        "functions": functions[:60],
        "imports": sorted(imports)[:50],
        "riskySinks": sorted(sinks)[:30],
        "parseError": None,
    }


def count_text_lines(path, size):
    if size > 4 * 1024 * 1024:
        return None
    try:
        with path.open("rb") as fh:
            head = fh.read(2048)
            if b"\x00" in head:
                return None
            count = head.count(b"\n")
            while True:
                chunk = fh.read(65536)
                if not chunk:
                    break
                if b"\x00" in chunk:
                    return None
                count += chunk.count(b"\n")
            return count + (1 if size > 0 else 0)
    except OSError:
        return None


def inspect_data_file(path, rel_posix, ext):
    size = path.stat().st_size if path.exists() else 0
    if ext in (".csv", ".tsv"):
        delim = "\t" if ext == ".tsv" else ","
        try:
            with path.open("r", encoding="utf-8", errors="replace", newline="") as fh:
                reader = csv.reader(fh, delimiter=delim)
                header = next(reader, [])
                row_count = 0
                sampled = False
                for _ in reader:
                    row_count += 1
                    if row_count >= 5000:
                        sampled = True
                        break
            return {
                "path": rel_posix, "kind": ext.lstrip("."), "size": size,
                "columns": [c[:64] for c in header[:50]],
                "rowCount": f">={row_count}" if sampled else row_count,
            }
        except Exception as exc:
            return {"path": rel_posix, "kind": ext.lstrip("."), "size": size, "error": str(exc)}

    if ext == ".parquet":
        try:
            import duckdb
            con = duckdb.connect(":memory:")
            try:
                esc = str(path).replace("'", "''")
                desc = con.execute(f"DESCRIBE SELECT * FROM read_parquet('{esc}')").fetchall()
                cols = [f"{r[0]}:{r[1]}" for r in desc[:50]]
                cnt = con.execute(f"SELECT COUNT(*) FROM read_parquet('{esc}')").fetchone()[0]
            finally:
                con.close()
            return {"path": rel_posix, "kind": "parquet", "size": size, "columns": cols, "rowCount": int(cnt)}
        except Exception as exc:
            return {"path": rel_posix, "kind": "parquet", "size": size, "error": str(exc)}

    if ext == ".json":
        if size > 5 * 1024 * 1024:
            return {"path": rel_posix, "kind": "json", "size": size, "note": "file > 5MB skipped"}
        try:
            obj = json.loads(path.read_text(encoding="utf-8", errors="replace"))
            if isinstance(obj, dict):
                return {
                    "path": rel_posix, "kind": "json", "size": size, "jsonType": "object",
                    "keys": [str(k)[:64] for k in list(obj.keys())[:40]],
                }
            if isinstance(obj, list):
                first_keys = [str(k)[:64] for k in list(obj[0].keys())[:40]] if obj and isinstance(obj[0], dict) else []
                return {
                    "path": rel_posix, "kind": "json", "size": size, "jsonType": "array",
                    "rowCount": len(obj), "keys": first_keys,
                }
            return {"path": rel_posix, "kind": "json", "size": size, "jsonType": type(obj).__name__}
        except Exception as exc:
            return {"path": rel_posix, "kind": "json", "size": size, "error": str(exc)}

    if ext == ".jsonl":
        try:
            keys = []
            row_count = 0
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    row_count += 1
                    if not keys:
                        try:
                            sample = json.loads(stripped)
                            if isinstance(sample, dict):
                                keys = [str(k)[:64] for k in list(sample.keys())[:40]]
                        except ValueError:
                            pass
                    if row_count >= 5000:
                        break
            return {"path": rel_posix, "kind": "jsonl", "size": size, "rowCount": row_count, "keys": keys}
        except Exception as exc:
            return {"path": rel_posix, "kind": "jsonl", "size": size, "error": str(exc)}

    if ext in (".db", ".sqlite", ".sqlite3"):
        try:
            uri_path = urllib.parse.quote(path.resolve().as_posix(), safe="/:")
            conn = sqlite3.connect(f"file:{uri_path}?mode=ro&immutable=1", uri=True)
            try:
                cur = conn.cursor()
                cur.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name LIMIT 20"
                )
                tables = []
                for (tname,) in cur.fetchall():
                    safe_t = tname.replace('"', '""')
                    cur.execute(f'PRAGMA table_info("{safe_t}")')
                    cols = [row[1] for row in cur.fetchall()][:30]
                    cur.execute(f'SELECT COUNT(*) FROM "{safe_t}"')
                    cnt = cur.fetchone()[0]
                    tables.append({"name": tname, "columns": cols, "rows": cnt})
            finally:
                conn.close()
            return {"path": rel_posix, "kind": "sqlite", "size": size, "tables": tables}
        except Exception as exc:
            return {"path": rel_posix, "kind": "sqlite", "size": size, "error": str(exc)}

    if ext == ".xlsx":
        try:
            import openpyxl
            wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
            try:
                sheets = []
                for sname in wb.sheetnames[:10]:
                    ws = wb[sname]
                    first_row = next(ws.iter_rows(min_row=1, max_row=1, values_only=True), ())
                    cols = [str(c)[:64] for c in first_row if c is not None][:30]
                    sheets.append({"name": sname, "columns": cols, "rows": ws.max_row})
            finally:
                wb.close()
            return {"path": rel_posix, "kind": "xlsx", "size": size, "tables": sheets}
        except Exception as exc:
            return {"path": rel_posix, "kind": "xlsx", "size": size, "error": str(exc)}

    if ext == ".pdf":
        try:
            import fitz
            doc = fitz.open(str(path))
            try:
                pages = doc.page_count
                title = (doc.metadata or {}).get("title") or ""
            finally:
                doc.close()
            return {"path": rel_posix, "kind": "pdf", "size": size, "pages": pages, "title": title[:120]}
        except Exception as exc:
            return {"path": rel_posix, "kind": "pdf", "size": size, "error": str(exc)}

    return {"path": rel_posix, "kind": ext.lstrip("."), "size": size}


def norm_target_rel(raw_path):
    s = str(raw_path or "").replace("\\", "/")
    if s.startswith("/target/"):
        return s[len("/target/"):]
    if s == "/target":
        return ""
    return s


def run_ruff():
    try:
        proc = subprocess.run(
            ["ruff", "check", "--output-format", "json", "--no-cache", "/target"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        raw = (proc.stdout or "").strip()
        if not raw:
            return []
        parsed = json.loads(raw)
        if not isinstance(parsed, list):
            return []
        out = []
        for item in parsed[:MAX_STATIC_FINDINGS]:
            loc = item.get("location") or {}
            out.append({
                "file": norm_target_rel(item.get("filename")),
                "line": int(loc.get("row") or 0),
                "code": str(item.get("code") or ""),
                "message": str(item.get("message") or "")[:200],
            })
        return out
    except Exception:
        return []


def run_bandit():
    try:
        proc = subprocess.run(
            [
                "bandit", "-r", "-f", "json", "-q",
                "-x", "/target/.git,/target/node_modules,/target/__pycache__,/target/.venv,/target/venv",
                "/target",
            ],
            capture_output=True,
            text=True,
            timeout=25,
        )
        raw = (proc.stdout or "").strip()
        if not raw:
            return []
        parsed = json.loads(raw)
        results = parsed.get("results") if isinstance(parsed, dict) else []
        if not isinstance(results, list):
            return []
        sev_rank = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
        results.sort(key=lambda r: (sev_rank.get(str(r.get("issue_severity", "")).upper(), 9), str(r.get("filename", ""))))
        out = []
        for item in results[:MAX_STATIC_FINDINGS]:
            out.append({
                "file": norm_target_rel(item.get("filename")),
                "line": int(item.get("line_number") or 0),
                "test_id": str(item.get("test_id") or ""),
                "severity": str(item.get("issue_severity") or ""),
                "confidence": str(item.get("issue_confidence") or ""),
                "issue_text": str(item.get("issue_text") or "")[:200],
            })
        return out
    except Exception:
        return []


def main():
    languages = {}
    files_list = []
    entrypoints = []
    python_ast = []
    data_schemas = []
    file_count = 0
    total_bytes = 0

    all_entries = []
    if ROOT.is_file():
        all_entries.append((ROOT.name, ROOT))
    else:
        for dirpath, dirnames, filenames in os.walk(ROOT, topdown=True, followlinks=False):
            dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDED_DIRS)
            for fname in sorted(filenames):
                full = pathlib.Path(dirpath) / fname
                rel = full.relative_to(ROOT).as_posix()
                all_entries.append((rel, full))

    has_python = False
    for rel, full in all_entries:
        ext = full.suffix.lower()
        if ext in SKIPPED_BINARY_EXTS:
            continue
        try:
            size = int(full.stat().st_size)
        except OSError:
            continue

        file_count += 1
        total_bytes += size
        lang_key = ext if ext else "<no_ext>"
        languages[lang_key] = languages.get(lang_key, 0) + 1

        if is_entrypoint(rel):
            entrypoints.append(rel)

        if len(files_list) < MAX_FILES_LIST:
            line_cnt = None if ext in (".parquet", ".db", ".sqlite", ".sqlite3", ".xlsx", ".pdf") else count_text_lines(full, size)
            files_list.append({"path": rel, "size": size, "lines": line_cnt})

        if ext == ".py":
            has_python = True
            if len(python_ast) < MAX_PYTHON_AST_FILES:
                python_ast.append(analyze_python_ast(full, rel))

        if ext in DATA_SCHEMA_EXTS and len(data_schemas) < MAX_DATA_SCHEMA_FILES:
            data_schemas.append(inspect_data_file(full, rel, ext))

    sorted_langs = dict(sorted(languages.items(), key=lambda kv: (-kv[1], kv[0])))
    static_findings = {
        "ruff": run_ruff() if has_python else [],
        "bandit": run_bandit() if has_python else [],
    }

    report = {
        "mode": "docker_sandbox",
        "targetName": "target",
        "fileCount": file_count,
        "totalBytes": total_bytes,
        "languages": sorted_langs,
        "files": files_list,
        "entrypoints": entrypoints[:50],
        "pythonAst": python_ast,
        "dataSchemas": data_schemas,
        "staticFindings": static_findings,
    }
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
"""


def _parse_scout_json(stdout: str) -> dict[str, Any]:
    text = (stdout or "").strip()
    if not text:
        raise RuntimeError("Sandbox scout produced empty stdout")
    try:
        parsed = json.loads(text)
    except ValueError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise RuntimeError(f"Sandbox scout output is not valid JSON: {text[:240]}")
        parsed = json.loads(text[start : end + 1])

    if not isinstance(parsed, dict):
        raise RuntimeError("Sandbox scout JSON root must be an object")
    return parsed


VALID_SCOUT_EXECUTION_MODES = ("auto", "host_readonly", "docker")


def run_deterministic_scout(
    runner: DockerSandboxRunner,
    target_path: str | pathlib.Path,
    retention_mode: str,
    run_id: str,
    artifacts_path: str | pathlib.Path | None = None,
    allow_host_readonly_fallback: bool | None = None,
    execution_mode: str = "auto",
) -> dict[str, Any]:
    """
    Execute the deterministic pre-LLM scout against target_path.

    Supports `execution_mode` in `("auto", "host_readonly", "docker")` (default `"auto"`):
    - `"auto"`: uses Docker sandbox if daemon + image are ready, otherwise falls back cleanly
      to pure-stdlib read-only host mode (unless `allow_host_readonly_fallback=False`).
    - `"host_readonly"`: runs directly in pure-stdlib read-only host mode with zero writes.
    - `"docker"`: requires Docker sandbox unless `allow_host_readonly_fallback=True`.
    """
    if retention_mode not in ALLOWED_RETENTION_MODES:
        raise ValueError(
            f"Invalid retention_mode {retention_mode!r}; must be one of {ALLOWED_RETENTION_MODES}."
        )

    norm_mode = (execution_mode or "auto").strip().lower()
    if norm_mode not in VALID_SCOUT_EXECUTION_MODES:
        raise ValueError(
            f"Invalid execution_mode {execution_mode!r}; must be one of {VALID_SCOUT_EXECUTION_MODES}."
        )

    resolved_target = pathlib.Path(target_path).resolve()
    if not resolved_target.exists():
        raise FileNotFoundError(f"Target path does not exist: {resolved_target}")

    if norm_mode == "host_readonly":
        report = _run_host_readonly_scout(resolved_target, fallback_reason="host_readonly mode selected")
        report["mode"] = "host_readonly"
        report["executionMode"] = "host_readonly"
        return report

    can_fallback = (
        allow_host_readonly_fallback
        if allow_host_readonly_fallback is not None
        else (norm_mode == "auto")
    )

    docker_ok, docker_msg = runner.is_docker_available()
    image_ok = runner.is_image_available() if docker_ok else False

    if not docker_ok or not image_ok:
        reason = docker_msg if not docker_ok else f"Sandbox image not found locally: {runner.image}"
        if can_fallback:
            report = _run_host_readonly_scout(resolved_target, fallback_reason=reason)
            report["mode"] = "host_readonly_fallback" if allow_host_readonly_fallback is True else "host_readonly"
            report["executionMode"] = "host_readonly"
            return report
        raise RuntimeError(f"Docker sandbox unavailable for deterministic scout: {reason}")

    result = runner.run_python(
        code=_SANDBOX_SCOUT_SCRIPT,
        target_path=resolved_target,
        retention_mode=retention_mode,
        run_id=run_id,
        artifacts_path=artifacts_path,
        timeout_sec=90,
        max_output_bytes=SCOUT_MAX_OUTPUT_BYTES,
    )

    if not result.ok:
        reason = result.error or result.stderr or f"exit code {result.exit_code}"
        if can_fallback:
            report = _run_host_readonly_scout(resolved_target, fallback_reason=f"sandbox failed: {reason}")
            report["mode"] = "host_readonly_fallback" if allow_host_readonly_fallback is True else "host_readonly"
            report["executionMode"] = "host_readonly"
            return report
        raise RuntimeError(f"Deterministic scout failed inside Docker sandbox: {reason}")

    report = _parse_scout_json(result.stdout)
    report["mode"] = "docker_sandbox"
    report["executionMode"] = "docker"
    report["dockerAvailable"] = True
    report["targetName"] = resolved_target.name or str(resolved_target)
    report.setdefault("staticFindings", {"ruff": [], "bandit": []})
    report["compactSummary"] = build_compact_summary(report, MAX_COMPACT_SUMMARY_CHARS)
    return report
