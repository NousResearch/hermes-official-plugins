"""Working-agent snapshots for Hermes Agent's Live Activity.

The app shows one Live Activity for the work on all of a device's servers.
Once the app leaves the screen it hears nothing, so the relay keeps the
activity current from what every gateway process reports here: a full
snapshot of this process's interactive turns that report to an install,
posted to ``POST {relay}/activity`` (the install key as its bearer token) from a
daemon thread.

What leaves the server is counts, start times, attention, a generic status
(thinking, tool, writing, waiting) and opaque keys: the profile and session
are HMAC-SHA256 digests keyed by the install key, which the app can compute
too. Never names, session ids, tool names or text.

Only turns a person is watching count, as the app's own count does: cron,
Kanban workers, subagents, webhooks and messaging platforms (which notify
natively) stay out.

Each process mints its own ``source`` and posts whole snapshots, so a lost
request is repaired by the next one. A change the user acts on (an agent
starting, finishing, or waiting on them) is posted at once; running detail
(tool count, status) at most every DETAIL_INTERVAL seconds; while anything
runs, a heartbeat every HEARTBEAT seconds renews its TTL_SECONDS lifetime.
An empty snapshot says idle. A relay that does not know the endpoint (404 or
405) or refuses the report for good (400, 409, 413, or 401 for a wrong key) is
left alone for BACKOFF_SECONDS; a rate limit (429), a server error or no answer
is tried again after RETRY_SECONDS. Such a report may still have reached the
relay, so the snapshot after it is posted even when it is the idle one.

A turn leaves the activity at its ``on_session_end``. Hermes does not always
fire it, so a turn is also known by its turn id and let go of when it is
clearly over:

- Context compression gives the session a new id mid-turn; the turn id stays,
  so the agent moves to the new session and ends there.
- A turn that returns early (an API error, an interrupt) has no end; the next
  turn in the session (a new turn id) starts afresh, and a turn no hook has
  touched for IDLE_SECONDS (BUSY_SECONDS while a tool runs or it waits on the
  person) is dropped. Should it carry on, it comes back with its start time.
- A detached fork (the background memory and skill review, a /btw side
  question) runs under the parent's session id and never ends; it does not
  count, and hooks of another turn id do not touch the session's agent.
"""

import hashlib
import hmac
import json
import logging
import threading
import time
import urllib.error
import urllib.request
import uuid

logger = logging.getLogger(__name__)

TTL_SECONDS = 300
HEARTBEAT = 120.0
DETAIL_INTERVAL = 15.0
BACKOFF_SECONDS = 3600.0
RETRY_SECONDS = 15.0
# Answers a retry will not change: the endpoint is unknown, or the key or the report is refused.
UNKNOWN_ENDPOINT = frozenset({404, 405})
REFUSED = frozenset({400, 401, 409, 413})
REQUEST_TIMEOUT_SECONDS = 10
MAX_AGENTS = 32
# How long a turn may go without a hook before it is taken for over. A running turn
# fires one at every model call, retry and tool; the longest silence between them
# with Hermes' defaults is one model call, which the stale-stream watchdog ends
# after at most 10 minutes for reasoning models (agent/reasoning_timeouts.py).
IDLE_SECONDS = 900.0
# A tool (a foreground command, up to 10 minutes by default; a delegated subagent)
# or a wait on the person (approvals time out after 5 minutes by default, but the
# timeout is the user's to raise) can be silent for longer.
BUSY_SECONDS = 3600.0
# Turns dropped as silent, by turn id, to restore their start if they resume.
DROPPED_TURNS = 64
# What a relay last holds after a report that got no clear answer: unknown, so
# it matches no snapshot and the next one is posted, idle included.
UNCONFIRMED = object()

THINKING, TOOL, WRITING, WAITING = "thinking", "tool", "writing", "waiting"

# Messaging platforms notify natively, so their turns are not Hermes Agent's to show;
# the adapter passes its NATIVELY_NOTIFYING set in.
UNATTENDED_PLATFORMS = frozenset({"cron", "kanban", "msgraph_webhook", "subagent", "webhook"})
UNATTENDED_PREFIXES = ("cron_", "kanban_", "subagent_")
# Detached forks that share a turn's session id after it ended: never ended, never watched.
DETACHED_ORIGINS = frozenset({"background_review", "side_question"})
_missing_fork_origin_logged = False


