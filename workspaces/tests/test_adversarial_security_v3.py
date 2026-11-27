"""WU-41 Step 41.2 — Deep Adversarial Security & Pentest Suite (V3)."""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import sys
from unittest import mock

import httpx
import pytest
from fastapi.testclient import TestClient

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from workspaces.local_llm_core.app import create_app
from workspaces.local_llm_core.prompt_firewall import redact_messages, redact_text
from workspaces.local_llm_core.security import get_gateway_token
from workspaces.local_llm_core.services.go_mode import (
    BREAK_GLASS_PHRASE,
    BREAK_GLASS_REQUIRED_CHECKBOXES,
    GoModeManager,
)
from workspaces.local_llm_core.services.model_manager import (
    _validate_allowlisted_download_url,
    prepare_model_download,
    stream_catalog_model_pull,
)


def _auth_headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    hdrs = {
        "Host": "127.0.0.1:8787",
        "X-Local-LLM-Token": get_gateway_token(),
        "Content-Type": "application/json",
    }
    if extra:
        hdrs.update(extra)
    return hdrs


def _synthetic_secrets() -> dict[str, str]:
    # Constructed via concatenation so static regex secret scanners ignore test fixtures.
    return {
        "pem": "-----BEGIN " + "RSA PRIVATE KEY-----\nMIIBOgIBAAJBAK\n-----END " + "RSA PRIVATE KEY-----",
        "openai": "sk-" + "test_" + "A1b2C3d4E5f6G7h8I9j0K1l2",
        "github": "ghp_" + "9Z8Y7X6W5V4U3T2S1R0Q9P8O7N6M5L4K3J2I",
        "aws": "AKIA" + "IOSFODNN7EXAMPLE",
        "iban": "IT60X0542811101000000123456",
        "cf": "RSSMRA85M01H501Z",
        "email": "security-test@example.invalid",
    }


def test_ssrf_allowlist_and_redirect_traps_blocked(tmp_path: pathlib.Path) -> None:
    evil_urls = [
        "http://169.254.169.254/latest/meta-data/",
        "https://169.254.169.254/latest/meta-data/",
        "http://127.0.0.1:22/",
        "https://[::1]/model.gguf",
        "http://[::ffff:127.0.0.1]/model.gguf",
        "https://[::ffff:127.0.0.1]/model.gguf",
        "https://2130706433/model.gguf",
        "https://2852039166/latest/meta-data/",
        "https://0177.0.0.1/model.gguf",
        "https://0x7f000001/model.gguf",
        "https://huggingface.co@127.0.0.1/model.gguf",
        "https://huggingface.co:8443/org/repo/resolve/main/m.gguf",
        "file:///etc/passwd",
    ]
    for url in evil_urls:
        with pytest.raises(PermissionError):
            _validate_allowlisted_download_url(url)

    # Mock 302 redirect from valid allowlisted host -> cloud metadata internal IP
    def _redirect_handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "huggingface.co":
            return httpx.Response(
                302,
                headers={"Location": "http://169.254.169.254/latest/meta-data/"},
            )
        return httpx.Response(200, content=b"should-never-reach")

    async def _exercise_prepare_and_pull() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(_redirect_handler),
            follow_redirects=False,
        ) as client:
            with pytest.raises(PermissionError):
                await prepare_model_download(
                    custom_url="https://huggingface.co/org/repo/resolve/main/model.gguf",
                    models_dir=tmp_path,
                    head_client=client,
                )

        # Also test stream_catalog_model_pull hop-by-hop redirect rejection
        orig_async_client = httpx.AsyncClient

        def _patched_client(*args: object, **kwargs: object) -> httpx.AsyncClient:
            return orig_async_client(
                transport=httpx.MockTransport(_redirect_handler),
                follow_redirects=False,
            )

        with mock.patch(
            "workspaces.local_llm_core.services.model_manager.httpx.AsyncClient",
            side_effect=_patched_client,
        ):
            events: list[dict[str, object]] = []
            async for raw_evt in stream_catalog_model_pull(
                {
                    "id": "redirect-ssrf-test",
                    "backend": "gguf",
                    "download_url": "https://huggingface.co/org/repo/resolve/main/model.gguf",
                    "filename": "model.gguf",
                },
                models_dir=tmp_path,
            ):
                line = raw_evt.decode("utf-8").strip()
                if line.startswith("data: "):
                    events.append(json.loads(line[6:]))
            assert any(e.get("stage") == "error" for e in events)
            assert not (tmp_path / "model.gguf").exists()

    asyncio.run(_exercise_prepare_and_pull())


