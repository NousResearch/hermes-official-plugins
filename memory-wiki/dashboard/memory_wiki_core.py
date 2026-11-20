"""Memory Wiki aggregation — deterministic subject/daily-log derivation over session history.

Originally ``hermes_cli/memory_wiki.py`` by Abdullah Raja (@Araja119, hermes-agent PR #31244),
salvaged in #89940 and moved into this standalone plugin. Stays stdlib-only: the helpers are
deterministic heuristics over the session/message dictionaries ``hermes_state.SessionDB``
returns (``list_sessions_rich`` / ``get_messages``); no LLM calls, no writes.

The per-session extraction is memoized by the sibling ``memory_wiki_cache`` module (#49):
aggregators accept an optional ``cache`` and skip re-reading sessions whose cheap
sessions-table fingerprint is unchanged; ``cache=None`` keeps the original uncached path.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import importlib.util
import re
import sys
import threading


@dataclass(frozen=True)
class SubjectCandidate:
    """A possible subject extracted from session metadata or messages."""

    name: str
    slug: str
    score: int = 1
    source: str = "text"
    user_supported: bool = False


_STOPWORDS = {
    "a",
    "about",
    "add",
    "agent",
    "an",
    "and",
    "are",
    "as",
    "assistant",
    "be",
    "build",
    "can",
    "chat",
    "conversation",
    "debug",
    "discuss",
    "do",
    "for",
    "from",
    "help",
    "hermes",
    "i",
    "in",
    "into",
    "is",
    "it",
    "me",
    "my",
    "of",
    "on",
    "please",
    "session",
    "that",
    "the",
    "this",
    "to",
    "want",
    "we",
    "work",
    "with",
    "you",
}

_PACKAGE_NAMES = {
    "django",
    "fastapi",
    "flask",
    "hermes-agent",
    "nextjs",
    "nodejs",
    "pytest",
    "react",
    "ruff",
    "svelte",
    "vite",
    "vue",
}

_PATH_RE = re.compile(
    r"(?<![\w.-])(?:[~.]?/)?(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+(?:\.[A-Za-z0-9]+)?"
)
_CAMEL_CASE_RE = re.compile(r"\b[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+\b")
_TITLE_CASE_PHRASE_RE = re.compile(r"\b[A-Z][a-z0-9]+(?:\s+[A-Z][a-z0-9]+){1,3}\b")
_QUOTED_PHRASE_RE = re.compile(r"[\"'“‘]([^\"'”’]{3,80})[\"'”’]")
_SLASH_COMMAND_RE = re.compile(r"(?<!\S)/[A-Za-z][A-Za-z0-9_-]*\b")
_TOOL_NAME_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9]*(?:[_.][A-Za-z_][A-Za-z0-9_]*)+\b")
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]*")
# Keep Han runs intact: these are bounded literal phrases, not semantic segmentation.
# Include Extension A and supplementary ideographs (e.g. 𠮷), as well as common Han.
_HAN_RANGES = "\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U0002fa1f\U00030000-\U000323af"
_HAN_RE = re.compile(f"[{_HAN_RANGES}]")
_MIXED_WORD_RE = re.compile(f"[A-Za-z0-9{_HAN_RANGES}]+")
_CHINESE_QUOTE_RE = re.compile(r"[\"'“‘「『]([^\"'”’」』]{2,80})[\"'”’」』]")
_CHINESE_TEXT_LIMIT = 512
_CHINESE_NAME_LIMIT = 48
_CHINESE_CANDIDATE_LIMIT = 8
_CHINESE_QUOTE_LIMIT = 4
_PACKAGE_RE = re.compile(
    r"\b(?:"
    + "|".join(re.escape(name) for name in sorted(_PACKAGE_NAMES, key=len, reverse=True))
    + r")\b",
    re.IGNORECASE,
)
_REPO_STYLE_RE = re.compile(r"\b[a-z][a-z0-9]+(?:-[a-z0-9]+)+\b")


def slugify_subject(name: str) -> str:
    """Return an idempotent URL slug, retaining Han and all legacy ASCII slugs.

    Unicode path segments are percent-encoded by HTTP clients. Retaining the
    characters avoids merging unrelated Chinese names into ``subject`` (or
    merging mixed names that happen to share the same Latin suffix).
    """

    slug = re.sub(f"[^a-z0-9{_HAN_RANGES}]+", "-", name.lower()).strip("-")
    return slug or "subject"


def extract_subject_candidates(session: dict, messages: list[dict]) -> list[SubjectCandidate]:
    """Extract deterministic subject candidates from a session and its messages.

    Only user content and the title introduce or score topics. User evidence
    ranks first; assistant content is used separately for matching context.
    """

    candidates: dict[str, SubjectCandidate] = {}
    chinese_slugs: set[str] = set()

    def add(name: str, *, score: int = 1, source: str = "text", user: bool = True) -> None:
        cleaned = _clean_candidate_name(name)
        if not cleaned:
            return
        slug = slugify_subject(cleaned)
        existing = candidates.get(slug)
        if existing is None:
            if _HAN_RE.search(cleaned):
                if len(chinese_slugs) >= _CHINESE_CANDIDATE_LIMIT:
                    return
                chinese_slugs.add(slug)
            candidates[slug] = SubjectCandidate(cleaned, slug, score, source, user)
        else:
            candidates[slug] = SubjectCandidate(
                existing.name,
                existing.slug,
                existing.score + score,
                existing.source,
                existing.user_supported or user,
            )

    title = str(session.get("title") or "")
    if title:
        title_phrase = _keyword_phrase(title)
        if title_phrase:
            add(title_phrase, score=6, source="title", user=False)
        # Title quotes are title evidence too: they rank, but never as user-raised topics.
        _extract_chinese_quotes(title, lambda name, **kw: add(name, user=False, **kw), base_score=4)

    first_user_text_seen = False
    for message in messages:
        if message.get("role") != "user":
            continue
        content = _message_text(message)
        _extract_from_text(content, add, base_score=3)
        # Every user turn supplies its English keyword phrase; an unquoted Han phrase comes only
        # from the first non-empty user text, so long Chinese prompts stay bounded.
        phrase = _keyword_phrase(content, allow_chinese=not first_user_text_seen)
        if content.strip():
            first_user_text_seen = True
        if phrase:
            add(phrase, score=3, source="user_phrase")
        _extract_chinese_quotes(content, add, base_score=3)

    return sorted(candidates.values(), key=lambda c: (
        not c.user_supported, -c.score, c.name.lower(),
    ))


def _is_identifier(match: str) -> bool:
    # Prose also matches the dotted pattern: abbreviations ("e.g", "i.e") have one-letter parts and a
    # missing space after a full stop ("it.Then") puts a capital after the dot.
    if "_" in match:
        return True
    parts = match.split(".")
    return all(len(part) >= 2 for part in parts) and not any(part[0].isupper() for part in parts[1:])


def _extract_from_text(text: str, add, *, base_score: int) -> None:
    for match in _TOOL_NAME_RE.findall(text):
        if _is_identifier(match):
            add(match, score=base_score + 1, source="identifier")
    for match in _PATH_RE.findall(text):
        add(match, score=base_score + 3, source="path")
    for match in _QUOTED_PHRASE_RE.findall(text):
        # Preserve legacy English extraction for all roles. Han quotes are
        # handled separately, only in metadata and user-authored text.
        phrase = _keyword_phrase(match, allow_chinese=False)
        if phrase:
            add(phrase, score=base_score + 2, source="quote")
    for match in _SLASH_COMMAND_RE.findall(text):
        add(match, score=base_score + 1, source="slash_command")
    for match in _TITLE_CASE_PHRASE_RE.findall(text):
        phrase = _keyword_phrase(match)
        if phrase:
            add(phrase, score=base_score + 2, source="title_case_phrase")
    for match in _CAMEL_CASE_RE.findall(text):
        add(match, score=base_score + 1, source="identifier")
    for match in _PACKAGE_RE.findall(text):
        add(match.lower(), score=base_score + 2, source="package")
    for match in _REPO_STYLE_RE.findall(text):
        add(match, score=base_score + 2, source="package")


def _extract_chinese_quotes(text: str, add, *, base_score: int) -> None:
    """Only inspect a short prefix and a few explicit quoted Han topics."""
    count = 0
    for match in _CHINESE_QUOTE_RE.finditer(text[:_CHINESE_TEXT_LIMIT]):
        if not _HAN_RE.search(match[1]):
            continue
        phrase = _keyword_phrase(match[1])
        if phrase:
            add(phrase, score=base_score + 2, source="quote")
            count += 1
            if count >= _CHINESE_QUOTE_LIMIT:
                break


def _keyword_phrase(text: str, *, allow_chinese: bool = True) -> str:
    # No n-grams, dictionary, model, or scanning of assistant/tool/system prose.
    # Literal Han/Latin/digit runs support the existing substring search; bound
    # both inspected text and display length so long Chinese prompts stay small.
    prefix = text[:_CHINESE_TEXT_LIMIT]
    if allow_chinese and _HAN_RE.search(prefix):
        words = [word.lower() for word in _MIXED_WORD_RE.findall(prefix)]
        words = [word for word in words if word not in _STOPWORDS]
        phrase = " ".join(words[:4])[:_CHINESE_NAME_LIMIT].strip()
        return phrase if len(phrase) >= 2 else ""

    # Keep the English heuristic (including hyphen aliases and score merging).
    normalized = re.sub(r"(?<=[A-Za-z0-9])[-_](?=[A-Za-z0-9])", " ", text)
    words = [word.lower() for word in _WORD_RE.findall(normalized)]
    words = [word for word in words if word not in _STOPWORDS and len(word) > 1]
    if len(words) < 2:
        return ""
    return " ".join(words[:4])


def _clean_candidate_name(name: str) -> str:
    cleaned = " ".join(str(name).strip().split())
    if not cleaned:
        return ""
    if cleaned.lower() in _STOPWORDS:
        return ""
    return cleaned


def build_memory_subjects(
    db,
    *,
    limit: int = 100,
    query: str | None = None,
    exact_slug: str | None = None,
    cache=None,
) -> list[dict]:
    """Aggregate recent subjects, optionally restricting to one normalized slug.

    ``exact_slug`` is an internal detail-lookup filter applied before aggregation
    and ranking; it does not change the session window or list query semantics.
    ``cache`` is an optional :class:`memory_wiki_cache.SessionUnitCache`; when
    given, only sessions whose cheap fingerprint changed are re-scanned and
    every other session reuses its memoized per-session aggregation unit.
    """

    if limit <= 0:
        return []
    sessions = _recent_sessions(db, limit=max(limit * 5, limit, 100))
    units = _session_units(db, sessions, cache)
    subjects: dict[str, dict] = {}

    for unit in units:
        candidates = [SubjectCandidate(**candidate) for candidate in unit["candidates"]]
        if exact_slug is not None:
            candidates = [candidate for candidate in candidates if candidate.slug == exact_slug]
        if not candidates:
            continue
        session_info = unit["session"]
        first_seen = unit["first_seen"]
        last_seen = unit["last_seen"]
        message_count = unit["message_count"]
        snippets_by_slug = unit["snippets_by_slug"]

        for candidate in candidates:
            subject = subjects.setdefault(
                candidate.slug,
                {
                    "slug": candidate.slug,
                    "name": candidate.name,
                    "keywords": [],
                    "session_count": 0,
                    "message_count": 0,
                    "first_seen": first_seen,
                    "last_seen": last_seen,
                    "sessions": [],
                    "snippets": [],
                    "_score": 0,
                    "_user_supported": False,
                    "_session_ids": set(),
                },
            )
            subject["_score"] += candidate.score
            subject["_user_supported"] |= candidate.user_supported
            if candidate.name not in subject["keywords"]:
                subject["keywords"].append(candidate.name)
            subject["first_seen"] = min(subject["first_seen"], first_seen)
            subject["last_seen"] = max(subject["last_seen"], last_seen)
            if session_info["id"] not in subject["_session_ids"]:
                subject["_session_ids"].add(session_info["id"])
                subject["sessions"].append(session_info)
                subject["session_count"] += 1
                subject["message_count"] += message_count
            subject["snippets"].extend(snippets_by_slug.get(candidate.slug, []))

    ranked = sorted(subjects.values(), key=lambda s: (
        not s["_user_supported"], -s["_score"], -s["session_count"], s["name"].lower(),
    ))
    results = [_finalize_subject(subject) for subject in ranked]
    if query:
        needle = query.lower().strip()
        results = [
            subject
            for subject in results
            if needle in subject["slug"]
            or needle in subject["name"].lower()
            or any(needle in keyword.lower() for keyword in subject["keywords"])
        ]

    return results[:limit]


def get_memory_subject(db, slug: str, *, cache=None) -> dict | None:
    """Return one aggregated memory subject by slug, or ``None``."""

    normalized = slugify_subject(slug)
    for subject in build_memory_subjects(db, limit=1000, exact_slug=normalized, cache=cache):
        if subject["slug"] == normalized:
            return subject
    return None


def build_daily_logs(db, *, limit_days: int = 60, cache=None) -> list[dict]:
    """Aggregate recent sessions into API-ready day log dictionaries."""

    if limit_days <= 0:
        return []
    # The daily log is an activity record: it keeps cron and subagent runs (#379).
    sessions = _recent_sessions(db, limit=max(limit_days * 25, 100), conversations_only=False)
    units = _session_units(db, sessions, cache)
    days: dict[str, dict] = {}

    # A session that spans midnight counts on every day it has messages (#262): each unit
    # carries one bucket per message day, so the cached path buckets exactly like the scan.
    for unit in units:
        session_info = unit["session"]
        for bucket in unit["days"]:
            day_key = bucket["day_key"]
            started = bucket["started_at"]
            last_active = bucket["last_active"]
            day = days.setdefault(
                day_key,
                {
                    "date": day_key,
                    "started_at_min": started,
                    "last_active_max": last_active,
                    "session_count": 0,
                    "message_count": 0,
                    "subjects": [],
                    "sessions": [],
                    "work_items": [],
                    "_subjects": {},
                },
            )
            day["started_at_min"] = min(day["started_at_min"], started)
            day["last_active_max"] = max(day["last_active_max"], last_active)
            day["session_count"] += 1
            day["message_count"] += bucket["message_count"]
            day["sessions"].append(session_info)

            for candidate in (SubjectCandidate(**candidate) for candidate in bucket["candidates"]):
                entry = day["_subjects"].setdefault(
                    candidate.slug,
                    {"slug": candidate.slug, "name": candidate.name, "count": 0,
                     "_user_supported": False, "_score": 0},
                )
                entry["count"] += 1
                entry["_user_supported"] |= candidate.user_supported
                entry["_score"] += candidate.score

            day["work_items"].extend(bucket["work_items"])

    logs = []
    for day in days.values():
        subjects = list(day.pop("_subjects").values())
        subjects.sort(key=lambda s: (
            not s["_user_supported"], -s["_score"], -s["count"], s["name"].lower(),
        ))
        for subject in subjects:
            subject.pop("_user_supported")
            subject.pop("_score")
        day["subjects"] = subjects[:20]
        day["sessions"].sort(key=lambda s: (-s["last_active"], s["id"]))
        day["work_items"].sort(key=lambda w: (w["timestamp"], w["session_id"], w["text"]))
        logs.append(day)

    logs.sort(key=lambda d: d["date"], reverse=True)
    return logs[:limit_days]


def get_daily_log(db, date: str, *, cache=None) -> dict | None:
    """Return one daily memory log by ``YYYY-MM-DD`` date, or ``None``."""

    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date or ""):
        return None
    for day in build_daily_logs(db, limit_days=1000, cache=cache):
        if day["date"] == date:
            return day
    return None


def build_memory_notes(memory_dir: Path | None = None) -> dict:
    """Return the curated persistent memory entries (MEMORY.md / USER.md), read-only.

    Every derived wiki view should link back to the memory the agent actually carries so the
    user can audit and prune it. Files are parsed with the same public entry delimiter the
    ``memory`` tool writes (``tools.memory_tool.ENTRY_DELIMITER``), so the wiki never disagrees
    with the system prompt about what an "entry" is. Edits stay with the ``memory`` tool.
    """

    from tools.memory_tool import ENTRY_DELIMITER, get_memory_dir

    root = memory_dir if memory_dir is not None else get_memory_dir()
    notes: dict[str, list[str]] = {}
    for target, filename in (("memory", "MEMORY.md"), ("user", "USER.md")):
        notes[target] = _read_memory_entries(root / filename, ENTRY_DELIMITER)
    return {
        "memory": notes["memory"],
        "user": notes["user"],
        "memory_count": len(notes["memory"]),
        "user_count": len(notes["user"]),
    }


def _read_memory_entries(path: Path, delimiter: str) -> list[str]:
    """Stripped, non-empty entries of one memory file; ``[]`` when missing or unreadable.
    ``utf-8-sig`` drops a Notepad BOM off the first entry, matching the memory tool's reader."""

    try:
        raw = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return []
    return [entry for entry in (chunk.strip() for chunk in raw.split(delimiter)) if entry]


