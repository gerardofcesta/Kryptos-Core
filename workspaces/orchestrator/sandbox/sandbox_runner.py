"""
Hardened offline Docker sandbox runner for Local LLM Orchestrator.

Pure Python stdlib only. Every container invocation enforces:
- --rm + guaranteed `docker rm -f` cleanup in `finally`
- --label local-llm-orchestrator=true (+ run_id, retention)
- --network none
- --read-only
- --cap-drop ALL --security-opt no-new-privileges
- --pids-limit, --memory, --cpus
- --tmpfs /work:rw,nosuid,size=<mb>m
- Target mounted strictly read-only (-v <target>:/target:ro)
- Ephemeral privacy invariant: ZERO writable host bind mounts in ephemeral mode
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import os
import pathlib
import re
import shutil
import subprocess
import time
from typing import Any
import uuid

SANDBOX_IMAGE = "local-llm-sandbox:offline"
ORCHESTRATOR_LABEL = "local-llm-orchestrator=true"
ALLOWED_RETENTION_MODES = ("ephemeral", "persistent")

_SAFE_ID_RE = re.compile(r"[^a-zA-Z0-9_-]+")


@dataclass
class SandboxResult:
    """Structured outcome of a single ephemeral Docker sandbox execution."""

    ok: bool
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool
    duration_ms: int
    container_name: str
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _creationflags() -> int:
    """Suppress console window popups when invoking docker.exe on Windows."""
    if os.name == "nt":
        return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return 0


def _sanitize_identifier(value: str, fallback: str = "run", max_len: int = 36) -> str:
    """Sanitize a run_id for safe use in Docker container names and labels."""
    raw = str(value or "").strip()
    cleaned = _SAFE_ID_RE.sub("-", raw).strip("-_")
    if not cleaned:
        cleaned = fallback
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len].rstrip("-_") or fallback
    return cleaned


def _truncate_output(raw: bytes | str | None, max_bytes: int) -> str:
    """Decode UTF-8 safely and truncate to max_bytes with a clear [TRUNCATED] marker."""
    if raw is None:
        return ""
    if isinstance(raw, str):
        data = raw.encode("utf-8", errors="replace")
    else:
        data = bytes(raw)

    if max_bytes <= 0:
        return ""

    if len(data) <= max_bytes:
        return data.decode("utf-8", errors="replace")

    short_marker = "[TRUNCATED]"
    short_marker_bytes = short_marker.encode("ascii")
    if max_bytes <= len(short_marker_bytes):
        return short_marker[:max_bytes]

    # Reserve room for informative marker; fallback to short marker if budget is tiny.
    approx_marker = f"\n...[TRUNCATED: {len(data)} bytes omitted]".encode("ascii")
    if max_bytes <= len(approx_marker) + 16:
        keep = max_bytes - len(short_marker_bytes)
        prefix = data[:keep].decode("utf-8", errors="ignore")
        return prefix + short_marker

    keep = max_bytes - len(approx_marker)
    prefix = data[:keep].decode("utf-8", errors="ignore")
    omitted = len(data) - len(prefix.encode("utf-8"))
    marker = f"\n...[TRUNCATED: {omitted} bytes omitted]"
    return prefix + marker


def _validate_retention_mode(retention_mode: str) -> str:
    if not isinstance(retention_mode, str) or retention_mode not in ALLOWED_RETENTION_MODES:
        raise ValueError(
            f"Invalid retention_mode {retention_mode!r}; must be one of {ALLOWED_RETENTION_MODES}."
        )
    return retention_mode


class DockerSandboxRunner:
    """Hardened offline Docker container execution wrapper."""

    def __init__(
        self,
        image: str = SANDBOX_IMAGE,
        memory_limit: str = "1g",
        cpus: str = "4",
        pids_limit: int = 128,
        tmpfs_size_mb: int = 512,
    ) -> None:
        if not isinstance(image, str) or not image.strip():
            raise ValueError("image must be a non-empty string")
        if not isinstance(memory_limit, str) or not memory_limit.strip():
            raise ValueError("memory_limit must be a non-empty string")
        if not isinstance(cpus, (str, int, float)) or not str(cpus).strip():
            raise ValueError("cpus must be a non-empty value")
        if not isinstance(pids_limit, int) or pids_limit < 16:
            raise ValueError("pids_limit must be an integer >= 16")
        if not isinstance(tmpfs_size_mb, int) or tmpfs_size_mb < 16:
            raise ValueError("tmpfs_size_mb must be an integer >= 16")

        self.image = image.strip()
        self.memory_limit = memory_limit.strip()
        self.cpus = str(cpus).strip()
        self.pids_limit = pids_limit
        self.tmpfs_size_mb = tmpfs_size_mb

    def is_docker_available(self) -> tuple[bool, str]:
        """Return (True, version_info) if Docker CLI and daemon respond within 4s."""
        if shutil.which("docker") is None:
            return False, "Docker CLI not found on PATH"

        try:
            proc = subprocess.run(
                ["docker", "info", "--format", "{{.ServerVersion}}"],
                capture_output=True,
                text=True,
                shell=False,
                timeout=4,
                creationflags=_creationflags(),
            )
        except subprocess.TimeoutExpired:
            return False, "Docker daemon check timed out after 4s"
        except FileNotFoundError:
            return False, "Docker CLI not found"
        except OSError as exc:
            return False, f"OS error checking Docker daemon: {exc}"

        if proc.returncode == 0:
            version = (proc.stdout or "").strip() or "unknown"
            return True, f"Docker daemon reachable (server {version})"

        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        summary = detail[0] if detail else f"exit code {proc.returncode}"
        return False, f"Docker daemon unreachable: {summary}"

    def is_image_available(self) -> bool:
        """Return True if the configured offline sandbox image exists locally."""
        if shutil.which("docker") is None:
            return False
        try:
            proc = subprocess.run(
                ["docker", "image", "inspect", self.image],
                capture_output=True,
                shell=False,
                timeout=4,
                creationflags=_creationflags(),
            )
            return proc.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return False

    def build_docker_run_cmd(
        self,
        argv: list[str],
        target_path: str | pathlib.Path,
        retention_mode: str,
        run_id: str,
        artifacts_path: str | pathlib.Path | None = None,
        use_stdin: bool = False,
        container_name: str | None = None,
    ) -> tuple[list[str], str]:
        """Construct the hardened `docker run` argv list and unique container name."""
        mode = _validate_retention_mode(retention_mode)
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
            raise ValueError("argv must be a non-empty list of strings")

        safe_run_id = _sanitize_identifier(run_id)
        cname = container_name or f"localllm-{safe_run_id}-{uuid.uuid4().hex[:8]}"
        resolved_target = pathlib.Path(target_path).resolve()

        cmd: list[str] = [
            "docker",
            "run",
            "--rm",
        ]
        if use_stdin:
            cmd.append("-i")

        cmd.extend(
            [
                "--name",
                cname,
                "--label",
                ORCHESTRATOR_LABEL,
                "--label",
                f"run_id={safe_run_id}",
                "--label",
                f"retention={mode}",
                "--network",
                "none",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--pids-limit",
                str(self.pids_limit),
                "--memory",
                self.memory_limit,
                "--cpus",
                self.cpus,
                "--tmpfs",
                f"/work:rw,nosuid,size={self.tmpfs_size_mb}m",
                "-v",
                f"{resolved_target}:/target:ro",
            ]
        )

        # CRITICAL PRIVACY INVARIANT:
        # In ephemeral mode, NEVER mount artifacts_path or any writable host bind mount.
        if mode == "persistent" and artifacts_path is not None:
            resolved_artifacts = pathlib.Path(artifacts_path).resolve()
            cmd.extend(["-v", f"{resolved_artifacts}:/artifacts:rw"])

        cmd.append(self.image)
        cmd.extend(argv)

        # Defensive invariant verification before returning command.
        self._assert_command_invariants(cmd, mode)
        return cmd, cname

    @staticmethod
    def _assert_command_invariants(cmd: list[str], retention_mode: str) -> None:
        """Fail closed if any mandatory security or privacy flag is missing."""
        required_pairs = (
            ("--network", "none"),
            ("--cap-drop", "ALL"),
            ("--security-opt", "no-new-privileges"),
            ("--label", ORCHESTRATOR_LABEL),
        )
        for flag, expected in required_pairs:
            matched = False
            for idx, token in enumerate(cmd[:-1]):
                if token == flag and cmd[idx + 1] == expected:
                    matched = True
                    break
            if not matched:
                raise RuntimeError(f"Sandbox invariant violation: missing {flag} {expected}")

        if "--rm" not in cmd or "--read-only" not in cmd:
            raise RuntimeError("Sandbox invariant violation: missing --rm or --read-only")

        mounts: list[str] = []
        for idx, token in enumerate(cmd[:-1]):
            if token in ("-v", "--volume", "--mount"):
                mounts.append(cmd[idx + 1])

        if not any(m.endswith(":/target:ro") for m in mounts):
            raise RuntimeError("Sandbox invariant violation: target must be mounted :/target:ro")

        if retention_mode == "ephemeral":
            for m in mounts:
                if not m.endswith(":ro"):
                    raise RuntimeError(
                        f"Privacy invariant violation: writable bind mount forbidden in ephemeral mode ({m})"
                    )

    def _force_remove_container(self, container_name: str) -> None:
        """Best-effort forced removal to guarantee zero orphan containers on timeout/error."""
        if not container_name:
            return
        try:
            subprocess.run(
                ["docker", "rm", "-f", container_name],
                capture_output=True,
                shell=False,
                timeout=5,
                creationflags=_creationflags(),
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass

    def run_command(
        self,
        argv: list[str],
        target_path: str | pathlib.Path,
        retention_mode: str,
        run_id: str,
        artifacts_path: str | pathlib.Path | None = None,
        stdin_text: str | None = None,
        timeout_sec: int = 60,
        max_output_bytes: int = 8192,
    ) -> SandboxResult:
        """Execute a command inside a hardened, offline, ephemeral Docker container."""
        mode = _validate_retention_mode(retention_mode)
        if timeout_sec <= 0:
            raise ValueError("timeout_sec must be > 0")
        if max_output_bytes <= 0:
            raise ValueError("max_output_bytes must be > 0")

        resolved_target = pathlib.Path(target_path).resolve()
        if not resolved_target.exists():
            return SandboxResult(
                ok=False,
                exit_code=-1,
                stdout="",
                stderr="",
                timed_out=False,
                duration_ms=0,
                container_name="",
                error=f"Target path does not exist: {resolved_target}",
            )

        effective_artifacts: pathlib.Path | None = None
        if mode == "persistent" and artifacts_path is not None:
            effective_artifacts = pathlib.Path(artifacts_path).resolve()
            effective_artifacts.mkdir(parents=True, exist_ok=True)

        cmd, container_name = self.build_docker_run_cmd(
            argv=argv,
            target_path=resolved_target,
            retention_mode=mode,
            run_id=run_id,
            artifacts_path=effective_artifacts,
            use_stdin=(stdin_text is not None),
        )

        stdin_bytes = stdin_text.encode("utf-8") if stdin_text is not None else None
        t0 = time.monotonic()
        try:
            proc = subprocess.run(
                cmd,
                input=stdin_bytes,
                capture_output=True,
                shell=False,
                timeout=timeout_sec,
                creationflags=_creationflags(),
            )
            duration_ms = int((time.monotonic() - t0) * 1000)
            stdout = _truncate_output(proc.stdout, max_output_bytes)
            stderr = _truncate_output(proc.stderr, max_output_bytes)
            ok = proc.returncode == 0
            error = None if ok else (stderr.strip().splitlines()[-1] if stderr.strip() else f"Exit code {proc.returncode}")
            return SandboxResult(
                ok=ok,
                exit_code=proc.returncode,
                stdout=stdout,
                stderr=stderr,
                timed_out=False,
                duration_ms=duration_ms,
                container_name=container_name,
                error=error,
            )
        except subprocess.TimeoutExpired as exc:
            duration_ms = int((time.monotonic() - t0) * 1000)
            stdout = _truncate_output(exc.stdout, max_output_bytes)
            stderr = _truncate_output(exc.stderr, max_output_bytes)
            return SandboxResult(
                ok=False,
                exit_code=-1,
                stdout=stdout,
                stderr=stderr,
                timed_out=True,
                duration_ms=duration_ms,
                container_name=container_name,
                error=f"Sandbox command timed out after {timeout_sec}s",
            )
        except FileNotFoundError:
            duration_ms = int((time.monotonic() - t0) * 1000)
            return SandboxResult(
                ok=False,
                exit_code=-1,
                stdout="",
                stderr="",
                timed_out=False,
                duration_ms=duration_ms,
                container_name=container_name,
                error="Docker CLI not found",
            )
        except OSError as exc:
            duration_ms = int((time.monotonic() - t0) * 1000)
            return SandboxResult(
                ok=False,
                exit_code=-1,
                stdout="",
                stderr="",
                timed_out=False,
                duration_ms=duration_ms,
                container_name=container_name,
                error=f"OS error invoking Docker: {exc}",
            )
        finally:
            self._force_remove_container(container_name)

    def run_python(
        self,
        code: str,
        target_path: str | pathlib.Path,
        retention_mode: str,
        run_id: str,
        artifacts_path: str | pathlib.Path | None = None,
        timeout_sec: int = 60,
        max_output_bytes: int = 8192,
    ) -> SandboxResult:
        """Execute Python code in isolated mode via stdin inside the offline sandbox."""
        if not isinstance(code, str) or not code.strip():
            raise ValueError("code must be a non-empty string")
        return self.run_command(
            argv=["python3", "-I", "-"],
            target_path=target_path,
            retention_mode=retention_mode,
            run_id=run_id,
            artifacts_path=artifacts_path,
            stdin_text=code,
            timeout_sec=timeout_sec,
            max_output_bytes=max_output_bytes,
        )

    def cleanup_run_containers(self, run_id: str | None = None) -> int:
        """List and force-remove any containers matching orchestrator labels."""
        if shutil.which("docker") is None:
            return 0

        list_cmd = [
            "docker",
            "ps",
            "-aq",
            "--filter",
            f"label={ORCHESTRATOR_LABEL}",
        ]
        if run_id is not None and str(run_id).strip():
            safe_run_id = _sanitize_identifier(run_id)
            list_cmd.extend(["--filter", f"label=run_id={safe_run_id}"])

        try:
            proc = subprocess.run(
                list_cmd,
                capture_output=True,
                text=True,
                shell=False,
                timeout=10,
                creationflags=_creationflags(),
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return 0

        if proc.returncode != 0:
            return 0

        container_ids = [line.strip() for line in (proc.stdout or "").splitlines() if line.strip()]
        if not container_ids:
            return 0

        try:
            rm_proc = subprocess.run(
                ["docker", "rm", "-f", *container_ids],
                capture_output=True,
                text=True,
                shell=False,
                timeout=15,
                creationflags=_creationflags(),
            )
            removed = [line.strip() for line in (rm_proc.stdout or "").splitlines() if line.strip()]
            return len(removed) if removed else (len(container_ids) if rm_proc.returncode == 0 else 0)
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return 0
