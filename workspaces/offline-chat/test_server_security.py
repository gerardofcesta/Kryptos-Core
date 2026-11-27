"""Security test suite for Local LLM :8787 gateway and Orchestrator target_path policy."""

from __future__ import annotations

import http.client
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]
OFFLINE_CHAT_DIR = pathlib.Path(__file__).resolve().parent
for _p in (str(PROJECT_ROOT), str(OFFLINE_CHAT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import server as gateway_server
from workspaces.orchestrator.engine.schemas import (
    RunRequest,
    SchemaValidationError,
    validate_target_path,
)


class TestGatewayServerSecurity(unittest.TestCase):
    """Live loopback HTTP security tests against ThreadingHTTPServer + OfflineChatHandler."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._token_file = pathlib.Path(self._tmpdir.name) / "LocalLLM" / "gateway.token"
        self._env_patch = mock.patch.dict(
            os.environ,
            {"LOCAL_LLM_GATEWAY_TOKEN_FILE": str(self._token_file)},
            clear=False,
        )
        self._env_patch.start()
        self.token = gateway_server.rotate_gateway_token()
        self.httpd = gateway_server.ThreadingHTTPServer(
            ("127.0.0.1", 0), gateway_server.OfflineChatHandler
        )
        self.port = self.httpd.server_address[1]
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._thread.start()

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self._thread.join(timeout=2)
        self._env_patch.stop()
        self._tmpdir.cleanup()

    def _raw_request(
        self,
        method: str,
        path: str,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.putrequest(method, path, skip_host=True)
            hdrs = headers or {}
            if "Host" not in hdrs and "host" not in hdrs:
                conn.putheader("Host", f"127.0.0.1:{self.port}")
            for k, v in hdrs.items():
                conn.putheader(k, v)
            if body is not None and "Content-Length" not in hdrs:
                conn.putheader("Content-Length", str(len(body)))
            conn.endheaders()
            if body is not None:
                conn.send(body)
            resp = conn.getresponse()
            data = resp.read()
            resp_headers = {k.lower(): v for k, v in resp.getheaders()}
            return resp.status, resp_headers, data
        finally:
            conn.close()

    def test_host_header_allowlist_blocks_dns_rebinding(self) -> None:
        for bad_host in ("evil.attacker.com", f"evil.attacker.com:{self.port}", "127.0.0.1:9999", "0.0.0.0:8787", ""):
            status, _, _ = self._raw_request(
                "GET",
                "/api/prompts",
                headers={"Host": bad_host, "Authorization": f"Bearer {self.token}"},
            )
            self.assertEqual(status, 403, f"Expected 403 for Host={bad_host!r}, got {status}")

        # Root UI also rejects DNS rebinding Host
        status_root, _, _ = self._raw_request("GET", "/", headers={"Host": "evil.attacker.com"})
        self.assertEqual(status_root, 403)

        # Allowed loopback Hosts (with matching port and without port)
        for good_host in (
            f"127.0.0.1:{self.port}",
            f"localhost:{self.port}",
            f"[::1]:{self.port}",
            "127.0.0.1",
            "localhost",
            "[::1]",
        ):
            status, _, _ = self._raw_request(
                "GET",
                "/api/prompts",
                headers={"Host": good_host, "Authorization": f"Bearer {self.token}"},
            )
            self.assertEqual(status, 200, f"Expected 200 for Host={good_host!r}, got {status}")

    def test_csrf_origin_and_sec_fetch_site_blocked(self) -> None:
        # SEC-01 live reproducer: cross-site simple request with text/plain
        status, _, _ = self._raw_request(
            "POST",
            "/api/control/engine",
            headers={
                "Origin": "https://evil.attacker.com",
                "Sec-Fetch-Site": "cross-site",
                "Content-Type": "text/plain",
            },
            body=b'{"action":"stop_all"}',
        )
        self.assertEqual(status, 403)

        # Cross-site POST to orchestrator even with application/json
        status2, _, _ = self._raw_request(
            "POST",
            "/api/orchestrator/start",
            headers={
                "Origin": "https://evil.attacker.com",
                "Sec-Fetch-Site": "cross-site",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.token}",
            },
            body=b"{}",
        )
        self.assertEqual(status2, 403)

        # Cross-port localhost origin & null origin blocked
        for bad_origin in ("http://127.0.0.1:3000", "http://localhost:9999", "null"):
            st, _, _ = self._raw_request(
                "POST",
                "/api/control/engine",
                headers={
                    "Origin": bad_origin,
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.token}",
                },
                body=b'{"action":"stop_all"}',
            )
            self.assertEqual(st, 403, f"Expected 403 for Origin={bad_origin!r}")

        # Sec-Fetch-Site same-site / cross-site blocked on GET /api/*
        for bad_sfs in ("cross-site", "same-site"):
            st, _, _ = self._raw_request(
                "GET",
                "/api/prompts",
                headers={
                    "Sec-Fetch-Site": bad_sfs,
                    "Authorization": f"Bearer {self.token}",
                },
            )
            self.assertEqual(st, 403, f"Expected 403 for Sec-Fetch-Site={bad_sfs!r}")

    def test_415_enforced_on_non_json_post(self) -> None:
        for endpoint in (
            "/api/control/engine",
            "/api/orchestrator/start",
            "/api/prompts/save",
            "/api/ollama/chat",
        ):
            for bad_ctype in ("text/plain", "application/x-www-form-urlencoded", ""):
                hdrs = {"Authorization": f"Bearer {self.token}"}
                if bad_ctype:
                    hdrs["Content-Type"] = bad_ctype
                status, _, _ = self._raw_request(
                    "POST", endpoint, headers=hdrs, body=b"{}"
                )
                self.assertEqual(
                    status,
                    415,
                    f"Expected 415 for {endpoint} with Content-Type={bad_ctype!r}, got {status}",
                )

    def test_gateway_token_acl_session_endpoint_and_auth(self) -> None:
        self.assertTrue(self._token_file.is_file())
        self.assertEqual(self._token_file.read_text(encoding="utf-8").strip(), self.token)
        self.assertGreaterEqual(len(self.token), 32)

        # Verify NTFS ACL restricts access to current user on Windows
        if sys.platform == "win32":
            icacls_exe = shutil.which("icacls")
            if icacls_exe:
                proc = subprocess.run(
                    [icacls_exe, str(self._token_file)],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(proc.returncode, 0)
                acl_out = proc.stdout.lower()
                user_name = (os.environ.get("USERNAME") or "").lower()
                if user_name:
                    self.assertIn(user_name, acl_out)
                self.assertNotIn("everyone", acl_out)
                self.assertNotIn("builtin\\users", acl_out)
                self.assertNotIn("authenticated users", acl_out)

        # Session token endpoint requires Sec-Fetch-Site: same-origin
        st_no_hdr, _, _ = self._raw_request("GET", "/api/auth/session-token")
        self.assertEqual(st_no_hdr, 403)

        st_cross, _, _ = self._raw_request(
            "GET", "/api/auth/session-token", headers={"Sec-Fetch-Site": "cross-site"}
        )
        self.assertEqual(st_cross, 403)

        st_ok, hdrs_ok, body_ok = self._raw_request(
            "GET",
            "/api/auth/session-token",
            headers={
                "Origin": f"http://127.0.0.1:{self.port}",
                "Sec-Fetch-Site": "same-origin",
            },
        )
        self.assertEqual(st_ok, 200)
        self.assertIn("no-store", hdrs_ok.get("cache-control", ""))
        self.assertEqual(json.loads(body_ok.decode("utf-8"))["token"], self.token)

        # Protected API calls require valid token
        st_unauth, hdrs_unauth, _ = self._raw_request("GET", "/api/prompts")
        self.assertEqual(st_unauth, 401)
        self.assertEqual(hdrs_unauth.get("www-authenticate"), "Bearer")

        st_bad_tok, _, _ = self._raw_request(
            "GET", "/api/prompts", headers={"Authorization": "Bearer wrong-token"}
        )
        self.assertEqual(st_bad_tok, 401)

        # Both X-Local-LLM-Token and Authorization: Bearer are accepted
        st_hdr, _, _ = self._raw_request(
            "GET", "/api/prompts", headers={"X-Local-LLM-Token": self.token}
        )
        self.assertEqual(st_hdr, 200)

        st_bearer, _, _ = self._raw_request(
            "GET", "/api/prompts", headers={"Authorization": f"Bearer {self.token}"}
        )
        self.assertEqual(st_bearer, 200)

        # Public root UI does not require token
        st_root, _, _ = self._raw_request("GET", "/")
        self.assertEqual(st_root, 200)

    def test_orchestrator_start_blocks_denied_target_path_via_http(self) -> None:
        win_dir = os.environ.get("WINDIR") or r"C:\Windows"
        payload = json.dumps(
            {
                "operator": "orchestrator",
                "retention_mode": "ephemeral",
                "mission": "Attempt forbidden target scan",
                "target_path": win_dir,
            }
        ).encode("utf-8")
        status, _, body = self._raw_request(
            "POST",
            "/api/orchestrator/start",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.token}",
            },
            body=payload,
        )
        self.assertEqual(status, 403, body.decode("utf-8", errors="replace"))


class TestTargetPathPolicy(unittest.TestCase):
    """Unit tests for schemas.validate_target_path & RunRequest target_path policy."""

    def test_blocks_traversal_unc_roots_and_system_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            traversal = os.path.join(tmpdir, "sub", "..", "other")
            os.makedirs(os.path.join(tmpdir, "other"), exist_ok=True)
            with self.assertRaises(SchemaValidationError):
                validate_target_path(traversal)

        for bad_path in (
            r"\\evil-server\share\folder",
            "//evil-server/share/folder",
            r"C:\\",
            "/",
        ):
            with self.assertRaises(SchemaValidationError, msg=f"Should reject {bad_path!r}"):
                validate_target_path(bad_path)

        # System & user sensitive paths
        candidates: list[str] = []
        for env_key in ("WINDIR", "ProgramFiles", "APPDATA", "LOCALAPPDATA", "USERPROFILE"):
            val = os.environ.get(env_key)
            if val and os.path.isdir(val):
                candidates.append(val)
        ssh_dir = pathlib.Path.home() / ".ssh"
        if ssh_dir.is_dir():
            candidates.append(str(ssh_dir))
        else:
            with tempfile.TemporaryDirectory() as tmpdir:
                fake_ssh = pathlib.Path(tmpdir) / ".ssh"
                fake_ssh.mkdir()
                candidates.append(str(fake_ssh))
                with self.assertRaises(SchemaValidationError):
                    validate_target_path(str(fake_ssh))

        for bad_dir in candidates:
            with self.assertRaises(SchemaValidationError, msg=f"Should deny {bad_dir!r}"):
                validate_target_path(bad_dir)

    def test_allowed_roots_allowlist_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as root_a, tempfile.TemporaryDirectory() as root_b:
            allowed_sub = pathlib.Path(root_a) / "project_ok"
            allowed_sub.mkdir()
            outside_sub = pathlib.Path(root_b) / "project_outside"
            outside_sub.mkdir()

            with mock.patch.dict(os.environ, {"ORCHESTRATOR_ALLOWED_ROOTS": root_a}, clear=False):
                resolved = validate_target_path(str(allowed_sub))
                self.assertEqual(resolved, allowed_sub.resolve())

                with self.assertRaises(SchemaValidationError):
                    validate_target_path(str(outside_sub))

                # RunRequest integration
                req = RunRequest.from_dict(
                    {
                        "operator": "orchestrator",
                        "retention_mode": "ephemeral",
                        "mission": "Audit allowed repo",
                        "target_path": str(allowed_sub),
                    }
                )
                self.assertEqual(req.target_path, str(allowed_sub.resolve()))


class TestGatewayReliabilityAndFsmFixes(unittest.TestCase):
    """Tests for EngineCoordinator lock, 409 conflict, mid-stream SSE error, telemetry cache, preflight & FSM."""

    def test_engine_coordinator_lock_and_409_during_active_orchestrator(self) -> None:
        from workspaces.orchestrator.engine.tier_switcher import EngineCoordinator

        with tempfile.TemporaryDirectory() as tmpdir:
            lock_file = pathlib.Path(tmpdir) / "engine.lock"
            coord = EngineCoordinator(lock_path=lock_file)
            with coord.acquire(timeout=2.0):
                # Re-entrant on same thread works without deadlock
                with coord.acquire(timeout=2.0):
                    self.assertTrue(lock_file.is_file())

        with mock.patch.object(
            gateway_server,
            "get_orchestrator_telemetry_summary",
            return_value={"active": True, "state": "INVESTIGATOR"},
        ):
            code, res = gateway_server.execute_engine_action({"action": "start_heavy"})
            self.assertEqual(code, 409)
            self.assertFalse(res["ok"])

    def test_proxy_post_stream_midstream_error_emits_sse_not_http_headers(self) -> None:
        class _FakeUpstreamResp:
            status = 200

            def __init__(self) -> None:
                self._reads = 0

            def getheader(self, name: str, default: str | None = None) -> str | None:
                if name.lower() == "content-type":
                    return "text/event-stream; charset=utf-8"
                return default

            def read1(self, n: int = 4096) -> bytes:
                self._reads += 1
                if self._reads == 1:
                    return b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
                raise RuntimeError("upstream socket dropped mid-stream")

        class _FakeConn:
            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

            def request(self, *args: object, **kwargs: object) -> None:
                pass

            def getresponse(self) -> _FakeUpstreamResp:
                return _FakeUpstreamResp()

            def close(self) -> None:
                pass

        with tempfile.TemporaryDirectory() as tmpdir:
            token_file = pathlib.Path(tmpdir) / "gateway.token"
            with mock.patch.dict(
                os.environ, {"LOCAL_LLM_GATEWAY_TOKEN_FILE": str(token_file)}, clear=False
            ):
                tok = gateway_server.rotate_gateway_token()
                httpd = gateway_server.ThreadingHTTPServer(
                    ("127.0.0.1", 0), gateway_server.OfflineChatHandler
                )
                port = httpd.server_address[1]
                t = threading.Thread(target=httpd.serve_forever, daemon=True)
                t.start()
                try:
                    real_http_conn = http.client.HTTPConnection

                    def _conn_factory(host: str, cport: int, *args: object, **kwargs: object) -> object:
                        if cport == port:
                            return real_http_conn(host, cport, *args, **kwargs)
                        return _FakeConn()

                    with mock.patch("http.client.HTTPConnection", side_effect=_conn_factory):
                        conn = real_http_conn("127.0.0.1", port, timeout=5)
                        conn.request(
                            "POST",
                            "/api/heavy/chat/completions",
                            body=b'{"model":"heavy","messages":[]}',
                            headers={
                                "Host": f"127.0.0.1:{port}",
                                "Content-Type": "application/json",
                                "Authorization": f"Bearer {tok}",
                            },
                        )
                        resp = conn.getresponse()
                        raw_body = resp.read().decode("utf-8", errors="replace")
                        conn.close()
                    self.assertEqual(resp.status, 200)
                    self.assertIn('data: {"choices"', raw_body)
                    self.assertIn('"error":', raw_body)
                    self.assertNotIn("HTTP/1.1 503", raw_body)
                finally:
                    httpd.shutdown()
                    httpd.server_close()
                    t.join(timeout=2)

    def test_telemetry_ttl_cache_and_heavy_skips_models_when_health_fails(self) -> None:
        called_paths: list[str] = []

        def fake_http_json(
            host: str, port: int, method: str, path: str, timeout: float = 0.9
        ) -> tuple[bool, int, object, str | None]:
            called_paths.append(f"{port}{path}")
            return False, 0, None, "offline"

        gateway_server.invalidate_telemetry_cache()
        with (
            mock.patch.object(gateway_server, "_http_json", side_effect=fake_http_json),
            mock.patch.object(
                gateway_server,
                "get_gpu_telemetry",
                return_value={"available": False},
            ) as mock_gpu,
        ):
            t1 = gateway_server.get_full_telemetry()
            t2 = gateway_server.get_full_telemetry()
            self.assertIs(t1, t2)
            self.assertEqual(mock_gpu.call_count, 1)
            # Heavy /v1/models must be skipped when /health fails
            self.assertIn(f"{gateway_server.HEAVY_PORT}/health", called_paths)
            self.assertNotIn(f"{gateway_server.HEAVY_PORT}/v1/models", called_paths)

        gateway_server.invalidate_telemetry_cache()

    def test_fsm_num_ctx_32768_protocol_escalation_and_preflight_route(self) -> None:
        from workspaces.orchestrator.api_router import handle_orchestrator_request
        from workspaces.orchestrator.engine.state_machine import OrchestratorManager, _RunController

        self.assertEqual(_RunController._resolve_fast_num_ctx("local-worker:latest"), 32768)

        with mock.patch("shutil.which", return_value=None):
            res = handle_orchestrator_request("GET", "/api/orchestrator/preflight")
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res[0], 200)
        self.assertTrue(res[1]["ok"])
        self.assertIn("dockerDaemon", res[1])
        self.assertIn("sandboxImageReady", res[1])
        self.assertIn("freeRamGb", res[1])

        # Verify ProtocolParseError increments escalations_used in Investigator
        from workspaces.orchestrator.engine.tool_protocol import ProtocolParseError

        observed_num_ctx: list[int] = []

        class _FakeTier:
            def cleanup_orchestrator_containers(self) -> tuple[bool, str]:
                return True, "0"

            def ensure_tier(self, **kwargs: object) -> tuple[bool, str]:
                return True, "ok"

            def chat_heavy(self, **kwargs: object) -> dict[str, object]:
                # Strategist produces 1 OPEN hypothesis; Adjudicator produces HALTED_CIRCUIT_BREAKER
                if "Adjudicator" in str(kwargs.get("messages")):
                    return {
                        "content": json.dumps(
                            {
                                "resolved_hypotheses": [],
                                "verdict": {
                                    "code": "HALTED_CIRCUIT_BREAKER",
                                    "summary": "Protocol escalation exceeded budget.",
                                },
                            }
                        )
                    }
                return {
                    "content": json.dumps(
                        {
                            "impossible_proven": False,
                            "hypotheses": [
                                {"id": "H1", "title": "Check bug", "priority": 1}
                            ],
                        }
                    )
                }

            def chat_ollama(self, **kwargs: object) -> dict[str, object]:
                if "num_ctx" in kwargs:
                    observed_num_ctx.append(int(kwargs["num_ctx"]))  # type: ignore[arg-type]
                return {"content": "not valid tool_call xml at all"}

        with tempfile.TemporaryDirectory() as tmpdir:
            target = pathlib.Path(tmpdir) / "repo"
            target.mkdir()
            (target / "app.py").write_text("print('ok')\n", encoding="utf-8")

            class _FakeToolkit:
                def __init__(self, **kwargs: object) -> None:
                    pass

                def execute(self, *args: object, **kwargs: object) -> dict[str, object]:
                    return {"ok": True}

            mgr = OrchestratorManager(
                tier_switcher=_FakeTier(),
                scout_fn=lambda **kwargs: {"files": ["app.py"], "total_files": 1},
                toolkit_factory=_FakeToolkit,
            )
            start_code, start_res = mgr.start_run(
                {
                    "operator": "orchestrator",
                    "retention_mode": "ephemeral",
                    "mission": "Verify protocol escalation counting",
                    "target_path": str(target),
                    "budget": {"max_heavy_escalations": 0, "max_turns_per_hypothesis": 1},
                }
            )
            self.assertEqual(start_code, 200)
            self.assertTrue(start_res.get("ok"))
            for _ in range(80):
                st = mgr.get_status()
                if not st.get("active"):
                    break
                threading.Event().wait(0.05)
            st_final = mgr.get_status()
            self.assertIn(32768, observed_num_ctx)
            self.assertGreaterEqual(st_final["escalations_used"], 1)
            self.assertEqual(st_final["verdict"]["code"], "HALTED_CIRCUIT_BREAKER")
            mgr.shutdown_cleanup()


if __name__ == "__main__":
    unittest.main()
