"""bubblewrap is a host-path backend. As a plugin it gets that classification from the provider's
flags, which core consults at each policy site: the approval gate treats it like local (the cwd and
read-write binds are real host directories, so the sandbox is not disposable), file tools resolve
host paths, the prompt builder keeps the host hints, and nothing remaps cache paths.

No test here spawns bwrap.
"""

import pytest

from tools import approval


@pytest.fixture(autouse=True)
def _registered(bwrap_provider):
    yield


class TestClassificationFlags:
    def test_provider_is_neither_remote_nor_a_container(self, bwrap_provider):
        provider, _settings = bwrap_provider
        assert provider.is_remote is False
        assert provider.is_container is False
        assert provider.skip_container_guards is False
        assert provider.cache_path_base is None
        assert provider.strip_env_keys == frozenset()

    def test_core_sites_classify_it_as_host_path(self):
        from agent.prompt_builder import _plugin_backend_is_remote
        from tools.env_probe import _plugin_backend_is_remote as probe_is_remote
        from tools.terminal_tool_config import _is_container_backend

        assert _is_container_backend("bubblewrap") is False
        assert _plugin_backend_is_remote("bubblewrap") is False
        assert probe_is_remote("bubblewrap") is False


class TestApprovalGate:
    @pytest.mark.parametrize("has_host_access", [True, False])
    def test_container_guards_are_not_skipped(self, has_host_access):
        assert approval._should_skip_container_guards("bubblewrap", has_host_access) is False
        assert approval._should_skip_container_guards("local", has_host_access) is False

    def test_hardline_command_gets_the_local_verdict(self):
        command = "rm -rf /"
        assert approval.detect_hardline_command(command)[0] is True
        local = approval.check_dangerous_command(command, "local")
        bubblewrap = approval.check_dangerous_command(command, "bubblewrap")
        assert bubblewrap == local
        assert local["approved"] is False

    def test_dangerous_command_reaches_the_approval_gate_as_for_local(self, monkeypatch):
        command = "rm -rf ./build"
        assert approval.detect_dangerous_command(command)[0] is True
        reached = []

        def fake_gate(**kwargs):
            reached.append(kwargs["pattern_key"])
            return {"approved": False, "message": "gate"}

        monkeypatch.setattr(approval, "_run_approval_gate", fake_gate)
        local = approval.check_dangerous_command(command, "local")
        bubblewrap = approval.check_dangerous_command(command, "bubblewrap")
        assert bubblewrap == local == {"approved": False, "message": "gate"}
        assert len(reached) == 2 and reached[0] == reached[1]
        # A sandboxed container backend skips the gate entirely.
        assert approval.check_dangerous_command(command, "docker")["approved"] is True
        assert len(reached) == 2
