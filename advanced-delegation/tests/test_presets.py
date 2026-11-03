"""Named route snapshots: isolated config writes and shared consumer API."""
from copy import deepcopy

import pytest
import hermes_yaml as yaml

from advanced_delegation_plugin import store


OPTION = {"name": "local", "description": "Private work", "model": "qwen",
          "base_url": "http://localhost:8080/v1", "api_key": "test-key",
          "api_mode": "chat_completions", "reasoning_effort": "high", "service_tier": "normal",
          "fallback_providers": [{"provider": "openrouter", "model": "backup"}]}


@pytest.fixture(params=["available", "baseline"])
def config_file(tmp_path, monkeypatch, request):
    if request.param == "baseline":
        monkeypatch.delattr("hermes_cli.config.update_user_config", raising=False)
    path = tmp_path / "os-home" / ".hermes" / "config.yaml"
    path.write_text(yaml.safe_dump({"model": {"default": "main"}, "delegation": {
        "backend": "model_map", "max_concurrent_children": 2,
        "backends": {"model_map": {"options": [deepcopy(OPTION)], "default": "local", "extra": 7},
                     "profile_routes": {"profiles": [{"name": "coder"}]}}}}))
    return path


def section(path):
    return yaml.safe_load(path.read_text())["delegation"]


def test_snapshot_round_trip_keeps_all_route_fields(config_file):
    before = section(config_file)
    store.save_preset("private")
    saved = section(config_file)
    assert store.list_presets(saved) == ["private"]
    assert store.get_preset(saved, "private") == {"options": [OPTION], "default": "local"}
    assert store.active_preset(saved) == "private"
    assert "Active preset: private" in store.describe(saved)
    drifted = deepcopy(saved)
    drifted["backends"]["model_map"]["options"][0]["model"] = "different"
    assert "Active preset:" not in store.describe(drifted)
    assert saved["backends"]["profile_routes"] == before["backends"]["profile_routes"]
    assert saved["backends"]["model_map"]["extra"] == 7
    assert yaml.safe_load(config_file.read_text())["model"] == {"default": "main"}


def test_crud_collision_overwrite_and_use_are_atomic(config_file, monkeypatch):
    import hermes_cli.config as core
    writes = []
    original = core.atomic_config_write

    def record(path, data):
        writes.append(deepcopy(data))
        original(path, data)

    monkeypatch.setattr(core, "atomic_config_write", record)
    store.save_preset("one")
    store.save_preset("two")
    before = config_file.read_bytes()
    with pytest.raises(ValueError, match="overwrite"):
        store.save_preset("one")
    with pytest.raises(ValueError, match="exists"):
        store.rename_preset("one", "two")
    assert config_file.read_bytes() == before
    store.save_routes([{**OPTION, "model": "changed"}], "")
    store.save_preset("two", overwrite=True)
    store.set_mode("profile_routes")
    count = len(writes)
    store.use_preset("one")
    assert len(writes) == count + 1
    current = section(config_file)
    assert current["backend"] == "model_map"
    assert current["backends"]["model_map"]["options"] == [OPTION]
    assert current["backends"]["model_map"]["default"] == "local"
    assert store.active_preset(current) == "one"
    store.rename_preset("one", "renamed")
    assert store.active_preset(section(config_file)) == "renamed"
    assert store.list_presets(section(config_file)) == ["renamed", "two"]
    store.delete_preset("renamed")
    assert store.active_preset(section(config_file)) == ""
    assert store.list_presets(section(config_file)) == ["two"]
    assert section(config_file)["backends"]["model_map"]["options"] == [OPTION]
    store.use_preset("two")
    assert section(config_file)["backends"]["model_map"]["options"][0]["model"] == "changed"
    store.delete_preset("two")
    assert store.list_presets(section(config_file)) == []


