"""``hermes import-agent grok-bot``: bring a Grok Bot's routines, skills, memories and role into Hermes.

Grok Bot keeps everything in its cloud account and has no export button, so the export is done by
the Bot itself: the user pastes :data:`EXPORT_PROMPT` into a Bot's chat, and the Bot writes its own
setup as one JSON document (a file it attaches, or a code block in its reply). This module reads
that document and maps it:

- routines with a schedule → Hermes cron jobs, created paused unless ``--activate-routines``
  (the Grok copy keeps running until the user turns it off, so two live copies would double-run);
- event-triggered routines (Slack, GitHub, email, webhook, ...) → listed in a notes file, since a
  schedule cannot express them;
- skills → ``skills/grok-bot-imports/<name>/SKILL.md``;
- memories → ``memories/MEMORY.md`` entries, facts about the user → ``memories/USER.md``;
- the Bot's role → ``SOUL.md`` while that still holds Hermes' seeded default, else memory entries;
- apps with a remote MCP URL → ``mcp_servers``; the others are reported (their logins stay in
  Grok Bot).

No network, no credentials: the document never carries a token, and secret-looking keys are refused.
"""

from __future__ import annotations

import inspect
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Optional, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .importer import (AgentImporter, atomic_write_text, extract_markdown_entries, is_secret_key,
                       read_text)

AGENT = "grok-bot"
EXPORT_FORMAT = "grok-bot-export"
SKILL_CATEGORY = "grok-bot-imports"
# The local Grok Bot desktop app (Electron userData / install dirs, per OS). Only used to tell the
# user an import is possible; nothing is read from them.
APP_DIRS = ("Library/Application Support/Grok Bot", "Applications/Grok Bot.app",
            ".config/Grok Bot", "AppData/Roaming/Grok Bot", "AppData/Local/Programs/Grok Bot")
SYSTEM_APP_DIRS = ("/Applications/Grok Bot.app",)
_EXPORT_GLOBS = ("grok-bot-export*.json", "grok-bot-export*.md", "grok-bot-export*.txt")
_FENCE_RE = re.compile(r"```(?:json)?\s*\n(.*?)\n```", re.DOTALL)
_SLUG_RE = re.compile(r"[^a-z0-9]+")
# Token shapes a Bot might paste into a memory or instruction despite the prompt; redacted on read.
_SECRET_VALUE_RE = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{16,}|xox[abprs]-[A-Za-z0-9-]{10,}|gh[pousr]_[A-Za-z0-9]{20,}|"
    r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})|(?i:bearer\s+[A-Za-z0-9._~+/-]{20,})")
_SCHEDULE_TRIGGERS = {"schedule", "scheduled", "time", "cron", "interval"}
# What chat apps and phones do to JSON on the way to a file: curly quotes, and trailing commas.
_SMART_QUOTES = str.maketrans({"\u201c": '"', "\u201d": '"', "\u2018": "'", "\u2019": "'"})
# Per-bot lists merged across documents (an export split over several messages or files).
_LIST_KEYS = ("routines", "skills", "apps", "memories", "user_facts")

EXPORT_PROMPT = """\
I'm moving to another assistant and need a complete, machine-readable copy of your setup. Do not \
change, pause or delete anything.

Write one JSON document with exactly this shape, then save it as grok-bot-export-<your name>.json \
(your name in lowercase, spaces as hyphens): in my Downloads folder if you can write to my computer, \
otherwise in your workspace, and attach it to your reply. If you cannot attach files, put the whole \
document in a single ```json code block instead.

{
  "format": "grok-bot-export",
  "version": 1,
  "bots": [
    {
      "name": "your name",
      "title": "your label/job title, or empty",
      "instructions": "your full role, job description and lasting rules, verbatim where you can",
      "memories": ["each thing you remember about the work, one string per fact"],
      "user_facts": ["each fact you know about me (name, time zone, preferences), one per string"],
      "skills": [
        {"name": "skill name", "description": "when to use it",
         "instructions": "the COMPLETE skill text, every step, rule and output format, verbatim"}
      ],
      "routines": [
        {"name": "routine name",
         "instruction": "the COMPLETE instruction the routine runs, verbatim",
         "trigger": "schedule | slack | github | linear | sentry | pagerduty | email | webhook | other",
         "schedule_cron": "5-field cron expression in the routine's time zone, e.g. 0 8 * * 1-5 \
(empty when not on a schedule)",
         "schedule_text": "When to run, in words, as the app shows it",
         "timezone": "IANA time zone, e.g. America/New_York",
         "paused": false,
         "skills": ["names of skills this routine uses"],
         "trigger_details": "for event triggers: channel, repository, phrase, senders, etc."}
      ],
      "apps": [{"name": "connected app or MCP server name", "mcp_url": "remote MCP URL if any, else empty"}]
    }
  ]
}

Rules: include EVERY routine you own (paused ones too) and EVERY skill enabled for you, with full \
text, not summaries. If you cannot read something (a routine's schedule, a skill's text), leave \
it out and tell me in your reply what you left out; never guess or fill it in from memory. Never include passwords, API keys, tokens, webhook keys, cookies or card \
numbers: leave those out entirely. Output valid JSON only (no comments).
If it is too long for one message or file, split it: send several documents, each complete and \
valid in this same shape with the same bot name, each holding the part the others do not. They \
are imported together.
"""


