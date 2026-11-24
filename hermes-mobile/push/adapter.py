"""Push platform adapter for the Hermes Agent mobile apps.

Send-only: forwards messages to a self-hosted push relay, which fans them out to
every device paired with this install, through APNs to iOS and FCM to Android.
There is no inbound side — the mobile app talks to Hermes over its own WebSocket.
The adapter lets cron jobs and webhook routes use `deliver: push`; the hooks below
notify when a turn finishes or the agent waits on the user (an approval, question,
password, secret or other input).

Private previews require cryptography; the HTTP call uses urllib on a worker thread.
"""

import asyncio
import importlib.util
import json
import logging
import math
import os
import queue
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult

from . import activity, privacy

logger = logging.getLogger(__name__)

# The PluginContext register() was given: session facts, cron execution and delivery seams.
_ctx = None
_missing_seams_logged = set()


def _log_missing_seam_once(name, detail):
    if name not in _missing_seams_logged:
        _missing_seams_logged.add(name)
        logger.debug("Hermes does not expose %s; %s", name, detail)

REQUEST_TIMEOUT_SECONDS = 15
# Cloudflare bans bare Python-urllib signatures (error 1010); identify honestly.
PLUGIN_VERSION = next(line.split(":", 1)[1].strip().strip("\"'")
                      for line in (Path(__file__).resolve().parents[1] / "plugin.yaml").read_text().splitlines()
                      if line.startswith("version:"))
USER_AGENT = f"hermes-mobile/{PLUGIN_VERSION}"
# How much of a message rides in the payload; privacy.py shortens the preview further.
# Not the platform's chunking limit: a push is never split (register() below).
MAX_MESSAGE_LENGTH = 1500


def _secret(name, default=""):
    try:
        from gateway.platforms._shared import get_scoped_secret
        return get_scoped_secret(name) or default
    except ImportError:
        return os.getenv(name, default)


def _gate(name):
    """A gate variable, per profile when profiles share the gateway; the environment on older Hermes."""
    try:
        from gateway.platforms._shared import platform_gate_env
    except ImportError:
        return (os.getenv(name) or "").strip()
    return platform_gate_env(name)


# Plain HTTP only to a relay on this machine (a local test relay): the install key
# rides in every request's Authorization header.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
RELAY_URL_RULE = "The push relay URL must start with https:// (http:// only for localhost)."
_refused_relay_urls = set()


def _relay_url_allowed(relay_url):
    try:
        parts = urlsplit(str(relay_url or "").strip())
        host = parts.hostname
    except ValueError:
        return False
    return bool(host) and (parts.scheme == "https" or (parts.scheme == "http" and host in _LOOPBACK_HOSTS))


def _relay_settings(extra=None):
    """The relay URL and install key; no URL when it is not HTTPS, so nothing is sent."""
    extra = extra or {}
    relay_url = (
        extra.get("relay_url") or _secret("HERMES_MOBILE_PUSH_RELAY_URL")
    ).rstrip("/")
    install_key = extra.get("install_key") or _secret("HERMES_MOBILE_PUSH_INSTALL_KEY")
    if relay_url and not _relay_url_allowed(relay_url):
        if relay_url not in _refused_relay_urls:
            _refused_relay_urls.add(relay_url)
            logger.warning("Push disabled: HERMES_MOBILE_PUSH_RELAY_URL must start with https:// "
                           "(http:// only for localhost), as the install key travels with every request.")
        relay_url = ""
    return relay_url, install_key


