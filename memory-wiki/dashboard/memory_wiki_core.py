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
_TOOL_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]*")
_PACKAGE_RE = re.compile(
    r"\b(?:"
    + "|".join(re.escape(name) for name in sorted(_PACKAGE_NAMES, key=len, reverse=True))
    + r")\b",
    re.IGNORECASE,
)
_REPO_STYLE_RE = re.compile(r"\b[a-z][a-z0-9]+(?:-[a-z0-9]+)+\b")


def slugify_subject(name: str) -> str:
    """Return a stable URL slug for a subject name."""

    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "subject"


def extract_subject_candidates(session: dict, messages: list[dict]) -> list[SubjectCandidate]:
    """Extract deterministic subject candidates from a session and its messages.

    The heuristic favors explicit metadata and structured mentions: session
    titles, paths, tool names, slash commands, quoted phrases, and CamelCase
    identifiers.  Duplicate subjects are merged by slug while preserving the
    first useful display name.
    """

    candidates: dict[str, SubjectCandidate] = {}

    def add(name: str, *, score: int = 1, source: str = "text") -> None:
        cleaned = _clean_candidate_name(name)
        if not cleaned:
            return
        slug = slugify_subject(cleaned)
        existing = candidates.get(slug)
        if existing is None:
            candidates[slug] = SubjectCandidate(cleaned, slug, score, source)
        else:
            candidates[slug] = SubjectCandidate(
                existing.name,
                existing.slug,
                existing.score + score,
                existing.source,
            )

    title = str(session.get("title") or "")
    if title:
        title_phrase = _keyword_phrase(title)
        if title_phrase:
            add(title_phrase, score=6, source="title")

    preview = str(session.get("preview") or "")
    _extract_from_text(preview, add, base_score=2)
    preview_phrase = _keyword_phrase(preview)
    if preview_phrase:
        add(preview_phrase, score=2, source="preview")

    first_user_text_seen = False
    for message in messages:
        tool_name = str(message.get("tool_name") or "")
        if tool_name and _TOOL_NAME_RE.match(tool_name):
            add(tool_name, score=5, source="tool")

        tool_calls = message.get("tool_calls") or ()
        if not isinstance(tool_calls, (list, tuple)):
            tool_calls = ()
        for tool_call in tool_calls:
            if isinstance(tool_call, dict):
                function = tool_call.get("function")
                function_name = function.get("name") if isinstance(function, dict) else None
                name = tool_call.get("name") or function_name
                if name and _TOOL_NAME_RE.match(str(name)):
                    add(str(name), score=5, source="tool")

        content = str(message.get("content") or "")
        is_user = message.get("role") == "user"
        _extract_from_text(content, add, base_score=3 if is_user else 1)
        if is_user and not first_user_text_seen:
            first_user_text_seen = True
            phrase = _keyword_phrase(content)
            if phrase:
                add(phrase, score=3, source="user_phrase")

    return sorted(candidates.values(), key=lambda c: (-c.score, c.name.lower()))


def _extract_from_text(text: str, add, *, base_score: int) -> None:
    for match in _PATH_RE.findall(text):
        add(match, score=base_score + 3, source="path")
    for match in _QUOTED_PHRASE_RE.findall(text):
        phrase = _keyword_phrase(match)
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


