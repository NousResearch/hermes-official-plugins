"""Every spawn of the environment is the prlimit + bwrap argv around the shell, and the tracked cwd
never follows a command onto / or into the environment's own state dir.

Hermes core has no argv seam in LocalEnvironment, so the environment overrides ``_run_bash``; these
tests pin what that override must keep from the local one (login bootstrap included, no pre-exec
callback, cwd recovery before the wrap).
"""

import os
from unittest.mock import MagicMock, patch

import pytest

import tools.environments.local as local_mod
from tools.environments.base_session_env import (
    _secret_fd_token, _secret_gen_token, _secret_restore_token, _secret_src_token)
from bubblewrap_plugin import sandbox
from bubblewrap_plugin.sandbox import BubblewrapEnvironment

FAKE_BASH = "/opt/fake/bash"


@pytest.fixture
def env_factory(tmp_path, monkeypatch):
    monkeypatch.setenv("TERMINAL_SANDBOX_DIR", str(tmp_path / "sandboxes"))
    monkeypatch.setattr(sandbox, "_probed_bwrap_path", "/usr/bin/bwrap")
    # The process limit probe spawns bwrap once per process; pin its result so it never runs under popen_spy.
    monkeypatch.setattr(sandbox, "_process_limit_scoped", True)
    work = tmp_path / "work"
    work.mkdir()
    made = []

    def make():
        with patch.object(local_mod.LocalEnvironment, "init_session", autospec=True, return_value=None):
            env = BubblewrapEnvironment(cwd=str(work), timeout=10)
        made.append(env)
        return env

    yield make, work
    for env in made:
        env._remove_state()


@pytest.fixture
def popen_spy():
    fake_proc = MagicMock()
    fake_proc.pid = os.getpid()
    # The fake proc's pid is this process: it must never be handed to the owner-death supervisor.
    with patch.object(sandbox.subprocess, "Popen", return_value=fake_proc) as spy, \
            patch.object(sandbox, "_find_bash", return_value=FAKE_BASH), \
            patch.object(sandbox, "_resolve_shell_init_files", return_value=[]), \
            patch.object(sandbox, "own_process_groups"):
        yield spy


class TestRunBashWraps:
    def test_command_spawn_is_prlimit_then_bwrap_then_the_shell(self, env_factory, popen_spy):
        make, work = env_factory
        env = make()
        env._run_bash("echo hi")
        args, kwargs = popen_spy.call_args
        argv = args[0]
        assert argv[0] == env._prlimit_path
        i = argv.index(env._bwrap_path)
        assert argv[argv.index("--chdir") + 1] == str(work)
        assert argv[-3:] == [FAKE_BASH, "-c", "echo hi"]
        assert i < argv.index("--") < len(argv) - 3
        assert "preexec_fn" not in kwargs
        assert kwargs["start_new_session"] is True

    def test_login_bootstrap_is_wrapped_too(self, env_factory, popen_spy):
        make, _work = env_factory
        env = make()
        env._run_bash("true", login=True)
        argv = popen_spy.call_args[0][0]
        assert env._bwrap_path in argv
        # The login script also re-adds the PATH entries a resetting profile drops, as LocalEnvironment does.
        assert argv[-4:-1] == [FAKE_BASH, "-l", "-c"]
        assert argv[-1].endswith("\ntrue")

    def test_wrap_sees_the_recovered_cwd(self, env_factory, popen_spy, tmp_path):
        make, work = env_factory
        env = make()
        gone = work / "gone"
        gone.mkdir()
        env.cwd = str(gone)
        gone.rmdir()
        env._run_bash("pwd")
        argv = popen_spy.call_args[0][0]
        assert argv[argv.index("--chdir") + 1] == str(work)
        assert popen_spy.call_args[1]["cwd"] == str(work)
        # As in core, only execute's cwd marker moves the tracked cwd, never the spawn.
        assert env.cwd == str(gone)

    def test_secret_pipe_tokens_become_fds_passed_to_the_shell(self, env_factory, popen_spy):
        make, _work = env_factory
        env = make()
        sid = env._session_id
        tokens = (_secret_fd_token(sid), _secret_gen_token(sid), _secret_src_token(sid), _secret_restore_token(sid))
        env._run_bash(" ".join(tokens))
        args, kwargs = popen_spy.call_args
        script = args[0][-1]
        assert not any(token in script for token in tokens)
        assert len(kwargs["pass_fds"]) == 3
        assert all(str(fd) in script.split() for fd in kwargs["pass_fds"])

    def test_wrap_command_passes_the_cwd_fallback_through(self, env_factory):
        make, work = env_factory
        env = make()
        assert env._cwd_fallback_marker in env._wrap_command("true", str(work), cwd_fallback=True)
        assert env._cwd_fallback_marker not in env._wrap_command("true", str(work))


class TestTrackedCwd:
    def test_root_and_state_dir_are_not_followed(self, env_factory):
        make, work = env_factory
        env = make()
        marker = env._cwd_marker
        for elsewhere in ("/", os.path.join(env.get_temp_dir(), "hermes_exec_x")):
            result = {"output": f"out\n{marker}{elsewhere}{marker}\n"}
            env._update_cwd(result)
            assert env.cwd == str(work)
            assert result["output"].strip() == "out"

    def test_a_real_cd_is_followed(self, env_factory, tmp_path):
        make, work = env_factory
        env = make()
        sub = work / "sub"
        sub.mkdir()
        marker = env._cwd_marker
        env._update_cwd({"output": f"{marker}{sub}{marker}\n"})
        assert env.cwd == str(sub)

    def test_replaced_cwd_maps_to_the_tracked_cwd(self, env_factory, monkeypatch):
        make, work = env_factory
        env = make()
        env._replaced_cwd = "/root"
        seen = []
        monkeypatch.setattr(local_mod.LocalEnvironment, "execute",
                            lambda self, command, cwd="", **kw: seen.append(cwd) or {"output": "", "returncode": 0})
        env.execute("true", cwd="/root")
        env.execute("true", cwd=str(work))
        assert seen == ["", str(work)]