def _slug(name: str) -> str:
    return _SLUG_RE.sub("-", (name or "").strip().lower()).strip("-")[:64] or "grok-bot"


def detect_grok_bot() -> Optional[Path]:
    """The Grok Bot app dir or an export file found on this computer, else None."""
    home = Path.home()
    for candidate in [home / d for d in APP_DIRS] + [Path(d) for d in SYSTEM_APP_DIRS]:
        if candidate.exists():
            return candidate
    exports = find_export_files(None)
    return exports[0] if exports else None


def find_export_files(source: Optional[Path]) -> list[Path]:
    """The export documents to read: ``source`` itself (a file), the export-looking files in it (a
    directory), or with no ``source`` the ``grok-bot-export*`` files in ``~/Downloads``."""
    if source is not None and source.is_file():
        return [source]
    folder = source if source is not None else Path.home() / "Downloads"
    if not folder.is_dir():
        return []
    patterns = ("*.json", "*.md", "*.txt") if source is not None else _EXPORT_GLOBS
    # Oldest first, so where two files hold the same routine the newer export's copy wins.
    return sorted({p for pattern in patterns for p in folder.glob(pattern)
                   if p.is_file() and _mentions_export_format(p)}, key=lambda p: (p.stat().st_mtime, p.name))


def _mentions_export_format(path: Path) -> bool:
    """Cheap filter so a folder scan skips unrelated files instead of reporting them as errors."""
    try:
        return EXPORT_FORMAT in read_text(path)
    except OSError:
        return False


class ExportFormatError(ValueError):
    """The document is not a Grok Bot export this importer can read."""


def redact_secrets(value: Any) -> tuple[Any, int]:
    """``value`` with token-shaped strings replaced by ``[redacted]``, and how many were."""
    if isinstance(value, str):
        return _SECRET_VALUE_RE.subn("[redacted]", value)
    if isinstance(value, list):
        pairs = [redact_secrets(v) for v in value]
        return [v for v, _ in pairs], sum(n for _, n in pairs)
    if isinstance(value, dict):
        pairs = {k: redact_secrets(v) for k, v in value.items()}
        return {k: v for k, (v, _) in pairs.items()}, sum(n for _, n in pairs.values())
    return value, 0


def parse_export(text: str) -> list[dict[str, Any]]:
    """Return the bots in a Grok Bot export: a bare JSON document, or every ```json block holding one
    inside a pasted reply (a Bot may split a long export into several), merged per bot. Curly quotes
    and trailing commas, which copying through a chat app or phone adds, are repaired."""
    blocks = _FENCE_RE.findall(text)
    candidates = blocks or [text.strip()]
    if not blocks and "{" in text:
        candidates.append(text[text.index("{"):text.rindex("}") + 1])
    # Inside a reply, only export-shaped blocks count: an example list of routines is not a bot.
    documents = [d for d in (_load_document(c, bare_ok=not blocks) for c in candidates) if d is not None]
    if blocks and not documents:  # fenced, but none parsed: try the whole text once
        documents = [d for d in (_load_document(text.strip(), bare_ok=False),) if d is not None]
    if not documents:
        if text.count("{") > text.count("}"):
            raise ExportFormatError("the export looks cut off (more { than }); ask the Bot to send the "
                                    "rest as another document in the same shape, or one bot per message")
        raise ExportFormatError("no Grok Bot export found (expected a JSON document with "
                                f"\"format\": \"{EXPORT_FORMAT}\" and a \"bots\" list)")
    return merge_bots([bot for document in documents for bot in _validated_bots(document)])


