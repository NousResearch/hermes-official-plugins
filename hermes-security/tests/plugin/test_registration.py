import json
import sys
from pathlib import Path
from types import SimpleNamespace


class FakeContext:
    def __init__(self, directory):
        self.state = SimpleNamespace(data_dir=directory)
        self.profile_name = "test"
        self.tools = {}
        self.commands = {}
        self.cli = {}
        self.skills = {}
        self.hooks = {}

    def register_hook(self, name, callback):
        self.hooks.setdefault(name, []).append(callback)

    def register_tool(self, name, toolset, schema, handler, check_fn=None, requires_env=None,
                      is_async=False, description="", emoji="", override=False):
        self.tools[name] = (toolset, schema, handler)

    def register_command(self, name, handler, description="", args_hint="", argument_mode=None):
        self.commands[name] = handler

    def register_cli_command(self, name, help, setup_fn, handler_fn=None, description=""):
        self.cli[name] = (setup_fn, handler_fn)

    def register_skill(self, name, path, description="", frontmatter=None):
        assert isinstance(path, Path) and path.is_file()
        self.skills[name] = path


def test_registration_is_lazy_and_exact(tmp_path, monkeypatch):
    from hermes_security import plugin
    monkeypatch.setattr(plugin, "get_service", lambda ctx=None: (_ for _ in ()).throw(AssertionError("eager service")))
    ctx = FakeContext(tmp_path)
    plugin.register(ctx)
    manifest = json.loads((Path(__file__).parents[2] / "plugin.yaml").read_text())
    assert set(ctx.tools) == set(manifest["provides_tools"])
    assert len(ctx.tools) == 9
    for name, (toolset, schema, handler) in ctx.tools.items():
        assert toolset == "security" and callable(handler)
        assert schema["name"] == name and schema["description"]
        assert schema["parameters"]["type"] == "object"
        assert schema["parameters"]["additionalProperties"] is False
        assert set(schema["parameters"]["required"]) <= set(schema["parameters"]["properties"])
        json.dumps(schema, allow_nan=False)
    assert set(ctx.skills) == {"security-audit", "threat-model", "security-diff-review", "validate-finding",
                              "fix-finding", "verify-fix", "define-security-policy"}
    assert set(ctx.commands) == {"security"}
    # `hermes security` belongs to Hermes core; the plugin CLI must not collide with it.
    assert set(ctx.cli) == {"security-review"}
    # Turn and worker hooks drive automatic continue; the plugin owns no timers or threads.
    assert set(ctx.hooks) == {"on_session_end", "subagent_start", "subagent_stop"}
    assert all(len(v) == 1 and callable(v[0]) for v in ctx.hooks.values())


def test_cached_service_is_profile_scoped(tmp_path, monkeypatch):
    from hermes_security import plugin
    calls = []
    def factory(directory, *, profile):
        obj = object()
        calls.append((directory, profile, obj))
        return obj
    monkeypatch.setitem(sys.modules, "hermes_security.service", SimpleNamespace(SecurityService=factory))
    monkeypatch.setitem(sys.modules, "hermes_security.config", SimpleNamespace(resolve_data_dir=lambda ctx=None: ctx.state.data_dir))
    monkeypatch.setattr(plugin, "_SERVICES", {})
    a = FakeContext(tmp_path / "a")
    b = FakeContext(tmp_path / "b")
    assert plugin.get_service(a) is plugin.get_service(a)
    assert plugin.get_service(b) is not plugin.get_service(a)
    assert len(calls) == 2
    assert calls[0][:2] == (tmp_path / "a", "test")


def test_old_host_without_cli_registration(tmp_path):
    from hermes_security.plugin import register
    ctx = FakeContext(tmp_path)
    ctx.register_cli_command = None
    ctx.register_hook = None
    register(ctx)
    assert len(ctx.tools) == 9 and ctx.hooks == {}


def test_loads_under_synthetic_hermes_module_name(monkeypatch):
    import importlib.util
    import subprocess
    import sys
    import textwrap
    root = Path(__file__).resolve().parents[2]
    code = textwrap.dedent(f"""
        import importlib.util, sys, types
        sys.path[:] = [p for p in sys.path if p not in ({str(root)!r}, '')]
        sys.modules['hermes_plugins'] = types.ModuleType('hermes_plugins'); sys.modules['hermes_plugins'].__path__ = []
        spec = importlib.util.spec_from_file_location('hermes_plugins.hermes_security_test', {str(root / '__init__.py')!r},
                                                      submodule_search_locations=[{str(root)!r}])
        mod = importlib.util.module_from_spec(spec); sys.modules[spec.name] = mod; spec.loader.exec_module(mod)
        class Ctx:
            def __init__(self): self.tools = []; self.hooks = []
            def register_tool(self, name, *a, **k): self.tools.append(name)
            def register_hook(self, name, cb): assert callable(cb); self.hooks.append(name)
            def __getattr__(self, name): return lambda *a, **k: None
        ctx = Ctx(); mod.register(ctx)
        assert 'hermes_security' not in sys.modules, 'absolute import leaked'
        print(len(ctx.tools), sorted(ctx.hooks))
    """)
    out = subprocess.run([sys.executable, "-c", code], cwd="/", capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.strip() == "9 ['on_session_end', 'subagent_start', 'subagent_stop']"