def build_memory_overview(
    db,
    *,
    subject_limit: int = 50,
    day_limit: int = 30,
    cache=None,
) -> dict:
    """Return overview payload for Memory Wiki dashboard/API consumers."""

    recent = db.list_sessions_rich(
        limit=10,
        include_children=False,
        order_by_last_active=True,
        exclude_sources=list(NON_CONVERSATION_SOURCES),
    )
    return {
        "subjects": build_memory_subjects(db, limit=subject_limit, cache=cache),
        "daily_logs": build_daily_logs(db, limit_days=day_limit, cache=cache),
        "recent_sessions": [_session_info(session) for session in recent],
        "notes": build_memory_notes(),
    }


# Cron scaffolding and delegated subagents are activity records, not conversations. Their prompt
# text otherwise dominates the subject index and the Recent sessions list (#379). The daily log
# keeps full coverage: it is an activity record, so it still includes them. The filter runs in the
# query so the limit counts conversations: filtering after it left Recent sessions empty whenever the
# newest rows were all cron runs.
NON_CONVERSATION_SOURCES = ("cron", "subagent")

_CORE_CACHE_MODULE = "hermes_dashboard_plugin_memory_wiki_cache"
_CACHE_LOAD_LOCK = threading.Lock()


def _cache_module():
    """The sibling ``memory_wiki_cache`` module, loaded by path the same way the host
    loads this file (``spec_from_file_location`` with no package)."""

    mod = sys.modules.get(_CORE_CACHE_MODULE)
    if mod is not None:
        return mod
    with _CACHE_LOAD_LOCK:  # double-checked: concurrent first requests must not exec twice
        mod = sys.modules.get(_CORE_CACHE_MODULE)
        if mod is not None:
            return mod
        path = Path(__file__).with_name("memory_wiki_cache.py")
        spec = importlib.util.spec_from_file_location(_CORE_CACHE_MODULE, path)
        if spec is None or spec.loader is None:  # pragma: no cover
            raise ImportError(f"cannot load memory-wiki cache module from {path}")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[_CORE_CACHE_MODULE] = mod
        try:
            spec.loader.exec_module(mod)
        except Exception:
            sys.modules.pop(_CORE_CACHE_MODULE, None)
            raise
        return mod