def test_readers_and_use_are_deeply_isolated(config_file):
    store.save_preset("one")
    current = section(config_file)
    snapshot = store.get_preset(current, "one")
    snapshot["options"][0]["fallback_providers"][0]["model"] = "mutated"
    assert store.get_preset(current, "one")["options"] == [OPTION]
    current["backends"]["model_map"]["options"][0]["fallback_providers"].clear()
    assert store.get_preset(current, "one")["options"] == [OPTION]
    store.use_preset("one")
    current = section(config_file)
    current["backends"]["model_map"]["options"][0]["fallback_providers"].clear()
    assert store.get_preset(current, "one")["options"] == [OPTION]


@pytest.mark.parametrize("change", [
    {"model": "edited"}, {"api_key": "new-key"}, {"api_mode": "responses"},
    {"reasoning_effort": "low"}, {"service_tier": ""},
    {"fallback_providers": [{"provider": "openrouter", "model": "other"}]},
])
def test_active_preset_detects_every_route_field_drift(config_file, change):
    store.save_preset("one")
    current = section(config_file)
    current["backends"]["model_map"]["options"][0].update(change)
    assert store.active_preset(current) == ""
    assert store.active_preset(section(config_file)) == "one"


def test_active_preset_detects_default_backend_and_corrupt_marker(config_file):
    store.save_preset("one")
    current = section(config_file)
    current["backends"]["model_map"]["default"] = ""
    assert store.active_preset(current) == ""
    store.set_mode("default")
    assert store.active_preset(section(config_file)) == ""
    store.save_preset("not-active")
    assert section(config_file)["backend"] == "default"
    assert store.active_preset(section(config_file)) == ""
    for marker in ([], {}, 1, "missing"):
        current = section(config_file)
        current["backend"] = "model_map"
        current["backends"]["model_map"]["active_preset"] = marker
        assert store.active_preset(current) == ""


@pytest.mark.parametrize("name", ["", "two words", " leading", "trailing ", "a\n", "../x", "-x", "a" * 65, None, 3])
def test_invalid_names_never_write(config_file, name):
    before = config_file.read_bytes()
    for operation in (lambda: store.save_preset(name), lambda: store.use_preset(name),
                      lambda: store.rename_preset(name, "ok"), lambda: store.delete_preset(name)):
        with pytest.raises(ValueError):
            operation()
        assert config_file.read_bytes() == before


@pytest.mark.parametrize("operation", [
    lambda: store.get_preset({}, "missing"), lambda: store.use_preset("missing"),
    lambda: store.rename_preset("missing", "new"), lambda: store.delete_preset("missing"),
])
def test_missing_presets_never_write(config_file, operation):
    before = config_file.read_bytes()
    with pytest.raises(ValueError, match="Unknown"):
        operation()
    assert config_file.read_bytes() == before


@pytest.mark.parametrize("patch", [
    {"options": None}, {"options": {}}, {"options": [None]},
    {"options": [{**OPTION, "name": "x\n"}]},
    {"options": [OPTION, OPTION]}, {"options": [{**OPTION, "model": 3}]},
    {"options": [{**OPTION, "description": ""}]},
    {"options": [{**OPTION, "reasoning_effort": "bogus"}]},
    {"options": [{**OPTION, "service_tier": "bogus"}]},
    {"options": [{**OPTION, "fallback_providers": "wrong"}]},
    {"options": [{**OPTION, "fallback_providers": [None]}]},
    {"options": [{**OPTION, "fallback_providers": [{"model": []}]}]},
    {"default": "missing"}, {"default": None}, {"default": []},
])
def test_malformed_snapshot_is_rejected_without_mutation(config_file, patch):
    data = yaml.safe_load(config_file.read_text())
    data["delegation"]["backends"]["model_map"].update(patch)
    config_file.write_text(yaml.safe_dump(data))
    before = config_file.read_bytes()
    with pytest.raises(ValueError):
        store.save_preset("bad")
    assert config_file.read_bytes() == before


@pytest.mark.parametrize("registry", [None, {}, [None], [{"name": "bad"}],
    [{"name": "same", "options": [], "default": ""}] * 2])