def test_mcp_ssrf_and_encoded_traversal_blocked() -> None:
    app = create_app()
    with TestClient(app, base_url="http://127.0.0.1:8787") as client:
        ssrf_vectors = [
            {"query": "http://169.254.169.254/latest/meta-data/"},
            {"query": "http://127.0.0.1:22"},
            {"query": "http://[::1]:8080/"},
            {"query": "http://[::ffff:127.0.0.1]/secret"},
            {"query": "http://2130706433/admin"},
            {"query": "http://0177.0.0.1/admin"},
            {"query": "http://0x7f000001/admin"},
            {"query": "file:///etc/passwd"},
            {"url": "169.254.169.254", "query": "ok"},
        ]
        for args in ssrf_vectors:
            resp = client.post(
                "/mcp",
                headers=_auth_headers(),
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "rag_query", "arguments": args},
                },
            )
            assert resp.status_code == 403, f"SSRF vector not blocked: {args}"

        path_vectors = [
            "%252e%252e%252f%252e%252e%252fetc%252fshadow",
            "safe/../../.env",
            "ok.txt\x00.md",
            "\\\\server\\share\\secret",
        ]
        for bad_path in path_vectors:
            resp = client.post(
                "/mcp",
                headers=_auth_headers(),
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "code_audit", "arguments": {"target_path": bad_path}},
                },
            )
            assert resp.status_code == 403, f"Path vector not blocked: {bad_path}"


def test_gateway_dns_rebinding_origin_csrf_and_auth_enforcement() -> None:
    app = create_app()
    with TestClient(app, base_url="http://127.0.0.1:8787") as client:
        # Host header DNS rebinding
        r_host = client.get("/api/telemetry", headers={"Host": "evil.com"})
        assert r_host.status_code == 403

        # Malicious / null Origin & cross-site Sec-Fetch-Site
        for bad_hdrs in (
            {"Origin": "https://attacker.example", "X-Local-LLM-Token": get_gateway_token()},
            {"Origin": "null", "X-Local-LLM-Token": get_gateway_token()},
            {"Sec-Fetch-Site": "cross-site", "X-Local-LLM-Token": get_gateway_token()},
        ):
            r_csrf = client.post(
                "/api/go-mode/propose",
                headers={"Host": "127.0.0.1:8787", "Content-Type": "application/json", **bad_hdrs},
                json={"action": "write_file", "path": "x.txt", "content": "hi"},
            )
            assert r_csrf.status_code == 403

        # Missing / invalid auth token -> 401
        r_noauth = client.post(
            "/api/go-mode/propose",
            headers={"Host": "127.0.0.1:8787", "Content-Type": "application/json"},
            json={"action": "write_file", "path": "x.txt", "content": "hi"},
        )
        assert r_noauth.status_code == 401

        # Wrong Content-Type -> 415
        r_ctype = client.post(
            "/api/go-mode/propose",
            headers={
                "Host": "127.0.0.1:8787",
                "X-Local-LLM-Token": get_gateway_token(),
                "Content-Type": "text/plain",
            },
            content="action=write_file",
        )
        assert r_ctype.status_code == 415


def test_go_mode_l5_traversal_symlink_and_break_glass_bypass(tmp_path: pathlib.Path) -> None:
    ws = tmp_path / "workspace"
    outside = tmp_path / "outside"
    ws.mkdir()
    outside.mkdir()
    outside_target = outside / "secret.txt"
    outside_target.write_text("top-secret", encoding="utf-8")

    mgr = GoModeManager()
    code_cfg, _ = mgr.update_config({"workspace_root": str(ws)})
    assert code_cfg == 200

    # 1. Direct OS paths, double-URL-encoded traversal, and null bytes -> 403
    for bad_path in (
        r"..\..\Windows\System32\drivers\etc\hosts",
        "/etc/shadow",
        "%252e%252e%252f%252e%252e%252fetc%252fshadow",
        "safe.txt\x00.md",
    ):
        status, body = mgr.propose_action(
            {"action": "write_file", "path": bad_path, "content": "pwn"}
        )
        assert status == 403, f"Expected 403 for {bad_path!r}, got {status}: {body}"
        assert body["ok"] is False

    # 2. Workspace symlink escape outside ws_root -> 403
    link_path = ws / "escape_link.txt"
    used_real_symlink = False
    try:
        os.symlink(outside_target, link_path)
        used_real_symlink = True
    except OSError:
        used_real_symlink = False

    if used_real_symlink:
        status_sym, body_sym = mgr.propose_action(
            {"action": "write_file", "path": "escape_link.txt", "content": "escape"}
        )
    else:
        orig_is_symlink = pathlib.Path.is_symlink
        orig_resolve = pathlib.Path.resolve

        def _fake_is_symlink(self: pathlib.Path) -> bool:
            if self.name == "escape_link.txt":
                return True
            return orig_is_symlink(self)

        def _fake_resolve(self: pathlib.Path, strict: bool = False) -> pathlib.Path:
            if self.name == "escape_link.txt":
                return outside_target.resolve()
            return orig_resolve(self, strict=strict)

        with (
            mock.patch.object(pathlib.Path, "is_symlink", _fake_is_symlink),
            mock.patch.object(pathlib.Path, "resolve", _fake_resolve),
        ):
            status_sym, body_sym = mgr.propose_action(
                {"action": "write_file", "path": "escape_link.txt", "content": "escape"}
            )

    assert status_sym == 403
    assert body_sym.get("blocked_by") == "symlink_workspace_escape"

    # 3. Break-Glass rejection with wrong phrase or only 2 of 3 checkboxes
    all_boxes = [item["id"] for item in BREAK_GLASS_REQUIRED_CHECKBOXES]
    st_2_boxes, body_2_boxes = mgr.unlock_break_glass(
        {"checkboxes": all_boxes[:2], "phrase": BREAK_GLASS_PHRASE}
    )
    assert st_2_boxes == 403
    assert body_2_boxes["ok"] is False

    st_wrong_phrase, body_wrong_phrase = mgr.unlock_break_glass(
        {"checkboxes": all_boxes, "phrase": "WRONG-PHRASE"}
    )
    assert st_wrong_phrase == 403
    assert body_wrong_phrase["ok"] is False

    # Verify audit log tracked blocks and denials
    audit_events = [evt["event"] for evt in mgr.get_sessions()["audit_log"]]
    assert "LEVEL5_BLOCKED" in audit_events
    assert "TRAVERSAL_BLOCKED" in audit_events
    assert "SYMLINK_ESCAPE_BLOCKED" in audit_events
    assert "BREAK_GLASS_DENIED" in audit_events