def _recent_sessions(db, *, limit: int, conversations_only: bool = True) -> list[dict]:
    """Recent sessions (hidden ones included) in the aggregation order every wiki view relies on."""

    kwargs = dict(
        limit=limit,
        include_children=False,
        order_by_last_active=True,
        exclude_sources=list(NON_CONVERSATION_SOURCES) if conversations_only else None,
    )
    try:
        return db.list_sessions_rich(include_hidden=True, **kwargs)
    except TypeError as exc:
        if "include_hidden" not in str(exc) or "unexpected keyword" not in str(exc):
            raise
        return db.list_sessions_rich(**kwargs)


def _session_units(db, sessions: list[dict], cache) -> list[dict]:
    """Per-session aggregation units in ``sessions`` order.

    With ``cache`` given, only sessions whose fingerprint changed (or that are
    unseen so far) trigger a ``get_messages`` scan; everything else comes from
    memoized units, which makes warm dashboard requests milliseconds cheap.
    The fingerprint is stamped only in this cached branch — the uncached path
    stays exactly the pre-cache aggregation with no cache-module dependency.
    """

    if cache is None:
        return [
            _build_session_unit(session, db.get_messages(session["id"]))
            for session in sessions
        ]

    session_fingerprint = _cache_module().session_fingerprint
    units_by_id: dict[str, dict] = {}
    stale: list[dict] = []
    for session in sessions:
        cached = cache.lookup(session)
        if cached is None:
            stale.append(session)
        else:
            units_by_id[str(session["id"])] = cached
    for session in stale:
        unit = _build_session_unit(session, db.get_messages(session["id"]))
        unit["fingerprint"] = session_fingerprint(session)
        cache.store(unit)
        units_by_id[str(session["id"])] = unit
    return [units_by_id[str(session["id"])] for session in sessions]