# Notification text is a lock-screen surface, not a transcript: markdown arrives as literal
# hashes and asterisks. The relay cleans what it can read; a sealed preview it can't, so the
# agent's prose (replies and deliveries) is cleaned here. Approvals and questions carry
# commands and prompts verbatim, where an asterisk means something.
_PROSE_KINDS = frozenset({"reply", "delivery"})
_MD_FENCE = re.compile(r"^\s*(```|~~~)")
_MD_RULE = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")
_MD_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+")
_MD_QUOTE = re.compile(r"^\s{0,3}>\s?")
_MD_BULLET = re.compile(r"^(\s*)[-*+]\s+")
_MD_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_MD_STRONG = re.compile(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1")
_MD_EMPHASIS = re.compile(r"(?<![\w*])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![\w*])|(?<![\w_])_(?=\S)([^_\n]+?)(?<=\S)_(?![\w_])")
_MD_STRIKE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~")
_MD_CODE = re.compile(r"`([^`\n]+)`")


def _plain_notification_text(text):
    """*text* without markdown syntax, keeping its words and line structure."""
    lines = []
    for raw in str(text or "").splitlines():
        if _MD_FENCE.match(raw) or _MD_RULE.match(raw):
            continue
        line = _MD_HEADING.sub("", raw)
        line = _MD_QUOTE.sub("", line)
        line = _MD_BULLET.sub(lambda m: m.group(1) + "• ", line)
        line = _MD_IMAGE.sub(lambda m: m.group(1), line)
        line = _MD_LINK.sub(lambda m: m.group(1), line)
        line = _MD_CODE.sub(lambda m: m.group(1), line)
        line = _MD_STRONG.sub(lambda m: m.group(2), line)
        line = _MD_STRIKE.sub(lambda m: m.group(1), line)
        line = _MD_EMPHASIS.sub(lambda m: m.group(1) or m.group(2), line)
        if line.strip() or (lines and lines[-1]):
            lines.append(line.rstrip())
    return "\n".join(lines).strip()


def _private_payload(payload, install_key):
    if payload.get("kind") in _PROSE_KINDS and isinstance(payload.get("body"), str):
        payload = {**payload, "body": _plain_notification_text(payload["body"])}
    try:
        return privacy.protect(payload, install_key)
    except Exception:
        return {"title": "Hermes Agent", "body": "New activity. Open Hermes Agent to view.",
                "kind": "delivery", "hermes": {"private_preview_unavailable": True}}


def _post_notify(relay_url, install_key, payload):
    """Blocking POST to the relay. Callers hop off the event loop first."""
    payload = _private_payload(payload, install_key)
    request = urllib.request.Request(
        f"{relay_url}/notify",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {install_key}", "Content-Type": "application/json",
                 "User-Agent": USER_AGENT},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
        return resp.status, resp.read().decode("utf-8", "replace")[:500]


def _notify_failure(status, response):
    """Why a /notify answer is a failed send; "" when it went out or the relay held it back on
    purpose (a muted conversation, the devices' preferences, a duplicate)."""
    if status != 200:
        return f"HTTP {status}: {response}"
    try:
        result = json.loads(response)
    except ValueError:
        return ""
    if isinstance(result, dict) and result.get("status") == "failed":
        return f"delivery failed: {response}"
    return ""


def _send_message(relay_url, install_key, content, **kwargs):
    """Build and POST an addressed message, on a worker thread: the payload reads and
    writes the session store. ``asyncio.to_thread`` carries the caller's context over,
    which names the session and profile the payload records."""
    return _post_notify(relay_url, install_key, _build_payload(content, **kwargs))


# Sessions whose surface already notifies the user natively; their own apps
# ping, so a push here would double every message.
NATIVELY_NOTIFYING = {
    "telegram", "discord", "slack", "whatsapp", "signal", "matrix",
    "mattermost", "teams", "sms", "email", "dingtalk", "feishu", "wecom",
    "weixin", "bluebubbles", "qqbot", "line", "ntfy",
}

# Kanban workers have their own delivery path: before the unified runtime their
# sessions are kanban_…; on it a worker runs in its own process, which Hermes
# gives HERMES_KANBAN_TASK. (A cron run notifies its output as a delivery
# wherever else the job delivers it, unless cron delivers it to push itself.)
_UNATTENDED_SESSION_PREFIXES = ("kanban_",)

def _cron_run(session_id):
    """The cron job this turn runs for, as ``(job id, job name, from_execution)``.

    ``ctx.current_cron_execution()`` names it on Hermes 0.21.6+. Before, Hermes
    names a cron run's session cron_<job id>_<YYYYMMDD>_<HHMMSS>, optionally
    followed by a compression suffix.
    """
    current = getattr(_ctx, "current_cron_execution", None)
    try:
        run = current() if callable(current) else None
    except Exception:
        run = None
    if run is not None:
        return str(run.job_id), str(run.job_name or ""), True
    match = re.match(r"^cron_(.+)_\d{8}_\d{6}(?:_|$)", session_id)
    return (match.group(1), "", False) if match else ("", "", False)


def _cron_delivery_platforms(job_id="", *, from_execution=False):
    """The platforms cron delivers this job's output to, or ``None`` when the seam is absent.

    Prefers ``ctx.cron_delivery_platforms`` (which also works under host isolation) and
    falls back to ``cron.plugin_api.delivery_platforms`` only when no ``ctx`` is bound.
    Inside a firing cron run, reads the firing run's own delivery settings first.
    """
    if _ctx is not None:
        accessor = getattr(_ctx, "cron_delivery_platforms", None)
    else:
        try:
            from cron.plugin_api import delivery_platforms as accessor
        except ImportError:
            accessor = None
    if not callable(accessor):
        _log_missing_seam_once(
            "cron_delivery_platforms",
            "cron duplicate-delivery suppression is off",
        )
        return None
    try:
        if from_execution:
            platforms = accessor()
            if platforms is not None or not job_id:
                return platforms
        if job_id:
            return accessor(job_id)
        return accessor()
    except Exception:
        logger.debug("Could not read cron delivery platforms", exc_info=True)
        return None


# Each turn's final reply, from ``post_llm_call`` to the ``on_session_end`` that
# follows it in the same turn finalization and says whether the turn completed:
# session id -> (turn id, reply). Bounded, for a turn whose end never came.
_replies = {}
_replies_lock = threading.Lock()


def _session_source():
    """The creation label of the calling session (``ctx.session_source()``), or ``""``."""
    accessor = getattr(_ctx, "session_source", None)
    if not callable(accessor):
        _log_missing_seam_once(
            "session_source",
            "launch-policy session source mapping is off",
        )
        return ""
    try:
        return str(accessor() or "").strip().lower()
    except Exception:
        logger.debug("Could not read session_source() for the turn", exc_info=True)
        return ""


def _session_surface():
    """The calling session's client surface (``ctx.session_surface()``), or ``""``."""
    accessor = getattr(_ctx, "session_surface", None)
    if not callable(accessor):
        return ""
    try:
        return str(accessor() or "").strip().lower()
    except Exception:
        logger.debug("Could not read session_surface() for the turn", exc_info=True)
        return ""


def _creation_source(platform):
    """The label the session of the turn running here was created with, else *platform*.

    The unified runtime (Hermes 0.22.1+) runs app and Desktop sessions through
    the gateway, which calls them "local"; their agents run as "desktop", and
    ``ctx.session_source()`` keeps the label they were created with, "gui". That
    label is what the relay sees: its Desktop filter matches "desktop", which
    would silence the phone's own sessions. An agent that is not the session's
    own (a delegated child, "subagent") keeps *platform*.
    """
    platform = str(platform or "").strip().lower()
    label = _session_source()
    if label and platform in {"local", label, _session_surface()}:
        return label
    return platform


# What a Hermes with the plugin session accessors says about the running turn:
# ``ctx.turn_client()`` names the client that submitted it, ``ctx.session_viewers()``
# the clients attached to its session. A Hermes without them, or an accessor that
# fails, leaves the fact unknown.
_UNKNOWN = object()


def _plugin_session_fact(name):
    """``ctx.<name>()`` for the running turn, or ``_UNKNOWN`` when this Hermes has no such
    accessor (found by attribute, never by version) or it raised."""
    accessor = getattr(_ctx, name, None)
    if not callable(accessor):
        return _UNKNOWN
    try:
        return accessor()
    except Exception:
        logger.debug("Could not read %s() for the turn", name, exc_info=True)
        return _UNKNOWN


# The label a reply push carries for the client that submitted its turn, by the
# client's kind; a phone names its platform.
_CLIENT_KIND_SOURCES = {"desktop": "desktop", "webapp": "desktop"}
_MOBILE_CLIENT_SOURCES = {"hermes-mobile-ios": "ios", "hermes-mobile-android": "android"}


def _well_formed_client(client):
    """Whether *client* is an answer ``turn_client()`` gives: ``None``, or a dict whose ``kind`` and
    ``name`` are each a string or ``None``. Anything else means Hermes cannot say."""
    if client is None:
        return True
    return (isinstance(client, dict) and isinstance(client.get("kind"), (str, type(None)))
            and isinstance(client.get("name"), (str, type(None))))


def _client_source(client):
    """The source label for the turn's *client* (``turn_client()``), ``""`` when it names none."""
    if client is None or not _well_formed_client(client):
        return ""
    kind, name = client.get("kind"), client.get("name")
    if kind in _CLIENT_KIND_SOURCES:
        return _CLIENT_KIND_SOURCES[kind]
    if kind == "mobile":
        return _MOBILE_CLIENT_SOURCES.get(name, "mobile") if isinstance(name, str) else "mobile"
    return ""


# Clients a person reads at a computer: the Desktop app and the Webapp.
_COMPUTER_CLIENT_KINDS = frozenset({"desktop", "webapp"})
# How recently a computer's user must have given input for it to count as active, unless the
# platform's ``active_seconds`` says otherwise.
DEFAULT_ACTIVE_SECONDS = 300
# The push platform's own settings the hooks read (``platforms.push`` in config.yaml, its
# ``extra``), by the Hermes home whose adapter was configured with them: the hooks run without it.
_PRESENCE_SETTINGS = ("active_seconds", "quiet_while_active")
_platform_settings = {}
# Homes already logged as using the defaults while another home has settings.
_unconfigured_homes_logged = set()
# Homes already warned about an ``active_seconds`` that is not a positive number.
_unusable_active_seconds_warned = set()


def _settings_home():
    """The running Hermes home the settings are kept by, or ``None`` when Hermes cannot name it."""
    try:
        from hermes_constants import get_hermes_home
        return str(get_hermes_home())
    except Exception:
        return None


def _remember_platform_settings(extra):
    home = _settings_home()
    if home is not None:  # Settings no home can be named for are never shared: nothing is kept.
        _platform_settings[home] = {key: extra[key] for key in _PRESENCE_SETTINGS if key in extra}


def _platform_setting(name):
    """The push platform's setting *name* for this turn's Hermes home; ``None`` when that home set
    none, or Hermes cannot name it. One home's settings never apply to another."""
    home = _settings_home()
    settings = _platform_settings.get(home) if home is not None else None
    if settings is None and home is not None and _platform_settings and home not in _unconfigured_homes_logged:
        # A gateway that builds a profile's adapter under another home lands here; say so once.
        _unconfigured_homes_logged.add(home)
        logger.debug("No push platform settings for Hermes home %s while %d other home(s) have some; "
                     "using the defaults", home, len(_platform_settings))
    return (settings or {}).get(name)


def _active_seconds():
    """``active_seconds``: a positive number of seconds, else ``DEFAULT_ACTIVE_SECONDS``."""
    value = _platform_setting("active_seconds")
    if value is None:
        return DEFAULT_ACTIVE_SECONDS
    seconds = None
    if not isinstance(value, bool):
        try:
            seconds = float(value)
        except (TypeError, ValueError, OverflowError):  # OverflowError: an integer too big for a float.
            pass
    if seconds is not None and math.isfinite(seconds) and seconds > 0:
        return seconds
    home = _settings_home()
    if home not in _unusable_active_seconds_warned:
        # Once per home, and never the value: whatever was typed there could be anything.
        _unusable_active_seconds_warned.add(home)
        logger.warning("The push platform's active_seconds for Hermes home %s is not a positive number "
                       "of seconds; using %d", home, DEFAULT_ACTIVE_SECONDS)
    return DEFAULT_ACTIVE_SECONDS


def _quiet_while_active():
    """``quiet_while_active``: whether a reply the user is at the computer to see is not pushed at all."""
    value = _platform_setting("quiet_while_active")
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "on", "1"}
    # The integer 1 is on, as the string "1" is; any other number is off, as "2" or "1.0" is.
    return value is True or (type(value) is int and value == 1)


def _active_at_computer(viewer, active_seconds):
    """Whether *viewer* (one of ``session_viewers()``) is a computer whose user gave input less
    than *active_seconds* ago and that can get the user's attention itself. A viewer of a shape
    Hermes does not report (a kind that is not a string, an idle time that is not a number of
    seconds from 0 up, a ``can_alert`` that is neither a boolean nor ``None``) does not count."""
    if not isinstance(viewer, dict) or not isinstance(viewer.get("kind"), str):
        return False
    if viewer["kind"] not in _COMPUTER_CLIENT_KINDS:
        return False
    idle, can_alert = viewer.get("idle_seconds"), viewer.get("can_alert")
    if isinstance(idle, bool) or not isinstance(idle, (int, float)):
        return False  # None: no input seen from it in this session.
    # The threshold is a finite number of seconds, so NaN and infinity are never within it.
    return 0 <= idle < active_seconds and (can_alert is True or can_alert is None)