def test_prompt_firewall_unit_and_endpoints() -> None:
    s = _synthetic_secrets()
    sample = (
        f"Key:\n{s['pem']}\n"
        f"OpenAI={s['openai']} GH={s['github']} AWS={s['aws']}\n"
        f"IBAN={s['iban']} CF={s['cf']} Mail={s['email']}"
    )
    redacted, counts = redact_text(sample)
    assert counts["private_key"] == 1
    assert counts["api_token"] == 3
    assert counts["iban"] == 1
    assert counts["codice_fiscale"] == 1
    assert counts["email"] == 1
    for secret_val in s.values():
        assert secret_val not in redacted
    assert "[REDACTED:PRIVATE_KEY]" in redacted
    assert "[REDACTED:API_TOKEN]" in redacted
    assert "[REDACTED:IBAN]" in redacted
    assert "[REDACTED:CODICE_FISCALE]" in redacted
    assert "[REDACTED:EMAIL]" in redacted

    msgs, msg_counts = redact_messages([{"role": "user", "content": sample}])
    assert sum(msg_counts.values()) == 7
    assert s["openai"] not in msgs[0]["content"]

    app = create_app()
    captured_payloads: list[dict[str, object]] = []

    class _FakeCoord:
        def resolve_model(self, model: str) -> object:
            return mock.MagicMock(
                alias="local-fast",
                tier="fast",
                backend_name="ollama",
                concrete_model="qwen2.5:7b-instruct",
            )

        async def ensure_tier(self, tier: str, allow_tier_switch: bool = False) -> bool:
            return False

        def get_fast_num_ctx(self) -> int:
            return 8192

        def get_backend(self, name: str) -> object:
            backend = mock.MagicMock()

            async def _chat(payload: dict[str, object]) -> tuple[int, dict[str, object]]:
                captured_payloads.append(payload)
                return 200, {
                    "id": "chatcmpl-fw",
                    "object": "chat.completion",
                    "choices": [
                        {"index": 0, "message": {"role": "assistant", "content": "sanitized ok"}, "finish_reason": "stop"}
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 3},
                }

            backend.chat_completions = _chat
            return backend

    with mock.patch(
        "workspaces.local_llm_core.routes.openai_v1.get_gateway_coordinator",
        return_value=_FakeCoord(),
    ):
        with TestClient(app, base_url="http://127.0.0.1:8787") as client:
            r_v1 = client.post(
                "/v1/chat/completions",
                headers=_auth_headers({"X-Prompt-Firewall": "true"}),
                json={
                    "model": "local-fast",
                    "messages": [{"role": "user", "content": f"Token {s['openai']} Email {s['email']}"}],
                },
            )
            assert r_v1.status_code == 200
            assert r_v1.headers.get("X-Prompt-Firewall") == "active"
            assert int(r_v1.headers.get("X-Prompt-Firewall-Redactions") or "0") >= 2
            forwarded_msg = captured_payloads[0]["messages"][0]["content"]  # type: ignore[index]
            assert s["openai"] not in forwarded_msg
            assert s["email"] not in forwarded_msg

            # /mcp with X-Prompt-Firewall: true redacts PII before calling rag_query
            captured_queries: list[str] = []

            class _FakeRag:
                def query(self, query: str, **kwargs: object) -> list[object]:
                    captured_queries.append(query)
                    return []

            with mock.patch(
                "workspaces.local_llm_core.routes.mcp.get_rag_manager",
                return_value=_FakeRag(),
            ):
                r_mcp = client.post(
                    "/mcp",
                    headers=_auth_headers({"X-Prompt-Firewall": "true"}),
                    json={
                        "jsonrpc": "2.0",
                        "id": 99,
                        "method": "tools/call",
                        "params": {
                            "name": "rag_query",
                            "arguments": {"query": f"Find CF {s['cf']} for {s['email']}"},
                        },
                    },
                )
                assert r_mcp.status_code == 200
                assert s["cf"] not in captured_queries[0]
                assert "[REDACTED:CODICE_FISCALE]" in captured_queries[0]