def test_corrupt_registry_rejected_by_reads_and_writes(config_file, registry):
    data = yaml.safe_load(config_file.read_text())
    block = data["delegation"]["backends"]["model_map"]
    block.update(presets=registry, active_preset="bad")
    config_file.write_text(yaml.safe_dump(data))
    before = config_file.read_bytes()
    for operation in (lambda: store.list_presets(data["delegation"]),
                      lambda: store.get_preset(data["delegation"], "bad"),
                      lambda: store.save_preset("new"), lambda: store.use_preset("bad"),
                      lambda: store.rename_preset("bad", "new"), lambda: store.delete_preset("bad")):
        with pytest.raises(ValueError):
            operation()
        assert config_file.read_bytes() == before
    assert store.active_preset(data["delegation"]) == ""


def test_snapshot_uses_effective_config_not_raw_file(config_file, monkeypatch):
    effective = section(config_file)
    effective["backends"]["model_map"]["options"][0]["model"] = "effective-model"
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: {"delegation": effective})
    store.save_preset("effective")
    assert store.get_preset(section(config_file), "effective")["options"][0]["model"] == "effective-model"
    assert section(config_file)["backends"]["model_map"]["options"][0]["model"] == "qwen"
    assert store.active_preset(section(config_file)) == ""


def test_managed_install_refuses_preset_write(config_file, monkeypatch):
    store.delegation_section()  # initialize the isolated layout before pretending it is managed
    monkeypatch.setattr("hermes_cli.config.is_managed", lambda: True)
    monkeypatch.setattr("hermes_cli.config.get_managed_system", lambda: "nix")
    before = config_file.read_bytes()
    with pytest.raises(RuntimeError, match="(?i)nix"):
        store.save_preset("one")
    assert config_file.read_bytes() == before


def test_empty_options_and_no_default_are_valid(config_file):
    store.save_routes([], "")
    store.save_preset("empty")
    assert store.get_preset(section(config_file), "empty") == {"options": [], "default": ""}
    store.use_preset("empty")
    assert store.active_preset(section(config_file)) == "empty"


def test_comments_survive_and_rename_to_self_is_noop(config_file):
    config_file.write_text("# keep me\n" + config_file.read_text())
    store.save_preset("one")
    before = config_file.read_bytes()
    store.rename_preset("one", "one")
    assert config_file.read_bytes() == before
    store.rename_preset("one", "new")
    store.delete_preset("new")
    assert "# keep me" in config_file.read_text()


def interleave_before_write(monkeypatch, path, change):
    """Commit a competing edit after effective reads, before the transaction starts."""
    original = store._write

    def write(mutate):
        data = yaml.safe_load(path.read_text())
        change(data["delegation"]["backends"]["model_map"])
        path.write_text(yaml.safe_dump(data))
        original(mutate)

    monkeypatch.setattr(store, "_write", write)


@pytest.mark.parametrize("operation, expected", [
    (lambda: store.save_preset("new"), ["one", "other", "new"]),
    (lambda: store.rename_preset("one", "new"), ["new", "other"]),
    (lambda: store.delete_preset("one"), ["other"]),
])
def test_intervening_registry_append_survives(config_file, monkeypatch, operation, expected):
    store.save_preset("one")
    interleave_before_write(monkeypatch, config_file, lambda b: b["presets"].append(
        {"name": "other", "options": [deepcopy(OPTION)], "default": "local"}))
    operation()
    assert store.list_presets(section(config_file)) == expected


@pytest.mark.parametrize("operation", [lambda: store.save_preset("new"),
                                      lambda: store.rename_preset("one", "new")])
def test_intervening_collision_refuses(config_file, monkeypatch, operation):
    store.save_preset("one")
    interleave_before_write(monkeypatch, config_file, lambda b: b["presets"].append(
        {"name": "new", "options": [], "default": ""}))
    with pytest.raises(ValueError, match="exists"):
        operation()
    assert store.list_presets(section(config_file)) == ["one", "new"]
    assert store.get_preset(section(config_file), "new")["options"] == []