def _desktop_active(facts, active_seconds):
    """Whether the user is active at a computer (``hermes.desktop_active``), by known *facts*
    (``_turn_facts``): the turn's client is a Desktop or Webapp, and a Desktop or Webapp attached
    to the session is active (``_active_at_computer``). That need not be the computer that sent
    the turn."""
    client, viewers = facts
    if client is None or client.get("kind") not in _COMPUTER_CLIENT_KINDS:
        return False
    return any(_active_at_computer(viewer, active_seconds) for viewer in viewers)


def _turn_facts(fields, platform):
    """What Hermes says about a finished turn whose reply is its own: ``(client, viewers)``, from
    ``turn_client()`` and ``session_viewers()``. Both or neither: ``_UNKNOWN`` when either accessor
    is missing, raises or answers in a shape it does not give (``_well_formed_client``, a list).
    A delegated child's completion and a webhook run's reply are no client's turn: for them
    neither accessor is read, and their push is today's."""
    if _delegated_turn(fields) or platform in _WEBHOOK_SURFACES:
        return _UNKNOWN
    client = _plugin_session_fact("turn_client")
    if client is _UNKNOWN or not _well_formed_client(client):
        return _UNKNOWN
    viewers = _plugin_session_fact("session_viewers")
    if not isinstance(viewers, (list, tuple)):
        return _UNKNOWN
    return client, viewers


def _turn_source(fields, platform, facts):
    """The source a finished turn's reply push carries: the client that submitted the turn
    when Hermes names one (*facts*, ``_turn_facts``), else the session's creation label
    (``_creation_source``). A delegated child and a webhook run keep their own label."""
    if facts is not _UNKNOWN:
        source = _client_source(facts[0])
        if source:
            return source
    return _creation_source(platform)


def _origin_metadata(fields, *, use_context=False):
    """Carry machine-readable origin through every push delivery path.

    Platform is the session source, not the host OS. Never classify a main
    agent's summary as delegated work merely because it mentions a subagent.
    """
    source = str(fields.get("source") or fields.get("platform") or "").strip().lower()
    session_id = str(fields.get("session_id") or fields.get("session_key") or "")
    if use_context and not source and not session_id:
        # An addressed message (send(), no hook): the session that sent it.
        source = _session_env("HERMES_SESSION_PLATFORM").strip().lower()
        session_id = _session_env("HERMES_SESSION_ID")
        # A child can inherit the parent's messaging platform in its tool
        # context. Hermes' task-local/subprocess marker identifies the actual
        # caller without mutating the parent's environment.
        if _in_delegated_child(include_subprocesses=True):
            source = "subagent"
    if source == "local":
        source = _creation_source(source)
    if source in ("", "local") and session_id:
        # Attention hooks may supply only an id. Read the current profile's
        # durable session record; open_persistence_reader never creates or migrates.
        try:
            from persistence_assembly import open_persistence_reader, profile_scope
            with open_persistence_reader(profile_scope().profile_dir) as store:
                record = store.sessions.get(session_id)
                if record is not None and record.source:
                    source = str(record.source).strip().lower() or source
        except Exception:  # noqa: S110 -- a best-effort lookup
            pass  # Older servers/missing rows remain unknown, hence eligible.
    origin = {}
    profile = fields.get("profile") or fields.get("profile_name")
    if isinstance(profile, str) and profile.strip():
        origin["profile"] = profile.strip()
    else:
        # Capture the task-local owner before handing the payload to the
        # shared delivery worker. That thread has no caller profile context.
        try:
            from hermes_constants import get_hermes_home
            home = Path(get_hermes_home())
            origin["profile"] = home.name if home.parent.name == "profiles" else "default"
        except (ImportError, AttributeError):
            pass
    if source and source != "local":
        origin["source"] = source
    if session_id:
        origin["session_id"] = session_id
    if fields.get("is_subagent") is True or source == "subagent" or session_id.startswith("subagent_"):
        origin["is_subagent"] = True
    return origin


def _store_delivery(content, metadata):
    """Give otherwise unaddressable deliveries a durable, profile-local transcript.

    Cron's standalone send API supplies no run ID. Never guess the newest
    run: queued deliveries can arrive after a later run has completed.
    """
    from uuid import uuid4

    from persistence.conversation.messages import NewMessage
    from persistence.conversation.sessions import NewSession
    from persistence_assembly import open_persistence, profile_scope

    is_cron = bool(metadata.get("job_id")) or str(content).startswith("Cronjob Response:")
    source = "cron" if is_cron else "push"
    session_id = "push_delivery_" + uuid4().hex
    with open_persistence(profile_scope()) as store:
        store.sessions.create(NewSession(session_id=session_id, source=source))
        store.messages.append(session_id, NewMessage(role="assistant", content=str(content)))
        title = _first_string(metadata, ("subtitle", "job_name", "job", "name"))
        try:
            store.sessions.set_title(session_id, title or ("Scheduled delivery" if is_cron else "Push delivery"))
        except Exception:
            # Duplicate/invalid titles (e.g. TitleConflict) must not discard a saved route.
            logger.debug("Push delivery title unavailable", exc_info=True)
    return {"session_id": session_id, "source": source}


def _build_payload(content, chat_id=None, thread_id=None, metadata=None):
    meta = metadata or {}
    # Hermes wraps cron output for general messaging platforms: a header naming
    # the job and its id, and a footer on how to stop it. On a lock screen that
    # is noise; the job's name goes on the second line instead. Device bridge
    # continuations are internal one-shot jobs, shown as a device update.
    cron_result = re.fullmatch(
        r'Cronjob Response: (?P<name>[^\n]*)\n'
        r'\(job_id: [^\n)]+\)\n-{13}\n\n(?P<body>.*?)'
        r'(?:\n\nTo stop or manage this job, send me a new message '
        r'\(e\.g\. "stop reminder (?P=name)"\)\.)?',
        str(content), re.DOTALL,
    )
    device_result = cron_result if cron_result and re.fullmatch(
        r"hermes-mobile-device-access-[0-9a-f]{32}", cron_result["name"]) else None
    if cron_result:
        content = cron_result["body"]
    payload = {
        "title": str(meta.get("title") or "Hermes"),
        "body": str(content)[:MAX_MESSAGE_LENGTH],
        # Addressed deliveries (cron reports, webhook routes) are things the
        # user asked to be told about, filed under the device's deliveries
        # toggle rather than as completions.
        "kind": _notification_kind(content, meta) if not cron_result or meta.get("kind") else "delivery",
    }
    # What produced this, on the notification's second line. Cron and webhook
    # metadata name the job under one of a few keys depending on the caller.
    subtitle = _first_string(meta, ("subtitle", "job_name", "job", "name", "source"))
    if device_result:
        subtitle = "Device update"
    elif cron_result and not subtitle:
        subtitle = cron_result["name"].strip()
    if subtitle:
        payload["subtitle"] = subtitle
    # chat_id is meaningless for push; reuse it as the notification thread
    # so repeated deliveries from one cron job group together.
    thread = meta.get("thread_id") or thread_id or (str(chat_id) if chat_id else "")
    if thread:
        payload["thread_id"] = str(thread)
    # Underscored keys are Hermes-internal send markers (``_interim_send``), not routing.
    # ``desktop_active`` is the turn-end hook's own finding, never a caller's.
    hermes = {
        k: v
        for k, v in meta.items()
        if k not in ("title", "thread_id", "desktop_active") and not str(k).startswith("_")
        and isinstance(v, (str, int, float, bool))
    }
    hermes.update(_origin_metadata(meta, use_context=True))
    # The apps file a notification by its sealed kind (the relay sees only the public one).
    hermes.setdefault("kind", payload["kind"])
    if payload["kind"] == "delivery" and not hermes.get("session_id"):
        try:
            # Persist before the preview is truncated and before the shared
            # relay worker loses the producer's profile context.
            hermes.update(_store_delivery(content, meta))
        except Exception:
            logger.warning("Could not save push delivery history", exc_info=True)
    if hermes:
        payload["hermes"] = hermes
    return payload


def _notification_kind(content, metadata):
    explicit = str(metadata.get("kind") or "").strip().lower()
    if explicit:
        return explicit
    text = str(content).strip().lower()
    if text.startswith(("⚠️ gateway ", "gateway shutting down", "gateway restarting",
                        "⚠️ hermes is shutting down", "⚠️ hermes is restarting")):
        return "system"
    if text.startswith(("♻️ gateway ", "♻ gateway ", "gateway online", "gateway restarted")):
        return "system"
    return "delivery"