def _keyword_phrase(text: str) -> str:
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
    cache=None,
) -> list[dict]:
    """Aggregate subject dictionaries from recent ``SessionDB`` sessions.

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
                    "_session_ids": set(),
                },
            )
            subject["_score"] += candidate.score
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

    results = [_finalize_subject(subject) for subject in subjects.values()]
    if query:
        needle = query.lower().strip()
        results = [
            subject
            for subject in results
            if needle in subject["slug"]
            or needle in subject["name"].lower()
            or any(needle in keyword.lower() for keyword in subject["keywords"])
        ]

    results.sort(key=lambda s: (-s["session_count"], -s["last_seen"], s["name"].lower()))
    return results[:limit]


def get_memory_subject(db, slug: str, *, cache=None) -> dict | None:
    """Return one aggregated memory subject by slug, or ``None``."""

    normalized = slugify_subject(slug)
    for subject in build_memory_subjects(db, limit=1000, cache=cache):
        if subject["slug"] == normalized:
            return subject
    return None


def build_daily_logs(db, *, limit_days: int = 60, cache=None) -> list[dict]:
    """Aggregate recent sessions into API-ready day log dictionaries."""

    if limit_days <= 0:
        return []
    sessions = _recent_sessions(db, limit=max(limit_days * 25, 100))
    units = _session_units(db, sessions, cache)
    days: dict[str, dict] = {}

    for unit in units:
        session_info = unit["session"]
        day_key = unit["day_key"]
        started = unit["started_at"]
        last_active = unit["last_seen"]
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
        day["message_count"] += unit["message_count"]
        day["sessions"].append(session_info)

        for candidate in (SubjectCandidate(**candidate) for candidate in unit["candidates"]):
            entry = day["_subjects"].setdefault(
                candidate.slug,
                {"slug": candidate.slug, "name": candidate.name, "count": 0},
            )
            entry["count"] += 1

        day["work_items"].extend(unit["work_items"])

    logs = []
    for day in days.values():
        subjects = list(day.pop("_subjects").values())
        subjects.sort(key=lambda s: (-s["count"], s["name"].lower()))
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

    recent = _recent_sessions(db, limit=10)
    return {
        "subjects": build_memory_subjects(db, limit=subject_limit, cache=cache),
        "daily_logs": build_daily_logs(db, limit_days=day_limit, cache=cache),
        "recent_sessions": [_session_info(session) for session in recent],
        "notes": build_memory_notes(),
    }


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


def _recent_sessions(db, *, limit: int) -> list[dict]:
    """Recent sessions in the aggregation order every wiki view relies on."""

    return db.list_sessions_rich(
        limit=limit,
        include_children=False,
        order_by_last_active=True,
    )


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
            _materialize_unit(_build_session_unit(session, db.get_messages(session["id"])))
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
            units_by_id[str(session["id"])] = _materialize_unit(cached)
    for session in stale:
        unit = _build_session_unit(session, db.get_messages(session["id"]))
        unit["fingerprint"] = session_fingerprint(session)
        cache.store(unit)
        units_by_id[str(session["id"])] = _materialize_unit(unit)
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
    # Snippets the aggregation actually has are stored per slug; the "no
    # snippets at all" fallback snippet is stored once — materializing it into
    # every empty slug at aggregation time keeps cached units small.
    fallback = None
    for message in messages:
        text = _message_text(message)
        if text:
            fallback = _snippet(session, message, text)
            break
    return {
        "session": _session_info(session),
        "candidates": [
            {
                "name": candidate.name,
                "slug": candidate.slug,
                "score": candidate.score,
                "source": candidate.source,
            }
            for candidate in candidates
        ],
        "fallback_snippet": fallback,
        "first_seen": first_ts,
        "last_seen": _last_timestamp(session, messages),
        "message_count": len(messages),
        "snippets_by_slug": (
            _snippets_for_candidates(session, messages, candidates) if candidates else {}
        ),
        "started_at": float(session.get("started_at") or first_ts),
        "day_key": _date_key(float(session.get("started_at") or first_ts)),
        "work_items": _work_items_for_session(session, messages),
    }


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
    if isinstance(content, str):
        return " ".join(content.split())
    if content is None:
        return ""
    return " ".join(str(content).split())


_SNIPPETS_PER_SLUG = 5  # _finalize_subject keeps the top 5 of the merged, sorted list,
# and the top-5 of a union always equals the top-5 of merging the per-part top-5s.


def _snippets_for_candidates(
    session: dict,
    messages: list[dict],
    candidates: list[SubjectCandidate],
) -> dict[str, list[dict]]:
    snippets: dict[str, list[dict]] = {candidate.slug: [] for candidate in candidates}
    candidate_by_slug = {candidate.slug: candidate for candidate in candidates}
    for message in messages:
        text = _message_text(message)
        haystack = " ".join((text, str(message.get("tool_name") or ""))).lower()
        if not haystack.strip():
            continue
        for slug, candidate in candidate_by_slug.items():
            terms = {candidate.name.lower(), slug.replace("-", " ")}
            if any(term and term in haystack for term in terms):
                snippets[slug].append(_snippet(session, message, text))
                break

    # Keep only the snippets the aggregation can actually surface: per slug the
    # finalize step sorts by (timestamp, session_id, message_id) and keeps the
    # first 5, so storing each session's 5 smallest makes units small without
    # changing any output — this is what keeps the cached units bounded.
    for slug, items in snippets.items():
        if len(items) > _SNIPPETS_PER_SLUG:
            items.sort(key=lambda s: (s["timestamp"], s.get("message_id") or 0))
            snippets[slug] = items[:_SNIPPETS_PER_SLUG]
    return snippets


def _materialize_unit(unit: dict) -> dict:
    """Fill empty slugs with the unit's fallback snippet, the way the aggregators read them.

    Units store the fallback once (it is identical for every slug of a session
    and can number hundreds of slugs); this expands it back to the exact shape
    the pre-cache aggregation consumed. The shared snippet dict is safe to
    share: aggregators only read snippets, never mutate them.
    """

    fallback = unit.get("fallback_snippet")
    if fallback is None:
        return unit
    out = dict(unit)
    out["snippets_by_slug"] = {
        slug: items if items else [fallback]
        for slug, items in unit["snippets_by_slug"].items()
    }
    return out


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
    result.pop("_session_ids", None)
    result["keywords"] = result["keywords"][:10]
    result["sessions"].sort(key=lambda s: (-s["last_active"], s["id"]))
    result["sessions"] = result["sessions"][:10]
    result["snippets"].sort(key=lambda s: (s["timestamp"], s["session_id"], s.get("message_id") or 0))
    result["snippets"] = result["snippets"][:5]
    return result


def _work_items_for_session(session: dict, messages: list[dict]) -> list[dict]:
    items = []
    for message in messages:
        role = message.get("role")
        text = _message_text(message)
        tool_name = str(message.get("tool_name") or "")
        if role == "tool" or tool_name:
            label = tool_name or text or "tool call"
            items.append(_work_item(session, message, "tool", f"Tool: {label}"))
        elif role == "user" and text:
            items.append(_work_item(session, message, _classify_work(text), text))
        elif role == "assistant" and text:
            lowered = text.lower()
            if any(word in lowered for word in ("implemented", "added", "fixed", "updated")):
                items.append(_work_item(session, message, _classify_work(text), text))
    return items[:8]


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
