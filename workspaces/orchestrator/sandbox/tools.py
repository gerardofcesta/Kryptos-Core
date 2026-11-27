"""
Five atomic read-only / sandboxed investigation tools for Local LLM Orchestrator.

Tools exposed to agents:
1. list_files(subdir="", glob="*", max_entries=100)
2. read_file_slice(rel_path, start_line=1, end_line=120)     [capped at 200 lines/call]
3. search_code(pattern, glob="", max_matches=40)             [rg inside /target:ro, Python regex fallback]
4. run_python_sandbox(code, timeout_sec=60)                  [ephemeral Docker container]
5. update_hypothesis(id, status, evidence, poc_snippet="")   [delegates to hypothesis_callback]

Pure Python stdlib only on the host side.
"""

from __future__ import annotations

import fnmatch
import inspect
import json
import os
import pathlib
import re
from typing import Any, Callable

from .sandbox_runner import ALLOWED_RETENTION_MODES, DockerSandboxRunner

TOOL_NAMES: tuple[str, ...] = (
    "list_files",
    "read_file_slice",
    "search_code",
    "run_python_sandbox",
    "update_hypothesis",
)

HOST_READONLY_TOOL_NAMES: tuple[str, ...] = (
    "list_files",
    "read_file_slice",
    "search_code",
    "update_hypothesis",
    "code_audit",
)

_EXCLUDED_HOST_DIRS = frozenset(
    {
        ".git",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
    }
)

VALID_HYPOTHESIS_STATUSES = frozenset(
    {"OPEN", "TESTING", "CONFIRMED", "REFUTED", "BLOCKED", "ESCALATE"}
)

MAX_SLICE_LINES = 200
MAX_LIST_ENTRIES = 500
MAX_SEARCH_MATCHES = 200
MAX_PYTHON_TIMEOUT_SEC = 120
TOOL_JSON_OUTPUT_CAP_BYTES = 65536

_DRIVE_PREFIX_RE = re.compile(r"^[a-zA-Z]:")
_RG_LINE_RE = re.compile(r"^(.*?):(\d+):(.*)$")


def _coerce_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    if value is None or isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def _parse_json_stdout(stdout: str) -> dict[str, Any]:
    text = (stdout or "").strip()
    if not text:
        raise ValueError("Empty sandbox tool output")
    try:
        obj = json.loads(text)
    except ValueError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise ValueError(f"Non-JSON sandbox tool output: {text[:200]}")
        obj = json.loads(text[start : end + 1])
    if not isinstance(obj, dict):
        raise ValueError("Expected JSON object from sandbox tool")
    return obj


def _invoke_hypothesis_callback(
    callback: Callable[..., Any],
    payload: dict[str, Any],
) -> Any:
    """Support both cb(payload_dict) and cb(id=..., status=..., evidence=..., poc_snippet=...)."""
    try:
        sig = inspect.signature(callback)
        params = list(sig.parameters.values())
    except (TypeError, ValueError):
        return callback(payload)

    has_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params)
    param_names = {p.name for p in params}
    if has_var_kw or {"id", "status", "evidence"}.issubset(param_names):
        return callback(**payload)
    if len(params) == 1:
        return callback(payload)
    if len(params) >= 4:
        return callback(
            payload["id"],
            payload["status"],
            payload["evidence"],
            payload["poc_snippet"],
        )
    return callback(payload)


