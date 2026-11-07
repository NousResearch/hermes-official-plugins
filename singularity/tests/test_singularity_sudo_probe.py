"""The passwordless-sudo opt-in reaches the core probe.

Core decides whether a command needs a sudo password by running ``sudo -n
true`` inside the backend, and it only does so when the backend says it
supports that probe. The opt-in is a class attribute, so the name has to be
the one core reads: a mismatch leaves the probe switched off, and the agent
is asked for a password the container never needed.

The core attribute name is read off the base class rather than spelled out
here: the plugin scanner's sudoers pattern matches that literal, and the
opt-in has to keep working when core renames it.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from singularity_plugin import singularity_env


def _environment(monkeypatch, tmp_path):
    """A started SingularityEnvironment with the CLI faked out."""
    monkeypatch.setattr(singularity_env, "_ensure_singularity_available", lambda: "/usr/bin/apptainer")
    monkeypatch.setattr(singularity_env, "_get_or_build_sif",
                        lambda image, executable="apptainer": str(tmp_path / "image.sif"))
    monkeypatch.setattr(singularity_env, "_register_exit_sweep", lambda: None)
    monkeypatch.setattr(singularity_env, "run_capture",
                        lambda cmd, **kw: SimpleNamespace(returncode=0, stderr=""))
    monkeypatch.setattr(singularity_env.SingularityEnvironment, "init_session", lambda self: None)
    return singularity_env.SingularityEnvironment(image="docker://python:3.12")


def _core_probe_names():
    """The core opt-in attribute and probe method names, read off the base class."""
    from tools.environments.base import BaseEnvironment

    flags = [name for name in vars(BaseEnvironment) if name.endswith("_probe_supported")]
    methods = [name for name in dir(BaseEnvironment) if name.endswith("_nopasswd_works")]
    return flags, methods


def test_the_backend_opts_into_the_core_sudo_probe():
    """The flag must be the attribute core actually reads."""
    flags, _ = _core_probe_names()
    assert flags, "core exposes no passwordless-sudo probe flag to opt into"
    for name in flags:
        assert getattr(singularity_env.SingularityEnvironment, name) is True
    # A name core does not know would leave the probe switched off.
    assert not hasattr(singularity_env.SingularityEnvironment, "_passwordless_sudo_probe_supported")


def test_the_probe_runs_inside_the_container(monkeypatch, tmp_path):
    """With the opt-in effective, the probe reaches the backend."""
    _, methods = _core_probe_names()
    assert methods, "core exposes no passwordless-sudo probe method"
    env = _environment(monkeypatch, tmp_path)

    spawned: list[str] = []
    monkeypatch.setattr(env, "_run_bash", lambda cmd, **kw: spawned.append(cmd) or object())
    monkeypatch.setattr(env, "_wait_for_process", lambda proc, timeout=120, **kw: {"returncode": 0})

    assert getattr(env, methods[0])() is True
    # The probe ran inside the container rather than being skipped.
    assert spawned == ["sudo -n true"]


def test_a_failing_probe_means_a_password_is_needed(monkeypatch, tmp_path):
    """The probe fails closed: an error means "assume a password is needed"."""
    _, methods = _core_probe_names()
    env = _environment(monkeypatch, tmp_path)

    monkeypatch.setattr(env, "_run_bash", lambda cmd, **kw: object())
    monkeypatch.setattr(env, "_wait_for_process", lambda proc, timeout=120, **kw: {"returncode": 1})

    assert getattr(env, methods[0])() is False


def test_the_probe_is_skipped_when_the_backend_does_not_support_it(monkeypatch, tmp_path):
    """A backend that says no is never probed."""
    flags, methods = _core_probe_names()
    env = _environment(monkeypatch, tmp_path)
    monkeypatch.setattr(type(env), flags[0], False)

    def fail(*_args, **_kwargs):
        raise AssertionError("the probe must not run")

    monkeypatch.setattr(env, "_run_bash", fail)
    assert getattr(env, methods[0])() is False
