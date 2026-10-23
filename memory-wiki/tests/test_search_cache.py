"""Synthetic DB regressions at the real router/cache boundary."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import sqlite3
import threading

import pytest


@pytest.fixture
def writer(hermes_home):
    from hermes_state import SessionDB

    db = SessionDB(hermes_home / "state.db")
    db.create_session("one", "cli")
    db.set_session_title("one", "Original Topic")
    db.append_message("one", "user", "Use React for SyntheticWidget")
    yield db
    db.close()


def search(api, home, limit=100, query=""):
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override

    token = set_hermes_home_override(home)
    try:
        return asyncio.run(api.get_subjects(query, limit))["subjects"]
    finally:
        reset_hermes_home_override(token)


@pytest.mark.parametrize("sql", [
    "UPDATE sessions SET title='Changed Topic' WHERE id='one'",
    "UPDATE messages SET content='Use Flask for ChangedWidget' WHERE session_id='one'",
    "DELETE FROM messages WHERE session_id='one'",
    "DELETE FROM sessions WHERE id='one'",
])
def test_wal_update_delete_invalidation(writer, hermes_home, plugin_api, memory_wiki_core, sql):
    before = search(plugin_api, hermes_home)
    main_stamp = (hermes_home / "state.db").stat().st_mtime_ns
    with sqlite3.connect(hermes_home / "state.db") as conn:
        conn.execute(sql)
    assert (hermes_home / "state.db").stat().st_mtime_ns == main_stamp
    after = search(plugin_api, hermes_home)
    assert before != after
    assert after == memory_wiki_core.build_memory_subjects(writer)


def test_insert_invalidation_and_detached_results(writer, hermes_home, plugin_api, memory_wiki_core):
    before = search(plugin_api, hermes_home)
    before[0]["sessions"][0]["title"] = "caller mutation"
    before[0]["keywords"].clear()
    assert search(plugin_api, hermes_home) == memory_wiki_core.build_memory_subjects(writer)
    writer.append_message("one", "user", "Use Flask with NewWidget")
    after_message = search(plugin_api, hermes_home)
    assert after_message == memory_wiki_core.build_memory_subjects(writer)
    writer.create_session("two", "cli")
    writer.set_session_title("two", "Second Topic")
    after_session = search(plugin_api, hermes_home)
    assert after_session != after_message
    assert after_session == memory_wiki_core.build_memory_subjects(writer)


def test_absence_and_replacement(hermes_home, plugin_api):
    from hermes_state import SessionDB

    assert search(plugin_api, hermes_home) == []
    path = hermes_home / "state.db"
    db = SessionDB(path)
    db.create_session("one", "cli")
    db.set_session_title("one", "Original Topic")
    db.close()
    assert search(plugin_api, hermes_home)
    path.unlink()
    assert search(plugin_api, hermes_home) == []
    # Remove only synthetic sidecars left by a read-only attach.
    for suffix in ("-wal", "-shm"):
        path.with_name(path.name + suffix).unlink(missing_ok=True)
    replacement = hermes_home / "replacement.db"
    db = SessionDB(replacement)
    db.create_session("two", "cli")
    db.set_session_title("two", "Replacement Topic")
    db.close()
    replacement.replace(path)
    result = search(plugin_api, hermes_home)
    assert {s["name"] for s in result} == {"replacement topic"}
    # Replace a present, cached DB as well.
    db = SessionDB(replacement)
    db.create_session("three", "cli")
    db.set_session_title("three", "Third Topic")
    db.close()
    for suffix in ("-wal", "-shm"):
        path.with_name(path.name + suffix).unlink(missing_ok=True)
    replacement.replace(path)
    assert {s["name"] for s in search(plugin_api, hermes_home)} == {"third topic"}


def test_scope_parity(writer, hermes_home, plugin_api, memory_wiki_core):
    for index in range(110):
        writer.create_session(f"extra-{index}", "cli")
        writer.set_session_title(f"extra-{index}", f"Synthetic Project {index}")
    for limit in (1, 20, 21, 50, 100):
        for query in ("", "original", "PROJECT", "no match", "  synthetic  "):
            assert search(plugin_api, hermes_home, limit, query) == memory_wiki_core.build_memory_subjects(
                writer, limit=limit, query=query.strip() or None,
            )


def test_concurrent_identical_work_coalesces(writer, hermes_home, plugin_api, memory_wiki_core, monkeypatch):
    original = memory_wiki_core.build_subject_index
    entered, release = threading.Event(), threading.Event()
    calls = []

    def held(*args, **kwargs):
        calls.append(1)
        entered.set()
        assert release.wait(3)
        return original(*args, **kwargs)

    monkeypatch.setattr(memory_wiki_core, "build_subject_index", held)
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(search, plugin_api, hermes_home) for _ in range(4)]
        try:
            assert entered.wait(3)
        finally:
            release.set()
        results = [future.result() for future in futures]
    assert len(calls) == 1
    assert all(result == results[0] for result in results)
    assert results[0] is not results[1]


def test_racing_write_is_not_cached(writer, hermes_home, plugin_api, memory_wiki_core, monkeypatch):
    original = memory_wiki_core.build_subject_index
    calls = []

    def raced(*args, **kwargs):
        result = original(*args, **kwargs)
        calls.append(1)
        if len(calls) == 1:
            writer.append_message("one", "user", "Use Flask for RacingWidget")
        return result

    monkeypatch.setattr(memory_wiki_core, "build_subject_index", raced)
    first = search(plugin_api, hermes_home)
    second = search(plugin_api, hermes_home)
    assert first != second
    assert len(calls) == 2
    assert search(plugin_api, hermes_home) == second
    assert len(calls) == 2


def test_expiry_and_size_bound(writer, hermes_home, plugin_api, memory_wiki_core, monkeypatch):
    original = memory_wiki_core.build_subject_index
    calls = []

    def counted(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(memory_wiki_core, "build_subject_index", counted)
    search(plugin_api, hermes_home)
    monkeypatch.setattr(plugin_api, "_CACHE_TTL", 0)
    search(plugin_api, hermes_home)
    assert len(calls) == 2
    monkeypatch.setattr(plugin_api, "_CACHE_TTL", 5)
    plugin_api._SUBJECT_CACHE.clear()
    monkeypatch.setattr(plugin_api, "_CACHE_BYTES", 1)
    search(plugin_api, hermes_home)
    search(plugin_api, hermes_home)
    assert len(calls) == 4
    assert not plugin_api._SUBJECT_CACHE


def test_overlapping_profile_contexts_all_routes(tmp_path, hermes_home, plugin_api):
    from fastapi import FastAPI, Request
    from httpx import ASGITransport, AsyncClient
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from hermes_state import SessionDB

    homes = {}
    for name in ("alpha", "beta"):
        home = tmp_path / name
        home.mkdir()
        db = SessionDB(home / "state.db")
        db.create_session(name, "cli")
        db.set_session_title(name, f"{name} topic")
        db.append_message(name, "user", f"{name} topic")
        db.close()
        (home / "memories").mkdir()
        (home / "memories" / "MEMORY.md").write_text(name)
        homes[name] = home

    app = FastAPI()

    @app.middleware("http")
    async def scope(request: Request, call_next):
        # Exercise the host's ContextVar primitive, not an environment mutation.
        token = set_hermes_home_override(homes[request.query_params["profile"]])
        try:
            return await call_next(request)
        finally:
            reset_hermes_home_override(token)

    app.include_router(plugin_api.router)

    async def run():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            async def check(name):
                async def get(path):
                    response = await client.get(path, params={"profile": name})
                    assert response.status_code == 200
                    return response.json()

                overview = await get("/overview")
                assert overview["notes"]["memory"] == [name]
                assert {s["id"] for s in overview["recent_sessions"]} == {name}
                subjects = (await get("/subjects"))["subjects"]
                assert {s["slug"] for s in subjects} == {f"{name}-topic"}
                detail = (await get(f"/subjects/{name}-topic"))["subject"]
                assert {s["id"] for s in detail["sessions"]} == {name}
                days = (await get("/days"))["daily_logs"]
                day = (await get(f"/days/{days[0]['date']}"))["daily_log"]
                assert {s["id"] for s in day["sessions"]} == {name}

            await asyncio.gather(check("alpha"), check("beta"), check("alpha"))
            await check("alpha")

    asyncio.run(run())


def test_entry_bound_and_failed_work_closes_db(writer, hermes_home, plugin_api, memory_wiki_core, monkeypatch):
    plugin_api._SUBJECT_CACHE.clear()
    for limit in range(21, 31):
        search(plugin_api, hermes_home, limit)
    assert len(plugin_api._SUBJECT_CACHE) == 8
    opened = []
    original_open = plugin_api._open_db

    def tracked(home):
        db = original_open(home)
        closed = []
        original_close = db.close

        def close():
            closed.append(True)
            original_close()

        db.close = close
        opened.append(closed)
        return db

    def fail(*args, **kwargs):
        raise OSError("synthetic read failure")

    monkeypatch.setattr(plugin_api, "_open_db", tracked)
    monkeypatch.setattr(memory_wiki_core, "build_subject_index", fail)
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as error:
        search(plugin_api, hermes_home, 100)
    assert error.value.status_code == 500
    assert error.value.detail == "Memory wiki subjects failed"
    assert opened == [[True]]