def _build_session_unit(session: dict, messages: list[dict]) -> dict:
    """Everything the aggregators derive from one session and its messages.

    Pure function of the session row plus its messages, so it is safe to
    memoize under ``memory_wiki_cache.session_fingerprint(session)`` and reuse
    across both the subject and the daily-log aggregations. ``day_key`` is
    derived here in local time; cached units keep their day bucket until the
    session's next write, which is an acceptable staleness window.
    """

    candidates = extract_subject_candidates(session, messages)
    first_ts = _first_timestamp(session, messages)
    # No fallback snippet: a subject shows only snippets that mention it, so a title-only
    # subject is not padded with an unrelated first message (#48).
    return {
        "session": _session_info(session),
        "candidates": [
            {
                "name": candidate.name,
                "slug": candidate.slug,
                "score": candidate.score,
                "source": candidate.source,
                "user_supported": candidate.user_supported,
            }
            for candidate in candidates
        ],
        "first_seen": first_ts,
        "last_seen": _last_timestamp(session, messages),
        "message_count": len(messages),
        "snippets_by_slug": (
            _snippets_for_candidates(session, messages, candidates) if candidates else {}
        ),
        "days": _day_buckets(session, messages),
    }


def _day_buckets(session: dict, messages: list[dict]) -> list[dict]:
    """One daily-log bucket per local message day; a message-less session lands on its start day."""

    session_started = float(session.get("started_at") or _first_timestamp(session, messages))
    messages_by_day: dict[str, list[dict]] = {}
    for message in messages:
        timestamp = message.get("timestamp")
        day_key = _date_key(float(timestamp) if timestamp is not None else session_started)
        messages_by_day.setdefault(day_key, []).append(message)
    if not messages_by_day:
        messages_by_day[_date_key(session_started)] = []

    buckets = []
    for day_key, day_messages in messages_by_day.items():
        timestamps = [
            float(message["timestamp"])
            for message in day_messages
            if message.get("timestamp") is not None
        ]
        buckets.append(
            {
                "day_key": day_key,
                "started_at": min(timestamps) if timestamps else session_started,
                "last_active": max(timestamps) if timestamps else _last_timestamp(session, day_messages),
                "message_count": len(day_messages),
                "candidates": [
                    {
                        "name": candidate.name,
                        "slug": candidate.slug,
                        "score": candidate.score,
                        "source": candidate.source,
                        "user_supported": candidate.user_supported,
                    }
                    for candidate in extract_subject_candidates(session, day_messages)
                ],
                "work_items": _work_items_for_session(session, day_messages),
            }
        )
    return buckets


