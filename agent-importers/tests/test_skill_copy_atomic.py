"""A failed skill re-import must leave the previously imported skill intact."""
from agent_importers_plugin import importer as importer_mod
from agent_importers_plugin.importer import AgentImporter


def test_failed_copy_keeps_the_previous_skill(tmp_path, monkeypatch):
    source = tmp_path / "claude" / "skills" / "keep-me"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("---\nname: keep-me\n---\n\nbody\n", encoding="utf-8")
    home = tmp_path / "hermes"
    home.mkdir()

    first = AgentImporter("claude-code", tmp_path / "claude", home, execute=True).run()
    skill_items = [i for i in first["items"] if i["kind"] == "skill" and i["status"] == "imported"]
    assert skill_items, first
    destination = __import__("pathlib").Path(skill_items[0]["destination"])
    marker = destination / "local-edit.txt"
    marker.write_text("keep me", encoding="utf-8")

    def explode(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(importer_mod.shutil, "copytree", explode)
    second = AgentImporter("claude-code", tmp_path / "claude", home, execute=True, overwrite=True).run()

    assert marker.exists() and marker.read_text(encoding="utf-8") == "keep me"
    assert any(i["status"] == "error" for i in second["items"]), second
