"""Chinese subject regression tests; synthetic data only, never the real profile."""

from copy import deepcopy
from urllib.parse import quote

import pytest


class _ReadOnlyDB:
    def __init__(self, sessions, messages=None):
        self.sessions = sessions
        self.messages = messages or {}

    def list_sessions_rich(self, *, limit, include_children, order_by_last_active):
        assert not include_children and order_by_last_active
        return self.sessions[:limit]

    def get_messages(self, session_id):
        return self.messages.get(session_id, [])


@pytest.mark.parametrize("source", ["title", "preview", "user"])
@pytest.mark.parametrize(
    "text,queries",
    [("示例甲检索核对", ["示例甲", "检索"]), ("示例 API SDK", ["示例", "API", "SDK"])],
)
def test_chinese_sources_are_substring_searchable(memory_wiki_core, source, text, queries):
    session = {"id": "synthetic", "started_at": 1}
    messages = []
    if source == "user":
        messages = [{"role": "user", "content": text}]
    else:
        session[source] = text
    db = _ReadOnlyDB([session], {"synthetic": messages})
    before = deepcopy((session, messages))
    for query in queries:
        subjects = memory_wiki_core.build_memory_subjects(db, query=query)
        assert subjects, (source, text, query)
        assert any("示例甲" in s["name"] or "示例" in s["name"] for s in subjects)
        for subject in subjects:
            assert memory_wiki_core.get_memory_subject(db, subject["slug"]) == subject
    assert (session, messages) == before


@pytest.mark.parametrize("quoted", ['"检索"', "‘检索’", "“检索”", "「检索」", "『检索』"])
@pytest.mark.parametrize("source", ["title", "preview", "user"])
def test_short_quoted_chinese_topics(memory_wiki_core, quoted, source):
    session = {}
    messages = []
    text = f"请核对{quoted}以及月底记录"
    if source == "user":
        messages = [{"role": "user", "content": text}]
    else:
        session[source] = text
    candidates = memory_wiki_core.extract_subject_candidates(session, messages)
    assert any(c.name == "检索" and c.source == "quote" for c in candidates)


@pytest.mark.parametrize("name", ["示例甲检索核对", "示例核对", "示例 API SDK", "样例 API SDK", "2026年11月检索", "範例甲核對", "𠮷字检索"])
def test_chinese_slugs_are_stable_and_idempotent(memory_wiki_core, name):
    slug = memory_wiki_core.slugify_subject(name)
    assert slug != "subject"
    assert slug == memory_wiki_core.slugify_subject(name)
    assert slug == memory_wiki_core.slugify_subject(slug)
    assert " " not in slug and "/" not in slug


def test_chinese_slugs_do_not_merge_distinct_subjects(memory_wiki_core):
    names = ["示例甲检索核对", "示例核对", "示例 API SDK", "样例 API SDK", "2026年11月检索", "2027年11月检索"]
    db = _ReadOnlyDB([{"id": str(i), "title": name} for i, name in enumerate(names)])
    subjects = memory_wiki_core.build_memory_subjects(db)
    assert len(subjects) == len(names)
    assert len({s["slug"] for s in subjects}) == len(names)
    assert all(s["session_count"] == 1 for s in subjects)


@pytest.mark.parametrize("text,queries", [("2026年11月：示例甲／检索核对！", ["2026", "11", "示例甲", "检索"]), ("示例API_SDK：样本2026", ["示例", "api", "sdk", "2026"])])
def test_chinese_numbers_punctuation_and_mixed_tokens(memory_wiki_core, text, queries):
    db = _ReadOnlyDB([{"id": "numbers", "title": text}])
    for query in queries:
        assert memory_wiki_core.build_memory_subjects(db, query=query), query


@pytest.mark.parametrize("role", ["assistant", "tool", "system"])
def test_non_user_chinese_prose_and_quotes_are_not_new_topics(memory_wiki_core, role):
    candidates = memory_wiki_core.extract_subject_candidates(
        {"title": "示例甲检索核对"},
        [{"role": role, "content": '“助手私有噪声”以及工具输出。' * 1000}],
    )
    assert [c.name for c in candidates] == ["示例甲检索核对"]


def test_long_chinese_text_and_quotes_are_bounded(memory_wiki_core):
    long_text = "示例甲检索核对" * 10000
    quotes = " ".join(f'“候选主题{i}”' for i in range(1000))
    messages = [{"role": "user", "content": long_text}]
    messages.extend({"role": "user", "content": f'“另外主题{i}”'} for i in range(20))
    candidates = memory_wiki_core.extract_subject_candidates({"title": long_text, "preview": quotes}, messages)
    assert len(candidates) == 8
    assert all(len(c.name) <= 48 for c in candidates)
    assert any("示例甲" in c.name for c in candidates)


def test_chinese_prefix_limit_does_not_scan_late_quotes(memory_wiki_core):
    text = "示例甲检索核对" * 100 + '“末尾噪声”'
    candidates = memory_wiki_core.extract_subject_candidates(
        {"title": text, "preview": text}, [{"role": "user", "content": text}],
    )
    assert len(candidates) == 1
    assert "末尾" not in candidates[0].name


@pytest.mark.parametrize("name,expected", [("Memory Wiki", "memory-wiki"), ("web/src/App.tsx", "web-src-app-tsx"), ("2026 / 11", "2026-11"), ("!!! --- ...", "subject"), ("", "subject")])
def test_legacy_ascii_slug_rules_unchanged(memory_wiki_core, name, expected):
    assert memory_wiki_core.slugify_subject(name) == expected


def test_long_chinese_slug_retains_distinguishing_suffix(memory_wiki_core):
    common = "示例甲检索核对" * 100
    assert memory_wiki_core.slugify_subject(common + "甲") != memory_wiki_core.slugify_subject(common + "乙")


def test_only_first_nonempty_user_text_supplies_unquoted_chinese(memory_wiki_core):
    candidates = memory_wiki_core.extract_subject_candidates({}, [
        {"role": "user", "content": "   "},
        {"role": "user", "content": "示例甲检索核对"},
        {"role": "user", "content": "后续长文噪声" * 1000},
    ])
    assert [c.name for c in candidates] == ["示例甲检索核对"]


def test_chinese_subject_http_roundtrip_is_read_only(plugin_api, hermes_home):
    from fastapi import FastAPI
    from starlette.testclient import TestClient
    from hermes_state import SessionDB

    db_path = hermes_home / "state.db"
    db = SessionDB(db_path=db_path)
    original = "示例甲检索核对"
    try:
        db.create_session("synthetic-zh", "cli")
        db.append_message("synthetic-zh", "user", original)
        before_messages = db.get_messages("synthetic-zh")
    finally:
        db.close()
    before_bytes = db_path.read_bytes()
    app = FastAPI()
    app.include_router(plugin_api.router, prefix="/api/plugins/memory-wiki")
    with TestClient(app) as client:
        response = client.get("/api/plugins/memory-wiki/subjects", params={"q": "检索"})
        assert response.status_code == 200
        subjects = response.json()["subjects"]
        assert subjects
        subject = subjects[0]
        detail = client.get("/api/plugins/memory-wiki/subjects/" + quote(subject["slug"], safe=""))
        assert detail.status_code == 200
        assert detail.json()["subject"] == subject
        assert subject["snippets"][0]["text"] == original
    assert db_path.read_bytes() == before_bytes
    db = SessionDB(db_path=db_path)
    try:
        assert db.get_messages("synthetic-zh") == before_messages
    finally:
        db.close()
