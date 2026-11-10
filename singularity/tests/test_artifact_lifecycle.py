"""Singularity persisted artifacts and teardown under profile multiplexing.

* Persistent overlays must be selected by durable identity (owner profile, image
  authority, task) — never by a scope generation, which is salted per interpreter.
  Exercised across two real interpreter processes.
* ``cleanup`` must issue ``instance stop`` with the authority captured when the
  instance started, whatever profile scope the caller happens to hold, and must
  keep the handle when the stop does not succeed.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from singularity_plugin import singularity_env
from tools.environments import base

REPO_ROOT = Path(base.__file__).resolve().parents[2]
PLUGIN_ROOT = Path(__file__).resolve().parents[2]

_CHILD = textwrap.dedent(
    """
    import json, sys
    from pathlib import Path
    from agent import secret_scope
    from hermes_constants import set_hermes_home_override
    from singularity import singularity_env as s

    source, target, image = (Path(p) for p in sys.argv[1:4])
    mode = sys.argv[4]
    s._ensure_singularity_available = lambda **_kw: "/usr/bin/apptainer"
    s.SingularityEnvironment._start_instance = lambda self: None
    s.SingularityEnvironment.init_session = lambda self: None
    secret_scope.set_multiplex_active(True)
    secret_scope.set_secret_scope(secret_scope.build_profile_secret_scope(target), profile_home=str(target))
    set_hermes_home_override(target)
    env = s.SingularityEnvironment(image=str(image), persistent_filesystem=True, task_id="session:a:b")
    if mode == "write":
        (env._overlay_dir / "marker").write_text("persisted", encoding="utf-8")
    print(json.dumps({
        "overlay": str(env._overlay_dir),
        "generation": env._profile_env_boundary.target_generation,
        "marker": (env._overlay_dir / "marker").read_text(encoding="utf-8")
        if (env._overlay_dir / "marker").exists() else None,
    }))
    """
)


def _profiles(tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    for home, value in ((source, "source-value"), (target, "target-value")):
        home.mkdir()
        (home / ".env").write_text(f"OVERLAY_LOGIN={value}\n", encoding="utf-8")
        (home / "config.yaml").write_text("{}\n", encoding="utf-8")
    return source, target


def _run_child(tmp_path, source, target, image, mode):
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path),
        "HERMES_HOME": str(source),
        "TERMINAL_SCRATCH_DIR": str(tmp_path / "scratch"),
        "PYTHONPATH": str(REPO_ROOT) + os.pathsep + str(PLUGIN_ROOT),
        "LANG": "C.UTF-8",
    }
    result = subprocess.run(
        [sys.executable, "-c", _CHILD, str(source), str(target), str(image), mode],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    assert result.returncode == 0, result.stderr[-4000:]
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_persistent_overlay_survives_interpreter_restart_and_follows_image_change(tmp_path):
    source, target = _profiles(tmp_path)
    image = tmp_path / "image.sif"
    image.write_bytes(b"immutable-sif-v1")

    first = _run_child(tmp_path, source, target, image, "write")
    second = _run_child(tmp_path, source, target, image, "read")

    # The scope generation really is process-local; the overlay must not be.
    assert first["generation"] != second["generation"]
    assert second["overlay"] == first["overlay"]
    assert second["marker"] == "persisted"

    image.write_bytes(b"immutable-sif-v2")  # a genuine image change
    changed = _run_child(tmp_path, source, target, image, "read")
    assert changed["overlay"] != first["overlay"]
    assert changed["marker"] is None


def test_single_profile_adopts_overlay_from_flat_layout(monkeypatch, tmp_path):
    scratch = tmp_path / "scratch"
    legacy = scratch / "hermes-overlays" / "overlay-default"
    legacy.mkdir(parents=True)
    (legacy / "marker").write_text("pre-upgrade", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(singularity_env, "_get_scratch_dir", lambda: scratch)
    monkeypatch.setattr(singularity_env, "_ensure_singularity_available", lambda **_kw: "apptainer")
    monkeypatch.setattr(singularity_env.SingularityEnvironment, "_start_instance", lambda self: None)
    monkeypatch.setattr(singularity_env.SingularityEnvironment, "init_session", lambda self: None)
    image = tmp_path / "image.sif"
    image.write_bytes(b"sif")

    env = singularity_env.SingularityEnvironment(
        image=str(image), persistent_filesystem=True, task_id="default")

    assert env._overlay_dir == legacy
    assert (env._overlay_dir / "marker").read_text(encoding="utf-8") == "pre-upgrade"


@pytest.fixture
def started_target_instance(monkeypatch, tmp_path):
    """A real SingularityEnvironment started for target B from launch profile A."""
    from agent import secret_scope
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    source, target = _profiles(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    (other / ".env").write_text("OVERLAY_LOGIN=other-value\n", encoding="utf-8")
    image = tmp_path / "image.sif"
    image.write_bytes(b"sif")
    monkeypatch.setenv("HERMES_HOME", str(source))
    monkeypatch.setenv("OVERLAY_LOGIN", "source-value")
    monkeypatch.setattr("hermes_constants.get_process_hermes_home", lambda: source)
    monkeypatch.setattr(singularity_env, "_get_scratch_dir", lambda: tmp_path / "scratch")
    monkeypatch.setattr(singularity_env, "_ensure_singularity_available", lambda **_kw: "apptainer")
    monkeypatch.setattr(singularity_env.SingularityEnvironment, "init_session", lambda self: None)
    import tools.credential_files as credential_files
    monkeypatch.setattr(credential_files, "get_skills_directory_mount", lambda: [])

    calls: list[tuple[list[str], dict]] = []
    stop_returncodes: list[int] = []

    def fake_run(cmd, **kwargs):
        calls.append((list(cmd), dict(kwargs.get("env") or {})))
        code = stop_returncodes.pop(0) if cmd[1:3] == ["instance", "stop"] and stop_returncodes else 0
        return subprocess.CompletedProcess(cmd, code, "", "stop failed" if code else "")

    monkeypatch.setattr(singularity_env.subprocess, "run", fake_run)
    secret_scope.set_multiplex_active(True)
    scope_token = secret_scope.set_secret_scope(
        secret_scope.build_profile_secret_scope(target), profile_home=str(target))
    home_token = set_hermes_home_override(target)
    env = None
    try:
        env = singularity_env.SingularityEnvironment(image=str(image))
        assert env._instance_started is True
        assert calls[-1][1].get("OVERLAY_LOGIN") != "source-value"  # boundary applied at start
        yield env, calls, stop_returncodes, source, target, other
    finally:
        reset_hermes_home_override(home_token)
        secret_scope.reset_secret_scope(scope_token)
        secret_scope.set_multiplex_active(False)
        if env is not None:
            env._instance_started = False


def _stop_calls(calls, env):
    return [c for c in calls if c[0] == [env.executable, "instance", "stop", env.instance_id]]


@pytest.mark.parametrize("caller", ["foreign", "stale"])
def test_cleanup_stops_with_captured_authority_under_any_caller_scope(started_target_instance, caller):
    from agent import secret_scope
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    env, calls, _codes, _source, target, other = started_target_instance
    start_env = dict(calls[-1][1])

    if caller == "foreign":
        scope = secret_scope.build_profile_secret_scope(other)
        scope_token = secret_scope.set_secret_scope(scope, profile_home=str(other))
        home_token = set_hermes_home_override(other)
    else:
        # The owner's credentials rotate after start: the caller's installed scope is stale.
        (target / ".env").write_text("OVERLAY_LOGIN=rotated-value\nEXTRA=1\n", encoding="utf-8")
        scope_token = home_token = None
    try:
        env.cleanup()
    finally:
        if home_token is not None:
            reset_hermes_home_override(home_token)
        if scope_token is not None:
            secret_scope.reset_secret_scope(scope_token)

    stops = _stop_calls(calls, env)
    assert len(stops) == 1  # base PR head made zero stop calls here
    assert stops[0][1] == start_env
    assert stops[0][1].get("OVERLAY_LOGIN") != "other-value"
    assert env._instance_started is False


def test_failed_stop_keeps_the_handle_for_a_retry(started_target_instance):
    env, calls, stop_returncodes, *_ = started_target_instance
    stop_returncodes.extend([1, 0])

    env.cleanup()
    assert env._instance_started is True  # not recorded as stopped

    env.cleanup()
    assert len(_stop_calls(calls, env)) == 2
    assert env._instance_started is False
