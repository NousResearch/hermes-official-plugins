"""The provider: classification flags core reads, image resolution with the keys users already have,
container limits passed through, and registration that stays out of the way of a core that still
ships the built-in backend."""

from types import SimpleNamespace

import pytest

import singularity_plugin
from singularity_plugin import provider as provider_mod
from singularity_plugin.provider import SingularityProvider, as_apptainer_ref, resolve_image


def _write_config(home, text):
    (home / "config.yaml").write_text(text, encoding="utf-8")


def test_classification_matches_the_former_built_in():
    p = SingularityProvider()
    assert p.name == "singularity"
    assert p.is_remote and p.is_container and p.supports_streams
    assert p.skip_container_guards  # disposable sandbox: no dangerous-command prompts, as before
    assert p.cache_path_base is None  # Apptainer paths are host paths; nothing is translated
    assert p.default_cwd is None  # core's container default (/root)
    assert p.strip_env_keys == frozenset()
    assert not p.session_isolated_when_nonpersistent
    assert p.env_description == "a Singularity container (Linux)"


def test_config_yaml_image_wins_over_the_env_var(_hermes_home, monkeypatch):
    _write_config(_hermes_home, "terminal:\n  singularity_image: docker://ubuntu:24.04\n")
    monkeypatch.setenv("TERMINAL_SINGULARITY_IMAGE", "docker://alpine:3")
    assert resolve_image(None) == "docker://ubuntu:24.04"


def test_env_var_image_when_config_has_none(monkeypatch):
    monkeypatch.setattr(provider_mod, "_terminal_config", dict)
    monkeypatch.setenv("TERMINAL_SINGULARITY_IMAGE", "/images/tools.sif")
    assert resolve_image("") == "/images/tools.sif"


def test_default_is_hermes_sandbox_image(monkeypatch):
    from hermes_cli.config_defaults import DEFAULT_SANDBOX_IMAGE
    monkeypatch.setattr(provider_mod, "_terminal_config", dict)
    assert resolve_image(None) == f"docker://{DEFAULT_SANDBOX_IMAGE}"


def test_per_task_override_wins(_hermes_home):
    _write_config(_hermes_home, "terminal:\n  singularity_image: docker://ubuntu:24.04\n")
    assert resolve_image("docker://python:3.12") == "docker://python:3.12"


@pytest.mark.parametrize("given, expected", [
    ("python:3.12", "docker://python:3.12"),
    ("nousresearch/hermes-sandbox:desktop", "docker://nousresearch/hermes-sandbox:desktop"),
    ("docker://python:3.12", "docker://python:3.12"),
    ("library://alpine:latest", "library://alpine:latest"),
    ("/images/tools.sif", "/images/tools.sif"),
])
def test_bare_references_become_docker_urls(given, expected):
    assert as_apptainer_ref(given) == expected


def test_existing_sandbox_directory_is_used_as_given(tmp_path):
    assert as_apptainer_ref(str(tmp_path)) == str(tmp_path)


def test_create_environment_passes_limits_and_image(monkeypatch):
    from singularity_plugin import singularity_env
    seen = {}
    monkeypatch.setattr(singularity_env, "SingularityEnvironment", lambda **kw: seen.update(kw) or SimpleNamespace())
    monkeypatch.setattr(singularity_env, "check_disk_usage_warning", lambda: False)
    SingularityProvider().create_environment(
        cwd="/root", timeout=60, task_id="t1", image="python:3.12",
        container_config={"container_cpu": 2, "container_memory": 1024, "container_persistent": False,
                          "docker_volumes": ["ignored"]},
        future_kwarg=True)
    assert seen == {"image": "docker://python:3.12", "cwd": "/root", "timeout": 60, "cpu": 2, "memory": 1024,
                    "disk": 51200, "persistent_filesystem": False, "task_id": "t1"}


def test_requirements_and_probe_follow_the_binary(monkeypatch):
    p = SingularityProvider()
    monkeypatch.setattr(provider_mod.shutil, "which", lambda name: None)
    assert not p.is_available() and not p.check_requirements({})
    assert p.probe()[0] == "needs_setup"
    assert p.doctor_checks()[0][0] is False
    monkeypatch.setattr(provider_mod.shutil, "which", lambda name: f"/usr/bin/{name}" if name == "apptainer" else None)
    monkeypatch.setattr(provider_mod.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0))
    assert p.is_available() and p.check_requirements({}) and p.probe() == ("ready", "")
    monkeypatch.setattr(provider_mod.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1))
    assert not p.check_requirements({})


class _Ctx:
    def __init__(self):
        self.providers = []

    def register_terminal_environment_provider(self, provider):
        self.providers.append(provider)


def test_registers_the_backend_on_a_core_without_it(monkeypatch):
    monkeypatch.setattr(singularity_plugin, "_core_ships_singularity", lambda: False)
    ctx = _Ctx()
    singularity_plugin.register(ctx)
    assert [p.name for p in ctx.providers] == ["singularity"]


def test_stays_inactive_on_a_core_that_still_ships_it(monkeypatch):
    monkeypatch.setattr(singularity_plugin, "_core_ships_singularity", lambda: True)
    ctx = _Ctx()
    singularity_plugin.register(ctx)
    assert ctx.providers == []


def test_core_detection_reads_cores_own_environments_package(monkeypatch, tmp_path):
    import inspect

    from tools.environments.base import BaseEnvironment
    real = inspect.getfile(BaseEnvironment)
    ships = (__import__("pathlib").Path(real).parent / "singularity.py").is_file()
    assert singularity_plugin._core_ships_singularity() is ships
    fake = tmp_path / "base.py"
    fake.write_text("")
    monkeypatch.setattr(singularity_plugin.inspect, "getfile", lambda obj: str(fake))
    assert singularity_plugin._core_ships_singularity() is False
    (tmp_path / "singularity.py").write_text("")
    assert singularity_plugin._core_ships_singularity() is True
