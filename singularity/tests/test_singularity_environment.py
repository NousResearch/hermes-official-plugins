"""The container the backend starts: hardening flags, overlay vs tmpfs, limits, mounts, overlay paths,
and the exec prefix Bot Desktop / computer_use / the browser use to follow the terminal inside."""

import subprocess
from types import SimpleNamespace

import pytest

from singularity_plugin import singularity_env
from tools.environments.path_utils import sanitize_task_id_for_path


@pytest.fixture
def started(monkeypatch, tmp_path):
    """SingularityEnvironment with the CLI faked: records every argv run_capture gets."""
    calls = []
    monkeypatch.setattr(singularity_env, "_ensure_singularity_available", lambda: "/usr/bin/apptainer")
    monkeypatch.setattr(singularity_env, "_get_or_build_sif",
                        lambda image, executable="apptainer": str(tmp_path / "image.sif"))
    monkeypatch.setattr(singularity_env, "_register_exit_sweep", lambda: None)
    monkeypatch.setattr(singularity_env, "run_capture",
                        lambda cmd, **kw: calls.append(cmd) or SimpleNamespace(returncode=0, stderr=""))
    monkeypatch.setattr(singularity_env.SingularityEnvironment, "init_session", lambda self: None)
    import tools.credential_files as cf
    monkeypatch.setattr(cf, "get_credential_file_mounts",
                        lambda: [{"host_path": "/h/creds.json", "container_path": "/root/.hermes/creds.json"}])
    monkeypatch.setattr(cf, "get_skills_directory_mount",
                        lambda *a, **k: [{"host_path": "/h/skills", "container_path": "/root/.hermes/skills"}])

    def make(**kwargs):
        env = singularity_env.SingularityEnvironment(image="docker://python:3.12", **kwargs)
        return env, calls
    return make


def test_persistent_instance_is_hardened_limited_and_mounts_read_only(started):
    env, calls = started(cpu=2, memory=4096, persistent_filesystem=True, task_id="default")
    start = calls[0]
    assert start[:5] == ["/usr/bin/apptainer", "instance", "start", "--containall", "--no-home"]
    assert start[start.index("--overlay") + 1] == str(env._overlay_dir)
    assert "--writable-tmpfs" not in start
    assert "/h/creds.json:/root/.hermes/creds.json:ro" in start
    assert "/h/skills:/root/.hermes/skills:ro" in start
    assert start[start.index("--memory") + 1] == "4096M"
    assert start[start.index("--cpus") + 1] == "2"
    assert start[-1] == env.instance_id


def test_nonpersistent_instance_uses_tmpfs_and_no_limits_when_zero(started):
    env, calls = started(cpu=0, memory=0, persistent_filesystem=False)
    start = calls[0]
    assert "--writable-tmpfs" in start and "--overlay" not in start
    assert "--memory" not in start and "--cpus" not in start
    assert env._overlay_dir is None


def test_failed_start_raises_with_stderr(monkeypatch, started):
    monkeypatch.setattr(singularity_env, "run_capture",
                        lambda cmd, **kw: SimpleNamespace(returncode=255, stderr="FATAL: no image"))
    with pytest.raises(RuntimeError, match="no image"):
        started()


def test_commands_and_exec_prefix_run_inside_the_instance(monkeypatch, started):
    env, _ = started()
    spawned = []
    monkeypatch.setattr(singularity_env, "_popen_bash", lambda cmd, stdin_data=None: spawned.append(cmd))
    env._run_bash("echo hi")
    assert spawned[0] == ["/usr/bin/apptainer", "exec", f"instance://{env.instance_id}", "bash", "-c", "echo hi"]
    assert env.exec_prefix(user="root", interactive=False) == [
        "/usr/bin/apptainer", "exec", f"instance://{env.instance_id}"]


def test_cleanup_stops_the_instance_and_exec_prefix_goes_away(started):
    env, calls = started()
    env.cleanup()
    assert calls[-1] == ["/usr/bin/apptainer", "instance", "stop", env.instance_id]
    assert env.exec_prefix() is None
    with pytest.raises(RuntimeError, match="not started"):
        env._run_bash("true")


def test_overlay_dir_sanitizes_colon_in_task_id(started):
    task_id = "session:agent:main:telegram:dm:12345"
    env, _ = started(persistent_filesystem=True, task_id=task_id)
    assert env._overlay_dir.name == f"overlay-{sanitize_task_id_for_path(task_id)}"
    assert ":" not in env._overlay_dir.name
    assert env._overlay_dir.is_dir()


def test_overlay_dir_keeps_safe_task_ids_verbatim(started, tmp_path):
    """Overlays created while the backend lived in core must keep resolving to the same directory."""
    env, _ = started(persistent_filesystem=True, task_id="default")
    assert env._overlay_dir == tmp_path / "scratch" / "hermes-overlays" / "overlay-default"


def test_distinct_session_keys_get_distinct_overlay_dirs(started):
    names = {started(persistent_filesystem=True, task_id=t)[0]._overlay_dir.name
             for t in ("session:agent:main:telegram:dm:111", "session_agent_main_telegram_dm_111")}
    assert len(names) == 2, f"overlay dirs collided: {names}"


def test_sif_is_built_once_from_a_docker_url_and_reused(monkeypatch, tmp_path):
    builds = []

    def fake_run(cmd, **kw):
        builds.append(cmd)
        open(cmd[2], "w").close()
        return SimpleNamespace(returncode=0, stderr="")
    monkeypatch.setattr(singularity_env, "run_capture", fake_run)
    first = singularity_env._get_or_build_sif("docker://python:3.12", "apptainer")
    second = singularity_env._get_or_build_sif("docker://python:3.12", "apptainer")
    assert first == second == str(tmp_path / "scratch" / ".apptainer" / "python-3.12.sif")
    assert len(builds) == 1 and builds[0][:2] == ["apptainer", "build"]


def test_failed_sif_build_falls_back_to_the_docker_url(monkeypatch):
    monkeypatch.setattr(singularity_env, "run_capture",
                        lambda cmd, **kw: SimpleNamespace(returncode=1, stderr="denied"))
    assert singularity_env._get_or_build_sif("docker://python:3.12") == "docker://python:3.12"


def test_exit_sweep_removes_scratch_dirs_only(tmp_path):
    scratch = tmp_path / "scratch"
    (scratch / "hermes-overlays" / "overlay-default").mkdir(parents=True)
    (scratch / ".apptainer").mkdir()
    singularity_env._sweep_scratch_dirs()
    assert not (scratch / "hermes-overlays").exists()
    assert (scratch / ".apptainer").is_dir()


def test_disk_warning_counts_scratch_dirs(monkeypatch, tmp_path):
    overlay = tmp_path / "scratch" / "hermes-overlays"
    overlay.mkdir(parents=True)
    (overlay / "blob").write_bytes(b"x" * 2048)
    monkeypatch.setitem(singularity_env._disk_usage_cache, "timestamp", float("-inf"))
    monkeypatch.setenv("TERMINAL_DISK_WARNING_GB", "0.000001")
    assert singularity_env.check_disk_usage_warning() is True
    monkeypatch.setitem(singularity_env._disk_usage_cache, "timestamp", float("-inf"))
    monkeypatch.setenv("TERMINAL_DISK_WARNING_GB", "500")
    assert singularity_env.check_disk_usage_warning() is False
