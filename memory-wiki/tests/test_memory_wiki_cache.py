"""Tests for dashboard/memory_wiki_cache.py — the per-session aggregation cache (#49).

Each test builds a small real state.db (SessionDB) and drives the aggregators
through a thin counting wrapper so the tests can see exactly which sessions
got re-scanned: warm requests must not re-read messages, a write to one
session must re-read only that session, and cached results must be
byte-identical to the uncached path.
"""

from __future__ import annotations

from pathlib import Path

import hermes_state


class CountingDB:
    """Delegates to a real SessionDB and counts get_messages per session id."""

    def __init__(self, real: hermes_state.SessionDB):
        self._real = real
        self.scanned: list[str] = []

    def list_sessions_rich(self, *args, **kwargs):
        # the aggregators' listing contract, pinned the same way _MemoryWikiFakeDB does
        assert kwargs.get("include_children") is False
        assert kwargs.get("order_by_last_active") is True
        return self._real.list_sessions_rich(*args, **kwargs)

    def get_messages(self, session_id: str):
        self.scanned.append(session_id)
        return self._real.get_messages(session_id)

    def close(self):
        self._real.close()


def _seed_db(db_path: Path, sessions: dict[str, list[tuple[str, str]]]) -> hermes_state.SessionDB:
    db = hermes_state.SessionDB(db_path=db_path)
    try:
        for session_id, messages in sessions.items():
            db.create_session(session_id, "cli")
            for role, text in messages:
                db.append_message(session_id, role, text)
    finally:
        db.close()
    return hermes_state.SessionDB(db_path=db_path, read_only=True)


def _three_sessions(db_path: Path) -> hermes_state.SessionDB:
    return _seed_db(
        db_path,
        {
            "s-alpha": [
                ("user", "Debug FastAPI pytest failure in tests/api"),
                ("assistant", "Planned and fixed the FastAPI route bug."),
            ],
            "s-beta": [
                ("user", "Refactor the Memory Wiki dashboard web/src/App.tsx"),
                ("assistant", "Implemented Memory Wiki subject cards."),
            ],
            "s-gamma": [
                ("user", "Research ruDLLMs for the on-prem summarizer"),
                ("assistant", "Compared ruDLLMs and picked the best fit."),
            ],
        },
    )


def test_fingerprint_tracks_every_field_the_extractors_read(tmp_path, memory_wiki_cache):
    db = _seed_db(
        tmp_path / "state.db",
        {"s-one": [("user", "Debug FastAPI pytest failure")]},
    )
    try:
        session = db.list_sessions_rich(limit=5, include_children=False, order_by_last_active=True)[0]
        fingerprint = memory_wiki_cache.session_fingerprint(session)
        assert fingerprint == memory_wiki_cache.session_fingerprint(dict(session)), "same row, same fingerprint"

        for field, value in (
            ("title", "different title"),
            ("preview", "different preview"),
            ("source", "other-source"),
            ("started_at", session["started_at"] + 1),
            ("last_active", session["last_active"] + 1),
            ("message_count", session["message_count"] + 1),
            ("tool_call_count", session["tool_call_count"] + 1),
            ("transcript_revision", session["transcript_revision"] + 1),
        ):
            mutated = dict(session, **{field: value})
            assert memory_wiki_cache.session_fingerprint(mutated) != fingerprint, field
    finally:
        db.close()


def test_warm_request_reuses_units_without_rescanning(tmp_path, memory_wiki_core, memory_wiki_cache):
    db = _three_sessions(tmp_path / "state.db")
    try:
        counting = CountingDB(db)
        cache = memory_wiki_cache.SessionUnitCache(tmp_path / "sidecar.json")

        cold = memory_wiki_core.build_memory_subjects(counting, limit=50, cache=cache)
        cold_scans = len(counting.scanned)
        assert cold_scans == 3, "cold request scans every session once"

        counting.scanned.clear()
        warm = memory_wiki_core.build_memory_subjects(counting, limit=50, cache=cache)
        assert counting.scanned == [], "warm request must not re-read messages"
        assert warm == cold, "warm result must be identical to the cold one"
    finally:
        db.close()


def test_writing_one_session_rescans_only_that_session(tmp_path, memory_wiki_core, memory_wiki_cache):
    db_path = tmp_path / "state.db"
    db = _three_sessions(db_path)
    try:
        counting = CountingDB(db)
        cache = memory_wiki_cache.SessionUnitCache(tmp_path / "sidecar.json")
        memory_wiki_core.build_memory_subjects(counting, limit=50, cache=cache)
        counting.scanned.clear()

        writer = hermes_state.SessionDB(db_path=db_path)
        try:
            writer.append_message("s-beta", "assistant", "Shipped the FastAPI route fix too.")
        finally:
            writer.close()

        memory_wiki_core.build_memory_subjects(counting, limit=50, cache=cache)
        assert counting.scanned == ["s-beta"], "only the touched session gets re-scanned"
    finally:
        db.close()


