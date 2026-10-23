"""Through the real terminal-environment registry: on a core that dropped its built-in backend, the
plugin's registration makes ``terminal.backend: singularity`` resolve to it, with the classification
every core policy site reads. Skipped on a core that still reserves the name (the plugin is inert there)."""

import pytest

from agent import terminal_env_registry as reg
import singularity_plugin

pytestmark = pytest.mark.skipif("singularity" in reg.BUILTIN_BACKEND_NAMES,
                                reason="this core still ships the built-in Singularity backend")


class _Ctx:
    def register_terminal_environment_provider(self, provider):
        reg.register_provider(provider)


@pytest.fixture
def registered():
    previous = reg.snapshot_registration("singularity")
    singularity_plugin.register(_Ctx())
    current = reg.get_provider("singularity")
    yield current
    reg.restore_registration("singularity", current, previous)


def test_backend_resolves_and_core_reads_its_flags(registered):
    assert registered is not None and registered.name == "singularity"
    assert "singularity" in reg.plugin_backend_names()
    assert reg.provider_flag("singularity", "is_container") is True
    assert reg.provider_flag("singularity", "supports_streams") is True
    from tools.approval import _should_skip_container_guards
    assert _should_skip_container_guards("singularity") is True
