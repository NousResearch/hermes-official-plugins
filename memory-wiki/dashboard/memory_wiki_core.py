"""Memory Wiki aggregation — deterministic subject/daily-log derivation over session history.

Originally ``hermes_cli/memory_wiki.py`` by Abdullah Raja (@Araja119, hermes-agent PR #31244),
salvaged in #89940 and moved into this standalone plugin. Stays stdlib-only: the helpers are
deterministic heuristics over the session/message dictionaries ``hermes_state.SessionDB``
returns (``list_sessions_rich`` / ``get_messages``); no LLM calls, no writes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import re


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

    The heuristic favors explicit metadata and structured mentions: session
    titles, paths, tool names, slash commands, quoted phrases, and CamelCase
    identifiers.  Duplicate subjects are merged by slug while preserving the
    first useful display name.
    """

    candidates: dict[str, SubjectCandidate] = {}
    chinese_slugs: set[str] = set()

    def add(name: str, *, score: int = 1, source: str = "text") -> None:
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
        _extract_chinese_quotes(title, add, base_score=4)

    preview = str(session.get("preview") or "")
    _extract_from_text(preview, add, base_score=2)
    preview_phrase = _keyword_phrase(preview)
    if preview_phrase:
        add(preview_phrase, score=2, source="preview")
    _extract_chinese_quotes(preview, add, base_score=2)

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
        if is_user and content.strip() and not first_user_text_seen:
            first_user_text_seen = True
            phrase = _keyword_phrase(content)
            if phrase:
                add(phrase, score=3, source="user_phrase")
        if is_user:
            _extract_chinese_quotes(content, add, base_score=3)

    return sorted(candidates.values(), key=lambda c: (-c.score, c.name.lower()))


def _extract_from_text(text: str, add, *, base_score: int) -> None:
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
    db, *, limit: int = 100, query: str | None = None, exact_slug: str | None = None,
) -> list[dict]:
    """Aggregate recent subjects, optionally restricting to one normalized slug.

    ``exact_slug`` is an internal detail-lookup filter applied before aggregation
    and ranking; it does not change the session window or list query semantics.
    """

    if limit <= 0:
        return []
    sessions = _load_memory_sessions(db, limit=max(limit * 5, limit, 100))
    subjects: dict[str, dict] = {}

    for session, messages in sessions:
        session_info = _session_info(session)
        candidates = extract_subject_candidates(session, messages)
        if exact_slug is not None:
            candidates = [candidate for candidate in candidates if candidate.slug == exact_slug]
        if not candidates:
            continue
        message_count = len(messages)
        first_seen = _first_timestamp(session, messages)
        last_seen = _last_timestamp(session, messages)
        snippets_by_slug = _snippets_for_candidates(session, messages, candidates)

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


def get_memory_subject(db, slug: str) -> dict | None:
    """Return one aggregated memory subject by slug, or ``None``."""

    normalized = slugify_subject(slug)
    for subject in build_memory_subjects(db, limit=1000, exact_slug=normalized):
        if subject["slug"] == normalized:
            return subject
    return None


def build_daily_logs(db, *, limit_days: int = 60) -> list[dict]:
    """Aggregate recent sessions into API-ready day log dictionaries."""

    if limit_days <= 0:
        return []
    sessions = _load_memory_sessions(db, limit=max(limit_days * 25, 100))
    days: dict[str, dict] = {}

    for session, messages in sessions:
        day_key = _date_key(float(session.get("started_at") or _first_timestamp(session, messages)))
        started = float(session.get("started_at") or _first_timestamp(session, messages))
        last_active = _last_timestamp(session, messages)
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
        day["message_count"] += len(messages)
        day["sessions"].append(_session_info(session))

        for candidate in extract_subject_candidates(session, messages):
            entry = day["_subjects"].setdefault(
                candidate.slug,
                {"slug": candidate.slug, "name": candidate.name, "count": 0},
            )
            entry["count"] += 1

        day["work_items"].extend(_work_items_for_session(session, messages))

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


def get_daily_log(db, date: str) -> dict | None:
    """Return one daily memory log by ``YYYY-MM-DD`` date, or ``None``."""

    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date or ""):
        return None
    for day in build_daily_logs(db, limit_days=1000):
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


def build_memory_overview(db, *, subject_limit: int = 50, day_limit: int = 30) -> dict:
    """Return overview payload for Memory Wiki dashboard/API consumers."""

    recent = db.list_sessions_rich(
        limit=10,
        include_children=False,
        order_by_last_active=True,
    )
    return {
        "subjects": build_memory_subjects(db, limit=subject_limit),
        "daily_logs": build_daily_logs(db, limit_days=day_limit),
        "recent_sessions": [_session_info(session) for session in recent],
        "notes": build_memory_notes(),
    }


def _load_memory_sessions(db, *, limit: int) -> list[tuple[dict, list[dict]]]:
    sessions = db.list_sessions_rich(
        limit=limit,
        include_children=False,
        order_by_last_active=True,
    )
    return [(session, db.get_messages(session["id"])) for session in sessions]


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

    fallback = None
    for message in messages:
        text = _message_text(message)
        if text:
            fallback = _snippet(session, message, text)
            break
    if fallback:
        for slug in snippets:
            if not snippets[slug]:
                snippets[slug].append(dict(fallback))
    return snippets


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