def test_cached_result_is_identical_to_the_uncached_path(tmp_path, memory_wiki_core, memory_wiki_cache):
    db_path = tmp_path / "state.db"
    db = _three_sessions(db_path)
    try:
        sidecar = tmp_path / "cache" / "memory-wiki" / "session_units-v1.json"
        cache = memory_wiki_cache.SessionUnitCache(sidecar)
        cached = memory_wiki_core.build_memory_overview(
            db, subject_limit=50, day_limit=30, cache=cache
        )
        cache.flush()

        reopened = memory_wiki_cache.SessionUnitCache(sidecar)  # fresh process: units come from disk
        from_disk = memory_wiki_core.build_memory_overview(
            db, subject_limit=50, day_limit=30, cache=reopened
        )
        uncached = memory_wiki_core.build_memory_overview(db, subject_limit=50, day_limit=30)

        assert cached == uncached
        assert from_disk == uncached, "restarted gateway must reproduce the same payload"
    finally:
        db.close()


def test_corrupt_sidecar_is_discarded_not_fatal(tmp_path, memory_wiki_core, memory_wiki_cache):
    db = _three_sessions(tmp_path / "state.db")
    try:
        sidecar = tmp_path / "sidecar.json"
        sidecar.write_text("{not json at all", encoding="utf-8")
        cache = memory_wiki_cache.SessionUnitCache(sidecar)
        counting = CountingDB(db)

        result = memory_wiki_core.build_memory_subjects(counting, limit=50, cache=cache)
        assert len(counting.scanned) == 3, "corrupt sidecar means a full rebuild"
        assert result == memory_wiki_core.build_memory_subjects(counting, limit=50, cache=cache)
    finally:
        db.close()


def test_store_is_bounded_by_unit_count(tmp_path, memory_wiki_core, memory_wiki_cache):
    db = _seed_db(
        tmp_path / "state.db",
        {
            "s-old": [("user", "Old session about the archive exporter")],
            "s-mid": [("user", "Mid session about the billing reconciler")],
            "s-new": [("user", "New session about the FastAPI router")],
        },
    )
    try:
        counting = CountingDB(db)
        cache = memory_wiki_cache.SessionUnitCache(tmp_path / "sidecar.json", max_units=2)
        memory_wiki_core.build_memory_subjects(counting, limit=50, cache=cache)
        cache.flush()

        reopened = memory_wiki_cache.SessionUnitCache(tmp_path / "sidecar.json", max_units=2)
        assert reopened.stats()["units"] == 2, "least-recently-active unit is evicted first"

        counting.scanned.clear()
        memory_wiki_core.build_memory_subjects(counting, limit=50, cache=reopened)
        assert sorted(counting.scanned) == ["s-old"], "evicted session is re-scanned, others are not"
    finally:
        db.close()


def _unit(session_id: str, last_active: float, filler: str) -> dict:
    """A minimal unit the way ``_build_session_unit`` shapes them, sized by ``filler``."""

    return {
        "session": {"id": session_id, "last_active": last_active},
        "candidates": [],
        "first_seen": 0.0,
        "last_seen": last_active,
        "message_count": 0,
        "snippets_by_slug": {},
        "started_at": 0.0,
        "day_key": "1970-01-01",
        "work_items": [],
        "filler": filler,
    }


def _row(session_id: str, last_active: float) -> dict:
    """A minimal sessions-table row (fingerprint fields default to empty/zero)."""

    return {"id": session_id, "last_active": last_active}


def test_store_is_bounded_by_serialized_size(tmp_path, memory_wiki_cache):
    mwc = memory_wiki_cache
    cache = mwc.SessionUnitCache(tmp_path / "sidecar.json", max_bytes=4096)
    for session_id, last_active in (("s-old", 1.0), ("s-mid", 2.0), ("s-new", 3.0)):
        unit = _unit(session_id, last_active, "x" * 1500)
        unit["fingerprint"] = mwc.session_fingerprint(_row(session_id, last_active))
        cache.store(unit)

    stats = cache.stats()
    assert stats["units"] == 2, "the byte bound evicts least-recently-active units first"
    assert stats["bytes"] <= 4096
    assert cache.lookup(_row("s-old", 1.0)) is None
    assert cache.lookup(_row("s-new", 3.0)) is not None


def test_one_oversized_unit_pins_the_store(tmp_path, memory_wiki_cache):
    cache = memory_wiki_cache.SessionUnitCache(tmp_path / "sidecar.json", max_bytes=1024)
    unit = _unit("s-huge", 1.0, "x" * 8000)
    unit["fingerprint"] = memory_wiki_cache.session_fingerprint(_row("s-huge", 1.0))
    cache.store(unit)

    assert cache.stats()["units"] == 1, "eviction never empties the store"


def test_default_cache_honors_off_switch_and_profile_scoping(tmp_path, memory_wiki_cache, monkeypatch):
    mwc = memory_wiki_cache

    monkeypatch.delenv("MEMORY_WIKI_CACHE", raising=False)
    monkeypatch.setattr(mwc, "_default_caches", {})

    first = mwc.default_cache(tmp_path / "home-a" / "cache" / "memory-wiki")
    assert isinstance(first, mwc.SessionUnitCache)
    assert mwc.default_cache(tmp_path / "home-a" / "cache" / "memory-wiki") is first

    second = mwc.default_cache(tmp_path / "home-b" / "cache" / "memory-wiki")
    assert second is not first, "a different HERMES_HOME must not share units"

    monkeypatch.setenv("MEMORY_WIKI_CACHE", "off")
    assert mwc.default_cache(tmp_path / "home-a" / "cache" / "memory-wiki") is None
