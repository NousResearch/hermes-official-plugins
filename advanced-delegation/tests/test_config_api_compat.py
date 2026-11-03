"""Baseline public guarded writer and optional public transaction compatibility."""
import pytest
import hermes_yaml as yaml
from hermes_cli import config
from advanced_delegation_plugin import store


@pytest.mark.parametrize("seam", ["missing", "not-callable"])
def test_baseline_editor_and_preset_crud(tmp_path, monkeypatch, seam):
    if seam == "missing":
        monkeypatch.delattr(config, "update_user_config", raising=False)
    else:
        monkeypatch.setattr(config, "update_user_config", None, raising=False)
    path = tmp_path / "os-home" / ".hermes" / "config.yaml"
    path.write_text("# preserve comment\nmodel:\n  default: main\ndelegation:\n  backend: default\n")
    option = {"name": "local", "description": "Local work", "model": "local-model",
              "base_url": "http://localhost:8080/v1", "api_key": "preserve-key"}
    profiles = [{"name": "coder", "description": "Coding"}]
    store.set_mode("profile_routes")
    store.save_profiles(profiles, "coder")
    store.save_routes([option], "local")
    store.set_mode("model_map")
    store.save_preset("one")
    store.save_routes([{**option, "model": "changed"}], "local")
    store.save_preset("one", overwrite=True)
    store.save_preset("two")
    store.rename_preset("one", "renamed")
    store.use_preset("renamed")
    assert store.active_preset(store.delegation_section()) == "renamed"
    store.delete_preset("renamed")
    store.delete_preset("two")
    current = store.delegation_section()
    assert store.current_mode(current) == "model_map"
    assert store.list_presets(current) == []
    assert store.routes(current) == [{**option, "model": "changed"}]
    assert store.route_default(current) == "local"
    assert store.listed_profiles(current) == profiles
    assert store.profile_default(current) == "coder"
    assert yaml.safe_load(path.read_text())["model"] == {"default": "main"}
    assert "# preserve comment" in path.read_text()


@pytest.mark.parametrize("bad", ["delegation: [", "- not-a-mapping\n", "model: kept\nmodel: duplicate\n"])
def test_baseline_bad_config_refused_without_write(tmp_path, monkeypatch, bad):
    monkeypatch.delattr(config, "update_user_config", raising=False)
    path = tmp_path / "os-home" / ".hermes" / "config.yaml"
    path.write_text(bad)
    before = path.read_bytes()
    for operation in (lambda: store.set_mode("model_map"),
                      lambda: store.save_routes([], ""), lambda: store.save_profiles([], ""),
                      lambda: store.use_preset("one"), lambda: store.rename_preset("one", "two"),
                      lambda: store.delete_preset("one")):
        with pytest.raises(RuntimeError):
            operation()
        assert path.read_bytes() == before


def test_baseline_managed_config_refused(tmp_path, monkeypatch):
    monkeypatch.delattr(config, "update_user_config", raising=False)
    monkeypatch.setattr(config, "is_managed", lambda: True)
    monkeypatch.setattr(config, "format_managed_message", lambda action: f"managed: {action}")
    path = tmp_path / "os-home" / ".hermes" / "config.yaml"
    path.write_text("delegation:\n  backend: default\n")
    before = path.read_bytes()
    with pytest.raises(RuntimeError, match="managed: change delegation settings"):
        store.set_mode("model_map")
    assert path.read_bytes() == before


@pytest.mark.parametrize("error", [RuntimeError("transaction failed"), ValueError("mutation refused")])
def test_public_transaction_errors_never_fall_back(tmp_path, monkeypatch, error):
    path = tmp_path / "os-home" / ".hermes" / "config.yaml"
    path.write_text("delegation:\n  backend: default\n")
    before = path.read_bytes()
    calls = []

    def update(edit):
        calls.append(edit)
        raise error

    def forbidden(*args, **kwargs):
        pytest.fail("must not fall back after a transaction error")

    monkeypatch.setattr(config, "update_user_config", update, raising=False)
    monkeypatch.setattr(config, "require_readable_config_before_write", forbidden)
    monkeypatch.setattr(config, "atomic_config_write", forbidden)
    with pytest.raises(type(error), match=str(error)):
        store.set_mode("model_map")
    assert len(calls) == 1
    assert path.read_bytes() == before


@pytest.mark.parametrize("baseline", [False, True])
def test_writer_uses_host_bound_profile_not_launch_environment(tmp_path, monkeypatch, baseline):
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override

    if baseline:
        monkeypatch.delattr(config, "update_user_config", raising=False)
    launch = tmp_path / "os-home" / ".hermes" / "config.yaml"
    launch.write_text("delegation:\n  backend: default\n")
    work = tmp_path / "work"
    work.mkdir()
    target = work / "config.yaml"
    target.write_text("delegation:\n  backend: default\n")
    token = set_hermes_home_override(work)
    try:
        store.set_mode("model_map")
        assert store.current_mode(store.delegation_section()) == "model_map"
        assert yaml.safe_load(target.read_text())["delegation"]["backend"] == "model_map"
    finally:
        reset_hermes_home_override(token)
    assert launch.read_text() == "delegation:\n  backend: default\n"
    assert store.current_mode(store.delegation_section()) == "default"