def fork_origin(ctx=None):
    """The running detached fork's label (``ctx.fork_origin()`` / ``hermes_cli.plugin_session.fork_origin``), or ""."""
    global _missing_fork_origin_logged
    if ctx is not None:
        accessor = getattr(ctx, "fork_origin", None)
    else:
        try:
            from hermes_cli.plugin_session import fork_origin as accessor
        except ImportError:
            accessor = None
    if not callable(accessor):
        if not _missing_fork_origin_logged:
            _missing_fork_origin_logged = True
            logger.debug("Hermes does not expose fork_origin(); detached-fork filtering is off")
        return ""
    try:
        return str(accessor() or "")
    except Exception:
        logger.debug("Could not read fork_origin() for the turn", exc_info=True)
        return ""


def opaque_key(install_key, kind, value):
    """``HMAC-SHA256(install_key, kind + ":" + value)``, the first 16 hex digits."""
    digest = hmac.new(str(install_key).encode("utf-8"), f"{kind}:{value}".encode(), hashlib.sha256)
    return digest.hexdigest()[:16]


def counts(session_id, platform, *, natively_notifying=(), kanban_worker=False, ctx=None):
    """Whether a turn in this session belongs on the activity."""
    session_id = str(session_id or "")
    platform = str(platform or "").strip().lower()
    if not session_id or kanban_worker:
        return False
    if session_id.startswith(UNATTENDED_PREFIXES) or bool(fork_origin(ctx)):
        return False
    return platform not in UNATTENDED_PLATFORMS and platform not in natively_notifying