def _session_info(session: dict) -> dict:
    return {
        "id": session.get("id"),
        "title": session.get("title"),
        "source": session.get("source"),
        "started_at": float(session.get("started_at") or 0),
        "last_active": float(session.get("last_active") or session.get("started_at") or 0),
        "preview": session.get("preview") or "",
    }


def _first_timestamp(session: dict, messages: list[dict]) -> float:
    values = [float(m["timestamp"]) for m in messages if m.get("timestamp") is not None]
    values.append(float(session.get("started_at") or 0))
    return min(values)


def _last_timestamp(session: dict, messages: list[dict]) -> float:
    values = [float(m["timestamp"]) for m in messages if m.get("timestamp") is not None]
    values.append(float(session.get("last_active") or session.get("started_at") or 0))
    return max(values)


def _date_key(timestamp: float) -> str:
    """Return the local-calendar date key for ``timestamp``."""

    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d")


def _message_text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, list):
        content = " ".join(
            block["text"] for block in content
            if isinstance(block, dict) and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        )
    if isinstance(content, str):
        return " ".join(content.split())
    return ""


_SNIPPETS_PER_SLUG = 5  # _finalize_subject keeps the top 5 of the merged, sorted list,
# and the top-5 of a union always equals the top-5 of merging the per-part top-5s.


