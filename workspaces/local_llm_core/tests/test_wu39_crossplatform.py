"""WU-39 verification tests: cross-platform lock, OS paths, pure-Python engine runner, hardware probe, Linux GUI fallback, and privacy_audit.py."""

from __future__ import annotations

import errno
import importlib
import json
import os
import pathlib
import subprocess
import sys
from types import SimpleNamespace
from unittest import mock

import pytest
from workspaces.local_llm_core import desktop, desktop_shell, engine_runner
from workspaces.local_llm_core.engine import coordinator
from workspaces.local_llm_core.instance_lock import CrossPlatformInstanceLock, SingleInstanceError
from workspaces.local_llm_core.paths import resolve_app_paths


def test_cross_platform_paths_win_linux_macos_and_portable(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: home_dir))
    monkeypatch.delenv("LOCAL_LLM_DATA_DIR", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)

    local_app = tmp_path / "LocalApp"
    monkeypatch.setenv("LOCALAPPDATA", str(local_app))
    win_paths = resolve_app_paths("LocalLLM", platform="win32")
    assert win_paths.data_dir == local_app / "LocalLLM" / "data"
    assert win_paths.binaries_dir == local_app / "Programs" / "LocalLLM"

    linux_paths = resolve_app_paths("LocalLLM", platform="linux")
    assert linux_paths.data_dir == home_dir / ".local" / "share" / "localllm"
    assert linux_paths.cache_dir == home_dir / ".cache" / "localllm"
    assert linux_paths.runtime_dir == home_dir / ".local" / "share" / "localllm" / "run"

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-d"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "xdg-r"))
    linux_xdg = resolve_app_paths("LocalLLM", platform="linux")
    assert linux_xdg.data_dir == tmp_path / "xdg-d" / "localllm"
    assert linux_xdg.runtime_dir == tmp_path / "xdg-r" / "localllm"

    mac_paths = resolve_app_paths("LocalLLM", platform="darwin")
    assert mac_paths.data_dir == home_dir / "Library" / "Application Support" / "LocalLLM"
    assert mac_paths.cache_dir == home_dir / "Library" / "Caches" / "LocalLLM"

    portable_dir = tmp_path / "portable"
    portable_dir.mkdir()
    (portable_dir / "portable.flag").write_text("1", encoding="utf-8")
    p_linux = resolve_app_paths("LocalLLM", portable_dir=portable_dir, platform="linux")
    assert p_linux.is_portable is True
    assert p_linux.data_dir == portable_dir / "data"


