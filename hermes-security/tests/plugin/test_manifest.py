"""Scaffold contracts; no Hermes runtime or network required."""
import importlib.util
import json
import pytest
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOLS = {
    "security_scan_start", "security_scan_get", "security_scan_checkpoint",
    "security_scan_submit_worker_result",
    "security_scan_record_validations", "security_scan_record_chains",
    "security_scan_finalize", "security_scan_cancel", "security_scan_export",
}


def test_manifest_identity_and_tools():
    # JSON is a YAML subset: keep this contract independent of a YAML package.
    manifest = json.loads((ROOT / "plugin.yaml").read_text(encoding='utf-8'))
    assert manifest["name"] == "hermes-security"
    assert manifest["version"] == "0.1.0"
    assert manifest["license"] == "Apache-2.0"
    assert manifest["requires_hermes"] == ">=0.21.5"
    assert manifest["kind"] == "standalone"
    assert set(manifest["provides_tools"]) == TOOLS
    assert len(manifest["provides_tools"]) == len(TOOLS)


def test_product_brand_and_license():
    manifest = (ROOT / "plugin.yaml").read_text(encoding='utf-8').lower()
    title = (ROOT / "README.md").read_text(encoding='utf-8').splitlines()[0].lower()
    for brand in ("openai", "codex"):
        assert brand not in manifest
        assert brand not in title
    license_text = (ROOT / "LICENSE").read_text(encoding='utf-8')
    assert "Apache License" in license_text
    assert "Version 2.0, January 2004" in license_text
    assert "END OF TERMS AND CONDITIONS" in license_text
    assert "9. Accepting Warranty or Additional Liability." in " ".join(license_text.split())


def test_provenance_pins_and_entries():
    provenance = json.loads((ROOT / "UPSTREAM_PROVENANCE.json").read_text(encoding='utf-8'))
    assert provenance["upstream"] == {
        "repository": "https://github.com/openai/codex-security",
        "commit": "89aae242136312467790947f3b122ca3f607614f",
        "license": "Apache-2.0",
    }
    assert provenance["hermes"] == {
        "repository": "NousResearch/hermes-agent",
        "commit": "15cf1417e4c53ebea9d415abb5bcd6af8b1577d3",
    }
    assert isinstance(provenance["files"], list)
    for entry in provenance["files"]:
        assert set(entry) == {"source", "destination", "license", "modified", "sourceSha256", "destinationSha256"}
        assert entry["modified"] is True
        assert entry["license"] == "Apache-2.0"
        for key in ("sourceSha256", "destinationSha256"):
            assert len(entry[key]) == 64
            int(entry[key], 16)


def test_register_is_lazy_and_forwards_context(monkeypatch):
    # Load the way Hermes does: synthetic package name with the plugin dir as its search path.
    name = "security_scaffold_entry"
    spec = importlib.util.spec_from_file_location(name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    assert callable(module.register)
    assert f"{name}.hermes_security" not in sys.modules
    calls = []
    plugin = types.ModuleType(f"{name}.hermes_security.plugin")
    plugin.register = calls.append
    monkeypatch.setitem(sys.modules, f"{name}.hermes_security.plugin", plugin)
    ctx = object()
    module.register(ctx)
    assert calls == [ctx]


def test_provenance_regeneration_and_drift(tmp_path):
    spec = importlib.util.spec_from_file_location("security_provenance", ROOT / "scripts/provenance.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    repo = tmp_path / "repo"
    source = tmp_path / "source"
    repo.mkdir()
    upstream = source / "plugins/codex-security/references"
    upstream.mkdir(parents=True)
    (upstream / "sample.md").write_text("Original methodology\n")
    adapted = repo / "sample.md"
    notice = "<!-- Modified from " + "Apache-2.0 source references/sample.md (see NOTICE). -->\n"
    adapted.write_text(notice + "Local adaptation\n")
    argv = ["--repo-root", str(repo), "--source-root", str(source)]
    assert module.main(argv + ["--check"]) == 1
    assert module.main(argv) == 0
    assert module.main(argv + ["--check"]) == 0
    entries = module.collect_entries(repo, source)
    assert len(entries) == 1
    assert entries[0]["source"] == "references/sample.md"
    adapted.write_text(notice + "Changed adaptation\n")
    assert module.main(argv + ["--check"]) == 1
    assert module.main(argv) == 0
    (repo / "second.md").write_text(notice + "New adaptation\n")
    assert module.main(argv + ["--check"]) == 1
    assert module.main(argv) == 0
    (upstream / "sample.md").write_text("Changed upstream\n")
    assert module.main(argv + ["--check"]) == 1
    (upstream / "sample.md").unlink()
    assert module.main(argv) == 1


def test_provenance_json_notice_and_root_relative_source(tmp_path):
    spec = importlib.util.spec_from_file_location("security_provenance", ROOT / "scripts/provenance.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    repo = tmp_path / "repo"
    source = tmp_path / "source"
    repo.mkdir()
    source.mkdir()
    (source / "original.json").write_text("{}")
    notice = "Modified from " + "Apache-2.0 source original.json (see NOTICE)."
    (repo / "adapted.json").write_text(json.dumps({"$comment": notice}, indent=2))
    entries = module.collect_entries(repo, source)
    assert len(entries) == 1
    assert entries[0]["source"] == "original.json"


def test_provenance_multi_source_notice(tmp_path):
    spec = importlib.util.spec_from_file_location("security_provenance", ROOT / "scripts/provenance.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    repo = tmp_path / "repo"
    source = tmp_path / "source"
    (source / "refs").mkdir(parents=True)
    repo.mkdir()
    (source / "refs/a.md").write_text("A")
    (source / "refs/b.md").write_text("B")
    notice = "<!-- # Modified from " + "Apache-2.0 source refs/a.md; refs/b.md (see NOTICE). -->\n"
    (repo / "merged.md").write_text(notice + "body\n")
    entries = module.collect_entries(repo, source)
    assert [e["source"] for e in entries] == ["refs/a.md", "refs/b.md"]
    (repo / "merged.md").write_text(notice.replace("refs/b.md", "refs/<b>.md") + "body\n")
    with pytest.raises(ValueError):
        module.collect_entries(repo, source)