def _snippets_for_candidates(
    session: dict,
    messages: list[dict],
    candidates: list[SubjectCandidate],
) -> dict[str, list[dict]]:
    snippets: dict[str, list[dict]] = {candidate.slug: [] for candidate in candidates}
    for message in messages:
        if message.get("role") not in ("user", "assistant"):
            continue
        text = _message_text(message)
        for candidate in candidates:
            if _matches_candidate(text, candidate):
                snippets[candidate.slug].append(_snippet(session, message, text))

    # Keep only the snippets the aggregation can actually surface: per slug the
    # finalize step sorts by (timestamp, session_id, message_id) and keeps the
    # first 5, so storing each session's 5 smallest makes units small without
    # changing any output — this is what keeps the cached units bounded.
    for slug, items in snippets.items():
        if len(items) > _SNIPPETS_PER_SLUG:
            items.sort(key=lambda s: (s["timestamp"], s.get("message_id") or 0))
            snippets[slug] = items[:_SNIPPETS_PER_SLUG]
    return snippets


def _matches_candidate(text: str, candidate: SubjectCandidate) -> bool:
    normalized = re.sub(f"[^a-z0-9{_HAN_RANGES}]+", "-", text.lower()).strip("-")
    if not normalized:
        return False
    if f"-{candidate.slug}-" in f"-{normalized}-":
        return True
    # Normalize whole-message, quoted and title-case phrases as extraction does.
    texts = [text, *_QUOTED_PHRASE_RE.findall(text), *_TITLE_CASE_PHRASE_RE.findall(text)]
    for phrase_text in texts:
        phrase = _keyword_phrase(phrase_text)
        if phrase and candidate.slug == slugify_subject(phrase):
            return True
    return False