def _load_document(candidate: str, bare_ok: bool) -> Optional[dict[str, Any]]:
    """``candidate`` as an export document: the export shape itself, or (``bare_ok``, for a file that
    is only JSON) a bare list of bots or a single bot object; None when it is none of those. Repairs
    are tried only after a strict parse fails, trailing commas alone before curly quotes."""
    for attempt in (candidate, _drop_trailing_commas(candidate),
                    _drop_trailing_commas(candidate.translate(_SMART_QUOTES))):
        try:
            data = json.loads(attempt)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, dict) and (data.get("format") == EXPORT_FORMAT or "bots" in data):
            return data
        if not bare_ok:
            continue
        if isinstance(data, list) and data and all(isinstance(b, dict) and "name" in b for b in data):
            return {"format": EXPORT_FORMAT, "version": 1, "bots": data}
        if isinstance(data, dict) and "name" in data and set(data) & {"instructions", *_LIST_KEYS}:
            return {"format": EXPORT_FORMAT, "version": 1, "bots": [data]}
    return None


def _drop_trailing_commas(text: str) -> str:
    """``text`` without commas that directly precede a ``}`` or ``]``, outside string values."""
    out: list[str] = []
    in_string = escaped = False
    for i, ch in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch == "," and text[i + 1:].lstrip()[:1] in ("}", "]"):
            continue
        out.append(ch)
    return "".join(out)