class PushAdapter(BasePlatformAdapter):
    # A push carries a short preview and the full text goes to the delivery session, so cron
    # output must reach send() whole rather than cut with a footer.
    splits_long_messages = True

    def __init__(self, config: PlatformConfig):
        super().__init__(config=config, platform=Platform("push"))
        extra = config.extra or {}
        self.relay_url, self.install_key = _relay_settings(extra)
        _remember_platform_settings(extra)
        # A URL was given but refused, rather than none given.
        self._relay_url_refused = not self.relay_url and bool(
            extra.get("relay_url") or _secret("HERMES_MOBILE_PUSH_RELAY_URL"))

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if self._relay_url_refused:
            logger.warning("push adapter not connected: %s The install key travels with every request.", RELAY_URL_RULE)
            return False
        if not self.relay_url or not self.install_key:
            logger.warning(
                "push adapter not configured: HERMES_MOBILE_PUSH_RELAY_URL and "
                "HERMES_MOBILE_PUSH_INSTALL_KEY are required"
            )
            return False
        self._mark_connected()
        return True

    async def disconnect(self) -> None:
        self._mark_disconnected()

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        try:
            status, response = await asyncio.to_thread(
                _send_message, self.relay_url, self.install_key, content, chat_id=chat_id, metadata=metadata
            )
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:200]
            except Exception:  # noqa: S110 -- the detail is optional; the rejection is logged below
                pass
            logger.warning("push relay rejected notification: %s %s", exc.code, detail)
            return SendResult(success=False, error=f"HTTP {exc.code}: {detail}")
        except Exception as exc:
            logger.warning("push relay unreachable: %s", exc)
            return SendResult(success=False, error=str(exc))

        failure = _notify_failure(status, response)
        if failure:
            logger.warning("push relay did not deliver the notification: %s", failure)
            return SendResult(success=False, error=failure)
        return SendResult(success=True, message_id=None)

    async def get_chat_info(self, chat_id):
        return {"name": "Push", "type": "push"}


def check_requirements() -> bool:
    """Pre-flight: stdlib-only, so "installable" reduces to "configured"."""
    relay_url, install_key = _relay_settings()
    return bool(relay_url and install_key)


def validate_config(config) -> bool:
    relay_url, install_key = _relay_settings(getattr(config, "extra", {}) or {})
    return bool(relay_url and install_key)


def is_connected(config) -> bool:
    return validate_config(config)


def _env_enablement():
    """Seed ``PlatformConfig.extra`` from env so env-only setups register
    without a config file. Returns ``None`` when not configured — the caller
    skips auto-enabling. ``home_channel`` is lifted onto the config by the
    core hook; any value works, send() fans out to all paired devices.
    """
    relay_url, install_key = _relay_settings()
    if not relay_url or not install_key:
        return None
    home = _secret("HERMES_MOBILE_PUSH_HOME_CHANNEL").strip() or "device"
    return {
        "relay_url": relay_url,
        "install_key": install_key,
        "home_channel": {"chat_id": home, "name": "Push"},
    }


async def _standalone_send(
    pconfig,
    chat_id,
    message,
    *,
    thread_id=None,
    media_files=None,
    force_document=False,
):
    """Out-of-process delivery for cron and send_message fallbacks. Without
    this hook, ``deliver=push`` cron jobs fail with "No live adapter" when
    cron runs separately from the gateway. ``media_files`` and
    ``force_document`` are accepted for signature parity only.
    """
    extra = getattr(pconfig, "extra", {}) or {}
    relay_url, install_key = _relay_settings(extra)
    if not relay_url or not install_key:
        return {"error": "push standalone send: relay URL / install key not configured"}

    try:
        status, response = await asyncio.to_thread(
            _send_message, relay_url, install_key, message, chat_id=chat_id, thread_id=thread_id
        )
    except Exception as exc:
        return {"error": f"push standalone send failed: {exc}"}
    failure = _notify_failure(status, response)
    if failure:
        return {"error": f"push relay {failure}"}
    return {"success": True, "platform": "push", "chat_id": chat_id or "device", "message_id": None}


def _on_api_request(*args, **kwargs):
    """``pre_api_request``: an LLM call starts. Show the turn on the Live Activity
    and note a gateway session for the prompts it may put to the user."""
    fields = _hook_fields(args, kwargs)
    try:
        _activity_begin(fields)
    except Exception:
        logger.debug("Could not report activity", exc_info=True)
    try:
        _watch_prompts(fields)
    except Exception:
        logger.debug("Could not note the turn for its prompts", exc_info=True)


# The Live Activity's reporter (activity.py), one per process: each gateway
# process reports its own turns under its own source id.
_activity_tracker = None
_activity_lock = threading.Lock()


def _activity():
    global _activity_tracker
    if _activity_tracker is None:
        with _activity_lock:
            if _activity_tracker is None:
                _activity_tracker = activity.Tracker(user_agent=USER_AGENT)
    return _activity_tracker


def _activity_profile():
    """The profile this turn runs as: the session's, else this home's."""
    profile = _session_env("HERMES_SESSION_PROFILE").strip()
    if profile:
        return profile
    try:
        from hermes_constants import get_hermes_home
        home = Path(get_hermes_home())
        return home.name if home.parent.name == "profiles" else "default"
    except Exception:
        return "default"


def _activity_begin(fields):
    """Show this turn on the Live Activity, if a person is watching it: not
    cron, Kanban, subagents, webhooks or messaging platforms."""
    session_id = str(fields.get("session_id") or "")
    platform = str(fields.get("platform") or "").lower()  # "subagent" for a delegated child
    tracker = _activity()
    if not tracker.counts(session_id, platform, natively_notifying=NATIVELY_NOTIFYING,
                          kanban_worker=bool(os.getenv("HERMES_KANBAN_TASK")), ctx=_ctx):
        return
    tracker.begin(session_id, profile=_activity_profile(), relay=_relay_settings(),
                  turn_id=str(fields.get("turn_id") or ""))


def _report_activity(method, *args):
    """Tells the Live Activity reporter; it never disturbs the hook it runs in."""
    try:
        getattr(_activity(), method)(*args)
    except Exception:
        logger.debug("Could not report activity", exc_info=True)


def _activity_session(args, kwargs):
    return str(_hook_fields(args, kwargs).get("session_id") or "")


def _activity_turn(args, kwargs):
    """The hook's session and turn ids: the turn tells a detached fork's hooks apart."""
    fields = _hook_fields(args, kwargs)
    return str(fields.get("session_id") or ""), str(fields.get("turn_id") or "")


def _on_tool_start(*args, **kwargs):
    """``pre_tool_call``: the turn uses a tool. Observes only; returns nothing."""
    _report_activity("tool_started", *_activity_turn(args, kwargs))


def _on_tool_end(*args, **kwargs):
    _report_activity("tool_finished", *_activity_turn(args, kwargs))


def _on_stream_start(*args, **kwargs):
    """The reply starts streaming. Stream hooks run off the turn's thread, so
    only the payload's session id names the turn."""
    _report_activity("writing", *_activity_turn(args, kwargs))


def _on_approval_response(*args, **kwargs):
    """The person answered an approval: the turn runs again, unless the gateway still
    holds another prompt of it open (``_on_session_prompt_settled`` clears it then)."""
    session_id = _activity_session(args, kwargs)
    with _request_lock:
        waiting = any(activity_id == session_id for _, activity_id in _open_prompts.values())
    if not waiting:
        _report_activity("attention", session_id, False)


def _on_unload():
    _prompt_watches.clear()
    _open_prompts.clear()
    _platform_settings.clear()
    _unconfigured_homes_logged.clear()
    _unusable_active_seconds_warned.clear()
    _missing_seams_logged.clear()
    _report_activity("unload")


def _on_turn_reply(*args, **kwargs):
    """``post_llm_call``: the turn's final reply, as the user reads it (after
    output transforms), on every surface. ``on_session_end`` decides on it."""
    fields = _hook_fields(args, kwargs)
    session_id = str(fields.get("session_id") or "")
    body = fields.get("assistant_response")
    if not session_id or not isinstance(body, str) or not body.strip():
        return
    with _replies_lock:
        _replies.pop(session_id, None)
        _replies[session_id] = (fields.get("turn_id"), body.strip())
        while len(_replies) > 256:
            _replies.pop(next(iter(_replies)))


