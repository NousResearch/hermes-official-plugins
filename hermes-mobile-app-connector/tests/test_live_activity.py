"""The push plugin's Live Activity reports (activity.py and its hooks), without Hermes or a relay."""
import io
import logging
import os
import tempfile
import threading
import unittest
import unittest.mock
import urllib.error
from pathlib import Path

from push_plugin import load_activity, load_push_adapter, registered_hooks

activity = load_activity()

INSTALL_KEY = "0123456789abcdef0123456789abcdef"
RELAY = ("https://relay.example", INSTALL_KEY)

# Shared with the mobile app's Live Activity key tests:
# the app finds a profile and a session by computing the same keys.
HMAC_VECTORS = [
    ("profile", "default", "610b56904bbba807"),
    ("profile", "research", "56453f6f31b63960"),
    ("session", "20261010_120000_abcdef", "81cf1782621b413d"),
]

# Hermes' plugin hook names at the pinned release (hermes_cli/plugins.py
# VALID_HOOKS, v0.21.5), for the hooks this plugin uses.
HERMES_HOOKS = {
    "pre_api_request", "post_llm_call", "on_session_end", "pre_approval_request",
    "post_approval_response", "pre_tool_call", "post_tool_call", "on_stream_start",
    # tools/human_input_hooks.py, Hermes 0.22.1+ (VALID_HOOKS in hermes_cli/plugins.py).
    "on_human_input_request", "on_human_input_resolved",
    # gateway/session_prompt_hooks.py: registered only on a Hermes that has it (an older one would
    # warn at every start that it does not know them).
    "on_session_prompt", "on_session_prompt_settled",
}


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Relay:
    def __init__(self):
        self.posts = []
        self.fail = None

    def __call__(self, relay, snapshot):
        if self.fail:
            raise self.fail
        self.posts.append((relay, snapshot))


def tracker():
    clock, relay = Clock(), Relay()
    wall = iter(range(1_791_633_600, 1_791_643_600)).__next__
    t = activity.Tracker(clock=clock, wall=wall, post=relay)
    t._kick = lambda: None  # The tests drive flush() themselves.
    return t, clock, relay


class OpaqueKeyTests(unittest.TestCase):
    def test_vectors_shared_with_the_app(self):
        for kind, value, expected in HMAC_VECTORS:
            with self.subTest(kind=kind, value=value):
                self.assertEqual(activity.opaque_key(INSTALL_KEY, kind, value), expected)


class CountsTests(unittest.TestCase):
    def test_only_watched_turns_count(self):
        natively = {"telegram", "slack"}
        self.assertTrue(activity.counts("20261010_120000_abcdef", "ios", natively_notifying=natively))
        self.assertTrue(activity.counts("20261010_120000_abcdef", "", natively_notifying=natively))
        self.assertTrue(activity.counts("20261010_120000_abcdef", "desktop", natively_notifying=natively))
        for session_id, platform in [
            ("cron_job1_20261010_120000", "cron"), ("kanban_task", ""), ("subagent_x", "ios"), ("s", "subagent"),
            ("20261010_120000_abcdef", "telegram"), ("20261010_120000_abcdef", "Slack"),
            ("20261010_120000_abcdef", "webhook"),
            ("20261010_120000_abcdef", "msgraph_webhook"), ("", "ios"),
        ]:
            with self.subTest(session_id=session_id, platform=platform):
                self.assertFalse(activity.counts(session_id, platform, natively_notifying=natively))
        self.assertFalse(activity.counts("s", "ios", kanban_worker=True))


