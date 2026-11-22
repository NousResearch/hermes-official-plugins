"""A sidecar that grew past the configured bounds is trimmed when it is loaded."""
import json


def _unit(i):
    return {"session": {"id": f"s{i}", "last_active": 1000 + i}, "fingerprint": f"f{i}", "body": "x" * 50}


def test_load_applies_the_unit_bound(tmp_path, memory_wiki_cache):
    path = tmp_path / "cache.json"
    path.write_text(json.dumps({"units": {f"s{i}": _unit(i) for i in range(5)}}), encoding="utf-8")
    cache = memory_wiki_cache.SessionUnitCache(path, max_units=2)
    stats = cache.stats()
    assert stats["units"] == 2
    assert stats["dirty"] is True  # the trimmed sidecar will be rewritten