def test_cross_platform_instance_lock_posix_flock_mock(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from workspaces.local_llm_core import instance_lock as lock_mod

    held: set[pathlib.Path] = set()

    class FakeFcntl:
        LOCK_EX = 2
        LOCK_NB = 4
        LOCK_UN = 8

        def __init__(self) -> None:
            self.fds: dict[int, pathlib.Path] = {}

        def flock(self, fd: int, op: int) -> None:
            target = lock_dir / "localllm.lock"
            if op & self.LOCK_UN:
                held.discard(self.fds.get(fd, target))
                return
            if target in held:
                raise BlockingIOError(errno.EAGAIN, "Resource temporarily unavailable")
            held.add(target)
            self.fds[fd] = target

    lock_dir = tmp_path / "run"
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(lock_mod, "_get_fcntl", lambda: FakeFcntl())

    l1 = CrossPlatformInstanceLock("LocalLLM", lock_dir=lock_dir)
    l2 = CrossPlatformInstanceLock("LocalLLM", lock_dir=lock_dir)
    try:
        assert l1.acquire() is True
        assert l1.lock_path == lock_dir / "localllm.lock"
        payload = json.loads(l1.lock_path.read_text(encoding="utf-8"))
        assert payload["pid"] == os.getpid()
        assert l2.acquire() is False

        with pytest.raises(SingleInstanceError), CrossPlatformInstanceLock("LocalLLM", lock_dir=lock_dir):
            pass

        l1.release()
        assert l2.acquire() is True
    finally:
        l1.release()
        l2.release()


def test_detect_hardware_profile_cuda_rocm_metal_cpu(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 1. CUDA
    with (
        mock.patch.object(
            engine_runner.shutil,
            "which",
            side_effect=lambda name: "/usr/bin/nvidia-smi" if name == "nvidia-smi" else None,
        ),
        mock.patch.object(
            engine_runner.subprocess,
            "run",
            return_value=SimpleNamespace(returncode=0, stdout="RTX Workstation, 16384, 15000, 42, 35.0, 5\n", stderr=""),
        ),
    ):
        cuda = engine_runner.detect_hardware_profile(platform_name="linux")
        assert cuda["gpu_backend"] == "cuda"
        assert cuda["vram_total_gb"] == 16.0
        assert cuda["unified_memory"] is False

    # 2. AMD ROCm via sysfs (no rocm-smi binary needed)
    drm_root = tmp_path / "sys_drm"
    card0 = drm_root / "card0" / "device"
    card0.mkdir(parents=True)
    (card0 / "mem_info_vram_total").write_text(str(16 * 1024**3), encoding="utf-8")
    (card0 / "mem_info_vram_used").write_text(str(2 * 1024**3), encoding="utf-8")
    with mock.patch.object(engine_runner.shutil, "which", return_value=None):
        rocm = engine_runner.detect_hardware_profile(platform_name="linux", sys_drm_root=drm_root)
        assert rocm["gpu_backend"] == "rocm"
        assert rocm["vram_total_gb"] == 16.0
        assert rocm["vram_free_gb"] == 14.0

    # 3. Apple Silicon Metal (70% unified memory)
    def fake_sysctl(cmd: list[str], **_: object) -> SimpleNamespace:
        if "machdep.cpu.brand_string" in cmd:
            return SimpleNamespace(returncode=0, stdout="Apple M3 Max\n", stderr="")
        if "hw.memsize" in cmd:
            return SimpleNamespace(returncode=0, stdout=str(32 * 1024**3) + "\n", stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="")

    with (
        mock.patch.object(
            engine_runner.shutil,
            "which",
            side_effect=lambda name: "/usr/sbin/sysctl" if name == "sysctl" else None,
        ),
        mock.patch.object(engine_runner.subprocess, "run", side_effect=fake_sysctl),
    ):
        metal = engine_runner.detect_hardware_profile(platform_name="darwin")
        assert metal["gpu_backend"] == "metal"
        assert metal["unified_memory"] is True
        assert metal["vram_total_gb"] == round(32.0 * 0.70, 2)

    # 4. CPU-only fallback
    with mock.patch.object(engine_runner.shutil, "which", return_value=None):
        cpu = engine_runner.detect_hardware_profile(platform_name="linux", sys_drm_root=tmp_path / "none")
        assert cpu["gpu_backend"] == "cpu"
        assert cpu["gpu_available"] is False


def test_pure_python_engine_runner_and_coordinator_routing(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = engine_runner.PurePythonEngineRunner(project_root=tmp_path)
    spawned: list[tuple[list[str], dict[str, str]]] = []

    class FakeProc:
        def __init__(self, pid: int = 4242) -> None:
            self.pid = pid
            self._alive = True

        def poll(self) -> int | None:
            return None if self._alive else 0

        def terminate(self) -> None:
            self._alive = False

        def wait(self, timeout: float | None = None) -> int:
            self._alive = False
            return 0

    def fake_spawn(cmd: list[str], env: dict[str, str], cwd: pathlib.Path) -> FakeProc:
        spawned.append((cmd, env))
        return FakeProc()

    monkeypatch.setattr(engine_runner.shutil, "which", lambda n: "/usr/bin/ollama" if n == "ollama" else None)
    monkeypatch.setattr(runner, "_spawn_detached", fake_spawn)

    monkeypatch.setenv("LOCAL_LLM_USE_PYTHON_RUNNER", "1")
    monkeypatch.setattr(coordinator, "get_gateway_coordinator", lambda: coordinator.GatewayEngineCoordinator())
    with mock.patch("workspaces.local_llm_core.engine_runner.get_python_engine_runner", return_value=runner):
        st_start, res_start = coordinator.execute_engine_action(
            {"action": "start_fast", "ctx": 8192},
            orch_summary_fn=lambda: {"active": False},
        )
        assert st_start == 200
        assert res_start["ok"] is True
        assert res_start["runner"] == "python"
        assert spawned[0][0] == ["/usr/bin/ollama", "serve"]
        assert spawned[0][1]["OLLAMA_NO_CLOUD"] == "1"
        assert spawned[0][1]["KVSAVE"] == "0"

        st_stop, res_stop = coordinator.execute_engine_action(
            {"action": "stop_all"},
            orch_summary_fn=lambda: {"active": False},
        )
        assert st_stop == 200
        assert res_stop["ok"] is True
        assert res_stop["runner"] == "python"


def test_linux_gui_app_mode_browser_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(desktop_shell.importlib.util, "find_spec", lambda name: None)
    assert desktop_shell.is_linux_webview_available() is False

    monkeypatch.setattr(
        desktop_shell.shutil,
        "which",
        lambda name: "/usr/bin/chromium" if name == "chromium" else None,
    )
    assert desktop_shell.find_app_mode_browser() == "/usr/bin/chromium"

    launched: list[list[str]] = []
    cfg = desktop.build_desktop_config(preferred_port=0, private_mode=True)
    rc = desktop.launch(
        config=cfg,
        attach_url="http://127.0.0.1:9111",
        browser_launcher=lambda cmd: launched.append(cmd),
        use_mutex=False,
    )
    assert rc == 0
    assert launched == [["/usr/bin/chromium", "--app=http://127.0.0.1:9111", "--new-window"]]


def test_privacy_audit_script_strict_passes_and_catches_violations(tmp_path: pathlib.Path) -> None:
    repo_root = pathlib.Path(__file__).resolve().parents[3]
    audit_script = repo_root / "scripts" / "privacy_audit.py"
    assert audit_script.is_file()

    proc = subprocess.run(
        [sys.executable, str(audit_script), "--strict", "--root", str(repo_root)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, f"privacy_audit --strict failed:\n{proc.stdout}\n{proc.stderr}"

    # Verify programmatic violation detection on a synthetic temp repo
    spec = importlib.util.spec_from_file_location("privacy_audit", str(audit_script))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    bad_repo = tmp_path / "bad_repo"
    bad_repo.mkdir()
    (bad_repo / ".gitignore").write_text("# empty\n", encoding="utf-8")
    (bad_repo / ".env").write_text("DO_NOT_TRACK=0\n", encoding="utf-8")
    (bad_repo / "leak.py").write_text('URL = "https://telemetry.example.com/collect"\n', encoding="utf-8")
    report = mod.run_audit(bad_repo, strict=True)
    assert report["passed"] is False
    failed_checks = [c["id"] for c in report["checks"] if not c["passed"]]
    assert len(failed_checks) >= 4