class TrackerTests(unittest.TestCase):
    def test_a_turn_becomes_one_opaque_agent(self):
        t, _clock, relay = tracker()
        t.begin("20261010_120000_abcdef", profile="research", relay=RELAY)
        t.tool_started("20261010_120000_abcdef")
        t.flush()
        (posted_relay, snapshot), = relay.posts
        self.assertEqual(posted_relay, RELAY)
        self.assertEqual(snapshot["v"], 1)
        self.assertEqual(snapshot["profile_key"], "56453f6f31b63960")
        self.assertEqual(snapshot["ttl_seconds"], 300)
        self.assertEqual(snapshot["agents"], [{
            "session_key": "81cf1782621b413d", "started_at": 1_791_633_600, "tool_calls": 1,
            "attention": False, "status": "tool"}])
        text = repr(snapshot)
        for secret in ("research", "20261010_120000_abcdef", INSTALL_KEY):
            self.assertNotIn(secret, text)

    def test_hooks_move_the_status(self):
        t, _clock, _relay = tracker()
        sid = "s1"
        t.begin(sid, profile="default", relay=RELAY)
        self.assertEqual(t._sessions[sid]["status"], "thinking")
        t.tool_started(sid)
        self.assertEqual((t._sessions[sid]["status"], t._sessions[sid]["tool_calls"]), ("tool", 1))
        t.tool_finished(sid)
        t.writing(sid)
        self.assertEqual(t._sessions[sid]["status"], "writing")
        t.attention(sid, True)
        t.tool_started(sid)
        self.assertEqual(t._sessions[sid]["status"], "waiting", "a waiting turn stays waiting")
        t.attention(sid, False)
        self.assertEqual(t._sessions[sid]["status"], "thinking")
        t.begin(sid, profile="default", relay=RELAY)
        self.assertEqual(t._sessions[sid]["started_at"], 1_791_633_600, "the turn keeps its start")
        # Hooks for a session that never began (excluded, or ended) change nothing.
        t.tool_started("other")
        t.writing("other")
        self.assertNotIn("other", t._sessions)

    def test_essentials_post_at_once_detail_waits_and_heartbeat_renews(self):
        t, clock, relay = tracker()
        t.begin("s1", profile="default", relay=RELAY)
        self.assertEqual(t.flush(), activity.HEARTBEAT)
        clock.now += 1
        t.tool_started("s1")
        self.assertAlmostEqual(t.flush(), activity.DETAIL_INTERVAL - 1)
        self.assertEqual(len(relay.posts), 1, "detail waits its interval")
        t.begin("s2", profile="default", relay=RELAY)
        t.flush()
        self.assertEqual(len(relay.posts), 2, "a new agent is posted at once")
        clock.now += activity.DETAIL_INTERVAL
        t.tool_started("s1")
        t.flush()
        self.assertEqual(len(relay.posts), 3)
        self.assertEqual(t.due(), activity.HEARTBEAT)
        clock.now += activity.HEARTBEAT
        t.flush()
        self.assertEqual(len(relay.posts), 4, "a heartbeat renews the snapshot")
        seqs = [snapshot["seq"] for _, snapshot in relay.posts]
        self.assertEqual(seqs, sorted(seqs))
        self.assertEqual(len({snapshot["source"] for _, snapshot in relay.posts}), 1)

    def test_attention_is_essential(self):
        t, _clock, relay = tracker()
        t.begin("s1", profile="default", relay=RELAY)
        t.flush()
        t.attention("s1", True)
        self.assertEqual(t.due(), 0.0)
        t.flush()
        self.assertEqual(relay.posts[-1][1]["agents"][0]["status"], "waiting")
        self.assertTrue(relay.posts[-1][1]["agents"][0]["attention"])

    def test_idle_posts_an_empty_snapshot_once(self):
        t, clock, relay = tracker()
        t.begin("s1", profile="default", relay=RELAY)
        t.flush()
        t.end("s1")
        self.assertIsNone(t.flush())
        self.assertEqual(relay.posts[-1][1]["agents"], [])
        clock.now += 3600
        self.assertIsNone(t.due(), "no heartbeat while idle")
        self.assertEqual(len(relay.posts), 2)

    def test_unload_says_idle(self):
        t, _clock, relay = tracker()
        t.begin("s1", profile="default", relay=RELAY)
        t.flush()
        t.unload()
        self.assertEqual(relay.posts[-1][1]["agents"], [])
        quiet, _, quiet_relay = tracker()
        quiet.unload()
        self.assertEqual(quiet_relay.posts, [], "nothing to retract")

    def test_an_old_relay_is_left_alone_for_an_hour(self):
        t, clock, relay = tracker()
        relay.fail = urllib.error.HTTPError("https://relay.example", 404, "Not Found", {}, io.BytesIO(b""))
        t.begin("s1", profile="default", relay=RELAY)
        with self.assertLogs(activity.logger, logging.INFO) as logs:
            self.assertEqual(t.flush(), activity.BACKOFF_SECONDS)
            t.begin("s2", profile="default", relay=RELAY)
            self.assertGreater(t.due(), 3500)
            clock.now += activity.BACKOFF_SECONDS
            t.flush()
        self.assertEqual(len(logs.output), 1, "logged once")
        relay.fail = None
        clock.now += activity.BACKOFF_SECONDS
        t.begin("s1", profile="default", relay=RELAY)  # Both still running.
        t.begin("s2", profile="default", relay=RELAY)
        t.flush()
        self.assertEqual(len(relay.posts[-1][1]["agents"]), 2)

    def test_the_install_key_rides_in_the_authorization_header(self):
        sent = []
        response = unittest.mock.MagicMock()
        response.__enter__.return_value = response
        with unittest.mock.patch.object(activity.urllib.request, "urlopen",
                                        lambda request, timeout: sent.append(request) or response):
            activity.Tracker()._post_http(RELAY, {"v": 1, "agents": []})
        self.assertEqual(sent[0].full_url, "https://relay.example/activity")
        self.assertEqual(sent[0].get_header("Authorization"), f"Bearer {INSTALL_KEY}")

    def test_a_refused_report_is_left_alone_for_an_hour(self):
        for code in [400, 401, 409, 413]:
            with self.subTest(code=code):
                t, clock, relay = tracker()
                relay.fail = urllib.error.HTTPError("https://relay.example", code, "Refused", {}, io.BytesIO(b""))
                t.begin("s1", profile="default", relay=RELAY)
                with self.assertLogs(activity.logger, logging.WARNING):
                    self.assertEqual(t.flush(), activity.BACKOFF_SECONDS)
                relay.fail = None
                clock.now += activity.BACKOFF_SECONDS
                t.begin("s1", profile="default", relay=RELAY)  # Still running.
                self.assertEqual(relay.posts, [])
                t.flush()
                self.assertEqual(len(relay.posts), 1)

    def test_a_rate_limit_or_server_error_is_retried(self):
        for code in [429, 500, 503]:
            with self.subTest(code=code):
                t, clock, relay = tracker()
                relay.fail = urllib.error.HTTPError("https://relay.example", code, "Busy", {}, io.BytesIO(b""))
                t.begin("s1", profile="default", relay=RELAY)
                with self.assertLogs(activity.logger, logging.WARNING):
                    self.assertEqual(t.flush(), activity.RETRY_SECONDS)
                relay.fail = None
                clock.now += activity.RETRY_SECONDS
                t.flush()
                self.assertEqual(len(relay.posts), 1)

    def test_a_turn_that_ends_after_an_unanswered_report_still_says_idle(self):
        for fail in [OSError("timed out"),
                     urllib.error.HTTPError("https://relay.example", 502, "Bad Gateway", {}, io.BytesIO(b""))]:
            with self.subTest(fail=fail):
                t, clock, relay = tracker()
                relay.fail = fail
                t.begin("s1", profile="default", relay=RELAY)
                with self.assertLogs(activity.logger, logging.DEBUG):
                    self.assertEqual(t.flush(), activity.RETRY_SECONDS)
                t.end("s1")
                relay.fail = None
                clock.now += activity.RETRY_SECONDS
                self.assertEqual(t.due(), 0.0, "the relay may hold the unanswered report")
                self.assertIsNone(t.flush())
                self.assertEqual([snapshot["agents"] for _, snapshot in relay.posts], [[]])
                clock.now += 3600
                self.assertIsNone(t.due(), "once confirmed, idle stays quiet")

    def test_unload_says_idle_after_an_unanswered_report(self):
        t, _clock, relay = tracker()
        relay.fail = OSError("timed out")
        t.begin("s1", profile="default", relay=RELAY)
        t.flush()
        relay.fail = None
        t.unload()
        self.assertEqual([snapshot["agents"] for _, snapshot in relay.posts], [[]])

    def test_other_profiles_name_their_own_key_and_order_is_waiting_then_oldest(self):
        t, _clock, relay = tracker()
        t.begin("old", profile="default", relay=RELAY)
        t.begin("new", profile="research", relay=RELAY)
        t.begin("asks", profile="default", relay=RELAY)
        t.attention("asks", True)
        t.flush()
        agents = relay.posts[-1][1]["agents"]
        self.assertEqual([a["attention"] for a in agents], [True, False, False])
        self.assertEqual(agents[2]["profile_key"], "56453f6f31b63960")
        self.assertNotIn("profile_key", agents[1])

    def test_snapshots_stay_within_the_relay_limit(self):
        t, _clock, relay = tracker()
        for i in range(40):
            t.begin(f"s{i}", profile="default", relay=RELAY)
        t.flush()
        self.assertEqual(len(relay.posts[-1][1]["agents"]), activity.MAX_AGENTS)

    def test_nothing_is_tracked_without_a_relay(self):
        t, _clock, relay = tracker()
        t.begin("s1", profile="default", relay=("", ""))
        self.assertIsNone(t.flush())
        self.assertEqual(relay.posts, [])

    def test_worker_thread_posts_on_a_hook(self):
        relay, posted = Relay(), threading.Event()
        def post(r, snapshot):
            relay(r, snapshot)
            posted.set()
        t = activity.Tracker(post=post)
        t.begin("s1", profile="default", relay=RELAY)
        self.assertTrue(posted.wait(5))