class Tracker:
    """This process's interactive turns, and the thread that reports them.

    Hooks call the ``on_*`` methods from the agent's own code path; they only
    update the map and wake the worker, so a slow relay never holds a turn up.
    The worker reads the latest state when it posts, which makes the map a
    single-slot mailbox: a burst of hooks becomes one request.

    Each session remembers the relay install (url, install key) its turn
    reports to. One process can serve profiles paired with different installs,
    so each install gets a snapshot of only its own sessions, labelled with the
    profile that first reported there, and keeps its own pacing and backoff.
    """

    counts = staticmethod(counts)

    def __init__(self, *, clock=time.monotonic, wall=time.time, post=None, user_agent="hermes-mobile-app-connector"):
        self._clock = clock
        self._wall = wall
        self._post = post or self._post_http
        self._user_agent = user_agent
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._worker = None
        self._sessions = {}  # session id -> agent state, including its relay and turn id
        self._dropped = {}  # turn id -> (started_at, tool_calls) of a turn dropped as silent
        self._relays = {}  # (url, install key) -> its profile label and what was last posted there
        self.source = str(uuid.uuid4())
        self._seq = 0
        self._warned = False

    # -- hooks ---------------------------------------------------------------

    def begin(self, session_id, *, profile, relay, turn_id=""):
        """An LLM call of a counted turn starts: track the session, thinking."""
        if not session_id or not all(relay or ()):
            return
        relay, turn_id = tuple(relay), str(turn_id or "")
        with self._lock:
            install = self._install(relay)
            if not install["profile"]:
                install["profile"] = profile or "default"
            state = self._sessions.get(session_id)
            if state is not None and turn_id and state["turn"] and state["turn"] != turn_id:
                state = None  # The session's last turn ended without saying so.
            if state is None and turn_id:
                # Compression renamed this turn's session.
                renamed = next((sid for sid, other in self._sessions.items() if other["turn"] == turn_id), None)
                if renamed is not None:
                    state = self._sessions.pop(renamed)
                    self._sessions[session_id] = state
            if state is None:
                started_at, tool_calls = self._dropped.pop(turn_id, None) or (int(self._wall()), 0)
                state = {"started_at": started_at, "tool_calls": tool_calls, "attention": False,
                         "status": THINKING, "profile": profile or install["profile"], "relay": relay,
                         "turn": turn_id}
                self._sessions[session_id] = state
            else:
                state["relay"] = relay
                state["turn"] = state["turn"] or turn_id
                if not state["attention"]:
                    state["status"] = THINKING
            state["touched"] = self._clock()
        self._kick()

    def tool_started(self, session_id, turn_id=""):
        self._update(session_id, turn_id, status=TOOL, tool=True)

    def tool_finished(self, session_id, turn_id=""):
        self._update(session_id, turn_id, status=THINKING)

    def writing(self, session_id, turn_id=""):
        self._update(session_id, turn_id, status=WRITING)

    def attention(self, session_id, waiting):
        """The turn is (or no longer is) waiting on the person."""
        with self._lock:
            state = self._sessions.get(session_id)
            if state is None or state["attention"] == bool(waiting):
                return
            state["attention"] = bool(waiting)
            state["status"] = WAITING if waiting else THINKING
            state["touched"] = self._clock()
        self._kick()

    def end(self, session_id, turn_id=""):
        """The turn ended: its agent leaves, under whichever session it ran last."""
        turn_id = str(turn_id or "")
        with self._lock:
            ended = [sid for sid, state in self._sessions.items()
                     if (sid == session_id and not (turn_id and state["turn"] and state["turn"] != turn_id))
                     or (turn_id and state["turn"] == turn_id)]
            if not ended:
                return
            for sid in ended:
                del self._sessions[sid]
        self._kick()

    def unload(self):
        """The plugin unloads: say idle now, best effort, without the worker."""
        with self._lock:
            self._sessions.clear()
            snapshots = [(relay, self._snapshot(relay)) for relay, install in self._relays.items()
                         if install["essentials"] not in (None, ())]
        for relay, snapshot in snapshots:
            try:
                self._post(relay, snapshot)
            except Exception:
                logger.debug("Could not report idle on unload", exc_info=True)

    def _update(self, session_id, turn_id="", *, status, tool=False):
        with self._lock:
            state = self._sessions.get(session_id)
            if state is None or (turn_id and state["turn"] and state["turn"] != turn_id):
                return  # Not tracked, or another turn's (a detached fork's) hook.
            state["touched"] = self._clock()
            if tool:
                state["tool_calls"] += 1
            if not state["attention"]:
                state["status"] = status
        self._kick()

    def _drop_silent(self, now):
        """Drops the turns no hook has touched for too long; call under the lock."""
        for session_id, state in list(self._sessions.items()):
            limit = BUSY_SECONDS if state["attention"] or state["status"] == TOOL else IDLE_SECONDS
            if now - state["touched"] < limit:
                continue
            del self._sessions[session_id]
            if state["turn"]:
                self._dropped[state["turn"]] = (state["started_at"], state["tool_calls"])
                while len(self._dropped) > DROPPED_TURNS:
                    self._dropped.pop(next(iter(self._dropped)))

    # -- reporting -----------------------------------------------------------

    def _install(self, relay):
        """*relay*'s profile label and posting state; call under the lock."""
        install = self._relays.get(relay)
        if install is None:
            install = self._relays[relay] = {"profile": "", "essentials": None, "detail": None,
                                             "posted_at": None, "backoff_until": 0.0, "retry_at": 0.0}
        return install

    def _agents(self, relay):
        """*relay*'s opaque agents, waiting first and then oldest, at most MAX_AGENTS."""
        install_key, label = relay[1], self._relays[relay]["profile"]
        ordered = sorted(((session_id, state) for session_id, state in self._sessions.items() if state["relay"] == relay),
                         key=lambda item: (not item[1]["attention"], item[1]["started_at"], item[0]))
        agents = []
        for session_id, state in ordered[:MAX_AGENTS]:
            agent = {"session_key": opaque_key(install_key, "session", session_id),
                     "started_at": state["started_at"], "tool_calls": state["tool_calls"],
                     "attention": state["attention"], "status": state["status"]}
            if state["profile"] != label:
                agent["profile_key"] = opaque_key(install_key, "profile", state["profile"])
            agents.append(agent)
        return agents

    @staticmethod
    def _essentials(agents):
        return tuple((a["session_key"], a.get("profile_key"), a["started_at"], a["attention"]) for a in agents)

    @staticmethod
    def _detail(agents):
        return tuple((a["tool_calls"], a["status"]) for a in agents)

    def _snapshot(self, relay):
        self._seq += 1
        profile = self._relays[relay]["profile"] or "default"
        return {"v": 1, "source": self.source, "profile_key": opaque_key(relay[1], "profile", profile),
                "seq": self._seq, "ttl_seconds": TTL_SECONDS, "agents": self._agents(relay)}

    def _due(self, relay, now):
        """Seconds until *relay*'s next post: 0 now, ``None`` only on a change. Under the lock."""
        install = self._relays[relay]
        if now < install["backoff_until"]:
            return install["backoff_until"] - now
        if now < install["retry_at"]:
            return install["retry_at"] - now
        agents = self._agents(relay)
        essentials, detail = self._essentials(agents), self._detail(agents)
        if install["essentials"] is None:
            return 0.0 if agents else None
        if essentials != install["essentials"]:
            return 0.0
        if not agents:
            return None
        if detail != install["detail"]:
            return max(0.0, install["posted_at"] + DETAIL_INTERVAL - now)
        return max(0.0, install["posted_at"] + HEARTBEAT - now)

    def due(self):
        """Seconds until the next post to any install: 0 now, ``None`` only on a change."""
        now = self._clock()
        with self._lock:
            self._drop_silent(now)
            waits = [wait for wait in (self._due(relay, now) for relay in self._relays) if wait is not None]
        return min(waits) if waits else None

    def flush(self):
        """Posts to every install a post is due for. Returns the seconds to wait before looking again."""
        now = self._clock()
        with self._lock:
            self._drop_silent(now)
            relays = [relay for relay in self._relays if self._due(relay, now) == 0.0]
        for relay in relays:
            self._flush(relay)
        return self.due()

    def _flush(self, relay):
        with self._lock:
            snapshot = self._snapshot(relay)
            essentials, detail = self._essentials(snapshot["agents"]), self._detail(snapshot["agents"])
        try:
            self._post(relay, snapshot)
        except urllib.error.HTTPError as error:
            if error.code in UNKNOWN_ENDPOINT or error.code in REFUSED:
                if error.code in REFUSED:
                    logger.warning("Push relay refused an activity report: HTTP %s; trying again in an hour",
                                   error.code)
                elif not self._warned:
                    logger.info("The push relay does not take Live Activity reports yet; trying again in an hour")
                    self._warned = True
                with self._lock:
                    install = self._relays[relay]
                    install["backoff_until"] = self._clock() + BACKOFF_SECONDS
                    install["essentials"] = None
                return
            logger.warning("Push relay rejected an activity report: HTTP %s", error.code)
            return self._retry(relay)
        except Exception as error:
            logger.debug("Push relay unreachable for an activity report: %s", error)
            return self._retry(relay)
        with self._lock:
            install = self._relays[relay]
            install["essentials"], install["detail"] = essentials, detail
            install["posted_at"] = self._clock()
            install["retry_at"] = 0.0

    def _retry(self, relay):
        with self._lock:
            install = self._relays[relay]
            install["retry_at"] = self._clock() + RETRY_SECONDS
            # The relay may have taken the report even so; until one is confirmed it
            # could hold agents, and a turn that ends meanwhile still says idle.
            install["essentials"] = UNCONFIRMED

    def _kick(self):
        self._wake.set()
        if self._worker is None or not self._worker.is_alive():
            with self._lock:
                if self._worker is None or not self._worker.is_alive():
                    self._worker = threading.Thread(target=self._run, daemon=True, name="hermes-push-activity")
                    self._worker.start()

    def _run(self):
        while True:
            self._wake.clear()
            try:
                wait = self.flush()
            except Exception:
                logger.warning("Live Activity report failed", exc_info=True)
                wait = RETRY_SECONDS
            self._wake.wait(timeout=wait)

    def _post_http(self, relay, snapshot):
        relay_url, install_key = relay
        request = urllib.request.Request(
            f"{relay_url}/activity",
            data=json.dumps(snapshot).encode("utf-8"),
            headers={"Authorization": f"Bearer {install_key}", "Content-Type": "application/json",
                     "User-Agent": self._user_agent},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            response.read(512)
