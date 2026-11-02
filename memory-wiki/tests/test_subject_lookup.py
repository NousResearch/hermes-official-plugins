"""Exact detail lookup must not depend on subject ranking (synthetic data only)."""

from urllib.parse import quote

import pytest


_NAMES = [
    ("示例甲主题", "示例甲主题"),
    ("示例甲 API", "示例甲-api"),
    ("Memory Wiki", "memory-wiki"),
]
_PREFIX_COUNT = 1001
_API = "/api/plugins/memory-wiki/subjects"


class _RankedDB:
    """Keep real window slicing: newest target, first visits, then second visits."""

    def __init__(self, title):
        titles = [title] + [
            f"{title} Variant{index}"
            for _visit in range(2)
            for index in range(_PREFIX_COUNT)
        ]
        self.sessions = [
            {
                "id": f"synthetic-{index}",
                "title": name,
                "source": "cli",
                "preview": "",
                "started_at": float(len(titles) - index),
                "last_active": float(len(titles) - index),
            }
            for index, name in enumerate(titles)
        ]
        self.messages = {
            session["id"]: [{
                "id": index,
                "role": "user",
                "content": "ok",  # No additional subject candidates.
                "timestamp": session["last_active"],
            }]
            for index, session in enumerate(self.sessions)
        }
        self.list_limits = []

    def list_sessions_rich(self, *, limit, include_children, order_by_last_active):
        assert include_children is False
        assert order_by_last_active is True
        self.list_limits.append(limit)
        return self.sessions[:limit]

    def get_messages(self, session_id):
        return self.messages[session_id]

    def close(self):
        pass


def _client(plugin_api, monkeypatch, db):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    monkeypatch.setattr(plugin_api, "_open_db", lambda: db)
    app = FastAPI()
    app.include_router(plugin_api.router, prefix="/api/plugins/memory-wiki")
    return TestClient(app)


@pytest.mark.parametrize("title,slug", _NAMES)
def test_visible_subject_survives_detail_ranking_and_prefix_collisions(memory_wiki_core, title, slug):
    db = _RankedDB(title)
    listed = memory_wiki_core.build_memory_subjects(db, limit=100, query=slug)
    assert len(listed) == 100
    target = listed[0]
    assert target["slug"] == slug
    assert target["session_count"] == 1

    # Within the same 5000-session detail window, 1001 prefix competitors have
    # two sessions each. Even query=slug cannot fix a ranked top-1000 lookup.
    ranked = memory_wiki_core.build_memory_subjects(db, limit=1000, query=slug)
    assert len(ranked) == 1000
    assert all(s["slug"].startswith(slug) and s["slug"] != slug for s in ranked)
    assert all(s["session_count"] == 2 for s in ranked)
    assert db.list_limits == [500, 5000]

    detail = memory_wiki_core.get_memory_subject(db, target["slug"])
    assert db.list_limits[-1] == 5000
    assert detail is not None, "A subject returned by the list must remain addressable"
    assert detail == target
    assert memory_wiki_core.get_memory_subject(db, f" {title.upper()} ") == target
    assert db.list_limits == [500, 5000, 5000, 5000]


@pytest.mark.parametrize("title,slug", _NAMES)
def test_http_list_slug_opens_exact_detail_despite_prefix_competitors(plugin_api, monkeypatch, title, slug):
    db = _RankedDB(title)
    with _client(plugin_api, monkeypatch, db) as client:
        response = client.get(_API, params={"q": slug, "limit": 100})
        assert response.status_code == 200
        payload = response.json()
        assert payload["query"] == slug
        assert payload["limit"] == 100
        assert len(payload["subjects"]) == 100
        target = payload["subjects"][0]
        assert target["slug"] == slug
        assert target["sessions"][0]["id"] == "synthetic-0"
        assert db.list_limits == [500]

        detail = client.get(_API + "/" + quote(target["slug"], safe=""))
        assert db.list_limits == [500, 5000]
        assert detail.status_code == 200
        assert detail.json()["subject"] == target

        # Exact matching still aggregates both visits; it must not stop at the
        # first matching session or mix the shorter target into this subject.
        competitor = payload["subjects"][1]
        detail = client.get(_API + "/" + quote(competitor["slug"], safe=""))
        assert detail.status_code == 200
        subject = detail.json()["subject"]
        assert subject["slug"] == competitor["slug"]
        assert subject["session_count"] == subject["message_count"] == 2
        assert {s["id"] for s in subject["sessions"]} == {
            "synthetic-1", f"synthetic-{1 + _PREFIX_COUNT}",
        }
        assert db.list_limits == [500, 5000, 5000]


def test_http_unknown_slug_is_404_even_when_it_is_a_common_prefix(plugin_api, monkeypatch):
    db = _RankedDB("示例甲主题")
    with _client(plugin_api, monkeypatch, db) as client:
        for slug in ("示例甲", "unknown-subject"):
            response = client.get(_API + "/" + quote(slug, safe=""))
            assert response.status_code == 404
            assert response.json() == {"detail": "Memory subject not found"}
    assert db.list_limits == [5000, 5000]
