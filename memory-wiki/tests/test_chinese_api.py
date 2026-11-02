"""Chinese subjects through the real read-only API and a temporary SessionDB."""

from __future__ import annotations

from urllib.parse import quote


def test_chinese_subjects_api_round_trip_without_database_writes(hermes_home, plugin_api):
    from fastapi import FastAPI
    from starlette.testclient import TestClient
    from hermes_state import SessionDB

    database = hermes_home / "state.db"
    raw_messages = {
        "title-example": "示例甲检索核对",
        "mixed-example": "示例 API SDK",
        "other-example": "示例乙示例核对",
    }
    db = SessionDB(db_path=database)
    try:
        for session_id, content in raw_messages.items():
            db.create_session(session_id, "cli")
            db.append_message(session_id, "user", content)
    finally:
        db.close()

    before = database.read_bytes()
    before_mtime = database.stat().st_mtime_ns
    app = FastAPI()
    app.include_router(plugin_api.router, prefix="/api/plugins/memory-wiki")
    with TestClient(app) as client:
        found_slugs = {}
        for keyword, expected_id in (
            ("示例甲", "title-example"),
            ("检索", "title-example"),
            ("示例", "mixed-example"),
            ("API", "mixed-example"),
            ("SDK", "mixed-example"),
            ("示例乙", "other-example"),
        ):
            response = client.get(
                "/api/plugins/memory-wiki/subjects",
                params={"q": keyword, "limit": 100},
            )
            assert response.status_code == 200
            subjects = response.json()["subjects"]
            assert subjects, keyword
            subject = next(
                entry for entry in subjects
                if any(session["id"] == expected_id for session in entry["sessions"])
            )
            found_slugs.setdefault(expected_id, subject["slug"])
            detail = client.get(
                "/api/plugins/memory-wiki/subjects/" + quote(subject["slug"], safe="")
            )
            assert detail.status_code == 200
            assert detail.json()["subject"]["slug"] == subject["slug"]
            assert any(
                snippet["text"] == raw_messages[expected_id]
                for snippet in detail.json()["subject"]["snippets"]
            )
        assert len(set(found_slugs.values())) == len(raw_messages)
        assert client.get(
            "/api/plugins/memory-wiki/subjects", params={"q": "不存在的主题"}
        ).json()["subjects"] == []
    assert database.read_bytes() == before
    assert database.stat().st_mtime_ns == before_mtime