class LifetimeTests(unittest.TestCase):
    """A turn leaves the activity even when Hermes never says it ended."""

    def test_a_turn_compressed_into_a_new_session_ends_under_it(self):
        # Context compression rotates the session id mid-turn; the turn id stays.
        t, clock, _relay = tracker()
        t.begin("old", profile="default", relay=RELAY, turn_id="turn-1")
        t.tool_started("old", turn_id="turn-1")
        clock.now += 5
        t.begin("new", profile="default", relay=RELAY, turn_id="turn-1")
        self.assertEqual(list(t._sessions), ["new"], "one turn, one agent")
        self.assertEqual((t._sessions["new"]["started_at"], t._sessions["new"]["tool_calls"]), (1_791_633_600, 1))
        t.end("new", turn_id="turn-1")
        self.assertEqual(t._sessions, {})

    def test_a_turn_that_ends_after_compressing_without_another_call_still_ends(self):
        t, _clock, _relay = tracker()
        t.begin("old", profile="default", relay=RELAY, turn_id="turn-1")
        t.end("new", turn_id="turn-1")
        self.assertEqual(t._sessions, {})

    def test_a_new_turn_in_the_session_starts_afresh(self):
        # The last turn returned on an API error or an interrupt: no on_session_end.
        t, _clock, _relay = tracker()
        t.begin("s1", profile="default", relay=RELAY, turn_id="turn-1")
        t.tool_started("s1", turn_id="turn-1")
        t.attention("s1", True)
        t.begin("s1", profile="default", relay=RELAY, turn_id="turn-2")
        state = t._sessions["s1"]
        self.assertEqual((state["started_at"], state["tool_calls"], state["attention"], state["status"]),
                         (1_791_633_601, 0, False, "thinking"))

    def test_the_end_of_another_turn_leaves_the_running_one(self):
        t, _clock, _relay = tracker()
        t.begin("s1", profile="default", relay=RELAY, turn_id="turn-2")
        t.end("s1", turn_id="turn-1")
        self.assertIn("s1", t._sessions)
        t.end("s1")
        self.assertEqual(t._sessions, {}, "an end without a turn id ends the session's turn")

    def test_hooks_of_another_turn_in_the_session_change_nothing(self):
        t, _clock, _relay = tracker()
        t.begin("s1", profile="default", relay=RELAY, turn_id="turn-1")
        t.tool_started("s1", turn_id="review")
        t.writing("s1", turn_id="review")
        self.assertEqual((t._sessions["s1"]["status"], t._sessions["s1"]["tool_calls"]), ("thinking", 0))

    def test_a_turn_no_hook_touches_is_dropped(self):
        # A turn that returned early (API error, interrupt) is never ended.
        t, clock, relay = tracker()
        t.begin("s1", profile="default", relay=RELAY, turn_id="turn-1")
        t.flush()
        clock.now += activity.IDLE_SECONDS - 1
        t.flush()
        self.assertIn("s1", t._sessions)
        clock.now += 1
        self.assertIsNone(t.flush())
        self.assertEqual(t._sessions, {})
        self.assertEqual(relay.posts[-1][1]["agents"], [], "the activity hears it is idle")

    def test_the_heartbeat_wakes_in_time_to_drop_a_silent_turn(self):
        t, clock, _relay = tracker()
        t.begin("s1", profile="default", relay=RELAY, turn_id="turn-1")
        for _ in range(100):
            wait = t.flush()
            if wait is None:
                break
            clock.now += wait
        self.assertEqual(t._sessions, {})
        self.assertLessEqual(clock.now - 1000.0, activity.IDLE_SECONDS + activity.HEARTBEAT)

    def test_a_running_tool_or_a_wait_on_the_person_lasts_longer(self):
        t, clock, _relay = tracker()
        t.begin("tool", profile="default", relay=RELAY, turn_id="turn-1")
        t.tool_started("tool", turn_id="turn-1")
        t.begin("asks", profile="default", relay=RELAY, turn_id="turn-2")
        t.attention("asks", True)
        clock.now += activity.IDLE_SECONDS + 1
        t.flush()
        self.assertEqual(set(t._sessions), {"tool", "asks"})
        clock.now += activity.BUSY_SECONDS
        t.flush()
        self.assertEqual(t._sessions, {})

    def test_hooks_keep_a_turn_alive(self):
        t, clock, _relay = tracker()
        t.begin("s1", profile="default", relay=RELAY, turn_id="turn-1")
        for _ in range(5):
            clock.now += activity.IDLE_SECONDS - 1
            t.writing("s1", turn_id="turn-1")
            t.flush()
        self.assertIn("s1", t._sessions)

    def test_a_dropped_turn_that_resumes_keeps_its_start(self):
        t, clock, _relay = tracker()
        t.begin("s1", profile="default", relay=RELAY, turn_id="turn-1")
        t.tool_started("s1", turn_id="turn-1")
        t.tool_finished("s1", turn_id="turn-1")
        clock.now += activity.IDLE_SECONDS
        t.flush()
        self.assertEqual(t._sessions, {})
        t.begin("s1", profile="default", relay=RELAY, turn_id="turn-1")
        self.assertEqual((t._sessions["s1"]["started_at"], t._sessions["s1"]["tool_calls"]), (1_791_633_600, 1))

    def test_a_detached_fork_does_not_count(self):
        # A background memory/skill review, /btw side question or plugin fork runs a copy of the
        # agent under the parent's session id after the turn ended, and never ends.
        self.assertTrue(activity.counts("s1", "ios", ctx=unittest.mock.Mock(fork_origin=lambda: "")))
        for origin in ("background_review", "side_question", "plugin_fork:reviewer"):
            with self.subTest(origin=origin):
                ctx = unittest.mock.Mock(fork_origin=lambda origin=origin: origin)
                self.assertFalse(activity.counts("s1", "ios", ctx=ctx))

    def test_missing_fork_origin_seam_leaves_filtering_off_and_logs_once(self):
        fresh = load_activity()
        ctx_without_seam = object()
        with self.assertLogs(fresh.logger, logging.DEBUG) as logs:
            self.assertTrue(fresh.counts("s1", "ios", ctx=ctx_without_seam))
            self.assertTrue(fresh.counts("s2", "ios", ctx=ctx_without_seam))
        self.assertEqual(len(logs.records), 1)
        self.assertIn("fork_origin", logs.output[0])


