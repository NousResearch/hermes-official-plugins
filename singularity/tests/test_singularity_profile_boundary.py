"""Profile-bound child environments for the plugin-owned Singularity backend.

These exercise the real Hermes subprocess sanitizer and secret-scope owners;
Apptainer itself is replaced with a recording process and is not claimed to run.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent import secret_scope
from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from singularity_plugin import singularity_env
from tools.environments.local import _HERMES_PROVIDER_ENV_BLOCKLIST

_AUTH_NAMES = (
    "APPTAINER_DOCKER_USERNAME",
    "APPTAINER_DOCKER_PASSWORD",
    "SINGULARITY_DOCKER_USERNAME",
    "SINGULARITY_DOCKER_PASSWORD",
    "DOCKER_USERNAME",
    "DOCKER_PASSWORD",
)
_BLOCKED = {
    "BWS_ACCESS_TOKEN": "fake-bootstrap",
    "OPENAI_API_KEY": "fake-provider",
    "GH_TOKEN": "fake-github",
    "AUXILIARY_VISION_API_KEY": "fake-auxiliary",
    "GATEWAY_RELAY_SECRET": "fake-relay",
}
_TUNNELS = {
    "APPTAINERENV_BWS_ACCESS_TOKEN": "fake-tunneled-bws",
    "SINGULARITYENV_GH_TOKEN": "fake-tunneled-github",
    "APPTAINERENV_OPENAI_API_KEY": "fake-tunneled-provider",
}


def _auth_values(prefix: str) -> dict[str, str]:
    return {name: f"{prefix}-{index}" for index, name in enumerate(_AUTH_NAMES)}


def _write_profile(home: Path, values: dict[str, str] | None = None) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text("{}\n", encoding="utf-8")
    (home / ".env").write_text(
        "".join(f"{key}={value}\n" for key, value in (values or {}).items()),
        encoding="utf-8",
    )


def _capture_child_environment(monkeypatch, tmp_path: Path):
    """Replace image-build execution but inspect its env in a real isolated child."""
    real_run = subprocess.run
    observations: list[dict[str, str | None]] = []
    names = [*_AUTH_NAMES, *_BLOCKED, *_TUNNELS, "DB_PASSWORD", "HERMES_TEST_BENIGN"]
    code = "import json,os; print(json.dumps({n:os.environ.get(n) for n in " + repr(names) + "}))"

    def fake_run(cmd, **kwargs):
        if len(cmd) >= 3 and cmd[1] == "build":
            child = real_run(
                [sys.executable, "-I", "-c", code],
                env=kwargs.get("env"), capture_output=True, text=True,
                encoding="utf-8", timeout=20, check=True,
            )
            observations.append(json.loads(child.stdout))
            Path(cmd[2]).write_bytes(b"test-sif")
            return subprocess.CompletedProcess(cmd, 0, "ok\n", "")
        return subprocess.CompletedProcess(cmd, 0, "ok\n", "")

    monkeypatch.setattr(singularity_env.subprocess, "run", fake_run)
    monkeypatch.setattr(singularity_env, "_get_apptainer_cache_dir", lambda: tmp_path / "cache")
    return observations


def test_spawn_environment_uses_core_filter_and_preserves_benign_controls(monkeypatch):
    for key in _HERMES_PROVIDER_ENV_BLOCKLIST:
        monkeypatch.setenv(key, "fake-blocked")
    for key, value in {**_BLOCKED, **_TUNNELS}.items():
        monkeypatch.setenv(key, value)
    for key, value in {
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "DB_PASSWORD": "user-shell-password",
        "HERMES_TEST_BENIGN": "keep-me",
    }.items():
        monkeypatch.setenv(key, value)

    env = singularity_env._singularity_subprocess_env()

    assert not (set(_HERMES_PROVIDER_ENV_BLOCKLIST) & env.keys())
    assert not (set(_BLOCKED) | set(_TUNNELS)) & env.keys()
    assert env["PATH"].split(os.pathsep)[-2:] == ["/usr/bin", "/bin"]
    assert env["LANG"] == "C.UTF-8"
    assert env["DB_PASSWORD"] == "user-shell-password"
    assert env["HERMES_TEST_BENIGN"] == "keep-me"


def test_singularity_exec_reaches_shared_sanitized_popen_boundary(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "fake-provider")
    calls = []

    class DummyProcess:
        stdin = stdout = stderr = None
        returncode = 0
        pid = 12345

        def poll(self):
            return self.returncode

    def fake_popen(cmd, **kwargs):
        calls.append((list(cmd), kwargs))
        return DummyProcess()

    monkeypatch.setattr("tools.environments.base.subprocess.Popen", fake_popen)
    env = object.__new__(singularity_env.SingularityEnvironment)
    env.executable = "apptainer"
    env.instance_id = "hermes_test"
    env._instance_started = True
    env._owner_home = None
    env._source_home = None
    env._profile_env_boundary = None

    env._run_bash("true")

    assert len(calls) == 1
    assert calls[0][0] == ["apptainer", "exec", "instance://hermes_test", "bash", "-c", "true"]
    assert "OPENAI_API_KEY" not in calls[0][1]["env"]


def test_lifecycle_subprocesses_receive_sanitized_environments(monkeypatch, tmp_path):
    monkeypatch.setenv("BWS_ACCESS_TOKEN", "fake-bootstrap")
    monkeypatch.setenv("OPENAI_API_KEY", "fake-provider")
    monkeypatch.setenv("DB_PASSWORD", "keep-user-password")
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((list(cmd), kwargs))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(singularity_env.subprocess, "run", fake_run)
    monkeypatch.setattr(singularity_env, "_find_singularity_executable", lambda: "apptainer")
    monkeypatch.setattr(singularity_env, "_get_or_build_sif", lambda image, *a, **kw: image)
    monkeypatch.setattr(singularity_env, "_get_scratch_dir", lambda: tmp_path / "scratch")


    assert singularity_env._ensure_singularity_available() == "apptainer"
    assert "BWS_ACCESS_TOKEN" not in calls[-1][1]["env"]
    assert "OPENAI_API_KEY" not in calls[-1][1]["env"]
    assert calls[-1][1]["env"]["DB_PASSWORD"] == "keep-user-password"

    import tools.credential_files as credential_files
    monkeypatch.setattr(credential_files, "get_credential_file_mounts", lambda: [])
    monkeypatch.setattr(credential_files, "get_skills_directory_mount", lambda: [])
    monkeypatch.setattr(singularity_env.SingularityEnvironment, "init_session", lambda self: None)
    env = singularity_env.SingularityEnvironment(image="image.sif")
    assert "BWS_ACCESS_TOKEN" not in calls[-1][1]["env"]
    assert "OPENAI_API_KEY" not in calls[-1][1]["env"]
    assert calls[-1][1]["env"]["DB_PASSWORD"] == "keep-user-password"

    env.cleanup()
    assert calls[-1][0] == ["apptainer", "instance", "stop", env.instance_id]
    assert "BWS_ACCESS_TOKEN" not in calls[-1][1]["env"]
    assert "OPENAI_API_KEY" not in calls[-1][1]["env"]
    assert calls[-1][1]["env"]["DB_PASSWORD"] == "keep-user-password"


def test_image_build_explicitly_grants_only_registry_auth(monkeypatch, tmp_path):
    monkeypatch.setenv("BWS_ACCESS_TOKEN", "fake-bootstrap")
    monkeypatch.setenv("OPENAI_API_KEY", "fake-provider")
    monkeypatch.setenv("GH_TOKEN", "fake-github")
    monkeypatch.setenv("DB_PASSWORD", "keep-user-password")
    monkeypatch.setenv("HERMES_TEST_BENIGN", "keep-me")
    ambient = _auth_values("ambient")
    for key, value in ambient.items():
        monkeypatch.setenv(key, value)
    override = _AUTH_NAMES[0]
    token = secret_scope.set_secret_scope({override: "scope-override"})
    observations = _capture_child_environment(monkeypatch, tmp_path)
    try:
        result = singularity_env._get_or_build_sif("docker://example.invalid/private:latest", "apptainer")
    finally:
        secret_scope.reset_secret_scope(token)

    assert Path(result).is_file()
    assert len(observations) == 1
    child = observations[0]
    assert child[override] == "scope-override"
    for key, value in ambient.items():
        if key != override:
            assert child[key] == value
    for key in (*_BLOCKED, *_TUNNELS):
        assert child[key] is None
    assert child["DB_PASSWORD"] == "keep-user-password"
    assert child["HERMES_TEST_BENIGN"] == "keep-me"


@pytest.mark.parametrize("target_mode", ["absent", "different"])
def test_image_build_registry_auth_is_bound_to_target_profile(
    target_mode, monkeypatch, tmp_path
):
    source, target = tmp_path / "source", tmp_path / "target"
    source_values = _auth_values("source-A")
    target_values = {} if target_mode == "absent" else _auth_values("target-B")
    _write_profile(source, source_values)
    _write_profile(target, target_values)
    monkeypatch.setenv("HERMES_HOME", str(source))
    monkeypatch.setattr("hermes_constants.get_process_hermes_home", lambda: source)
    for key, value in source_values.items():
        monkeypatch.setenv(key, value)
    set_multiplex_active = secret_scope.set_multiplex_active
    set_multiplex_active(True)
    home_token = set_hermes_home_override(target)
    scope = secret_scope.build_profile_secret_scope(target)
    scope_token = secret_scope.set_secret_scope(scope, profile_home=str(target))
    observations = _capture_child_environment(monkeypatch, tmp_path)
    try:
        result = singularity_env._get_or_build_sif(
            "docker://example.invalid/scoped:latest", "apptainer",
            owner_home=target, source_home=source, policy_generation=scope.generation,
        )
    finally:
        secret_scope.reset_secret_scope(scope_token)
        reset_hermes_home_override(home_token)
        set_multiplex_active(False)

    assert Path(result).is_file()
    assert len(observations) == 1
    child = observations[0]
    for key, source_value in source_values.items():
        assert child[key] != source_value
        if target_mode == "absent":
            assert child[key] is None
        else:
            assert child[key] == target_values[key]


def test_multiplex_image_build_without_a_bound_owner_fails_closed(monkeypatch, tmp_path):
    _capture_child_environment(monkeypatch, tmp_path)
    secret_scope.set_multiplex_active(True)
    try:
        with pytest.raises(RuntimeError, match="without an active target-profile secret scope"):
            singularity_env._get_or_build_sif("docker://example.invalid/unscoped:latest", "apptainer")
    finally:
        secret_scope.set_multiplex_active(False)
