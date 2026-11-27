"""
Unit and optional live-Docker isolation tests for `workspaces/orchestrator/sandbox`.

Pure Python stdlib (`unittest` + `unittest.mock`).
Live Docker tests auto-skip gracefully if Docker daemon or `local-llm-sandbox:offline`
image is not available on the machine.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

_SANDBOX_DIR = pathlib.Path(__file__).resolve().parent
_ORCHESTRATOR_DIR = _SANDBOX_DIR.parent

if __package__:
    from . import (
        DockerSandboxRunner,
        SANDBOX_IMAGE,
        SandboxResult,
        SandboxToolkit,
        TOOL_NAMES,
        run_deterministic_scout,
    )
    from .sandbox_runner import ORCHESTRATOR_LABEL, _truncate_output
    from .scout import MAX_COMPACT_SUMMARY_CHARS, _SANDBOX_SCOUT_SCRIPT, build_compact_summary
else:
    if str(_ORCHESTRATOR_DIR) not in sys.path:
        sys.path.insert(0, str(_ORCHESTRATOR_DIR))
    from sandbox import (
        DockerSandboxRunner,
        SANDBOX_IMAGE,
        SandboxResult,
        SandboxToolkit,
        TOOL_NAMES,
        run_deterministic_scout,
    )
    from sandbox.sandbox_runner import ORCHESTRATOR_LABEL, _truncate_output
    from sandbox.scout import MAX_COMPACT_SUMMARY_CHARS, _SANDBOX_SCOUT_SCRIPT, build_compact_summary


def _find_flag_values(cmd: list[str], flag: str) -> list[str]:
    values: list[str] = []
    for idx, token in enumerate(cmd[:-1]):
        if token == flag:
            values.append(cmd[idx + 1])
    return values


class TestDockerSandboxRunnerUnit(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.target = pathlib.Path(self.tmpdir.name) / "repo"
        self.target.mkdir()
        (self.target / "main.py").write_text("print('hello')\n", encoding="utf-8")
        self.artifacts = pathlib.Path(self.tmpdir.name) / "vault_artifacts"
        self.runner = DockerSandboxRunner()

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_ephemeral_command_enforces_hardened_flags_and_zero_rw_mounts(self) -> None:
        cmd, cname = self.runner.build_docker_run_cmd(
            argv=["python3", "-I", "-"],
            target_path=self.target,
            retention_mode="ephemeral",
            run_id="run_01",
            artifacts_path=self.artifacts,  # MUST be ignored in ephemeral mode
            use_stdin=True,
        )

        self.assertTrue(cname.startswith("localllm-run_01-"))
        self.assertEqual(cmd[:3], ["docker", "run", "--rm"])
        self.assertIn("-i", cmd)
        self.assertIn("--read-only", cmd)
        self.assertIn("none", _find_flag_values(cmd, "--network"))
        self.assertIn("ALL", _find_flag_values(cmd, "--cap-drop"))
        self.assertIn("no-new-privileges", _find_flag_values(cmd, "--security-opt"))
        self.assertIn("128", _find_flag_values(cmd, "--pids-limit"))
        self.assertIn("1g", _find_flag_values(cmd, "--memory"))
        self.assertIn("4", _find_flag_values(cmd, "--cpus"))
        self.assertIn("/work:rw,nosuid,size=512m", _find_flag_values(cmd, "--tmpfs"))

        labels = _find_flag_values(cmd, "--label")
        self.assertIn(ORCHESTRATOR_LABEL, labels)
        self.assertIn("run_id=run_01", labels)
        self.assertIn("retention=ephemeral", labels)

        mounts = _find_flag_values(cmd, "-v")
        self.assertEqual(len(mounts), 1)
        self.assertEqual(mounts[0], f"{self.target.resolve()}:/target:ro")
        self.assertFalse(any(m.endswith(":rw") for m in mounts))
        self.assertFalse(self.artifacts.exists())

        img_idx = cmd.index(SANDBOX_IMAGE)
        self.assertEqual(cmd[img_idx + 1 :], ["python3", "-I", "-"])

    def test_persistent_command_mounts_artifacts_rw(self) -> None:
        cmd, _ = self.runner.build_docker_run_cmd(
            argv=["rg", "--version"],
            target_path=self.target,
            retention_mode="persistent",
            run_id="run_02",
            artifacts_path=self.artifacts,
            use_stdin=False,
        )

        self.assertNotIn("-i", cmd)
        mounts = _find_flag_values(cmd, "-v")
        self.assertEqual(len(mounts), 2)
        self.assertIn(f"{self.target.resolve()}:/target:ro", mounts)
        self.assertIn(f"{self.artifacts.resolve()}:/artifacts:rw", mounts)
        labels = _find_flag_values(cmd, "--label")
        self.assertIn("retention=persistent", labels)

    def test_invalid_retention_mode_raises(self) -> None:
        with self.assertRaises(ValueError):
            self.runner.build_docker_run_cmd(
                argv=["echo", "hi"],
                target_path=self.target,
                retention_mode="ram_only",
                run_id="bad",
            )

    def test_run_command_always_calls_docker_rm_f_on_success_and_timeout(self) -> None:
        # 1. Success path
        fake_ok = subprocess.CompletedProcess(args=["docker"], returncode=0, stdout=b"ok\n", stderr=b"")
        fake_rm = subprocess.CompletedProcess(args=["docker", "rm", "-f"], returncode=0, stdout=b"", stderr=b"")
        with mock.patch("subprocess.run", side_effect=[fake_ok, fake_rm]) as mock_run:
            res = self.runner.run_command(
                argv=["echo", "ok"],
                target_path=self.target,
                retention_mode="ephemeral",
                run_id="r_ok",
                artifacts_path=self.artifacts,
            )

        self.assertTrue(res.ok)
        self.assertEqual(res.exit_code, 0)
        self.assertFalse(res.timed_out)
        self.assertFalse(self.artifacts.exists())
        self.assertEqual(mock_run.call_count, 2)
        rm_argv = mock_run.call_args_list[1].args[0]
        self.assertEqual(rm_argv[:3], ["docker", "rm", "-f"])
        self.assertEqual(rm_argv[3], res.container_name)

        # 2. Timeout path
        timeout_exc = subprocess.TimeoutExpired(cmd=["docker"], timeout=5, output=b"partial", stderr=b"err")
        with mock.patch("subprocess.run", side_effect=[timeout_exc, fake_rm]) as mock_run:
            res_to = self.runner.run_command(
                argv=["sleep", "99"],
                target_path=self.target,
                retention_mode="ephemeral",
                run_id="r_timeout",
                timeout_sec=5,
            )

        self.assertFalse(res_to.ok)
        self.assertTrue(res_to.timed_out)
        self.assertEqual(res_to.stdout, "partial")
        self.assertEqual(mock_run.call_count, 2)
        rm_argv = mock_run.call_args_list[1].args[0]
        self.assertEqual(rm_argv, ["docker", "rm", "-f", res_to.container_name])

    def test_run_python_passes_isolated_stdin_flags(self) -> None:
        fake_ok = subprocess.CompletedProcess(args=["docker"], returncode=0, stdout=b"42\n", stderr=b"")
        fake_rm = subprocess.CompletedProcess(args=["docker", "rm", "-f"], returncode=0, stdout=b"", stderr=b"")
        with mock.patch("subprocess.run", side_effect=[fake_ok, fake_rm]) as mock_run:
            res = self.runner.run_python(
                code="print(42)",
                target_path=self.target,
                retention_mode="ephemeral",
                run_id="py_test",
            )

        self.assertTrue(res.ok)
        self.assertEqual(res.stdout, "42\n")
        run_call = mock_run.call_args_list[0]
        cmd = run_call.args[0]
        self.assertIn("-i", cmd)
        self.assertEqual(cmd[-3:], ["python3", "-I", "-"])
        self.assertEqual(run_call.kwargs["input"], b"print(42)")
        self.assertFalse(run_call.kwargs["shell"])

    def test_truncate_output_respects_byte_budget_and_marks_truncated(self) -> None:
        big = ("ciao mondo èàìòù " * 1000).encode("utf-8")
        out = _truncate_output(big, max_bytes=256)
        self.assertIn("[TRUNCATED", out)
        self.assertLessEqual(len(out.encode("utf-8")), 256)

        small = _truncate_output(b"x" * 100, max_bytes=20)
        self.assertIn("[TRUNCATED]", small)
        self.assertLessEqual(len(small.encode("utf-8")), 20)

    def test_cleanup_run_containers_filters_by_labels(self) -> None:
        ps_proc = subprocess.CompletedProcess(args=[], returncode=0, stdout="cid1\ncid2\n", stderr="")
        rm_proc = subprocess.CompletedProcess(args=[], returncode=0, stdout="cid1\ncid2\n", stderr="")
        with (
            mock.patch("shutil.which", return_value="docker"),
            mock.patch("subprocess.run", side_effect=[ps_proc, rm_proc]) as mock_run,
        ):
            removed = self.runner.cleanup_run_containers(run_id="run_99")

        self.assertEqual(removed, 2)
        ps_cmd = mock_run.call_args_list[0].args[0]
        self.assertIn(f"label={ORCHESTRATOR_LABEL}", ps_cmd)
        self.assertIn("label=run_id=run_99", ps_cmd)
        rm_cmd = mock_run.call_args_list[1].args[0]
        self.assertEqual(rm_cmd, ["docker", "rm", "-f", "cid1", "cid2"])


class TestSandboxToolkitUnit(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.target = pathlib.Path(self.tmpdir.name) / "proj"
        self.target.mkdir()
        (self.target / "app.py").write_text("line1\nline2\nline3\n", encoding="utf-8")
        self.mock_runner = mock.MagicMock(spec=DockerSandboxRunner)
        self.toolkit = SandboxToolkit(
            runner=self.mock_runner,
            target_path=self.target,
            retention_mode="ephemeral",
            run_id="tk_run",
            artifacts_path=pathlib.Path(self.tmpdir.name) / "ignored_artifacts",
        )

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_ephemeral_toolkit_drops_artifacts_path(self) -> None:
        self.assertIsNone(self.toolkit.artifacts_path)

    def test_path_traversal_rejected_without_calling_runner(self) -> None:
        bad_paths = [
            "..",
            "../secret.txt",
            "src/../../etc/passwd",
            "..\\windows\\system.ini",
            "/etc/passwd",
            "\\Windows\\System32",
            "C:\\Windows\\win.ini",
            "~/secrets",
            "foo\x00bar.py",
        ]
        for bad in bad_paths:
            with self.assertRaises(ValueError, msg=f"Should reject {bad!r}"):
                self.toolkit.validate_relative_path(bad)
            res_slice = self.toolkit.execute("read_file_slice", {"rel_path": bad})
            self.assertFalse(res_slice["ok"], msg=f"read_file_slice should reject {bad!r}")
            res_list = self.toolkit.execute("list_files", {"subdir": bad})
            self.assertFalse(res_list["ok"], msg=f"list_files should reject {bad!r}")

        self.mock_runner.run_python.assert_not_called()
        self.mock_runner.run_command.assert_not_called()

    def test_read_file_slice_clamps_to_200_lines_and_compiles_embedded_script(self) -> None:
        captured_code: list[str] = []

        def fake_run_python(**kwargs):
            captured_code.append(kwargs["code"])
            compile(kwargs["code"], "<read_file_slice>", "exec")
            return SandboxResult(
                ok=True,
                exit_code=0,
                stdout=json.dumps(
                    {
                        "ok": True,
                        "rel_path": "app.py",
                        "start_line": 10,
                        "end_line": 209,
                        "total_lines": 500,
                        "content": "10: hello",
                    }
                ),
                stderr="",
                timed_out=False,
                duration_ms=12,
                container_name="c1",
            )

        self.mock_runner.run_python.side_effect = fake_run_python
        out = self.toolkit.execute(
            "read_file_slice",
            {"rel_path": "app.py", "start_line": 10, "end_line": 999},
        )
        self.assertTrue(out["ok"])
        self.assertTrue(out["clamped"])
        self.assertIn("END_LINE = 209", captured_code[0])

    def test_list_files_compiles_embedded_script(self) -> None:
        def fake_run_python(**kwargs):
            compile(kwargs["code"], "<list_files>", "exec")
            return SandboxResult(
                ok=True,
                exit_code=0,
                stdout=json.dumps({"ok": True, "entries": [{"path": "app.py", "size": 18, "type": "file"}], "count": 1, "truncated": False}),
                stderr="",
                timed_out=False,
                duration_ms=8,
                container_name="c2",
            )

        self.mock_runner.run_python.side_effect = fake_run_python
        out = self.toolkit.execute("list_files", {"subdir": "", "glob": "*.py", "max_entries": 50})
        self.assertTrue(out["ok"])
        self.assertEqual(out["count"], 1)

    def test_search_code_ripgrep_and_python_fallback(self) -> None:
        # 1. rg finds matches (exit_code 0)
        self.mock_runner.run_command.return_value = SandboxResult(
            ok=True,
            exit_code=0,
            stdout="/target/app.py:2:line2\n",
            stderr="",
            timed_out=False,
            duration_ms=5,
            container_name="c3",
        )
        res = self.toolkit.execute("search_code", {"pattern": "line2", "glob": "*.py", "max_matches": 10})
        self.assertTrue(res["ok"])
        self.assertEqual(res["engine"], "ripgrep")
        self.assertEqual(res["matches"], [{"path": "app.py", "line": 2, "text": "line2"}])

        # 2. rg returns exit_code 1 (no matches — still ok=True)
        self.mock_runner.run_command.return_value = SandboxResult(
            ok=False,
            exit_code=1,
            stdout="",
            stderr="",
            timed_out=False,
            duration_ms=4,
            container_name="c4",
        )
        res_empty = self.toolkit.execute("search_code", {"pattern": "not_found"})
        self.assertTrue(res_empty["ok"])
        self.assertEqual(res_empty["count"], 0)

        # 3. rg unsupported lookahead (exit_code 2) -> Python regex fallback inside sandbox
        self.mock_runner.run_command.return_value = SandboxResult(
            ok=False,
            exit_code=2,
            stdout="",
            stderr="regex parse error: look-around is not supported",
            timed_out=False,
            duration_ms=4,
            container_name="c5",
        )

        def fake_py_fallback(**kwargs):
            compile(kwargs["code"], "<search_code_fallback>", "exec")
            return SandboxResult(
                ok=True,
                exit_code=0,
                stdout=json.dumps({"ok": True, "matches": [{"path": "app.py", "line": 1, "text": "line1"}], "count": 1, "truncated": False}),
                stderr="",
                timed_out=False,
                duration_ms=9,
                container_name="c6",
            )

        self.mock_runner.run_python.side_effect = fake_py_fallback
        res_fb = self.toolkit.execute("search_code", {"pattern": "line(?=1)"})
        self.assertTrue(res_fb["ok"])
        self.assertEqual(res_fb["engine"], "python_regex")
        self.assertEqual(res_fb["count"], 1)

    def test_update_hypothesis_supports_dict_and_kwargs_callbacks(self) -> None:
        recorded_dict: list[dict] = []
        res1 = self.toolkit.execute(
            "update_hypothesis",
            {"id": "H1", "status": "confirmed", "evidence": "PoC exit 0", "poc_snippet": "assert True"},
            hypothesis_callback=lambda item: recorded_dict.append(item) or "saved",
        )
        self.assertTrue(res1["ok"])
        self.assertTrue(res1["callback_invoked"])
        self.assertEqual(recorded_dict[0]["status"], "CONFIRMED")

        recorded_kw: list[tuple] = []

        def kw_cb(id, status, evidence, poc_snippet=""):
            recorded_kw.append((id, status, evidence, poc_snippet))
            return True

        res2 = self.toolkit.execute(
            "update_hypothesis",
            {"id": "H2", "status": "REFUTED", "evidence": "Not reproducible"},
            hypothesis_callback=kw_cb,
        )
        self.assertTrue(res2["ok"])
        self.assertEqual(recorded_kw[0], ("H2", "REFUTED", "Not reproducible", ""))

        res_bad = self.toolkit.execute(
            "update_hypothesis",
            {"id": "H3", "status": "INVALID_STATE", "evidence": "..."},
        )
        self.assertFalse(res_bad["ok"])

    def test_run_python_sandbox_tool_dispatch_and_timeout_clamping(self) -> None:
        self.mock_runner.run_python.return_value = SandboxResult(
            ok=True,
            exit_code=0,
            stdout="poc_verified\n",
            stderr="",
            timed_out=False,
            duration_ms=25,
            container_name="localllm-tk_run-abcd1234",
        )
        res = self.toolkit.execute("run_python_sandbox", {"code": "print('poc_verified')", "timeout_sec": 9999})
        self.assertTrue(res["ok"])
        self.assertEqual(res["exit_code"], 0)
        self.assertEqual(res["stdout"], "poc_verified\n")
        call_kwargs = self.mock_runner.run_python.call_args.kwargs
        self.assertEqual(call_kwargs["retention_mode"], "ephemeral")
        self.assertIsNone(call_kwargs["artifacts_path"])
        self.assertEqual(call_kwargs["timeout_sec"], 120)

    def test_describe_tools_for_prompt_lists_all_5_tools_and_xml_syntax(self) -> None:
        spec = self.toolkit.describe_tools_for_prompt()
        for name in TOOL_NAMES:
            self.assertIn(name, spec)
        self.assertIn('<tool_call name="', spec)
        self.assertIn("</tool_call>", spec)


class TestDeterministicScoutUnit(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.target = pathlib.Path(self.tmpdir.name) / "sample_project"
        self.target.mkdir()
        (self.target / "main.py").write_text(
            "import subprocess\n\nclass Engine:\n    pass\n\ndef run_cmd(x):\n    return subprocess.run(x)\n",
            encoding="utf-8",
        )
        (self.target / "data.csv").write_text("id,amount\n1,10.5\n2,20.0\n", encoding="utf-8")
        (self.target / "config.json").write_text('{"env": "offline", "retries": 3}\n', encoding="utf-8")
        db_path = self.target / "records.sqlite"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute("INSERT INTO items (name) VALUES ('alpha'), ('beta')")
        conn.commit()
        conn.close()

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_embedded_scout_script_compiles_cleanly(self) -> None:
        compile(_SANDBOX_SCOUT_SCRIPT, "<sandbox_scout_script>", "exec")

    def test_scout_docker_sandbox_success_path(self) -> None:
        mock_runner = mock.MagicMock(spec=DockerSandboxRunner)
        mock_runner.image = SANDBOX_IMAGE
        mock_runner.is_docker_available.return_value = (True, "Docker daemon reachable")
        mock_runner.is_image_available.return_value = True
        fake_json = {
            "mode": "docker_sandbox",
            "targetName": "target",
            "fileCount": 2,
            "totalBytes": 120,
            "languages": {".py": 1, ".csv": 1},
            "files": [{"path": "main.py", "size": 90, "lines": 7}],
            "entrypoints": ["main.py"],
            "pythonAst": [{"path": "main.py", "lines": 7, "classes": ["Engine"], "functions": ["run_cmd"], "imports": ["subprocess"], "riskySinks": ["subprocess.run"], "parseError": None}],
            "dataSchemas": [{"path": "data.csv", "kind": "csv", "columns": ["id", "amount"], "rowCount": 2}],
            "staticFindings": {
                "ruff": [{"file": "main.py", "line": 1, "code": "F401", "message": "unused"}],
                "bandit": [{"file": "main.py", "line": 7, "test_id": "B603", "severity": "LOW", "confidence": "HIGH", "issue_text": "subprocess call"}],
            },
        }
        mock_runner.run_python.return_value = SandboxResult(
            ok=True,
            exit_code=0,
            stdout=json.dumps(fake_json),
            stderr="",
            timed_out=False,
            duration_ms=110,
            container_name="localllm-scout_ok-12345678",
        )

        report = run_deterministic_scout(
            runner=mock_runner,
            target_path=self.target,
            retention_mode="ephemeral",
            run_id="scout_ok",
        )
        self.assertEqual(report["mode"], "docker_sandbox")
        self.assertEqual(report["targetName"], "sample_project")
        self.assertEqual(len(report["staticFindings"]["ruff"]), 1)
        self.assertEqual(len(report["staticFindings"]["bandit"]), 1)
        self.assertLessEqual(len(report["compactSummary"]), MAX_COMPACT_SUMMARY_CHARS)
        self.assertIn("sample_project", report["compactSummary"])

    def test_scout_fails_closed_when_docker_unavailable_and_fallback_false(self) -> None:
        mock_runner = mock.MagicMock(spec=DockerSandboxRunner)
        mock_runner.is_docker_available.return_value = (False, "daemon stopped")
        with self.assertRaises(RuntimeError):
            run_deterministic_scout(
                runner=mock_runner,
                target_path=self.target,
                retention_mode="ephemeral",
                run_id="scout_fail",
                allow_host_readonly_fallback=False,
            )

    def test_scout_host_readonly_fallback_scans_without_writes(self) -> None:
        mock_runner = mock.MagicMock(spec=DockerSandboxRunner)
        mock_runner.is_docker_available.return_value = (False, "daemon stopped")
        before_files = sorted(p.relative_to(self.target).as_posix() for p in self.target.rglob("*"))

        report = run_deterministic_scout(
            runner=mock_runner,
            target_path=self.target,
            retention_mode="ephemeral",
            run_id="scout_fb",
            allow_host_readonly_fallback=True,
        )

        after_files = sorted(p.relative_to(self.target).as_posix() for p in self.target.rglob("*"))
        self.assertEqual(before_files, after_files)
        self.assertEqual(report["mode"], "host_readonly_fallback")
        self.assertEqual(report["targetName"], "sample_project")
        self.assertEqual(report["fileCount"], 4)
        self.assertIn("main.py", report["entrypoints"])
        self.assertEqual(len(report["pythonAst"]), 1)
        self.assertIn("Engine", report["pythonAst"][0]["classes"])
        self.assertIn("run_cmd", report["pythonAst"][0]["functions"])
        self.assertIn("subprocess.run", report["pythonAst"][0]["riskySinks"])
        kinds = {d["kind"] for d in report["dataSchemas"]}
        self.assertEqual(kinds, {"csv", "json", "sqlite"})
        self.assertLessEqual(len(report["compactSummary"]), MAX_COMPACT_SUMMARY_CHARS)

    def test_compact_summary_enforces_hard_4800_char_cap(self) -> None:
        huge_report = {
            "mode": "docker_sandbox",
            "targetName": "mega_repo",
            "fileCount": 500,
            "totalBytes": 10_000_000,
            "languages": {".py": 400, ".csv": 100},
            "entrypoints": [f"dir_{i}/main.py" for i in range(40)],
            "pythonAst": [
                {
                    "path": f"pkg/mod_{i}.py",
                    "lines": 500,
                    "classes": [f"ClassLongName_{j}" for j in range(10)],
                    "functions": [f"function_long_name_{j}" for j in range(15)],
                    "riskySinks": ["subprocess.run", "eval", "exec", "pickle.loads", "os.system", "open"],
                }
                for i in range(50)
            ],
            "dataSchemas": [
                {
                    "path": f"data/table_{i}.csv",
                    "kind": "csv",
                    "rowCount": 100000,
                    "columns": [f"column_name_{j}" for j in range(25)],
                }
                for i in range(20)
            ],
            "staticFindings": {
                "ruff": [{"file": f"pkg/mod_{i}.py", "line": i, "code": "F401", "message": "unused " * 30} for i in range(30)],
                "bandit": [{"file": f"pkg/mod_{i}.py", "line": i, "test_id": "B602", "severity": "HIGH", "issue_text": "shell=True " * 30} for i in range(30)],
            },
        }
        summary = build_compact_summary(huge_report, max_chars=MAX_COMPACT_SUMMARY_CHARS)
        self.assertLessEqual(len(summary), MAX_COMPACT_SUMMARY_CHARS)


class TestSandboxPackagingArtifacts(unittest.TestCase):
    def test_dockerfile_contains_required_offline_packages_and_nonroot_user(self) -> None:
        dockerfile = (_SANDBOX_DIR / "Dockerfile.sandbox").read_text(encoding="utf-8")
        for token in (
            "FROM python:3.12-slim",
            "ripgrep",
            "duckdb",
            "pandas",
            "pyarrow",
            "pymupdf",
            "openpyxl",
            "ruff",
            "bandit",
            "pytest",
            "sandboxuser",
            "USER sandboxuser",
            "WORKDIR /work",
        ):
            self.assertIn(token, dockerfile)

    def test_build_sandbox_ps1_ast_parses_with_zero_errors(self) -> None:
        pwsh = shutil.which("pwsh") or shutil.which("powershell")
        if not pwsh:
            self.skipTest("PowerShell not available")
        ps1_path = str((_SANDBOX_DIR / "build-sandbox.ps1").resolve())
        cmd = [
            pwsh,
            "-NoProfile",
            "-Command",
            (
                "$tokens = $null; $errors = $null; "
                f"[void][System.Management.Automation.Language.Parser]::ParseFile('{ps1_path}', [ref]$tokens, [ref]$errors); "
                "if ($errors -and $errors.Count -gt 0) { $errors | ForEach-Object { Write-Error $_.Message }; exit 1 } "
                "Write-Output 'AST_OK'"
            ),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        self.assertEqual(proc.returncode, 0, msg=f"PowerShell AST errors: {proc.stderr}")
        self.assertIn("AST_OK", proc.stdout)


class TestLiveDockerIsolation(unittest.TestCase):
    """Live container isolation tests (auto-skipped if Docker daemon/image unavailable)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.runner = DockerSandboxRunner()
        ok, reason = cls.runner.is_docker_available()
        if not ok:
            raise unittest.SkipTest(f"Docker daemon unavailable: {reason}")
        if not cls.runner.is_image_available():
            raise unittest.SkipTest(f"Sandbox image not built yet: {SANDBOX_IMAGE}")

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.target = pathlib.Path(self.tmpdir.name) / "live_target"
        self.target.mkdir()
        (self.target / "sample.csv").write_text("k,v\na,10\nb,25\n", encoding="utf-8")
        (self.target / "main.py").write_text("def greet():\n    return 'offline_ok'\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_live_readonly_network_none_and_cleanup(self) -> None:
        # (a) Reads /target:ro and runs DuckDB
        res_duck = self.runner.run_python(
            code="import duckdb; print(duckdb.connect(':memory:').execute(\"SELECT SUM(v) FROM read_csv_auto('/target/sample.csv')\").fetchall()[0][0])",
            target_path=self.target,
            retention_mode="ephemeral",
            run_id="live_duck",
        )
        self.assertTrue(res_duck.ok, msg=res_duck.stderr)
        self.assertEqual(res_duck.stdout.strip(), "35")

        # (b) Fails write attempts to /target and /
        res_ro_target = self.runner.run_python(
            code="open('/target/escape.txt', 'w').write('nope')",
            target_path=self.target,
            retention_mode="ephemeral",
            run_id="live_ro_target",
        )
        self.assertFalse(res_ro_target.ok)
        self.assertFalse((self.target / "escape.txt").exists())

        res_ro_root = self.runner.run_python(
            code="open('/escape.txt', 'w').write('nope')",
            target_path=self.target,
            retention_mode="ephemeral",
            run_id="live_ro_root",
        )
        self.assertFalse(res_ro_root.ok)

        # (c) Fails outbound network connection (--network none)
        res_net = self.runner.run_python(
            code="import socket; s = socket.create_connection(('1.1.1.1', 53), timeout=2)",
            target_path=self.target,
            retention_mode="ephemeral",
            run_id="live_net",
        )
        self.assertFalse(res_net.ok)

        # (d) Zero leftover containers
        self.assertEqual(self.runner.cleanup_run_containers(), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