def _on_session_end(*args, **kwargs):
    """Notify at Hermes' explicit per-turn boundary.

    ``on_session_end`` is slightly misnamed in the plugin API: turn_finalizer
    fires it after every run_conversation call, right after ``post_llm_call``,
    with whether the turn completed. Only a completed turn's reply notifies.
    """
    fields = _hook_fields(args, kwargs)
    session_id = str(fields.get("session_id") or "")
    if not _delegated_turn(fields):
        # A subagent shares its parent's app session, whose turn is still running.
        _unwatch_prompts(session_id)
    if not session_id:
        return
    _report_activity("end", session_id, str(fields.get("turn_id") or ""))
    with _replies_lock:
        turn_id, body = _replies.pop(session_id, (None, ""))
    if not body or turn_id != fields.get("turn_id"):
        return
    if fields.get("interrupted") or fields.get("failed") or not fields.get("completed"):
        return

    platform = str(fields.get("platform") or "").lower()
    if platform == "cron" or session_id.startswith("cron_"):
        job_id, job_name, from_execution = _cron_run(session_id)
        platforms = _cron_delivery_platforms(job_id, from_execution=from_execution)
        if platforms is not None:
            lowered = {str(p).strip().lower() for p in platforms}
            if "push" in lowered:
                return  # The addressed delivery emits this job's notification.
            if "bot-chat" in lowered:
                return  # The Bot Chat turn emits this job's notification.
        if _autonomous_silence(body):
            return
        subtitle = job_name or "Scheduled task"
        try:
            # Match the scheduler's outward-delivery policy, even when
            # transcript redaction is disabled in the user's config.
            from agent.redact import redact_sensitive_text
            body = redact_sensitive_text(body, force=True)
            subtitle = redact_sensitive_text(subtitle, force=True)
        except Exception:
            body = "[REDACTED - redaction failed]"
            subtitle = "Scheduled task"
        _deliver_in_background({
            "title": "Hermes",
            "subtitle": subtitle,
            "body": body[:MAX_MESSAGE_LENGTH],
            "kind": "delivery",
            "thread_id": session_id,
            "hermes": {
                **_origin_metadata({**fields, "platform": "cron", "source": "cron"}),
                "kind": "delivery", "source": "cron", "session_id": session_id,
            },
        })
        return
    if (session_id.startswith(_UNATTENDED_SESSION_PREFIXES) or platform in _UNATTENDED_SOURCES
            or os.getenv("HERMES_KANBAN_TASK")):
        return
    if not _gateway_platform_notifies(platform):
        return
    if platform in _WEBHOOK_SURFACES and _autonomous_silence(body):
        # A webhook run, like cron, may end silently on purpose; Hermes still ends its turn.
        return
    if platform == "webhook" and _webhook_route_delivers_to_push():
        return  # The route's delivery emits this run's notification.

    body = _deliverable_reply(body)
    if not body:
        # The agent chose not to answer (a background process or subagent finished, say).
        return

    surface = _surface_label(platform)
    # Read here, in the turn's own finalization, where its context still names the turn and its
    # session, and only for a turn that notifies; the stream hooks run off the turn's thread.
    # ``facts``: the turn's client and its session's viewers, or unknown (``_turn_facts``).
    facts = _turn_facts(fields, platform)
    hermes = {
        **_origin_metadata({**fields, "platform": platform, "source": _turn_source(fields, platform, facts)}),
        "kind": "agent_end",
        "session_id": session_id,
        "platform": platform,
        "turn_id": str(fields.get("turn_id") or ""),
    }
    # Unknown facts (among them a delegated child's completion and a webhook run's reply) say
    # nothing about a computer. The relay reads the fact only on a Desktop reply, so no other
    # reply carries it.
    if facts is not _UNKNOWN and hermes.get("source") == "desktop":
        hermes["desktop_active"] = _desktop_active(facts, _active_seconds())
    if hermes.get("desktop_active") is True and _quiet_while_active():
        # The user is active at a computer. The turn's end is already reported
        # and its reply taken, as for a reply that says nothing.
        return
    _deliver_in_background({
        "title": "Hermes",
        **({"subtitle": surface} if surface else {}),
        "body": body[:MAX_MESSAGE_LENGTH],
        "kind": "reply",
        "thread_id": session_id,
        "hermes": hermes,
    })


def _webhook_route_delivers_to_push():
    """Whether the webhook route this turn runs for delivers its reply through push.

    The webhook platform names the route in the session's user id
    (``webhook:<route>``); its ``deliver`` is in the profile's config.yaml or,
    for routes the agent subscribed, ``webhook_subscriptions.json``, config first,
    as the adapter reads them. A shared listener reads the process's home, so it
    is consulted after the profile's. When the route cannot be read the turn
    notifies: a duplicate beats a missed notification.
    """
    user_id = _session_env("HERMES_SESSION_USER_ID")
    if not user_id.startswith("webhook:"):
        return False
    try:
        route = _webhook_route(user_id[len("webhook:"):])
    except Exception:
        logger.debug("Could not read the webhook route", exc_info=True)
        return False
    deliver = route.get("deliver") if isinstance(route, dict) else None
    targets = deliver if isinstance(deliver, (list, tuple)) else [deliver]
    return any(str(target or "").strip().lower() == "push" for target in targets)


def _webhook_route(name):
    """The webhook route *name* as the profile's, else the process's, Hermes home defines it."""
    from hermes_constants import get_hermes_home, get_process_hermes_home
    homes = [Path(get_hermes_home())]
    if Path(get_process_hermes_home()) != homes[0]:
        homes.append(Path(get_process_hermes_home()))
    for home in homes:
        for routes in (_webhook_config_routes(home), _webhook_subscriptions(home)):
            if isinstance(routes, dict) and isinstance(routes.get(name), dict):
                return routes[name]
    return None


def _webhook_config_routes(home):
    """The routes config.yaml gives the webhook platform, through Hermes' own gateway config loader."""
    from gateway.config import load_gateway_config
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    token = set_hermes_home_override(home)
    try:
        config = load_gateway_config()
    finally:
        reset_hermes_home_override(token)
    webhook = config.platforms.get(Platform("webhook"))
    return (getattr(webhook, "extra", None) or {}).get("routes")


def _webhook_subscriptions(home):
    path = Path(home) / "webhook_subscriptions.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _in_delegated_child(*, include_subprocesses=False):
    """Whether the calling code runs for a ``delegate_task`` child, or ``False`` without the seam."""
    accessor = getattr(_ctx, "in_delegated_child", None)
    if not callable(accessor):
        _log_missing_seam_once(
            "in_delegated_child",
            "delegated-child context detection is off",
        )
        return False
    try:
        return bool(accessor(include_subprocesses=include_subprocesses))
    except Exception:
        logger.debug("Could not read in_delegated_child() for the turn", exc_info=True)
        return False


def _delegated_turn(fields):
    """Whether the hook fires for a delegated child (``delegate_task``), not the session's own turn."""
    if str(fields.get("platform") or "").strip().lower() == "subagent":
        return True
    return _in_delegated_child()


def _gateway_platform_notifies(platform):
    """Whether a finished turn on *platform* notifies, by ``HERMES_MOBILE_PUSH_HOOK_PLATFORMS``.

    Set, it names exactly the gateway platforms (messaging platforms, webhooks,
    the API server) whose turns notify; unset, those that do not notify natively
    do. The app's, the desktop's and the CLI's own turns and subagents are not
    gateway platforms here and always notify (cron and Kanban are decided before).
    """
    if platform in _OWN_SURFACES or platform == "subagent":
        return True
    allowed = _gate("HERMES_MOBILE_PUSH_HOOK_PLATFORMS")
    if allowed:
        return platform in {p.strip().lower() for p in allowed.split(",") if p.strip()}
    return platform not in NATIVELY_NOTIFYING


def _autonomous_silence(body):
    """The run ended on a silence marker: cron and webhook runs that had nothing to say."""
    try:
        from gateway.response_filters import is_autonomous_silence_response
    except ImportError:
        # Older Hermes releases only recognize the original sentinel.
        return body.upper().startswith("[SILENT]")
    return is_autonomous_silence_response(body)


# Hermes' interactive silence markers, for a release without ``gateway.response_filters``'
# ``is_intentional_silence_response``.
_SILENCE_MARKERS = frozenset({"[SILENT]", "SILENT", "NO_REPLY", "NO REPLY"})


def _deliverable_reply(body):
    """*body* as Hermes delivers a chat reply: ``""`` when it is only a silence marker, and
    without a marker that closes a real reply on its own line (``"Done.\n\nNO_REPLY"``)."""
    try:
        from gateway.response_filters import (
            is_intentional_silence_response,
            strip_trailing_silence_marker,
        )
    except ImportError:  # Older Hermes delivers a closing marker as written; so does its notification.
        return "" if " ".join(body.strip().upper().split()) in _SILENCE_MARKERS else body
    return "" if is_intentional_silence_response(body) else strip_trailing_silence_marker(body).strip()