def _snippet(session: dict, message: dict, text: str) -> dict:
    return {
        "message_id": message.get("id"),
        "session_id": session.get("id"),
        "role": message.get("role"),
        "timestamp": float(message.get("timestamp") or session.get("started_at") or 0),
        "text": text[:240],
    }


def _finalize_subject(subject: dict) -> dict:
    result = dict(subject)
    result.pop("_score", None)
    result.pop("_user_supported", None)
    result.pop("_session_ids", None)
    result["keywords"] = result["keywords"][:10]
    result["sessions"].sort(key=lambda s: (-s["last_active"], s["id"]))
    result["sessions"] = result["sessions"][:10]
    result["snippets"].sort(key=lambda s: (
        s["role"] != "user", s["timestamp"], s["session_id"], s.get("message_id") or 0,
    ))
    result["snippets"] = result["snippets"][:5]
    return result


def _work_items_for_session(session: dict, messages: list[dict]) -> list[dict]:
    users = []
    support = []
    candidates = extract_subject_candidates(session, messages)
    for message in messages:
        role = message.get("role")
        text = _message_text(message)
        if role == "user" and text:
            users.append(_work_item(session, message, _classify_work(text), text))
        elif role == "assistant" and text:
            lowered = text.lower()
            if (any(word in lowered for word in ("implemented", "added", "fixed", "updated"))
                    and any(_matches_candidate(text, candidate) for candidate in candidates)):
                item = _work_item(session, message, _classify_work(text), text)
                if not any(existing["text"] == item["text"] for existing in support):
                    support.append(item)
    return (users + support)[:8]


def _work_item(session: dict, message: dict, kind: str, text: str) -> dict:
    return {
        "session_id": session.get("id"),
        "kind": kind,
        "text": " ".join(text.split())[:240],
        "timestamp": float(message.get("timestamp") or session.get("started_at") or 0),
    }


def _classify_work(text: str) -> str:
    lowered = text.lower()
    if any(word in lowered for word in ("test", "fix", "debug", "implement", "code", ".py", ".tsx")):
        return "coding"
    if any(word in lowered for word in ("research", "investigate", "review", "search")):
        return "research"
    if any(word in lowered for word in ("plan", "roadmap", "design")):
        return "planning"
    return "conversation"