class SandboxToolkit:
    """Executes the 5 atomic agent tools strictly inside the hardened Docker sandbox."""

    def __init__(
        self,
        runner: DockerSandboxRunner,
        target_path: str | pathlib.Path,
        retention_mode: str,
        run_id: str,
        artifacts_path: str | pathlib.Path | None = None,
    ) -> None:
        if retention_mode not in ALLOWED_RETENTION_MODES:
            raise ValueError(
                f"Invalid retention_mode {retention_mode!r}; must be one of {ALLOWED_RETENTION_MODES}."
            )
        self.runner = runner
        self.target_path = pathlib.Path(target_path).resolve()
        self.retention_mode = retention_mode
        self.run_id = str(run_id)
        # Defense-in-depth: never retain host artifacts_path in ephemeral mode.
        if retention_mode == "persistent" and artifacts_path is not None:
            self.artifacts_path: pathlib.Path | None = pathlib.Path(artifacts_path).resolve()
        else:
            self.artifacts_path = None

    def validate_relative_path(self, raw_path: Any, allow_empty: bool = False) -> str:
        """Validate a target-relative POSIX path and reject any path traversal or escape."""
        if raw_path is None:
            raw_path = ""
        if not isinstance(raw_path, str):
            raise ValueError("Path argument must be a string")

        stripped = raw_path.strip()
        if not stripped:
            if allow_empty:
                return ""
            raise ValueError("Path argument must not be empty")

        if "\x00" in stripped or "\n" in stripped or "\r" in stripped:
            raise ValueError("Path contains forbidden control characters")

        if stripped.startswith(("/", "\\", "~")) or _DRIVE_PREFIX_RE.match(stripped):
            raise ValueError(f"Absolute or drive-qualified path rejected: {raw_path!r}")

        normalized = stripped.replace("\\", "/")
        raw_parts = normalized.split("/")
        clean_parts: list[str] = []
        for part in raw_parts:
            if part in ("", "."):
                continue
            if part == "..":
                raise ValueError(f"Path traversal ('..') rejected: {raw_path!r}")
            clean_parts.append(part)

        clean_rel = "/".join(clean_parts)
        if not clean_rel and not allow_empty:
            raise ValueError(f"Path resolves to empty relative path: {raw_path!r}")

        # Host-side containment verification against resolved target_path.
        candidate = (self.target_path / clean_rel).resolve() if clean_rel else self.target_path
        try:
            candidate.relative_to(self.target_path)
        except ValueError as exc:
            raise ValueError(f"Path escapes target root: {raw_path!r}") from exc

        return clean_rel

    @staticmethod
    def _validate_glob(raw_glob: Any, default: str = "") -> str:
        if raw_glob is None:
            return default
        if not isinstance(raw_glob, str):
            raise ValueError("glob must be a string")
        cleaned = raw_glob.strip()
        if not cleaned:
            return default
        if "\x00" in cleaned or "\n" in cleaned or "\r" in cleaned:
            raise ValueError("glob contains forbidden control characters")
        if ".." in cleaned.replace("\\", "/").split("/"):
            raise ValueError(f"Path traversal rejected in glob: {raw_glob!r}")
        if cleaned.startswith("-"):
            raise ValueError(f"Invalid glob starting with '-': {raw_glob!r}")
        return cleaned

    def execute(
        self,
        tool_name: str,
        args: dict[str, Any] | None = None,
        hypothesis_callback: Callable[..., Any] | None = None,
    ) -> dict[str, Any]:
        """Dispatch a single atomic tool call and return a JSON-serializable result dict."""
        if not isinstance(tool_name, str) or tool_name not in TOOL_NAMES:
            return {
                "ok": False,
                "tool": str(tool_name),
                "error": f"Unknown tool {tool_name!r}. Allowed tools: {', '.join(TOOL_NAMES)}",
            }

        if args is None:
            args = {}
        if not isinstance(args, dict):
            return {
                "ok": False,
                "tool": tool_name,
                "error": "Tool args must be a JSON object (dict)",
            }

        try:
            if tool_name == "list_files":
                return self._tool_list_files(args)
            if tool_name == "read_file_slice":
                return self._tool_read_file_slice(args)
            if tool_name == "search_code":
                return self._tool_search_code(args)
            if tool_name == "run_python_sandbox":
                return self._tool_run_python_sandbox(args)
            if tool_name == "update_hypothesis":
                return self._tool_update_hypothesis(args, hypothesis_callback)
        except ValueError as exc:
            return {"ok": False, "tool": tool_name, "error": str(exc)}

        return {"ok": False, "tool": tool_name, "error": "Unhandled tool"}

    def _tool_list_files(self, args: dict[str, Any]) -> dict[str, Any]:
        subdir = self.validate_relative_path(args.get("subdir", ""), allow_empty=True)
        glob_pattern = self._validate_glob(args.get("glob", "*"), default="*")
        max_entries = _coerce_int(args.get("max_entries"), default=100, minimum=1, maximum=MAX_LIST_ENTRIES)

        script = (
            "import fnmatch, json, os, pathlib\n"
            f"SUBDIR = {json.dumps(subdir)}\n"
            f"GLOB = {json.dumps(glob_pattern)}\n"
            f"MAX_ENTRIES = {max_entries}\n"
            "EXCLUDED = {'.git', 'node_modules', '__pycache__', '.venv', 'venv', '.mypy_cache', '.pytest_cache', '.ruff_cache'}\n"
            "root = pathlib.Path('/target').resolve()\n"
            "base = (root / SUBDIR).resolve() if SUBDIR else root\n"
            "try:\n"
            "    base.relative_to(root)\n"
            "except ValueError:\n"
            "    print(json.dumps({'ok': False, 'error': 'Subdir escapes /target'}))\n"
            "    raise SystemExit(0)\n"
            "if not base.exists():\n"
            "    print(json.dumps({'ok': False, 'error': 'Directory not found: ' + (SUBDIR or '/')}))\n"
            "    raise SystemExit(0)\n"
            "entries = []\n"
            "truncated = False\n"
            "if base.is_file():\n"
            "    rel = base.relative_to(root).as_posix()\n"
            "    if fnmatch.fnmatch(base.name, GLOB) or fnmatch.fnmatch(rel, GLOB):\n"
            "        entries.append({'path': rel, 'size': base.stat().st_size, 'type': 'file'})\n"
            "else:\n"
            "    for dirpath, dirnames, filenames in os.walk(base, topdown=True, followlinks=False):\n"
            "        dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDED)\n"
            "        for fname in sorted(filenames):\n"
            "            full = pathlib.Path(dirpath) / fname\n"
            "            rel = full.relative_to(root).as_posix()\n"
            "            rel_sub = full.relative_to(base).as_posix()\n"
            "            if fnmatch.fnmatch(fname, GLOB) or fnmatch.fnmatch(rel_sub, GLOB) or fnmatch.fnmatch(rel, GLOB):\n"
            "                if len(entries) >= MAX_ENTRIES:\n"
            "                    truncated = True\n"
            "                    break\n"
            "                try:\n"
            "                    size = int(full.stat().st_size)\n"
            "                except OSError:\n"
            "                    size = 0\n"
            "                entries.append({'path': rel, 'size': size, 'type': 'file'})\n"
            "        if truncated:\n"
            "            break\n"
            "print(json.dumps({'ok': True, 'entries': entries, 'count': len(entries), 'truncated': truncated}, ensure_ascii=False))\n"
        )

        res = self.runner.run_python(
            code=script,
            target_path=self.target_path,
            retention_mode=self.retention_mode,
            run_id=self.run_id,
            artifacts_path=self.artifacts_path,
            timeout_sec=30,
            max_output_bytes=TOOL_JSON_OUTPUT_CAP_BYTES,
        )
        if not res.ok:
            return {
                "ok": False,
                "tool": "list_files",
                "error": res.error or res.stderr or f"Sandbox exit code {res.exit_code}",
            }

        try:
            payload = _parse_json_stdout(res.stdout)
        except ValueError as exc:
            return {"ok": False, "tool": "list_files", "error": str(exc)}

        payload["tool"] = "list_files"
        payload["subdir"] = subdir
        payload["glob"] = glob_pattern
        return payload

    def _tool_read_file_slice(self, args: dict[str, Any]) -> dict[str, Any]:
        rel_path = self.validate_relative_path(args.get("rel_path"), allow_empty=False)
        start_line = _coerce_int(args.get("start_line"), default=1, minimum=1, maximum=10_000_000)
        requested_end = _coerce_int(
            args.get("end_line"),
            default=start_line + 119,
            minimum=start_line,
            maximum=10_000_000,
        )
        clamped = False
        if (requested_end - start_line + 1) > MAX_SLICE_LINES:
            end_line = start_line + MAX_SLICE_LINES - 1
            clamped = True
        else:
            end_line = requested_end

        script = (
            "import json, pathlib\n"
            f"REL_PATH = {json.dumps(rel_path)}\n"
            f"START_LINE = {start_line}\n"
            f"END_LINE = {end_line}\n"
            "root = pathlib.Path('/target').resolve()\n"
            "target = (root / REL_PATH).resolve()\n"
            "try:\n"
            "    target.relative_to(root)\n"
            "except ValueError:\n"
            "    print(json.dumps({'ok': False, 'error': 'Path escapes /target (symlink blocked)'}))\n"
            "    raise SystemExit(0)\n"
            "if not target.exists():\n"
            "    print(json.dumps({'ok': False, 'error': f'File not found: {REL_PATH}'}))\n"
            "    raise SystemExit(0)\n"
            "if not target.is_file():\n"
            "    print(json.dumps({'ok': False, 'error': f'Not a regular file: {REL_PATH}'}))\n"
            "    raise SystemExit(0)\n"
            "with target.open('rb') as bf:\n"
            "    head = bf.read(4096)\n"
            "if b'\\x00' in head:\n"
            "    print(json.dumps({'ok': False, 'error': f'Binary file cannot be sliced as text: {REL_PATH}'}))\n"
            "    raise SystemExit(0)\n"
            "selected = []\n"
            "total_lines = 0\n"
            "with target.open('r', encoding='utf-8', errors='replace') as fh:\n"
            "    for idx, line in enumerate(fh, start=1):\n"
            "        total_lines = idx\n"
            "        if START_LINE <= idx <= END_LINE:\n"
            "            selected.append(f'{idx}: {line.rstrip(chr(10)).rstrip(chr(13))}')\n"
            "actual_end = min(END_LINE, total_lines) if total_lines >= START_LINE else START_LINE - 1\n"
            "print(json.dumps({\n"
            "    'ok': True,\n"
            "    'rel_path': REL_PATH,\n"
            "    'start_line': START_LINE,\n"
            "    'end_line': actual_end,\n"
            "    'total_lines': total_lines,\n"
            "    'content': '\\n'.join(selected),\n"
            "}, ensure_ascii=False))\n"
        )

        res = self.runner.run_python(
            code=script,
            target_path=self.target_path,
            retention_mode=self.retention_mode,
            run_id=self.run_id,
            artifacts_path=self.artifacts_path,
            timeout_sec=30,
            max_output_bytes=TOOL_JSON_OUTPUT_CAP_BYTES,
        )
        if not res.ok:
            return {
                "ok": False,
                "tool": "read_file_slice",
                "error": res.error or res.stderr or f"Sandbox exit code {res.exit_code}",
            }

        try:
            payload = _parse_json_stdout(res.stdout)
        except ValueError as exc:
            return {"ok": False, "tool": "read_file_slice", "error": str(exc)}

        payload["tool"] = "read_file_slice"
        payload["clamped"] = clamped
        return payload

    def _tool_search_code(self, args: dict[str, Any]) -> dict[str, Any]:
        raw_pattern = args.get("pattern")
        if not isinstance(raw_pattern, str) or not raw_pattern.strip():
            raise ValueError("pattern must be a non-empty string")
        if len(raw_pattern) > 500:
            raise ValueError("pattern exceeds max length of 500 characters")
        if "\x00" in raw_pattern:
            raise ValueError("pattern contains forbidden null byte")

        glob_pattern = self._validate_glob(args.get("glob", ""), default="")
        max_matches = _coerce_int(args.get("max_matches"), default=40, minimum=1, maximum=MAX_SEARCH_MATCHES)

        rg_argv = [
            "rg",
            "--no-heading",
            "--with-filename",
            "--line-number",
            "--color",
            "never",
            "--max-count",
            str(max_matches),
            "--max-columns",
            "300",
            "--max-columns-preview",
            "--glob",
            "!.git/**",
            "--glob",
            "!node_modules/**",
            "--glob",
            "!__pycache__/**",
            "--glob",
            "!.venv/**",
        ]
        if glob_pattern:
            rg_argv.extend(["--glob", glob_pattern])
        rg_argv.extend(["--", raw_pattern, "/target"])

        res = self.runner.run_command(
            argv=rg_argv,
            target_path=self.target_path,
            retention_mode=self.retention_mode,
            run_id=self.run_id,
            artifacts_path=self.artifacts_path,
            timeout_sec=30,
            max_output_bytes=TOOL_JSON_OUTPUT_CAP_BYTES,
        )

        if res.timed_out:
            return {
                "ok": False,
                "tool": "search_code",
                "error": res.error or "search_code timed out",
            }

        # rg exit_code 0 = matches found, 1 = no matches found
        if res.exit_code in (0, 1):
            matches: list[dict[str, Any]] = []
            truncated = False
            for line in (res.stdout or "").splitlines():
                if not line.strip() or line.startswith("...[TRUNCATED"):
                    continue
                m = _RG_LINE_RE.match(line)
                if not m:
                    continue
                raw_file, lineno_str, text = m.group(1), m.group(2), m.group(3)
                rel = raw_file.replace("\\", "/")
                if rel.startswith("/target/"):
                    rel = rel[len("/target/") :]
                elif rel == "/target":
                    rel = self.target_path.name
                if len(matches) >= max_matches:
                    truncated = True
                    break
                matches.append({"path": rel, "line": int(lineno_str), "text": text[:300]})
            return {
                "ok": True,
                "tool": "search_code",
                "engine": "ripgrep",
                "pattern": raw_pattern,
                "glob": glob_pattern,
                "count": len(matches),
                "truncated": truncated,
                "matches": matches,
            }

        # Do not invoke fallback if Docker itself failed before running container.
        if res.exit_code == -1:
            return {
                "ok": False,
                "tool": "search_code",
                "error": res.error or res.stderr or "Docker execution failed",
            }

        stderr_lower = (res.stderr or "").lower()
        if "regex parse error" in stderr_lower and "look-around" not in stderr_lower and "backreference" not in stderr_lower:
            return {
                "ok": False,
                "tool": "search_code",
                "error": f"Invalid regex pattern: {(res.stderr or '').strip()[:300]}",
            }

        # Fallback to Python regex search inside the same hardened Docker sandbox.
        return self._search_code_python_fallback(raw_pattern, glob_pattern, max_matches)

    def _search_code_python_fallback(
        self,
        pattern: str,
        glob_pattern: str,
        max_matches: int,
    ) -> dict[str, Any]:
        script = (
            "import fnmatch, json, os, pathlib, re\n"
            f"PATTERN = {json.dumps(pattern)}\n"
            f"GLOB = {json.dumps(glob_pattern)}\n"
            f"MAX_MATCHES = {max_matches}\n"
            "EXCLUDED = {'.git', 'node_modules', '__pycache__', '.venv', 'venv'}\n"
            "try:\n"
            "    rx = re.compile(PATTERN)\n"
            "except re.error as exc:\n"
            "    print(json.dumps({'ok': False, 'error': f'Invalid regex: {exc}'}))\n"
            "    raise SystemExit(0)\n"
            "root = pathlib.Path('/target').resolve()\n"
            "matches = []\n"
            "truncated = False\n"
            "def scan_file(full, rel):\n"
            "    global truncated\n"
            "    if GLOB and not (fnmatch.fnmatch(full.name, GLOB) or fnmatch.fnmatch(rel, GLOB)):\n"
            "        return\n"
            "    try:\n"
            "        if full.stat().st_size > 4 * 1024 * 1024:\n"
            "            return\n"
            "        with full.open('rb') as bf:\n"
            "            if b'\\x00' in bf.read(2048):\n"
            "                return\n"
            "        with full.open('r', encoding='utf-8', errors='replace') as fh:\n"
            "            for lineno, line in enumerate(fh, start=1):\n"
            "                if rx.search(line):\n"
            "                    if len(matches) >= MAX_MATCHES:\n"
            "                        truncated = True\n"
            "                        return\n"
            "                    matches.append({'path': rel, 'line': lineno, 'text': line.rstrip()[:300]})\n"
            "    except OSError:\n"
            "        return\n"
            "if root.is_file():\n"
            "    scan_file(root, root.name)\n"
            "else:\n"
            "    for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):\n"
            "        dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDED)\n"
            "        for fname in sorted(filenames):\n"
            "            full = pathlib.Path(dirpath) / fname\n"
            "            rel = full.relative_to(root).as_posix()\n"
            "            scan_file(full, rel)\n"
            "            if truncated:\n"
            "                break\n"
            "        if truncated:\n"
            "            break\n"
            "print(json.dumps({'ok': True, 'matches': matches, 'count': len(matches), 'truncated': truncated}, ensure_ascii=False))\n"
        )

        res = self.runner.run_python(
            code=script,
            target_path=self.target_path,
            retention_mode=self.retention_mode,
            run_id=self.run_id,
            artifacts_path=self.artifacts_path,
            timeout_sec=30,
            max_output_bytes=TOOL_JSON_OUTPUT_CAP_BYTES,
        )
        if not res.ok:
            return {
                "ok": False,
                "tool": "search_code",
                "error": res.error or res.stderr or f"Sandbox exit code {res.exit_code}",
            }
        try:
            payload = _parse_json_stdout(res.stdout)
        except ValueError as exc:
            return {"ok": False, "tool": "search_code", "error": str(exc)}

        payload["tool"] = "search_code"
        payload["engine"] = "python_regex"
        payload["pattern"] = pattern
        payload["glob"] = glob_pattern
        return payload

    def _tool_run_python_sandbox(self, args: dict[str, Any]) -> dict[str, Any]:
        code = args.get("code")
        if not isinstance(code, str) or not code.strip():
            raise ValueError("code must be a non-empty string")
        timeout_sec = _coerce_int(
            args.get("timeout_sec"),
            default=60,
            minimum=1,
            maximum=MAX_PYTHON_TIMEOUT_SEC,
        )

        res = self.runner.run_python(
            code=code,
            target_path=self.target_path,
            retention_mode=self.retention_mode,
            run_id=self.run_id,
            artifacts_path=self.artifacts_path,
            timeout_sec=timeout_sec,
        )
        return {
            "ok": res.ok,
            "tool": "run_python_sandbox",
            "exit_code": res.exit_code,
            "stdout": res.stdout,
            "stderr": res.stderr,
            "timed_out": res.timed_out,
            "duration_ms": res.duration_ms,
            "container_name": res.container_name,
            "error": res.error,
        }

    def _tool_update_hypothesis(
        self,
        args: dict[str, Any],
        hypothesis_callback: Callable[..., Any] | None,
    ) -> dict[str, Any]:
        hyp_id = args.get("id")
        if not isinstance(hyp_id, str) or not hyp_id.strip():
            raise ValueError("id must be a non-empty string")

        raw_status = args.get("status")
        if not isinstance(raw_status, str) or not raw_status.strip():
            raise ValueError("status must be a non-empty string")
        status = raw_status.strip().upper()
        if status not in VALID_HYPOTHESIS_STATUSES:
            allowed = ", ".join(sorted(VALID_HYPOTHESIS_STATUSES))
            raise ValueError(f"Invalid hypothesis status {status!r}; allowed: {allowed}")

        evidence = args.get("evidence", "")
        if not isinstance(evidence, str):
            raise ValueError("evidence must be a string")

        poc_snippet = args.get("poc_snippet", "")
        if poc_snippet is None:
            poc_snippet = ""
        if not isinstance(poc_snippet, str):
            raise ValueError("poc_snippet must be a string")

        payload = {
            "id": hyp_id.strip(),
            "status": status,
            "evidence": evidence.strip(),
            "poc_snippet": poc_snippet,
        }

        cb_invoked = False
        cb_result: Any = None
        if callable(hypothesis_callback):
            cb_result = _invoke_hypothesis_callback(hypothesis_callback, payload)
            cb_invoked = True

        return {
            "ok": True,
            "tool": "update_hypothesis",
            "hypothesis": payload,
            "callback_invoked": cb_invoked,
            "callback_result": cb_result,
        }

    def describe_tools_for_prompt(self) -> str:
        """Return a concise bilingual (IT/EN) specification of the 5 atomic tools."""
        return (
            "## Sandbox Tools (Offline Docker `--network none`, `/target` read-only, `/work` tmpfs)\n"
            "Invoca un solo tool per turno usando esattamente questa sintassi:\n"
            '<tool_call name="TOOL_NAME">{"arg": "valore"}</tool_call>\n\n'
            "Tool disponibili / Available tools:\n"
            '1. `list_files`: elenca file in `/target`. Args: `{"subdir": "", "glob": "*", "max_entries": 100}` (no `..`).\n'
            '2. `read_file_slice`: legge una porzione di file (max 200 righe/chiamata). Args: `{"rel_path": "src/app.py", "start_line": 1, "end_line": 120}`.\n'
            '3. `search_code`: cerca regex con ripgrep in `/target`. Args: `{"pattern": "subprocess", "glob": "*.py", "max_matches": 40}`.\n'
            '4. `run_python_sandbox`: esegue Python/DuckDB/Pandas/PoC in container effimero offline (target in `/target`, scratch in `/work`). Args: `{"code": "print(1+1)", "timeout_sec": 60}`.\n'
            '5. `update_hypothesis`: aggiorna stato ipotesi (`OPEN|TESTING|CONFIRMED|REFUTED|BLOCKED|ESCALATE`). Args: `{"id": "H1", "status": "CONFIRMED", "evidence": "...", "poc_snippet": "..."}`.\n\n'
            "Esempio / Example:\n"
            '<tool_call name="read_file_slice">{"rel_path": "main.py", "start_line": 1, "end_line": 80}</tool_call>'
        )