def _preview_excerpt(value, limit):
    text = str(value or "").strip()
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _redacted(text):
    """*text* with credentials masked, even when transcript redaction is off: an approval's
    or password prompt's command goes to the lock screen, as cron output does."""
    try:
        from agent.redact import redact_sensitive_text
    except ImportError:
        return text  # Only outside Hermes: every supported release has the redactor.
    try:
        return redact_sensitive_text(text, force=True)
    except Exception:
        return "[REDACTED - redaction failed]"


def _approval_preview(fields):
    description = _redacted(str(fields.get("description") or "").strip())
    command = _redacted(str(fields.get("command") or "").strip())
    if description and command and description != command:
        return "Reason: " + _preview_excerpt(description, 170) + "\nCommand: " + _preview_excerpt(command, 370)
    return _preview_excerpt(command or description or "The agent is waiting for approval.", 580)


def _question_preview(payload):
    """Only mark text answers whose displayed question has a server identity.

    For a batch, display and address its first unanswered question explicitly;
    the answer receipt directs the user to any remaining questions.
    """
    question = payload
    metadata = {}
    questions = payload.get("questions")
    if questions:
        if not isinstance(questions, list):
            return "Open Hermes Agent to review these questions.", metadata
        answers = payload.get("answers") if isinstance(payload.get("answers"), dict) else {}
        remaining = [q for q in questions if isinstance(q, dict) and q.get("qid") not in answers]
        if not remaining or not isinstance(remaining[0].get("qid"), str) or not remaining[0]["qid"].strip():
            return "Open Hermes Agent to review these questions.", metadata
        question = remaining[0]
        metadata["question_id"] = question["qid"]
    text = str(question.get("question") or "").strip()
    if not text:
        return "Open Hermes Agent to review the question.", {}
    body = _preview_excerpt(text, 400)
    choices = question.get("choices")
    if isinstance(choices, list) and choices:
        body += "\nOptions: " + _preview_excerpt(", ".join(str(c) for c in choices), 140)
    if question.get("multi_select"):
        body += "\nYou can choose more than one."
    if payload.get("request_id"):
        metadata["answer_mode"] = "text"
    return body, metadata


def _on_approval_request(*args, **kwargs):
    """Observer for `pre_approval_request`, on Hermes before 0.21.6 only: the
    agent has stopped and is waiting for a human. Newer Hermes reports the
    approvals a person is actually asked through ``on_human_input_request``.

    Covers the surfaces that do not ask through a server→client request (a
    messaging platform, the CLI). An app or desktop session's approval goes out
    as an ``approval`` request right after this hook, and that request carries
    the id the notification's actions answer, so it notifies from there.
    """
    if _HUMAN_INPUT_HOOKS:
        return
    fields = _hook_fields(args, kwargs)
    surface = str(fields.get("surface") or "").strip()
    # Smart mode fires this observer before its auxiliary LLM decides. An
    # approval here may never reach a human; if Smart denies, Hermes emits a
    # second request on the real gateway/CLI surface and we notify from that.
    if surface.lower() == "smart":
        return
    _report_activity("attention", str(fields.get("session_id") or ""), True)
    session_key = str(fields.get("session_key") or "")
    description = _approval_preview(fields)
    session_id = str(fields.get("session_id") or "") or _session_env("HERMES_SESSION_ID")
    source = _session_env("HERMES_SESSION_PLATFORM").strip().lower()
    origin = _origin_metadata({**fields, **({"session_id": session_id} if session_id else {}),
                               **({"source": source} if source else {})})
    payload = {
        "title": "Approval needed",
        "subtitle": "Waiting on you" if not surface else f"Waiting on you · {surface}",
        "body": description,
        "kind": "approval",
        "hermes": {**origin, "kind": "approval", "session_key": session_key},
    }
    if session_id or session_key:
        payload["thread_id"] = session_id or session_key
    _deliver_in_background(payload)


# Server→client requests a person answers, by JSON-RPC method: the
# notification kind and title. The others (terminal, preview and window reads,
# the tour) are answered by a desktop window, never by a person.
_HUMAN_REQUESTS = {
    "approval": ("approval", "Approval needed"),
    "clarify": ("clarify", "Question"),
    "sudo": ("sudo", "Password needed"),
    "secret": ("secret", "Secret needed"),
    "vault.unlock_prompt": ("input", "Input needed"),
    "vault.code": ("input", "Input needed"),
    "vault.save_login": ("input", "Input needed"),
}


def _server_request_payload(method, request_id, params, origin):
    """The push for one server→client request a person must answer, or ``None``.

    *request_id* is the request's own ``srq-`` id, which answers a question
    (``clarify.lock`` / ``request.answer``). An approval is answered by its
    approval-queue id instead (``approval.respond``), which its params carry.
    *origin* is the turn's ``_origin_metadata``: profile, source, stored session id.
    """
    if method not in _HUMAN_REQUESTS:
        return None
    kind, title = _HUMAN_REQUESTS[method]
    answer_metadata = {}
    if method == "approval":
        body = _approval_preview(params)
        request_id = str(params.get("request_id") or "")
    elif method == "clarify":
        body, answer_metadata = _question_preview({**params, "request_id": request_id})
    elif method == "sudo":
        body = "Hermes needs your administrator password to continue."
        command = str(params.get("command") or "").strip()
        if command:
            body += "\nCommand: " + _preview_excerpt(_redacted(command), 400)
    elif method == "secret":
        body = str(params.get("prompt") or params.get("env_var") or "").strip() or "Hermes needs a secret to continue."
    elif method == "vault.unlock_prompt":
        name = str(params.get("display_name") or params.get("backend") or "").strip() or "your password manager"
        body = f"Unlock {name} to continue."
    elif method == "vault.code":
        site = str(params.get("site") or "").strip()
        body = f"Hermes needs a one-time code for {site}." if site else "Hermes needs a one-time code."
        hint = str(params.get("hint") or "").strip()
        if hint:
            body += "\n" + _preview_excerpt(hint, 200)
    else:
        site = str(params.get("site") or params.get("origin") or "").strip()
        body = f"Save a login for {site}?" if site else "Save a login?"

    session_id = str(origin.get("session_id") or "")
    hermes = {**origin, "kind": kind, **answer_metadata}
    if request_id:
        hermes["request_id"] = request_id
    payload = {
        "title": title,
        "subtitle": "Waiting on you",
        "body": _preview_excerpt(body, 600),
        "kind": kind,
        "hermes": hermes,
    }
    if session_id:
        payload["thread_id"] = session_id
    return payload


# A turn whose end was never reported stops being watched after this long.
REQUEST_WATCH_TTL_SECONDS = 6 * 60 * 60

_notified_requests = {}  # prompt id -> None, oldest first; bounded
_request_lock = threading.Lock()


def _session_env(name):
    try:
        from gateway.session_context import get_session_env
        return str(get_session_env(name, "") or "")
    except Exception:
        return ""


# Whether this Hermes reports every wait on a person through
# ``on_human_input_request`` (0.21.6+): only once a person is asked, never for a
# Smart-mode decision or an approval that joins one already asked, with the
# session it waits in and a force-redacted prompt.
try:
    _HUMAN_INPUT_HOOKS = importlib.util.find_spec("tools.human_input_hooks") is not None
except (ImportError, ValueError):
    _HUMAN_INPUT_HOOKS = False


# Whether this Hermes reports the prompts its gateway opens for a person
# (``on_session_prompt`` / ``on_session_prompt_settled``): each approval and
# question it publishes to a session's viewers and each password, secret and
# password manager request, once registered where its id answers it.
try:
    _SESSION_PROMPT_HOOKS = importlib.util.find_spec("gateway.session_prompt_hooks") is not None
except (ImportError, ValueError):
    _SESSION_PROMPT_HOOKS = False

# The gateway's own session key for an app session: agent:<profile or main>:local:dm:<live session id>.
_LOCAL_SESSION_KEY = re.compile(r"^agent:[^:]+:local:dm:(.+)$")
# A turn in a gateway session, by every id the gateway may name its session by: the
# turn's stored session id (which a compression rotates) and the live id in its
# session key -> {"keys", "notifies", "origin", "relay", "activity_id", "touched"}.
_prompt_watches = {}
_PROMPT_WATCH_LIMIT = 256  # turns noted at once; the oldest is forgotten first
_open_prompts = {}  # prompt id -> (the hook's session id, the Live Activity's), oldest first; bounded


def _gateway_reports_prompts():
    """Whether the current turn's session is one whose waits the plugin announces from ``on_session_prompt``.

    That is a gateway app session (session platform ``local``) on a Hermes with the session prompt
    hooks. ``_watch_prompts`` and ``_human_input`` both run in the turn's own context and ask this
    one question, so exactly one of them announces each wait. The session's platform decides, not
    ``HERMES_PLATFORM``, which Hermes puts first in ``on_human_input_request``'s ``platform`` and
    which may name another surface of the same process.
    """
    return (_SESSION_PROMPT_HOOKS
            and _session_env("HERMES_SESSION_PLATFORM").strip().lower() == "local")