@pytest.mark.parametrize("removed", [False, True])
def test_use_reconciles_current_target(config_file, monkeypatch, removed):
    store.save_preset("one")

    def change(block):
        if removed:
            block["presets"].clear()
        else:
            block["presets"][0]["options"][0]["model"] = "replacement"

    interleave_before_write(monkeypatch, config_file, change)
    if removed:
        with pytest.raises(ValueError, match="Unknown"):
            store.use_preset("one")
        assert section(config_file)["backends"]["model_map"]["options"][0]["model"] == "qwen"
    else:
        store.use_preset("one")
        assert section(config_file)["backends"]["model_map"]["options"][0]["model"] == "replacement"


@pytest.mark.parametrize("operation", [lambda: store.save_preset("new"),
    lambda: store.rename_preset("one", "new"), lambda: store.delete_preset("one")])
def test_intervening_active_marker_preserved(config_file, monkeypatch, operation):
    store.save_preset("other")
    store.save_preset("one")
    interleave_before_write(monkeypatch, config_file, lambda b: b.update(active_preset="other"))
    operation()
    assert store.active_preset(section(config_file)) == "other"


def test_stale_provider_does_not_brick_registry(config_file, monkeypatch):
    store.save_preset("healthy")
    data = yaml.safe_load(config_file.read_text())
    block = data["delegation"]["backends"]["model_map"]
    stale = {"name": "stale", "options": [{**OPTION, "provider": "custom:removed"}], "default": "local"}
    block["presets"].append(stale)
    config_file.write_text(yaml.safe_dump(data))
    original = store._provider_known
    monkeypatch.setattr(store, "_provider_known", lambda p: p != "custom:removed" and original(p))
    assert store.list_presets(section(config_file)) == ["healthy", "stale"]
    assert store.get_preset(section(config_file), "stale")["options"] == stale["options"]
    store.use_preset("healthy")
    store.save_preset("another")
    before = config_file.read_bytes()
    with pytest.raises(ValueError, match="Unknown provider"):
        store.use_preset("stale")
    assert config_file.read_bytes() == before
    store.rename_preset("stale", "old")
    store.delete_preset("old")
    assert store.list_presets(section(config_file)) == ["healthy", "another"]


@pytest.mark.parametrize("operation", [lambda: store.rename_preset("one", "new"),
                                      lambda: store.delete_preset("one")])
def test_mutations_refuse_intervening_target_removal(config_file, monkeypatch, operation):
    store.save_preset("one")
    interleave_before_write(monkeypatch, config_file, lambda b: b["presets"].clear())
    with pytest.raises(ValueError, match="Unknown"):
        operation()
    assert store.list_presets(section(config_file)) == []


def test_save_validates_provider_at_transaction_time(config_file, monkeypatch):
    original = store._write

    def write(mutate):
        monkeypatch.setattr(store, "_provider_known", lambda p: False)
        original(mutate)

    monkeypatch.setattr(store, "_write", write)
    before = config_file.read_bytes()
    # The fallback provider was available during the effective snapshot read.
    with pytest.raises(ValueError, match="Unknown provider"):
        store.save_preset("one")
    assert config_file.read_bytes() == before


def test_write_uses_public_raw_config_mutation(config_file, monkeypatch):
    import hermes_cli.config as core
    original = getattr(core, "update_user_config", None)
    if not callable(original):
        pytest.skip("Optional public transaction is not available on this core")
    assert callable(original)
    observations = []

    def update(mutate):
        def edit(data):
            observations.append(deepcopy(data))
            mutate(data)
        original(edit)

    monkeypatch.setattr(core, "update_user_config", update)
    store.set_mode("default")
    assert len(observations) == 1
    assert observations[0]["model"] == {"default": "main"}
    assert section(config_file)["backend"] == "default"


def test_fallback_reasoning_is_validated(config_file):
    data = yaml.safe_load(config_file.read_text())
    data["delegation"]["backends"]["model_map"]["options"][0]["fallback_providers"][0]["reasoning_effort"] = "bogus"
    config_file.write_text(yaml.safe_dump(data))
    before = config_file.read_bytes()
    with pytest.raises(ValueError, match="reasoning"):
        store.save_preset("bad")
    assert config_file.read_bytes() == before
