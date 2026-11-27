"""Headless-safe cross-platform CLI (`local-llm`) for SSH servers, power users, and diagnostics."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import pathlib
import platform
import socket
import sys
import urllib.parse
import zipfile
from typing import Any

from .prompt_firewall import redact_text

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _ensure_utf8_stdout() -> None:
    reconf = getattr(sys.stdout, "reconfigure", None)
    if callable(reconf):
        try:
            reconf(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _is_loopback_host(host: str) -> bool:
    clean = (host or "").strip().strip("[]").lower()
    return clean in _LOOPBACK_HOSTS


def _validate_loopback_base_url(base_url: str) -> str:
    parsed = urllib.parse.urlsplit((base_url or "").strip())
    host = (parsed.hostname or "").strip().lower()
    if parsed.scheme.lower() != "http" or not _is_loopback_host(host):
        raise ValueError(
            f"Refusing non-loopback gateway URL {base_url!r}; only loopback HTTP is allowed."
        )
    return base_url.rstrip("/")


def _pick_free_loopback_port(host: str = "127.0.0.1") -> int:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def redact_support_text(text: str) -> str:
    """
    Strip PII, API secrets, local username, hostname, and home directories from diagnostic text.
    """
    sanitized, _ = redact_text(text)
    home_str = str(pathlib.Path.home())
    candidates: list[tuple[str, str]] = [
        (home_str, "<HOME>"),
        (home_str.replace("\\", "/"), "<HOME>"),
    ]
    for env_key, placeholder in (
        ("USERPROFILE", "<HOME>"),
        ("LOCALAPPDATA", "<LOCALAPPDATA>"),
        ("APPDATA", "<APPDATA>"),
        ("HOME", "<HOME>"),
    ):
        val = os.environ.get(env_key, "").strip()
        if val and len(val) > 3:
            candidates.append((val, placeholder))
            candidates.append((val.replace("\\", "/"), placeholder))

    try:
        user_name = getpass.getuser().strip()
        if user_name and len(user_name) >= 2:
            candidates.append((user_name, "<USER>"))
    except Exception:
        pass
    try:
        host_name = socket.gethostname().strip()
        if host_name and len(host_name) >= 2:
            candidates.append((host_name, "<HOST>"))
    except Exception:
        pass

    # Replace longest paths before shorter tokens (username)
    candidates.sort(key=lambda item: len(item[0]), reverse=True)
    for raw_tok, repl in candidates:
        sanitized = sanitized.replace(raw_tok, repl)
    return sanitized


def _cmd_serve(args: argparse.Namespace) -> int:
    host = (args.host or "127.0.0.1").strip()
    if not _is_loopback_host(host):
        print(
            f"Error: Refusing to bind non-loopback host {host!r}. Only loopback addresses are allowed.",
            file=sys.stderr,
        )
        return 2

    import uvicorn

    from .app import create_app
    from .paths import resolve_app_paths
    from .runtime_state import remove_runtime_file, write_runtime_file

    req_port = int(args.port)
    bind_port = req_port if req_port > 0 else _pick_free_loopback_port(host)
    paths = resolve_app_paths("KryptosCore", env_prefix="KRYPTOS", ephemeral_cache=True)
    runtime_path = write_runtime_file(
        paths.data_dir,
        host=host,
        port=bind_port,
        developer_gateway_pinned=(bind_port == 8787),
    )
    print(f"Serving Kryptos Core gateway on loopback http://{host}:{bind_port} (runtime: {runtime_path.name})")
    try:
        uvicorn.run(
            create_app(),
            host=host,
            port=bind_port,
            log_level=args.log_level,
        )
    finally:
        remove_runtime_file(runtime_path)
    return 0


def _resolve_chat_target(explicit_base_url: str | None) -> str:
    from .runtime_state import DEVELOPER_DEFAULT_PORT, read_active_runtime

    if explicit_base_url:
        return _validate_loopback_base_url(explicit_base_url)
    active = read_active_runtime()
    if active and active.get("base_url"):
        return _validate_loopback_base_url(str(active["base_url"]))
    return f"http://127.0.0.1:{DEVELOPER_DEFAULT_PORT}"


def _cmd_chat(args: argparse.Namespace) -> int:
    import httpx

    from .security import get_gateway_token

    try:
        base_url = _resolve_chat_target(args.base_url)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    token = get_gateway_token()
    parsed = urllib.parse.urlsplit(base_url)
    headers = {
        "Host": parsed.netloc,
        "Content-Type": "application/json",
        "X-Kryptos-Token": token,
    }
    if args.firewall:
        headers["X-Prompt-Firewall"] = "true"

    def _send_turn(prompt_text: str) -> int:
        payload = {
            "model": args.model,
            "messages": [{"role": "user", "content": prompt_text}],
            "stream": False,
        }
        try:
            with httpx.Client(base_url=base_url, timeout=60.0) as client:
                resp = client.post("/v1/chat/completions", headers=headers, json=payload)
        except Exception as exc:
            print(f"Gateway unreachable at {base_url}: {exc}", file=sys.stderr)
            return 1
        if resp.status_code >= 400:
            print(f"Gateway error ({resp.status_code}): {resp.text}", file=sys.stderr)
            return 1
        data = resp.json()
        choices = data.get("choices") or []
        content = ""
        if choices and isinstance(choices[0], dict):
            msg = choices[0].get("message") or {}
            content = str(msg.get("content") or "")
        print(content)
        return 0

    if args.prompt:
        return _send_turn(" ".join(args.prompt).strip())

    print(f"Connected to {base_url} (model={args.model}). Ctrl+D / 'exit' to quit.")
    while True:
        try:
            line = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line or line.lower() in ("exit", "quit", "/quit"):
            if line.lower() in ("exit", "quit", "/quit"):
                break
            continue
        rc = _send_turn(line)
        if rc != 0:
            return rc
    return 0


def _cmd_models_list(_args: argparse.Namespace) -> int:
    from .services.model_manager import get_annotated_model_catalog

    data = get_annotated_model_catalog()
    models = data.get("models") or []
    print(f"{'ID':<28} {'BACKEND':<9} {'SIZE_GB':>7} {'FIT':<4} {'TOK/S':>6} {'INSTALLED':<9}")
    print("-" * 70)
    for item in models:
        fit = item.get("hardware_fit") or {}
        emoji = str(fit.get("emoji") or "?")
        tok_s = fit.get("est_tok_s") or 0
        size_gb = item.get("disk_required_gb") or item.get("vram_required_gb") or 0
        installed = "yes" if item.get("installed") else "no"
        print(
            f"{str(item.get('id', '')):<28} "
            f"{str(item.get('backend', '')):<9} "
            f"{float(size_gb):>7.2f} "
            f"{emoji:<4} "
            f"{float(tok_s):>6.1f} "
            f"{installed:<9}"
        )
    return 0


def _cmd_models_discover(_args: argparse.Namespace) -> int:
    from .services.model_manager import discover_local_models

    res = discover_local_models()
    items = res.get("models") or []
    print(f"Zero-Copy Local Discovery: {len(items)} model(s) found (0.0 GB copied).")
    for item in items:
        fit = item.get("hardware_fit") or {}
        print(
            f"- [{item.get('source', 'local')}] {item.get('name', 'model')} "
            f"({item.get('size_gb', 0)} GB) {fit.get('emoji', '')} {fit.get('label', '')}"
        )
    return 0


def _cmd_models_pull(args: argparse.Namespace) -> int:
    from .services.model_manager import (
        prepare_model_download,
        stream_resumable_model_download,
    )

    try:
        prep = asyncio.run(prepare_model_download(model_id=args.model_id))
    except Exception as exc:
        print(f"Prepare failed: {exc}", file=sys.stderr)
        return 1

    fit = prep.get("hardware_fit") or {}
    print(
        f"Model: {prep.get('model_id')} | Backend: {prep.get('backend')} | "
        f"Size: {prep.get('exact_gb')} GB | License: {prep.get('license_spdx')} | "
        f"Fit: {fit.get('emoji', '')} {fit.get('label', '')} (~{fit.get('est_tok_s', 0)} tok/s)"
    )
    if prep.get("expected_sha256"):
        print(f"Expected SHA-256: {prep['expected_sha256']}")

    if not args.yes:
        ans = input("Authorize download? [y/N]: ").strip().lower()
        if ans not in ("y", "yes"):
            print("Download cancelled by user.")
            return 1

    async def _run_pull() -> int:
        async for raw_evt in stream_resumable_model_download(
            str(prep["download_token"]),
            confirm_authorize=True,
        ):
            line = raw_evt.decode("utf-8", errors="replace").strip()
            if not line.startswith("data: "):
                continue
            evt = json.loads(line[6:])
            stage = evt.get("stage")
            if stage == "error":
                print(f"Error: {evt.get('error')}", file=sys.stderr)
                return 1
            if stage == "completed":
                print(f"Completed: {evt.get('status')} (verified={evt.get('verified')})")
                return 0
            print(f"[{stage}] {evt.get('pct', 0)}%", flush=True)
        return 0

    return asyncio.run(_run_pull())


def collect_doctor_report() -> dict[str, Any]:
    """Collect privacy-redacted hardware, tier sizing, runtime, and models directory status."""
    from .engine_runner import detect_hardware_profile
    from .runtime_state import read_active_runtime
    from .services.hardware_wizard import compute_hardware_tier_plan, detect_hardware_specs
    from .services.model_manager import resolve_models_dir

    hw_prof = detect_hardware_profile()
    hw_specs = detect_hardware_specs()
    tier_plan = compute_hardware_tier_plan(
        vram_gb=float(hw_prof.get("vram_total_gb") or hw_specs.get("vram_gb") or 0.0),
        ram_gb=float(hw_prof.get("ram_total_gb") or hw_specs.get("ram_gb") or 8.0),
        cpu_threads=int(hw_specs.get("cpu_threads") or 4),
        unified_memory=bool(hw_prof.get("unified_memory", False)),
        gpu_backend=str(hw_prof.get("gpu_backend") or "cpu"),
        gpu_name=str(hw_prof.get("gpu_name") or hw_specs.get("gpu_name") or ""),
        free_disk_gb=float(hw_specs.get("free_disk_gb") or 100.0),
    )
    models_dir, models_source = resolve_models_dir()
    active_rt = read_active_runtime()
    report = {
        "ok": True,
        "version": "3.0.0",
        "os": platform.system(),
        "os_release": platform.release(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "hardware_profile": hw_prof,
        "tier_plan": tier_plan,
        "models_dir": str(models_dir),
        "models_dir_source": models_source,
        "models_dir_exists": models_dir.is_dir(),
        "runtime_active": bool(active_rt),
        "runtime_port": active_rt.get("port") if active_rt else None,
    }
    redacted_json = redact_support_text(json.dumps(report, indent=2, ensure_ascii=False))
    return json.loads(redacted_json)


def _cmd_doctor(args: argparse.Namespace) -> int:
    report = collect_doctor_report()
    report_json = json.dumps(report, indent=2, ensure_ascii=False)
    summary_lines = [
        f"Local LLM Doctor v{report['version']} ({report['os']} {report['machine']}, Python {report['python']})",
        f"Hardware Tier: {report['tier_plan']['tier_label']}",
        f"Recommended Model: {report['tier_plan']['recommended_model']} (num_ctx={report['tier_plan']['num_ctx']}, n_gpu_layers={report['tier_plan']['n_gpu_layers']}, oom_safe={report['tier_plan']['oom_safe']})",
        f"Models Dir ({report['models_dir_source']}): {report['models_dir']} [exists={report['models_dir_exists']}]",
        f"Runtime Active: {report['runtime_active']} (port={report['runtime_port']})",
    ]
    summary_txt = redact_support_text("\n".join(summary_lines) + "\n")

    if args.support_bundle:
        bundle_path = pathlib.Path(args.support_bundle).expanduser().resolve()
        bundle_path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(bundle_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("doctor_report.json", report_json + "\n")
            zf.writestr("system_summary.txt", summary_txt)
        print(f"Created PII-redacted diagnostic support bundle: {redact_support_text(str(bundle_path))}")
        return 0

    if args.json:
        print(report_json)
    else:
        print(summary_txt.rstrip())
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="local-llm",
        description="Local LLM v3 headless CLI & diagnostics.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_serve = sub.add_parser("serve", help="Run loopback gateway server without GUI/X11.")
    p_serve.add_argument("--host", default="127.0.0.1", help="Loopback bind host (default: 127.0.0.1)")
    p_serve.add_argument("--port", type=int, default=0, help="Port (0 = ephemeral free port)")
    p_serve.add_argument("--log-level", default="info", help="Uvicorn log level")
    p_serve.set_defaults(func=_cmd_serve)

    p_chat = sub.add_parser("chat", help="Terminal chat connected via runtime.json.")
    p_chat.add_argument("prompt", nargs="*", help="Optional single-shot prompt")
    p_chat.add_argument("--model", default="local-fast", help="Target model alias or ID")
    p_chat.add_argument("--base-url", default=None, help="Explicit loopback gateway URL")
    p_chat.add_argument("--firewall", action="store_true", help="Enable X-Prompt-Firewall header")
    p_chat.set_defaults(func=_cmd_chat)

    p_models = sub.add_parser("models", help="Inspect, discover, or pull local models.")
    msub = p_models.add_subparsers(dest="models_action", required=True)
    p_mlist = msub.add_parser("list", help="List curated catalog with Hardware-Fit.")
    p_mlist.set_defaults(func=_cmd_models_list)

    p_mdisc = msub.add_parser("discover", help="Zero-Copy scan of local Ollama/HF/LM Studio caches.")
    p_mdisc.set_defaults(func=_cmd_models_discover)

    p_mpull = msub.add_parser("pull", help="Prepare & download a model with explicit confirmation.")
    p_mpull.add_argument("model_id", help="Catalog model_id")
    p_mpull.add_argument("--yes", action="store_true", help="Skip interactive confirmation prompt")
    p_mpull.set_defaults(func=_cmd_models_pull)

    p_doc = sub.add_parser("doctor", help="Check hardware sizing, paths, and export redacted support bundle.")
    p_doc.add_argument("--json", action="store_true", help="Output machine-readable JSON report")
    p_doc.add_argument("--support-bundle", default="", help="Write redacted diagnostics zip archive")
    p_doc.set_defaults(func=_cmd_doctor)

    return parser


def main(argv: list[str] | None = None) -> int:
    _ensure_utf8_stdout()
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