def _forget_prompt_watch(watch):
    """Drop every id *watch* is noted under. Caller holds ``_request_lock``."""
    for key in watch["keys"]:
        if _prompt_watches.get(key) is watch:
            _prompt_watches.pop(key, None)


def _prune_prompt_watches(current):
    """Forget stale turns, then the least recently noted beyond the bound, each with all its ids; never
    *current*, the turn just noted. Caller holds ``_request_lock``."""
    now = current["touched"]
    others = [watch for watch in {id(watch): watch for watch in _prompt_watches.values()}.values()
              if watch is not current]
    for watch in others:
        if now - watch["touched"] > REQUEST_WATCH_TTL_SECONDS:
            _forget_prompt_watch(watch)
    live = sorted((watch for watch in others if now - watch["touched"] <= REQUEST_WATCH_TTL_SECONDS),
                  key=lambda watch: watch["touched"])
    for watch in live[:max(0, len(live) + 1 - _PROMPT_WATCH_LIMIT)]:
        _forget_prompt_watch(watch)


def _watch_prompts(fields):
    """Note the current turn of a gateway session for the prompts the gateway opens in it.

    Called from the turn's own context; ``on_session_prompt`` may arrive on the
    gateway's event loop, with none of it. A delegated child shares its parent's session.
    Only an app session (platform ``local``) notifies from the prompt: elsewhere the
    person is asked where they already are, and ``on_human_input_request`` notifies.
    """
    if not _SESSION_PROMPT_HOOKS or _delegated_turn(fields):
        return
    stored = str(fields.get("session_id") or "") or _session_env("HERMES_SESSION_ID")
    live = _LOCAL_SESSION_KEY.match(_session_env("HERMES_SESSION_KEY"))
    keys = {key for key in (stored, live and live.group(1)) if key}
    platform = _session_env("HERMES_SESSION_PLATFORM").strip().lower()
    watch = {"keys": keys, "notifies": _gateway_reports_prompts(), "activity_id": stored, "touched": time.monotonic()}
    if not watch["notifies"] and stored and stored not in _prompt_watches:
        # A messaging session's prompts name the root of its compression lineage
        # (``SessionAuthority.logical_owner``), which a compression leaves behind.
        root = _lineage_root(stored)
        if root:
            keys.add(root)
    if watch["notifies"]:
        watch["origin"] = _origin_metadata({"session_id": stored, "profile": _session_env("HERMES_SESSION_PROFILE"),
                                            "source": _creation_source(platform)})
        watch["relay"] = _relay_settings()
    with _request_lock:
        for key in list(keys):
            if key in _prompt_watches:
                keys.update(_prompt_watches[key]["keys"])  # The same turn before a compression renamed it.
        for key in keys:
            _prompt_watches[key] = watch
        # A turn whose end is never reported, and a compressed turn's old ids, go without waiting for a lookup.
        _prune_prompt_watches(watch)


def _lineage_root(session_id):
    """The first id of *session_id*'s compression lineage, or "" when it has none or it cannot be read."""
    try:
        from persistence_assembly import open_persistence_reader, profile_scope
        with open_persistence_reader(profile_scope().profile_dir) as store:
            lineage = store.lineage.lineage(session_id)
    except Exception:
        return ""
    return str(lineage[0]) if lineage else ""


def _unwatch_prompts(session_id):
    """The turn ended: its gateway session's next prompt belongs to a later turn, and
    none of its prompts is still open (one whose settle never came would hold the next
    turn's Live Activity on "waiting")."""
    live = _LOCAL_SESSION_KEY.match(_session_env("HERMES_SESSION_KEY"))
    with _request_lock:
        ended = {key for key in (session_id, live and live.group(1)) if key}
        for key in list(ended):
            watch = _prompt_watches.get(key)
            if watch is not None:
                ended.update(watch["keys"])
                _forget_prompt_watch(watch)
        for prompt_id, (hook_session_id, activity_id) in list(_open_prompts.items()):
            if hook_session_id in ended or activity_id in ended:
                _open_prompts.pop(prompt_id, None)


def _prompt_watch(session_id):
    """The turn *session_id* names, unless it is stale. Caller holds ``_request_lock``."""
    watch = _prompt_watches.get(session_id)
    if watch is not None and time.monotonic() - watch["touched"] > REQUEST_WATCH_TTL_SECONDS:
        _forget_prompt_watch(watch)
        return None
    return watch


def _on_session_prompt(*args, **kwargs):
    """``on_session_prompt``: the gateway opened a prompt a person answers, where its id answers it.

    Notifies once per prompt id, as the request watch would for the same request:
    an approval and a question are answered by the gateway's ``prompt_id``
    (``approval.respond`` / ``clarify.respond``), a request frame by its ``srq-`` id.
    The push is addressed from the turn ``_watch_prompts`` noted; without one, from
    the hook's own session and the current relay settings, rather than not at all.
    """
    fields = _hook_fields(args, kwargs)
    session_id = str(fields.get("session_id") or "")
    kind = str(fields.get("kind") or "")
    prompt_id = str(fields.get("prompt_id") or "")
    params = fields.get("params") if isinstance(fields.get("params"), dict) else {}
    if kind not in _HUMAN_REQUESTS or not prompt_id:
        return
    with _request_lock:
        watch = _prompt_watch(session_id)
        if watch is not None and not watch["notifies"]:
            return  # Notified from on_human_input_request.
        if prompt_id in _notified_requests:
            return
        _notified_requests[prompt_id] = None
        while len(_notified_requests) > 512:
            _notified_requests.pop(next(iter(_notified_requests)))
    if watch is None:
        origin, relay, activity_id = _origin_metadata({"session_id": session_id}), _relay_settings(), session_id
    else:
        origin, relay, activity_id = watch["origin"], watch["relay"], watch["activity_id"]
    with _request_lock:
        _open_prompts[prompt_id] = (session_id, activity_id)
        while len(_open_prompts) > 512:
            _open_prompts.pop(next(iter(_open_prompts)))
    _report_activity("attention", activity_id, True)
    if kind == "approval":
        params = {**params, "request_id": prompt_id}  # What answers it on the gateway (``approval.respond``).
    _deliver_in_background(_server_request_payload(kind, prompt_id, params, origin), relay=relay)


def _on_session_prompt_settled(*args, **kwargs):
    """``on_session_prompt_settled``: the prompt closed; once no prompt of its Live Activity is open, the turn
    runs again. Prompts of one turn may name different sessions (stored and live ids), so the activity decides."""
    prompt_id = str(_hook_fields(args, kwargs).get("prompt_id") or "")
    with _request_lock:
        opened = _open_prompts.pop(prompt_id, None)
        if opened is None:
            return
        waiting = any(activity_id == opened[1] for _, activity_id in _open_prompts.values())
    if not waiting:
        _report_activity("attention", opened[1], False)


def _human_input(fields):
    """The wait on a person *fields* describe, as ``(kind, session id)``, or
    ``None`` when this hook does not notify it.

    A gateway app session on a Hermes with the session prompt hooks notifies from
    the prompt the gateway opens for it (``_on_session_prompt``), which carries
    the id that answers it. Questions and password prompts notify only in app
    sessions (platform "local"); elsewhere they are asked where the person already is.
    """
    kind = str(fields.get("kind") or "")
    if kind not in ("approval", "clarify", "sudo"):
        return None
    platform = str(fields.get("platform") or "").strip().lower()
    if kind != "approval" and platform != "local":
        return None
    if _gateway_reports_prompts():
        return None  # The gateway reports this wait once it opens it (``on_session_prompt``), with its id.
    return kind, str(fields.get("session_id") or "")


def _on_human_input_request(*args, **kwargs):
    """``on_human_input_request``: the agent waits on a person (Hermes 0.21.6+)."""
    fields = _hook_fields(args, kwargs)
    request = _human_input(fields)
    if request is None:
        return
    kind, session_id = request
    _report_activity("attention", session_id, True)
    # Hermes force-redacts the prompt: the command, or the question's text. The
    # notification opens the chat; the request id here answers nothing the app can.
    prompt = str(fields.get("prompt") or "").strip()
    platform = str(fields.get("platform") or "").strip().lower()
    if kind == "approval":
        body = _preview_excerpt(prompt, 580) or "The agent is waiting for approval."
    elif kind == "clarify":
        body = _preview_excerpt(prompt, 400) or "Open Hermes Agent to review the question."
    else:
        body = "Hermes needs your administrator password to continue."
        if prompt:
            body += "\nCommand: " + _preview_excerpt(prompt, 400)
    surface = _surface_label(platform)
    session_key = str(fields.get("session_key") or "")
    payload = {
        "title": _HUMAN_REQUESTS[kind][1],
        "subtitle": f"Waiting on you · {surface}" if surface else "Waiting on you",
        "body": body,
        "kind": kind,
        "hermes": {**_origin_metadata({"session_id": session_id, "platform": platform,
                                       "source": _creation_source(platform)}), "kind": kind,
                   **({"session_key": session_key} if session_key else {})},
    }
    if session_id or session_key:
        payload["thread_id"] = session_id or session_key
    _deliver_in_background(payload)