def merge_bots(bots: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """One bot per name (case-insensitive): lists are unioned, dropping only exact repeats (two
    different routines may share a name), scalar fields keep the last non-empty value, so a split
    or repeated export imports each thing once."""
    merged: dict[str, dict[str, Any]] = {}
    for bot in bots:
        key = str(bot["name"]).strip().lower()
        into = merged.setdefault(key, {})
        for field, value in bot.items():
            if field in _LIST_KEYS and isinstance(value, list):
                into[field] = _union(into.get(field, []), value)
            elif value not in (None, "", [], {}) or field not in into:
                into[field] = value
    return list(merged.values())


def _union(current: list[Any], incoming: list[Any]) -> list[Any]:
    def ident(item: Any) -> str:
        return item.strip() if isinstance(item, str) else json.dumps(item, sort_keys=True)
    order = {ident(item): item for item in current}
    for item in incoming:
        order.setdefault(ident(item), item)
    return list(order.values())


def _secret_keys(value: Any, path: str = "") -> list[str]:
    """Dotted paths of secret-looking keys anywhere in ``value``."""
    found: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            here = f"{path}.{key}" if path else str(key)
            if is_secret_key(str(key)) or "password" in str(key).lower():
                found.append(here)
            found.extend(_secret_keys(child, here))
    elif isinstance(value, list):
        for i, child in enumerate(value):
            found.extend(_secret_keys(child, f"{path}[{i}]"))
    return found


def _validated_bots(data: dict[str, Any]) -> list[dict[str, Any]]:
    if data.get("version", 1) != 1:
        raise ExportFormatError(f"unsupported export version {data.get('version')!r}")
    if secrets := _secret_keys(data):
        raise ExportFormatError("the export carries credential-looking keys, so it was not read: "
                                + ", ".join(secrets[:5]) + ". Ask the Bot to export again without them.")
    bots = data.get("bots")
    if not isinstance(bots, list) or not bots:
        raise ExportFormatError("the export has no bots")
    for i, bot in enumerate(bots):
        if not isinstance(bot, dict) or not str(bot.get("name") or "").strip():
            raise ExportFormatError(f"bots[{i}] needs a name")
    return bots


def _strings(value: Any) -> list[str]:
    return [str(v).strip() for v in value if str(v).strip()] if isinstance(value, list) else []


def _dicts(value: Any) -> list[dict[str, Any]]:
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


_FRONTMATTER_NAME_RE = re.compile(r"\A(---\s*\n(?:.*\n)*?)name:[^\n]*\n", re.MULTILINE)


def _rename_skill(skill_md: Path, name: str) -> None:
    """Make a copied SKILL.md's frontmatter ``name`` its new folder's name: Hermes finds a skill by
    either, so a copy renamed to ``x-2`` that still says ``name: x`` makes ``x`` ambiguous."""
    text = read_text(skill_md)
    renamed = _FRONTMATTER_NAME_RE.sub(lambda m: f"{m.group(1)}name: {name}\n", text, count=1)
    if renamed != text:
        atomic_write_text(skill_md, renamed)


def _skill_markdown(skill: dict[str, Any], name: str) -> str:
    description = " ".join(str(skill.get("description") or f"Imported Grok Bot skill: {skill.get('name')}").split())
    body = str(skill.get("instructions") or "").strip()
    if not body.startswith("# "):  # a Bot often copies the skill with its own title heading
        body = f"# {str(skill.get('name')).strip()}\n\n{body}"
    return f"---\nname: {name}\ndescription: {json.dumps(description[:300])}\n---\n\n{body}\n"


class GrokBotImporter(AgentImporter):
    """Plan/apply a Grok Bot export into one Hermes home; same item records as the other importers.

    Works on normalized bot dicts (the export's shape), so another product's reader (OpenBot) only
    overrides :meth:`load_bots`. A skill may carry ``path`` (a folder with ``SKILL.md``, copied) instead
    of ``instructions``; a bot may carry ``mcp_servers`` (Claude/Codex-shaped entries)."""

    AGENT = AGENT
    PRODUCT = "Grok Bot"
    SKILL_CATEGORY = SKILL_CATEGORY

    def __init__(self, export_files: Sequence[Path], target_root: Path, execute: bool = False,
                 overwrite: bool = False, activate_routines: bool = False,
                 bot_names: Sequence[str] = (), pasted: Optional[str] = None) -> None:
        self.agent = self.AGENT
        self.pasted = pasted  # an export read from stdin (``--source -``) instead of a file
        self.source_root = (PASTED if pasted is not None else
                            Path(export_files[0]).parent if export_files else Path.home())
        self.export_files = [Path(p) for p in export_files]
        self.target_root = Path(target_root)
        self.execute = execute
        self.overwrite = overwrite
        self.activate_routines = activate_routines
        self.bot_names = {n.strip().lower() for n in bot_names if n.strip()}
        self.sync_skills: dict[str, Optional[str]] = {}
        self.items: list[dict[str, Any]] = []
        self.stripped_secrets: list[str] = []
        self._claimed_slugs: dict[str, str] = {}  # skill folder name → which skill took it this run
        self._exported: dict[str, set[str]] = {}  # bot name (lowercase) → slugs of its exported routines

    def run(self) -> dict[str, Any]:
        bots = self.load_bots()
        if self.bot_names:
            bots = [(p, b) for p, b in bots if str(b.get("name")).strip().lower() in self.bot_names]
            if not bots:
                self.record("export", None, None, "skipped", "No bot or agent matches --bot")
                return self.build_report()  # a mistyped name imports nothing, shared items included
        for path, bot in bots:
            self._import_bot(path, bot, single=len(bots) == 1)
        self.import_shared()
        return self.build_report()

    def import_shared(self) -> None:
        """Check the export against what the Grok Bot app on this computer caches: Bots it lists
        that no export covered, and routines a Bot announced in its chat that its export left out
        (a Bot can miss or skip things when it describes itself)."""
        from .grok_bot_cache import read_cache
        cache = read_cache()
        missing_bots = [b for b in cache.bots if b.lower() not in self._exported]
        if missing_bots and not self.bot_names:
            self.record("bots", "Grok Bot app", None, "skipped",
                        "Not in the export, export these Bots too to bring them over: " + ", ".join(missing_bots))
        for bot, routines in cache.routines.items():
            have = self._exported.get(bot.lower())
            left_out = [r for r in routines if have is not None and _slug(r) not in have]
            if left_out:
                self.record("routine-check", f"Grok Bot app: {bot}", None, "skipped",
                            f"{bot} created routine(s) its export did not include: {', '.join(left_out)}. "
                            "Ask the Bot to export again, naming them")

    def load_bots(self) -> list[tuple[Path, dict[str, Any]]]:
        """(where it came from, normalized bot) for every bot in the sources; problems are recorded."""
        found: list[tuple[Path, dict[str, Any]]] = []
        for path in self.export_files:
            try:
                text = self.pasted if self.pasted is not None and path == PASTED else read_text(path)
                parsed, redacted = redact_secrets(parse_export(text))
            except (OSError, ExportFormatError) as exc:
                self.record("export", path, None, "error", str(exc))
                continue
            if redacted:
                self.stripped_secrets.append(f"{path.name}: {redacted} token-like value(s) redacted")
            found.extend((path, bot) for bot in parsed)
        # The same Bot across files (a split export, or one exported twice) imports once.
        origin = {str(b["name"]).strip().lower(): p for p, b in found}
        return [(origin[str(b["name"]).strip().lower()], b) for b in merge_bots([b for _, b in found])]

    def _import_bot(self, path: Path, bot: dict[str, Any], single: bool) -> None:
        name = str(bot["name"]).strip()
        self._exported[name.lower()] = {_slug(str(r.get("name") or "")) for r in _dicts(bot.get("routines"))}
        source = f"{path.name}: {name}"
        self._import_role(source, bot, single)
        memories = self.target_root / "memories"
        self._merge_list("memories", source, memories / "MEMORY.md", _strings(bot.get("memories")))
        self._merge_list("user-facts", source, memories / "USER.md", _strings(bot.get("user_facts")))
        imported_skills = self._import_skills(source, _dicts(bot.get("skills")))
        self._import_routines(source, name, _dicts(bot.get("routines")), imported_skills)
        self._import_apps(source, _dicts(bot.get("apps")))
        if isinstance(bot.get("mcp_servers"), dict) and bot["mcp_servers"]:
            self.import_mcp_servers(bot["mcp_servers"], kind="mcp-servers")

    def _merge_list(self, kind: str, source: str, destination: Path, entries: list[str]) -> None:
        if not entries:
            self.record(kind, source, destination, "skipped", f"No {kind.replace('-', ' ')} found")
            return
        self.merge_memory_entries(kind, source, destination, entries)

    def _import_role(self, source: str, bot: dict[str, Any], single: bool) -> None:
        """The Bot's role becomes SOUL.md while that is still Hermes' seeded default and only one Bot
        is imported; otherwise its lines join the memory entries."""
        name, title = str(bot["name"]).strip(), str(bot.get("title") or "").strip()
        product = self.PRODUCT
        instructions = str(bot.get("instructions") or "").strip()
        if not instructions:
            self.record("role", source, None, "skipped", "No instructions found")
            return
        soul = self.target_root / "SOUL.md"
        current = read_text(soul) if soul.is_file() else ""
        if instructions in current:  # imported before: a re-run must not copy it into memory too
            self.record("role", source, soul, "skipped", "Already in SOUL.md")
            return
        if single and (not current.strip() or _is_seeded_soul(current) or self.overwrite):
            heading = f"# {name}" + (f" — {title}" if title else "")

            def write() -> None:
                if current.strip():
                    atomic_write_text(soul.with_suffix(f".md.bak.{self.AGENT}"), current)
                atomic_write_text(soul, f"{heading}\n\n{instructions}\n")

            self.apply("role", source, soul, "Would write the Bot's role to SOUL.md", write)
            return
        entries = [f"{product} {name}: {e}" for e in extract_markdown_entries(instructions)]
        self.merge_memory_entries("role", source, self.target_root / "memories" / "MEMORY.md", entries)

    def _import_skills(self, source: str, skills: list[dict[str, Any]]) -> dict[str, str]:
        """Write each skill; returns export skill name (lowercased) → imported Hermes skill name."""
        root = self.target_root / "skills" / self.SKILL_CATEGORY
        mapped: dict[str, str] = {}
        if not skills:
            self.record("skills", source, root, "skipped", "No skills found")
            return mapped
        for skill in skills:
            label = str(skill.get("name") or "").strip()
            folder = Path(skill["path"]) if skill.get("path") else None
            if folder is not None and not (folder / "SKILL.md").is_file():
                self.record("skill", f"{source} / {label}", None, "skipped", f"No SKILL.md in {folder}")
                continue
            if not label or not (folder or str(skill.get("instructions") or "").strip()):
                self.record("skill", f"{source} / {label or '?'}", None, "skipped",
                            "Skill has no name or no instructions")
                continue
            slug = self._unique_slug(_slug(label), f"{label.lower()}\0{folder or skill.get('instructions')}")
            destination = root / slug
            if destination.exists() and not self.overwrite:
                self.record("skill", f"{source} / {label}", destination, "conflict",
                            "Destination skill already exists")
                mapped[label.lower()] = slug
                continue
            def write(destination=destination, skill=skill, folder=folder) -> None:
                if folder is not None:
                    if destination.exists():
                        shutil.rmtree(destination)
                    shutil.copytree(folder, destination, ignore=shutil.ignore_patterns(".openbot-managed.json"))
                    _rename_skill(destination / "SKILL.md", destination.name)
                    return
                destination.mkdir(parents=True, exist_ok=True)
                atomic_write_text(destination / "SKILL.md", _skill_markdown(skill, destination.name))

            self.apply("skill", f"{source} / {label}", destination,
                       "Would copy skill directory" if folder else "Would write SKILL.md", write)
            mapped[label.lower()] = slug
        return mapped

    def _unique_slug(self, slug: str, identity: str) -> str:
        """Two different skills whose names slug alike in one run ("Daily Brief" and "daily-brief", or
        two agents' own "standup") get ``-2``, ``-3``... instead of the second overwriting the first;
        the same skill seen twice keeps one name."""
        candidate, n = slug, 2
        while self._claimed_slugs.get(candidate, identity) != identity:
            candidate, n = f"{slug}-{n}", n + 1
        self._claimed_slugs[candidate] = identity
        return candidate

    def _import_routines(self, source: str, bot_name: str, routines: list[dict[str, Any]],
                         skills: dict[str, str]) -> None:
        if not routines:
            self.record("routines", source, None, "skipped", "No routines found")
            return
        from cron import jobs as cron_jobs
        unscheduled: list[dict[str, Any]] = []
        with cron_jobs.use_cron_store(self.target_root):
            existing = {str(j.get("name")) for j in cron_jobs.load_jobs()}
            planned: set[str] = set()
            for routine in routines:
                label = str(routine.get("name") or "").strip() or "routine"
                job_name = f"{bot_name}: {label}"
                if job_name not in existing:  # two different routines sharing a name: "X", "X (2)"
                    base, n = job_name, 2
                    while job_name in planned:
                        job_name, n = f"{base} ({n})", n + 1
                where = f"cron job '{job_name}'"
                instruction = str(routine.get("instruction") or "").strip()
                if not instruction:
                    self.record("routine", f"{source} / {label}", None, "skipped", "Routine has no instruction")
                    continue
                schedule = _routine_schedule(routine, cron_jobs.parse_schedule)
                if schedule is None:
                    unscheduled.append(routine)
                    continue
                if job_name in existing:
                    self.record("routine", f"{source} / {label}", where, "skipped",
                                "A cron job with this name already exists (imported before?)")
                    continue
                planned.add(job_name)
                job_skills = [skills[s.lower()] for s in _strings(routine.get("skills")) if s.lower() in skills]
                paused = not self.activate_routines or bool(routine.get("paused"))
                kwargs = _create_job_kwargs(cron_jobs.create_job, routine, paused, self.PRODUCT)

                details: dict[str, Any] = {"schedule": schedule, "paused": paused, "skills": job_skills}
                if zone_note := _zone_note(routine, kwargs):  # imported rows print only where they went
                    details["timezone_note"] = zone_note
                    where = f"{where} ({zone_note})"

                def create(job_name=job_name, instruction=instruction, schedule=schedule,
                           job_skills=job_skills, kwargs=kwargs, details=details) -> None:
                    with cron_jobs.use_cron_store(self.target_root):
                        job = cron_jobs.create_job(prompt=instruction, schedule=schedule, name=job_name,
                                                   skills=job_skills or None, **kwargs)
                    details["job_id"] = job.get("id")

                self.apply("routine", f"{source} / {label}", where,
                           f"Would create a {'paused ' if paused else ''}cron job ({schedule})",
                           create, details)
        if unscheduled:
            self._note_event_routines(source, bot_name, unscheduled)

    def _note_event_routines(self, source: str, bot_name: str, routines: list[dict[str, Any]]) -> None:
        """Event-triggered routines have no cron equivalent: keep their full text where the user can
        rebuild them (a webhook subscription, a gateway channel, or a polling cron job)."""
        destination = self.target_root / f"{self.AGENT}-import" / f"{_slug(bot_name)}-event-routines.md"
        lines = [f"# {bot_name}: {self.PRODUCT} routines that run on an event", "",
                 "These start on something happening instead of a schedule, so they were not turned into "
                 "cron jobs. Recreate each in Hermes (ask Hermes to set it up from this file).", ""]
        for r in routines:
            lines += [f"## {str(r.get('name') or 'routine').strip()}", "",
                      f"- Trigger: {r.get('trigger') or 'unknown'}",
                      f"- Details: {r.get('trigger_details') or r.get('schedule_text') or 'n/a'}", "",
                      str(r.get("instruction") or "").strip(), ""]
        text = "\n".join(lines)
        if destination.is_file():
            current = read_text(destination)
            if current == text:
                self.record("event-routines", source, destination, "skipped", "Notes already saved")
                return
            if not self.overwrite:
                self.record("event-routines", source, destination, "conflict", "Notes file exists and was edited")
                return

        def write() -> None:
            destination.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(destination, text)

        self.apply("event-routines", source, destination,
                   f"Would save {len(routines)} routine(s) with no schedule as notes to recreate", write)

    def _import_apps(self, source: str, apps: list[dict[str, Any]]) -> None:
        if not apps:
            return
        servers = {_slug(str(a.get("name"))): {"url": str(a["mcp_url"]).strip()}
                   for a in apps if str(a.get("mcp_url") or "").startswith(("https://", "http://"))}
        if servers:
            self.import_mcp_servers(servers, kind="mcp-servers")
        others = [str(a.get("name") or "").strip() for a in apps
                  if _slug(str(a.get("name"))) not in servers and str(a.get("name") or "").strip()]
        if others:
            self.record("apps", source, None, "skipped",
                        f"Connect these in Hermes yourself (their logins stay in {self.PRODUCT}): " + ", ".join(others))


def _is_seeded_soul(text: str) -> bool:
    try:
        from hermes_cli.default_soul import DEFAULT_SOUL_MD, is_legacy_template_soul
    except ImportError:
        return False
    return text.strip() == DEFAULT_SOUL_MD.strip() or is_legacy_template_soul(text)


def _routine_schedule(routine: dict[str, Any], parse_schedule) -> Optional[str]:
    """A schedule string Hermes cron accepts, or None for an event-triggered routine."""
    trigger = str(routine.get("trigger") or "schedule").strip().lower()
    if trigger not in _SCHEDULE_TRIGGERS:
        return None
    for candidate in (str(routine.get("schedule_cron") or "").strip(),
                      str(routine.get("schedule_text") or "").strip()):
        if not candidate:
            continue
        try:
            parse_schedule(candidate)
        except (ValueError, TypeError):
            continue
        return candidate
    return None


def _create_job_kwargs(create_job, routine: dict[str, Any], paused: bool, product: str) -> dict[str, Any]:
    """``paused`` always; the routine's time zone only where this Hermes can pin one per job (older
    releases schedule in the profile's zone)."""
    kwargs: dict[str, Any] = {"paused": paused}
    if paused:
        kwargs["paused_reason"] = f"Imported from {product}: resume once the {product} routine is off"
    tz = _valid_zone(routine)
    if tz and "timezone" in inspect.signature(create_job).parameters:
        kwargs["timezone"] = tz
    return kwargs


def _valid_zone(routine: dict[str, Any]) -> str:
    tz = str(routine.get("timezone") or "").strip()
    try:
        return tz if tz and ZoneInfo(tz) else ""
    except (ZoneInfoNotFoundError, ValueError):
        return ""


def _zone_note(routine: dict[str, Any], kwargs: dict[str, Any]) -> str:
    """Say when the job will not run on the routine's own clock, instead of shifting it silently."""
    tz = str(routine.get("timezone") or "").strip()
    if not tz or "timezone" in kwargs:
        return ""
    if not _valid_zone(routine):
        return f"unknown time zone {tz!r}, runs in this profile's time zone"
    return f"this Hermes cannot pin a zone per job, so it runs in the profile's time zone, not {tz}"


def print_export_instructions(prompt_file: Optional[Path]) -> None:
    from hermes_cli.cli_output import print_header, print_info

    from .grok_bot_cache import read_cache
    print()
    print_header("Step 1 — ask each Grok Bot to export itself")
    print_info("Grok Bot keeps your routines in its cloud account and has no export button, so each Bot")
    print_info("writes its own export. Open a Bot's chat, paste the message below, and send it.")
    print_info("Do this once per Bot you want to bring over. Nothing in Grok Bot is changed.")
    if bots := read_cache().bots:
        print_info(f"Your Bots in the Grok Bot app on this computer: {', '.join(bots)}")
    if prompt_file is not None:
        print_info(f"(The same message is saved in {prompt_file})")
    print()
    print(EXPORT_PROMPT)
    print_header("Step 2 — bring the export back")
    print_info("Copy the Bot's whole reply and press Enter when asked, or save the file it attaches")
    print_info("to ~/Downloads (grok-bot-export-<bot>.json, one per Bot) and run this command again.")
    print_info("Exports in ~/Downloads named grok-bot-export*.json are found without --source; a file")
    print_info("elsewhere: hermes import-agent grok-bot --source <file>")


# Stands for the export read from stdin (``--source -``); the text rides on the importer.
PASTED = Path("<pasted export>")


def read_pasted_export() -> str:
    """The export from stdin: piped (``pbpaste | hermes import-agent grok-bot --source -``) or pasted
    at the terminal, ended with Ctrl-D (Ctrl-Z then Enter on Windows)."""
    from hermes_cli.cli_output import print_info
    if sys.stdin.isatty():
        print_info("Paste the Bot's whole reply.")
        return read_terminal_paste()
    return sys.stdin.read()


def read_clipboard_text() -> Optional[str]:
    """The clipboard's text through the OS tool (pbpaste, PowerShell, wl-paste/xclip/xsel); None
    when there is none to run."""
    import shutil
    import subprocess
    commands = {"darwin": [["pbpaste"]],
                "win32": [["powershell", "-NoProfile", "-Command",
                           "[Console]::OutputEncoding=[Text.Encoding]::UTF8; Get-Clipboard -Raw"]]}.get(
        sys.platform, [["wl-paste", "--no-newline"], ["xclip", "-selection", "clipboard", "-o"],
                       ["xsel", "--clipboard", "--output"]])
    for command in commands:
        exe = shutil.which(command[0])
        if exe is None:
            continue
        try:
            done = subprocess.run([exe, *command[1:]], capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", stdin=subprocess.DEVNULL, timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if done.returncode == 0:
            return done.stdout
    return None


def _discard_pending_input() -> None:
    """Drop the rest of a paste still streaming into the terminal, so it does not answer the next
    prompts: flush until the input has been quiet for half a second."""
    try:
        import select
        import termios
        fd = sys.stdin.fileno()
        for _ in range(40):
            pending = select.select([fd], [], [], 0.5)[0]
            termios.tcflush(fd, termios.TCIFLUSH)
            if not pending:
                return
    except (ImportError, OSError, ValueError):  # Windows console, or stdin is not a terminal
        return


def read_terminal_paste() -> str:
    """Text pasted at a terminal, up to Ctrl-D. A POSIX terminal in line mode cuts each line at
    ~4 KB (1 KB on macOS) and a Bot's skill text is one long JSON line, so the tty is switched to
    raw input for the read; elsewhere, plain stdin."""
    from hermes_cli.cli_output import print_info
    print_info("Reading the rest of the reply: press Ctrl-D when it has all pasted (Ctrl-Z then Enter on Windows).")
    try:
        import termios
    except ImportError:
        return sys.stdin.read()
    import os
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    raw = termios.tcgetattr(fd)
    raw[3] &= ~(termios.ICANON | termios.ECHO)
    raw[6][termios.VMIN], raw[6][termios.VTIME] = 1, 0
    chunks: list[bytes] = []
    try:
        termios.tcsetattr(fd, termios.TCSANOW, raw)
        while not chunks or b"\x04" not in chunks[-1]:
            chunks.append(os.read(fd, 65536))
            if not chunks[-1]:
                break
    finally:
        termios.tcsetattr(fd, termios.TCSANOW, saved)
    return b"".join(chunks).split(b"\x04", 1)[0].decode("utf-8", "replace").replace("\r", "\n")


def resolve_export_files(args) -> list[Path]:
    """The export files for this run; with none found, print the export steps and (interactively)
    ask for a path."""
    from hermes_cli.cli_output import print_error
    from hermes_constants import get_hermes_home
    if args.source == "-":
        return [PASTED]
    source = Path(args.source).expanduser().resolve() if args.source else None
    if source is not None and not source.exists():
        print()
        print_error(f"Export file not found: {source}")
        raise SystemExit(1)
    files = find_export_files(source)
    if files:
        return files
    if source is not None:
        print()
        print_error(f"No Grok Bot export found in {source}")
        raise SystemExit(1)
    prompt_file: Optional[Path] = None
    if not args.dry_run:
        prompt_file = get_hermes_home() / "grok-bot-import" / "export-prompt.txt"
        try:
            prompt_file.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(prompt_file, EXPORT_PROMPT)
        except OSError:
            prompt_file = None
    print_export_instructions(prompt_file)
    if args.dry_run or args.yes or not sys.stdin.isatty():
        return []
    while True:
        print()
        try:
            answer = input("Copy the Bot's reply and press Enter (or type the path to its file; q to stop): ").strip()
        except (EOFError, KeyboardInterrupt):
            return []
        if answer.lower() in ("q", "quit"):
            return []
        if not answer:
            if found := find_export_files(None):  # saved to ~/Downloads meanwhile
                return found
            text = read_clipboard_text()
            if text and EXPORT_FORMAT in text:
                args.pasted_text = text
                return [PASTED]
            print_error("The clipboard holds no Grok Bot export" + ("" if text is not None else
                        " (no clipboard tool found)") + ". Copy the Bot's whole reply, or save it as")
            print_error("~/Downloads/grok-bot-export-<bot>.json, then press Enter again.")
            continue
        picked = Path(answer.strip("'\"")).expanduser()
        if picked.exists():
            break
        # Pasted into the terminal: a terminal line holds only ~4 KB (1 KB on macOS), so a reply
        # arrives cut; drop what is still queued and point at the clipboard instead.
        _discard_pending_input()
        print_error(f"No file at {answer[:60]!r}. If that was the reply pasted here: a terminal cuts it short,")
        print_error("so copy it and just press Enter instead (or pipe it in with --source -).")
    found = find_export_files(picked) if picked.exists() else []
    if not found:
        print_error(f"No export found at {picked}")
        raise SystemExit(1)
    return found