class HookTests(unittest.TestCase):
    """The adapter's hooks drive the tracker as Hermes calls them."""

    def setUp(self):
        self.tracker, self.clock, self.relay = tracker()
        self.env = {"HERMES_SESSION_PROFILE": "research"}
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        adapter, _ = load_push_adapter(Path(directory.name))
        adapter.NATIVELY_NOTIFYING = {"telegram"}
        adapter._relay_settings = lambda: RELAY
        adapter._session_env = lambda name: self.env.get(name, "")
        adapter._activity = lambda: self.tracker
        adapter._prompt_watches["live"] = {}
        self.adapter = adapter

    def test_a_turn_through_its_hooks(self):
        self.adapter._activity_begin({"session_id": "s1", "platform": "ios"})
        self.adapter._on_tool_start(tool_name="terminal", args={}, session_id="s1", task_id="", tool_call_id="c1")
        self.assertEqual(self.tracker._sessions["s1"]["status"], "tool")
        self.assertEqual(self.tracker._sessions["s1"]["profile"], "research")
        self.adapter._on_tool_end(tool_name="terminal", session_id="s1", result="ok")
        self.adapter._on_stream_start(session_id="s1", surface="ios", turn_id="t")
        self.assertEqual(self.tracker._sessions["s1"]["status"], "writing")
        self.tracker.attention("s1", True)
        self.adapter._on_approval_response(session_id="s1", choice="once")
        self.assertFalse(self.tracker._sessions["s1"]["attention"])
        self.adapter._on_unload()
        self.assertEqual(self.adapter._prompt_watches, {})
        self.assertEqual(self.tracker._sessions, {})

    def test_the_hooks_follow_the_turn_through_a_compression(self):
        kw = {"platform": "ios", "turn_id": "turn-1"}
        self.adapter._prompt_watches.clear()
        self.adapter._on_api_request(session_id="old", **kw)
        self.adapter._on_tool_start(tool_name="terminal", args={}, session_id="old", turn_id="turn-1")
        self.adapter._on_api_request(session_id="new", **kw)
        self.adapter._on_tool_start(tool_name="terminal", args={}, session_id="new", turn_id="review")
        self.assertEqual(self.tracker._sessions["new"]["tool_calls"], 1, "another turn's tool is not this turn's")
        self.adapter._on_session_end(session_id="new", completed=True, failed=False, interrupted=False, **kw)
        self.assertEqual(self.tracker._sessions, {})

    def test_excluded_turns_never_reach_the_tracker(self):
        self.adapter._activity_begin({"session_id": "s1", "platform": "telegram"})
        self.adapter._activity_begin({"session_id": "cron_job_20261010_120000", "platform": "cron"})
        self.adapter._activity_begin({"session_id": "s2", "platform": "subagent"})
        os.environ["HERMES_KANBAN_TASK"] = "task-1"
        try:
            self.adapter._activity_begin({"session_id": "s3", "platform": "ios"})
        finally:
            del os.environ["HERMES_KANBAN_TASK"]
        self.assertEqual(self.tracker._sessions, {})

    def test_hooks_never_return_a_directive(self):
        # pre_tool_call results are read as block/modify directives.
        self.assertIsNone(self.adapter._on_tool_start(tool_name="x", session_id="s1"))


class ManifestTests(unittest.TestCase):
    def registered_hooks(self):
        with tempfile.TemporaryDirectory() as home:
            adapter, constants = load_push_adapter(Path(home))
            return set(registered_hooks(adapter, constants))

    def test_every_hook_exists_in_hermes(self):
        self.assertLessEqual(self.registered_hooks(), HERMES_HOOKS)

    def test_the_session_prompt_hooks_are_registered_only_where_hermes_fires_them(self):
        prompt_hooks = {"on_session_prompt", "on_session_prompt_settled"}
        for fired in (True, False):
            with self.subTest(fired=fired), tempfile.TemporaryDirectory() as home:
                adapter, constants = load_push_adapter(Path(home))
                adapter._SESSION_PROMPT_HOOKS = fired
                hooks = set(registered_hooks(adapter, constants))
                self.assertEqual(prompt_hooks <= hooks, fired)
                self.assertIn("on_human_input_request", hooks)


if __name__ == "__main__":
    unittest.main()