def _on_human_input_resolved(*args, **kwargs):
    """The wait ended (answered, withdrawn, timed out): the turn runs again."""
    request = _human_input(_hook_fields(args, kwargs))
    if request is not None:
        _report_activity("attention", request[1], False)


def _first_string(mapping, keys):
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


# Where a person talks to the agent through Hermes Agent or Hermes itself. A reply
# there needs no label for where it ran: the line it took ("Ios") stood out
# above the reply it belonged to.
# "gui" is the unified runtime's (Hermes 0.22.1+) creation label for Hermes Agent's and the
# Desktop's sessions; their agents run as "desktop" ("gui" in a managed worker), and
# the gateway itself calls them "local".
_OWN_SURFACES = frozenset({"", "ios", "android", "gui", "web", "desktop", "cli", "tui", "local"})
# Turns no person waits on: Kanban workers.
_UNATTENDED_SOURCES = frozenset({"kanban"})
# Turns an incoming webhook started: no person asked, and a silent run says nothing.
_WEBHOOK_SURFACES = frozenset({"webhook", "msgraph_webhook"})
# The mobile app's Show Source switch removes these names again: change both together.
_SURFACE_NAMES = {"whatsapp": "WhatsApp", "imessage": "iMessage", "bluebubbles": "iMessage", "ios": "iOS"}


def _surface_label(platform):
    """Where a turn ran, on the notification's second line; empty for Hermes Agent's own surfaces."""
    key = str(platform or "").strip().lower()
    if key in _OWN_SURFACES:
        return ""
    return _SURFACE_NAMES.get(key) or key.replace("_", " ").title()


def _hook_fields(args, kwargs):
    """Plugin hooks are called with kwargs, but tolerate a payload dict too —
    the calling convention is not identical across hook families.
    """
    fields = dict(kwargs)
    for arg in args:
        if isinstance(arg, dict):
            fields.update(arg)
    return fields


_delivery_queue = queue.Queue(maxsize=64)
_delivery_lock = threading.Lock()
_delivery_worker = None


def _drain_deliveries():
    while True:
        relay_url, install_key, payload = _delivery_queue.get()
        try:
            status, response = _post_notify(relay_url, install_key, payload)
            # The relay answers with delivery counts only; nothing private.
            if _notify_failure(status, response):
                logger.warning("Push relay could not deliver a %s notification: HTTP %s %s",
                               payload.get("kind") or "delivery", status, response[:200])
            else:
                logger.info("Push relay accepted a %s notification: HTTP %s %s",
                            payload.get("kind") or "delivery", status, response[:200])
        except urllib.error.HTTPError as error:
            if error.code == 404:
                # An install no device has paired with yet answers 404 to every notification.
                logger.info("Push relay has no device paired with this install; dropped a %s notification",
                            payload.get("kind") or "delivery")
            else:
                logger.warning("Push relay rejected a %s notification: HTTP %s",
                               payload.get("kind") or "delivery", error.code)
        except Exception:
            logger.exception("Push relay delivery failed")
        finally:
            _delivery_queue.task_done()


def _deliver_in_background(payload, relay=None):
    """POST from a daemon thread. Hooks fire inside the agent's own code path
    — an approval prompt, the end of a turn — and a slow relay must never
    hold that up. Failures are logged and dropped; a missed ping is not worth
    breaking a run over.
    """
    relay_url, install_key = relay or _relay_settings()
    if not relay_url or not install_key:
        return

    global _delivery_worker
    with _delivery_lock:
        if _delivery_worker is None or not _delivery_worker.is_alive():
            _delivery_worker = threading.Thread(target=_drain_deliveries, daemon=True, name="hermes-push-delivery")
            _delivery_worker.start()
        if _delivery_queue.full():
            try:
                _delivery_queue.get_nowait()
                _delivery_queue.task_done()
                logger.warning("Push delivery queue full; dropped oldest notification")
            except queue.Empty:
                pass
        _delivery_queue.put_nowait((relay_url, install_key, payload))


def _registration_extras():
    """Registry fields only newer Hermes knows: an unknown keyword would refuse the platform."""
    try:
        from dataclasses import fields

        from gateway.platform_registry import PlatformEntry
        known = {field.name for field in fields(PlatformEntry)}
    except Exception:
        return {}
    extras = {}
    if "env_value_rules" in known:
        # The dashboard refuses a plain-HTTP relay URL when it is entered.
        extras["env_value_rules"] = {"HERMES_MOBILE_PUSH_RELAY_URL": (_relay_url_allowed, RELAY_URL_RULE)}
    return extras


def register(ctx):
    """Plugin entry point — called by the Hermes plugin system."""
    global _ctx
    _ctx = ctx
    try:
        privacy.load_key(create=True)
    except Exception:
        logger.warning("Private previews unavailable; check cryptography and the preview key file")
    ctx.register_platform(
        name="push",
        label="Push (Hermes Agent)",
        adapter_factory=lambda cfg: PushAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=["HERMES_MOBILE_PUSH_RELAY_URL", "HERMES_MOBILE_PUSH_INSTALL_KEY"],
        env_enablement_fn=_env_enablement,
        # Any value works as the delivery channel — send() fans out to all
        # paired devices regardless — it only has to be set for
        # `deliver: push` to resolve in cron jobs and webhook routes.
        cron_deliver_env_var="HERMES_MOBILE_PUSH_HOME_CHANNEL",
        # Cron often runs outside the gateway process; without this,
        # `deliver: push` fails there with "No live adapter".
        standalone_sender_fn=_standalone_send,
        **_registration_extras(),
        # 0: never chunk. One delivery is one push and one saved transcript;
        # the preview is shortened here and in privacy.py, the transcript keeps it all.
        max_message_length=0,
        pii_safe=True,
        platform_hint=(
            "You are delivering a push notification to the user's phone. "
            "Keep it short: one or two plain-text sentences, no markdown."
        ),
        emoji="📲",
    )
    if hasattr(ctx, "on_unload"):
        # Stop watching, and tell the relay this process went idle.
        ctx.on_unload(_on_unload)
    # Ping the phone when the agent stops to ask the user something.
    # Guarded: register_hook shipped after pluggable platforms, and a missing
    # approval ping must not take the whole platform down with it.
    if hasattr(ctx, "register_hook"):
        # Keep registrations identical to the manifest on every supported runtime.
        # Approvals before Hermes 0.21.6, on surfaces that do not ask by
        # server→client request (messaging platforms, the CLI).
        ctx.register_hook("pre_approval_request", _on_approval_request)
        # Turn completion, wherever the agent runs: post_llm_call carries the
        # final reply, on_session_end whether the turn completed. pre_api_request
        # starts watching the turn's session for questions to the user.
        ctx.register_hook("pre_api_request", _on_api_request)
        ctx.register_hook("post_llm_call", _on_turn_reply)
        ctx.register_hook("on_session_end", _on_session_end)
        # What the Live Activity shows of a running turn (activity.py): a
        # tool in use, a reply streaming, an approval answered.
        ctx.register_hook("pre_tool_call", _on_tool_start)
        ctx.register_hook("post_tool_call", _on_tool_end)
        ctx.register_hook("on_stream_start", _on_stream_start)
        ctx.register_hook("post_approval_response", _on_approval_response)
        # Approvals, questions and password prompts a person is asked (Hermes
        # 0.21.6+), except in app and desktop sessions a request watch sees.
        ctx.register_hook("on_human_input_request", _on_human_input_request)
        ctx.register_hook("on_human_input_resolved", _on_human_input_resolved)
        # The prompts a gateway opens for a person, with the id that answers each
        # (Hermes with gateway.session_prompt_hooks): a gateway app session's waits.
        # Registered only where Hermes fires them: an older Hermes warns about an
        # unknown hook at every start, on every home this plugin is installed on.
        if _SESSION_PROMPT_HOOKS:
            ctx.register_hook("on_session_prompt", _on_session_prompt)
            ctx.register_hook("on_session_prompt_settled", _on_session_prompt_settled)