class HostReadOnlyToolkit:
    """
    Pure-stdlib read-only investigation toolkit for non-Docker `host_readonly` mode.

    Strictly bounded by `validate_target_path` and `validate_relative_path`;
    arbitrary Python execution (`run_python_sandbox`) is permanently disabled here.
    """

    def __init__(
        self,
        target_path: str | pathlib.Path,
        retention_mode: str = "ephemeral",
        run_id: str = "",
        artifacts_path: str | pathlib.Path | None = None,
    ) -> None:
        if retention_mode not in ALLOWED_RETENTION_MODES:
            raise ValueError(
                f"Invalid retention_mode {retention_mode!r}; must be one of {ALLOWED_RETENTION_MODES}."
            )
        from ..engine.schemas import validate_target_path

        self.target_path = validate_target_path(str(target_path))
        self.retention_mode = retention_mode
        self.run_id = str(run_id)
        self.artifacts_path = (
            pathlib.Path(artifacts_path).resolve()
            if retention_mode == "persistent" and artifacts_path is not None
            else None
        )
        self._path_helper = SandboxToolkit(
            runner=DockerSandboxRunner(),
            target_path=self.target_path,
            retention_mode=retention_mode,
            run_id=self.run_id,
            artifacts_path=self.artifacts_path,
        )

    def validate_relative_path(self, raw_path: Any, allow_empty: bool = False) -> str:
        """Validate a target-relative POSIX path and reject traversal or escapes."""
        return self._path_helper.validate_relative_path(raw_path, allow_empty=allow_empty)

    def execute(
        self,
        tool_name: str,
        args: dict[str, Any] | None = None,
        hypothesis_callback: Callable[..., Any] | None = None,
    ) -> dict[str, Any]:
        """Dispatch a read-only host tool call with arbitrary Python execution blocked."""
        if tool_name == "run_python_sandbox":
            return {
                "ok": False,
                "tool": "run_python_sandbox",
                "disabled": True,
                "error": (
                    "Arbitrary Python execution (run_python_sandbox) is disabled in "
                    "host_readonly safe mode outside Docker."
                ),
            }

        if not isinstance(tool_name, str) or tool_name not in HOST_READONLY_TOOL_NAMES:
            return {
                "ok": False,
                "tool": str(tool_name),
                "error": (
                    f"Unknown or disallowed tool {tool_name!r} in host_readonly mode. "
                    f"Allowed tools: {', '.join(HOST_READONLY_TOOL_NAMES)}"
                ),
            }

        if args is None:
            args = {}
        if not isinstance(args, dict):
            return {
                "ok": False,
                "tool": tool_name,
                "error": "Tool args must be a JSON object (dict)",
            }

        try:
            if tool_name == "list_files":
                return self._tool_list_files(args)
            if tool_name == "read_file_slice":
                return self._tool_read_file_slice(args)
            if tool_name == "search_code":
                return self._tool_search_code(args)
            if tool_name == "update_hypothesis":
                return self._path_helper._tool_update_hypothesis(args, hypothesis_callback)
            if tool_name == "code_audit":
                return self._tool_code_audit(args)
        except ValueError as exc:
            return {"ok": False, "tool": tool_name, "error": str(exc)}

        return {"ok": False, "tool": tool_name, "error": "Unhandled tool"}

    def _tool_list_files(self, args: dict[str, Any]) -> dict[str, Any]:
        subdir = self.validate_relative_path(args.get("subdir", ""), allow_empty=True)
        glob_pattern = SandboxToolkit._validate_glob(args.get("glob", "*"), default="*")
        max_entries = _coerce_int(args.get("max_entries"), default=100, minimum=1, maximum=MAX_LIST_ENTRIES)

        base = (self.target_path / subdir).resolve() if subdir else self.target_path
        try:
            base.relative_to(self.target_path)
        except ValueError:
            return {"ok": False, "tool": "list_files", "error": "Subdir escapes target root"}
        if not base.exists():
            return {"ok": False, "tool": "list_files", "error": f"Directory not found: {subdir or '/'}"}

        entries: list[dict[str, Any]] = []
        truncated = False
        if base.is_file():
            rel = base.relative_to(self.target_path).as_posix()
            if fnmatch.fnmatch(base.name, glob_pattern) or fnmatch.fnmatch(rel, glob_pattern):
                entries.append({"path": rel, "size": int(base.stat().st_size), "type": "file"})
        else:
            for dirpath, dirnames, filenames in os.walk(base, topdown=True, followlinks=False):
                dirnames[:] = sorted(d for d in dirnames if d not in _EXCLUDED_HOST_DIRS)
                for fname in sorted(filenames):
                    full = (pathlib.Path(dirpath) / fname).resolve()
                    try:
                        rel = full.relative_to(self.target_path).as_posix()
                        rel_sub = full.relative_to(base).as_posix()
                    except ValueError:
                        continue
                    if (
                        fnmatch.fnmatch(fname, glob_pattern)
                        or fnmatch.fnmatch(rel_sub, glob_pattern)
                        or fnmatch.fnmatch(rel, glob_pattern)
                    ):
                        if len(entries) >= max_entries:
                            truncated = True
                            break
                        try:
                            size = int(full.stat().st_size)
                        except OSError:
                            size = 0
                        entries.append({"path": rel, "size": size, "type": "file"})
                if truncated:
                    break

        return {
            "ok": True,
            "tool": "list_files",
            "mode": "host_readonly",
            "subdir": subdir,
            "glob": glob_pattern,
            "entries": entries,
            "count": len(entries),
            "truncated": truncated,
        }

    def _tool_read_file_slice(self, args: dict[str, Any]) -> dict[str, Any]:
        rel_path = self.validate_relative_path(args.get("rel_path"), allow_empty=False)
        start_line = _coerce_int(args.get("start_line"), default=1, minimum=1, maximum=10_000_000)
        requested_end = _coerce_int(
            args.get("end_line"),
            default=start_line + 119,
            minimum=start_line,
            maximum=10_000_000,
        )
        clamped = False
        if (requested_end - start_line + 1) > MAX_SLICE_LINES:
            end_line = start_line + MAX_SLICE_LINES - 1
            clamped = True
        else:
            end_line = requested_end

        target = (self.target_path / rel_path).resolve()
        try:
            target.relative_to(self.target_path)
        except ValueError:
            return {"ok": False, "tool": "read_file_slice", "error": "Path escapes target root (symlink blocked)"}
        if not target.exists():
            return {"ok": False, "tool": "read_file_slice", "error": f"File not found: {rel_path}"}
        if not target.is_file():
            return {"ok": False, "tool": "read_file_slice", "error": f"Not a regular file: {rel_path}"}

        try:
            with target.open("rb") as bf:
                head = bf.read(4096)
            if b"\x00" in head:
                return {
                    "ok": False,
                    "tool": "read_file_slice",
                    "error": f"Binary file cannot be sliced as text: {rel_path}",
                }
            selected: list[str] = []
            total_lines = 0
            with target.open("r", encoding="utf-8", errors="replace") as fh:
                for idx, line in enumerate(fh, start=1):
                    total_lines = idx
                    if start_line <= idx <= end_line:
                        selected.append(f"{idx}: {line.rstrip(chr(10)).rstrip(chr(13))}")
        except OSError as exc:
            return {"ok": False, "tool": "read_file_slice", "error": str(exc)}

        actual_end = min(end_line, total_lines) if total_lines >= start_line else start_line - 1
        return {
            "ok": True,
            "tool": "read_file_slice",
            "mode": "host_readonly",
            "rel_path": rel_path,
            "start_line": start_line,
            "end_line": actual_end,
            "total_lines": total_lines,
            "content": "\n".join(selected),
            "clamped": clamped,
        }

    def _tool_search_code(self, args: dict[str, Any]) -> dict[str, Any]:
        raw_pattern = args.get("pattern")
        if not isinstance(raw_pattern, str) or not raw_pattern.strip():
            raise ValueError("pattern must be a non-empty string")
        if len(raw_pattern) > 500:
            raise ValueError("pattern exceeds max length of 500 characters")
        if "\x00" in raw_pattern:
            raise ValueError("pattern contains forbidden null byte")

        glob_pattern = SandboxToolkit._validate_glob(args.get("glob", ""), default="")
        max_matches = _coerce_int(args.get("max_matches"), default=40, minimum=1, maximum=MAX_SEARCH_MATCHES)

        try:
            rx = re.compile(raw_pattern)
        except re.error as exc:
            return {"ok": False, "tool": "search_code", "error": f"Invalid regex: {exc}"}

        matches: list[dict[str, Any]] = []
        truncated = False

        def scan_file(full: pathlib.Path, rel: str) -> None:
            nonlocal truncated
            if glob_pattern and not (
                fnmatch.fnmatch(full.name, glob_pattern) or fnmatch.fnmatch(rel, glob_pattern)
            ):
                return
            try:
                if full.stat().st_size > 4 * 1024 * 1024:
                    return
                with full.open("rb") as bf:
                    if b"\x00" in bf.read(2048):
                        return
                with full.open("r", encoding="utf-8", errors="replace") as fh:
                    for lineno, line in enumerate(fh, start=1):
                        if rx.search(line):
                            if len(matches) >= max_matches:
                                truncated = True
                                return
                            matches.append({"path": rel, "line": lineno, "text": line.rstrip()[:300]})
            except OSError:
                return

        if self.target_path.is_file():
            scan_file(self.target_path, self.target_path.name)
        else:
            for dirpath, dirnames, filenames in os.walk(self.target_path, topdown=True, followlinks=False):
                dirnames[:] = sorted(d for d in dirnames if d not in _EXCLUDED_HOST_DIRS)
                for fname in sorted(filenames):
                    full = (pathlib.Path(dirpath) / fname).resolve()
                    try:
                        rel = full.relative_to(self.target_path).as_posix()
                    except ValueError:
                        continue
                    scan_file(full, rel)
                    if truncated:
                        break
                if truncated:
                    break

        return {
            "ok": True,
            "tool": "search_code",
            "mode": "host_readonly",
            "engine": "host_python_regex",
            "pattern": raw_pattern,
            "glob": glob_pattern,
            "matches": matches,
            "count": len(matches),
            "truncated": truncated,
        }

    def _tool_code_audit(self, args: dict[str, Any]) -> dict[str, Any]:
        subdir = self.validate_relative_path(args.get("subdir", ""), allow_empty=True)
        audit_target = (self.target_path / subdir).resolve() if subdir else self.target_path
        try:
            audit_target.relative_to(self.target_path)
        except ValueError:
            return {"ok": False, "tool": "code_audit", "error": "Subdir escapes target root"}

        from ..engine.code_audit import run_code_audit

        res = run_code_audit(
            str(audit_target),
            check_secrets=bool(args.get("check_secrets", True)),
            check_slopsquatting=bool(args.get("check_slopsquatting", True)),
            use_gitleaks=bool(args.get("use_gitleaks", True)),
        )
        return {
            "ok": bool(res.get("ok", True)),
            "tool": "code_audit",
            "mode": "host_readonly",
            **res,
        }

    def describe_tools_for_prompt(self) -> str:
        """Return a concise specification of the host read-only tools (no arbitrary code exec)."""
        return (
            "## Host Read-Only Safe Mode Tools (No Docker; strictly bounded to target directory; NO code execution)\n"
            'Invoke one tool per turn: <tool_call name="TOOL_NAME">{"arg": "value"}</tool_call>\n\n'
            "Available tools:\n"
            '1. `list_files`: {"subdir": "", "glob": "*", "max_entries": 100}\n'
            '2. `read_file_slice`: {"rel_path": "app.py", "start_line": 1, "end_line": 120}\n'
            '3. `search_code`: {"pattern": "regex", "glob": "*.py", "max_matches": 40}\n'
            '4. `code_audit`: {"subdir": "", "check_secrets": true, "check_slopsquatting": true}\n'
            '5. `update_hypothesis`: {"id": "H1", "status": "CONFIRMED|REFUTED|BLOCKED|ESCALATE|TESTING", '
            '"evidence": "...", "poc_snippet": ""}\n'
            "Note: `run_python_sandbox` is disabled outside Docker.\n"
        )

