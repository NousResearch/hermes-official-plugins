"""The plugin as Hermes loads it: found by Hermes' own plugin discovery in a temporary Hermes home,
its hooks fired by Hermes' own code or dispatch, with the arguments Hermes passes."""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from push_plugin import PUSH


def _session_prompt_hooks():
    """Whether this Hermes reports its gateway's prompts to plugins (``on_session_prompt``)."""
    import importlib.util
    try:
        return importlib.util.find_spec("gateway.session_prompt_hooks") is not None
    except (ImportError, ValueError):
        return False


def _hermes_plugins():
    try:
        from hermes_cli import plugins
        return plugins
    except Exception:
        return None


def _has_plugin_session_seam(name):
    try:
        from hermes_cli import plugin_session
    except Exception:
        return False
    return callable(getattr(plugin_session, name, None))


def _has_cron_delivery_seam():
    try:
        from cron import plugin_api
    except Exception:
        return False
    return callable(getattr(plugin_api, "delivery_platforms", None))


@unittest.skipUnless(_hermes_plugins(), "needs a Hermes checkout on PYTHONPATH (run on the test server)")
class HermesTurnHookTests(unittest.TestCase):
    def setUp(self):
        plugins = _hermes_plugins()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        home = Path(directory.name).resolve()
        shutil.copytree(PUSH, home / "plugins" / PUSH.name, ignore=shutil.ignore_patterns("tests", "__pycache__"))
        (home / "config.yaml").write_text(f"plugins:\n  enabled: [{PUSH.name}]\n")
        environment = patch.dict("os.environ", {
            "HERMES_HOME": str(home), "HERMES_MOBILE_PUSH_RELAY_URL": "https://relay.example",
            "HERMES_MOBILE_PUSH_INSTALL_KEY": "k" * 43})
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop("HERMES_PLATFORM", None)  # The session's platform names the surface.
        plugins._reset_plugin_managers_for_tests()
        self.addCleanup(plugins._reset_plugin_managers_for_tests)
        plugins.discover_plugins(force=True)
        self.invoke_hook = plugins.invoke_hook
        self.adapter = next(module for name, module in list(sys.modules.items()) if name.endswith(".adapter")
                            and Path(getattr(module, "__file__", None) or "/").resolve().is_relative_to(home))
        self.sent = []
        posting = patch.object(self.adapter, "_post_notify",
                               lambda relay_url, install_key, payload: self.sent.append(payload) or (200, "{}"))
        posting.start()
        self.addCleanup(posting.stop)

    def finish(self, session_id, platform, reply):
        """What agent/turn_finalizer.py fires at the end of a completed turn."""
        self.invoke_hook("post_llm_call", session_id=session_id, task_id="task", turn_id="turn-1",
                         user_message="Hello", assistant_response=reply, conversation_history=[],
                         model="test/model", platform=platform)
        self.invoke_hook("on_session_end", session_id=session_id, task_id="task", turn_id="turn-1",
                         completed=True, failed=False, interrupted=False,
                         turn_exit_reason="text_response(stop)", model="test/model", platform=platform)
        self.adapter._delivery_queue.join()

    def test_the_live_activity_lets_go_of_compressed_turns(self):
        tracker = self.adapter._activity()
        tracker._kick = lambda: None
        self.invoke_hook("pre_api_request", session_id="20261019_090000_abcdef", turn_id="turn-1", platform="desktop")
        # Compression rotates the session id mid-turn; the turn ends under the new one.
        self.finish("20261019_091500_fedcba", "desktop", "Done.")
        self.assertEqual(tracker._sessions, {})

    @unittest.skipUnless(_has_plugin_session_seam("fork_origin"), "needs hermes-agent PR 159657")
    def test_the_live_activity_skips_detached_review_forks(self):
        from types import SimpleNamespace

        from agent.turn_facade import bind_detached_fork_origin
        tracker = self.adapter._activity()
        tracker._kick = lambda: None
        # The background review fork runs under the parent's session id and never ends.
        with bind_detached_fork_origin(SimpleNamespace(_turn_origin="background_review")):
            self.invoke_hook("pre_api_request", session_id="20261019_091500_fedcba", turn_id="review",
                             platform="desktop")
        self.assertEqual(tracker._sessions, {})

    def test_an_app_turn_notifies_its_reply_once(self):
        self.finish("20261019_090000_abcdef", "desktop", "The draft is ready.")
        self.assertEqual([(p["kind"], p["body"], p["thread_id"]) for p in self.sent],
                         [("reply", "The draft is ready.", "20261019_090000_abcdef")])

    def test_silent_cron_and_webhook_turns_notify_nobody(self):
        self.finish("cron_job1_20261019_090000", "cron", "[SILENT]")
        self.finish("20261019_090500_webhook", "webhook", "[SILENT] Nothing new.")
        self.assertEqual(self.sent, [])

    def test_an_app_turn_the_agent_leaves_silent_notifies_nobody(self):
        self.finish("20261019_090000_abcdef", "desktop", "[SILENT]")
        self.finish("20261019_090000_abcdef", "tui", "NO_REPLY")
        self.finish("20261019_090000_abcdef", "desktop", "The build passed.\n\n[SILENT]")
        self.assertEqual([p["body"] for p in self.sent], ["The build passed."])

    def test_a_local_cron_run_notifies_with_its_job_name(self):
        from cron.execution_identity import enter_cron_execution, exit_cron_execution
        from cron.jobs import create_job
        job = create_job("Write the brief", "every 1h", name="Morning brief", deliver="local")
        run = enter_cron_execution(job, "execution-1", {"source": "builtin"})
        try:
            self.finish("local-local", "cron", "The brief is ready.")
        finally:
            exit_cron_execution(run)
        self.assertEqual([(p["kind"], p["subtitle"], p["body"]) for p in self.sent],
                         [("delivery", "Morning brief", "The brief is ready.")])

    @unittest.skipUnless(_has_cron_delivery_seam(), "needs hermes-agent PR 159657")
    def test_a_cron_run_notifies_unless_it_delivers_to_push(self):
        from cron.execution_identity import enter_cron_execution, exit_cron_execution
        from cron.jobs import create_job
        for deliver, name in [("push", "Delivered brief"), ("local", "Morning brief")]:
            job = create_job("Write the brief", "every 1h", name=name, deliver=deliver)
            run = enter_cron_execution(job, "execution-1", {"source": "builtin"})
            try:
                self.finish("local-" + deliver, "cron", "The brief is ready.")
            finally:
                exit_cron_execution(run)
        self.assertEqual([(p["kind"], p["subtitle"], p["body"]) for p in self.sent],
                         [("delivery", "Morning brief", "The brief is ready.")])

    def cron_notifications(self, deliver, **job_fields):
        """The hook's notifications for one completed run of a job that delivers to *deliver*."""
        from cron.execution_identity import enter_cron_execution, exit_cron_execution
        from cron.jobs import create_job, update_job
        job = create_job("Write the brief", "every 1h", name="Brief", deliver=deliver)
        if job_fields:
            job = update_job(job["id"], job_fields)
        self.sent.clear()
        run = enter_cron_execution(job, "execution-1", {"source": "builtin"})
        try:
            self.finish("local-run", "cron", "The brief is ready.")
        finally:
            exit_cron_execution(run)
        return [p["kind"] for p in self.sent]

    @unittest.skipUnless(_has_cron_delivery_seam(), "needs hermes-agent PR 159657")
    def test_a_cron_run_delivered_to_a_bot_chat_notifies_through_that_chat_only(self):
        # The Bot Chat delivery runs its own turn there, and that turn notifies.
        # Cron resolves bot-chat:<profile> only for an existing local profile.
        work = Path(os.environ["HERMES_HOME"]) / "profiles" / "work"
        work.mkdir(parents=True)
        (work / "config.yaml").write_text("{}\n")
        for deliver in ("bot-chat", "Bot-Chat:work", "local,bot-chat"):
            self.assertEqual(self.cron_notifications(deliver), [], deliver)
        # Missing bot-chat profile is skipped by cron's resolver, so the hook still notifies.
        self.assertEqual(self.cron_notifications("Bot-Chat:missing"), ["delivery"])
        self.assertEqual(self.cron_notifications("local"), ["delivery"])

    @unittest.skipUnless(_has_cron_delivery_seam(), "needs hermes-agent PR 159657")
    def test_a_cron_run_that_cron_delivers_to_push_by_a_routing_token_notifies_once(self):
        from cron.scheduler_delivery import _resolve_delivery_targets
        # "all" reaches every platform with a home channel, and push always has one; "origin"
        # without an origin falls back to the first home channel, which here is push.
        for deliver in ("all", "origin", "local,all"):
            with self.subTest(deliver=deliver):
                from cron.jobs import create_job
                self.assertIn("push", [t["platform"] for t in _resolve_delivery_targets(create_job("x", "every 1h", deliver=deliver))])
                self.assertEqual(self.cron_notifications(deliver), [])

    def test_a_cron_run_whose_origin_falls_back_to_another_home_channel_notifies(self):
        with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "1:x", "TELEGRAM_HOME_CHANNEL": "42"}):
            from cron.jobs import create_job
            from cron.scheduler_delivery import _resolve_single_delivery_target
            target = _resolve_single_delivery_target(create_job("x", "every 1h", deliver="origin"), "origin")
            if (target or {}).get("platform") != "telegram":
                self.skipTest("Telegram is not this Hermes' first home channel")
            self.assertEqual(self.cron_notifications("origin"), ["delivery"])
        self.assertEqual(self.cron_notifications("origin", origin={"platform": "telegram", "chat_id": "42"}),
                         ["delivery"])

    def test_a_webhook_run_that_delivers_to_push_notifies_through_its_delivery_only(self):
        """A route with ``deliver: push`` sends the run's reply as a delivery; the reply itself must not
        notify too. Routes come from config.yaml and from webhook_subscriptions.json."""
        import json

        from gateway.session_context import clear_session_vars, set_session_vars
        from hermes_constants import get_hermes_home
        home = Path(get_hermes_home())
        (home / "config.yaml").write_text(
            f"plugins:\n  enabled: [{PUSH.name}]\n"
            "platforms:\n  webhook:\n    extra:\n      routes:\n"
            "        builds: {secret: s, deliver: push}\n"
            "        logged: {secret: s, deliver: log}\n"
            "        chat: {secret: s, deliver: telegram, deliver_extra: {chat_id: '42'}}\n")
        subscriptions = home / "webhook_subscriptions.json"
        subscriptions.write_text(json.dumps({"alerts": {"secret": "s", "deliver": "push"},
                                             "builds": {"secret": "s", "deliver": "log"}}))

        def notifications(route):
            self.sent.clear()
            tokens = set_session_vars(platform="webhook", chat_id=f"webhook:{route}:1", chat_type="webhook",
                                      user_id=f"webhook:{route}", user_name=route,
                                      session_id="20261019_090000_webhook")
            try:
                self.finish("20261019_090000_webhook", "webhook", "The build passed.")
            finally:
                clear_session_vars(tokens)
            return [p["kind"] for p in self.sent]

        for route, expected in [("builds", []), ("alerts", []), ("logged", ["reply"]),
                                ("chat", ["reply"]), ("unknown", ["reply"])]:
            with self.subTest(route=route):
                self.assertEqual(notifications(route), expected)
        subscriptions.write_text("{not json")
        self.assertEqual(notifications("alerts"), ["reply"])  # Unreadable: notify rather than not.

    @unittest.skipUnless(_has_plugin_session_seam("in_delegated_child"), "needs hermes-agent PR 159657")
    def test_a_finished_subagent_leaves_its_parents_session_watched(self):
        """A delegated child shares its parent's app session (it copies the parent's context);
        its end must not stop the prompt watch, or the parent's prompt loses its routing."""
        from agent.delegation_context import delegated_child_context
        from gateway.session_context import clear_session_vars, set_session_vars
        if not self.adapter._SESSION_PROMPT_HOOKS:
            self.skipTest("needs gateway.session_prompt_hooks")
        tokens = set_session_vars(platform="local", session_id="20261019_090000_parent",
                                  session_key="agent:main:local:dm:live-1")
        try:
            self.invoke_hook("pre_api_request", session_id="20261019_090000_parent", platform="desktop")
            self.assertIn("live-1", self.adapter._prompt_watches)
            with delegated_child_context():
                # Even when platform is "desktop", in_delegated_child() identifies the child.
                self.invoke_hook("pre_api_request", session_id="20261019_090100_child", platform="desktop")
                self.finish("20261019_090100_child", "desktop", "Found it.")
                origin = self.adapter._origin_metadata({}, use_context=True)
                self.assertEqual(origin.get("source"), "subagent")
                self.assertIs(origin.get("is_subagent"), True)
            self.assertIn("live-1", self.adapter._prompt_watches)
            self.finish("20261019_090000_parent", "desktop", "Done.")
            self.assertNotIn("live-1", self.adapter._prompt_watches)
        finally:
            clear_session_vars(tokens)

    @unittest.skipUnless(_has_plugin_session_seam("session_source"), "needs hermes-agent PR 159657")
    def test_a_local_session_creation_source_is_read_through_the_seam(self):
        from types import SimpleNamespace

        from gateway import session_policy
        token = session_policy._ACTIVE_POLICY.set(SimpleNamespace(source="gui", platform="desktop"))
        try:
            self.finish("20261019_090000_local", "desktop", "Hello from app.")
        finally:
            session_policy._ACTIVE_POLICY.reset(token)
        self.assertEqual(self.sent[-1]["hermes"]["source"], "gui")

    def test_an_approval_a_person_is_asked_notifies_once_with_the_command_redacted(self):
        from gateway.session_context import clear_session_vars, set_session_vars
        from tools.human_input_hooks import human_input_request
        token = "sk-abcdefghijklmnopqrstuvwxyz123456"
        # Smart mode's auxiliary LLM decides first: nobody is asked yet.
        self.invoke_hook("pre_approval_request", command="ls", description="List", pattern_key="ls",
                         pattern_keys=["ls"], session_key="agent:main:telegram:dm:1", surface="smart")
        tokens = set_session_vars(platform="telegram", session_id="20261019_090000_abcdef",
                                  session_key="agent:main:telegram:dm:1")
        try:
            with human_input_request("approval", prompt=f"curl -H 'Authorization: Bearer {token}' https://api") as human:
                human.outcome = "once"
        finally:
            clear_session_vars(tokens)
        self.adapter._delivery_queue.join()
        self.assertEqual([(p["kind"], p["subtitle"], p["thread_id"]) for p in self.sent],
                         [("approval", "Waiting on you · Telegram", "20261019_090000_abcdef")])
        self.assertIn("curl", self.sent[0]["body"])
        self.assertNotIn(token, self.sent[0]["body"])


class _Viewer:
    """A gateway viewer's transport: the frames the session owner writes it."""

    def __init__(self):
        self.frames = []

    def write(self, frame):
        self.frames.append(frame)
        return True

    def close(self):
        pass


@unittest.skipUnless(_hermes_plugins() and _session_prompt_hooks(),
                     "needs a Hermes with gateway.session_prompt_hooks on PYTHONPATH")
class HermesSessionPromptTests(unittest.TestCase):
    """A gateway app session's waits on a person, opened by Hermes' own session owner code."""

    setUp = HermesTurnHookTests.setUp

    def test_gateway_prompts_notify_once_with_the_id_that_answers_them(self):
        import threading
        import time
        from types import SimpleNamespace

        from gateway import session_server_requests
        from gateway.session_authority import LiveSession, SessionAuthority
        from gateway.session_context import clear_session_vars, set_session_vars
        from persistence.conversation.sessions import NewSession
        from persistence_assembly import open_persistence, profile_scope
        from tools import approval, clarify_gateway
        from tools.approval_gateway_wait import _ApprovalEntry
        from tools.human_input_hooks import human_input_request
        live, route = "20261102_100000_live01", "agent:main:local:dm:20261102_100000_live01"
        tracker = self.adapter._activity()
        tracker._kick = lambda: None
        tokens = set_session_vars(platform="local", session_id=live, session_key=route, profile="research")
        try:
            self.invoke_hook("pre_api_request", session_id=live, turn_id="turn-1", platform="desktop")
            # Hermes still reports the approval at tool level, before the owner opens it: no push from there.
            with human_input_request("approval", prompt="rm -rf build") as human:
                human.outcome = "once"
        finally:
            clear_session_vars(tokens)
        self.adapter._delivery_queue.join()
        self.assertEqual(self.sent, [])

        # The session's owner, as the gateway runs it: a claimed turn of a live session.
        persistence = open_persistence(profile_scope())
        self.addCleanup(persistence.close)
        persistence.sessions.create(NewSession(session_id=live, source="local"))
        epoch = persistence.runtime.begin_epoch(instance_id="current")
        authority = SessionAuthority(SimpleNamespace(_draining=False), profile_id="research",
                                     instance_id="current", epoch=epoch, persistence=persistence)
        authority.sessions[live] = session = LiveSession(None, route)
        persistence.admissions.admit(epoch=epoch, principal_id="research:phone", session_id=live,
                                     request_id="turn-1", payload={"text": "go"})
        generation = persistence.admissions.claim(epoch=epoch, session_id=live).generation
        self.addCleanup(approval._gateway_queues.pop, route, None)
        self.addCleanup(clarify_gateway.clear_session, route)
        viewer = _Viewer()
        session.event_stream.fanout.attach(viewer)

        pending = _ApprovalEntry({"command": "rm -rf build", "description": "Delete the build"})
        approval._gateway_queues.setdefault(route, []).append(pending)
        authority.register_approval(live, generation, route, pending.data)
        authority.register_clarify(live, generation, clarify_gateway.register(
            "clar-1", route, "Which format?", ["PDF", "Markdown"]))
        asked = threading.Thread(target=session_server_requests.ask_viewer, args=(
            session.event_stream, live, "secret", {"env_var": "API_KEY", "prompt": "Enter the API key"}),
            kwargs={"timeout": 5})
        asked.start()
        self.addCleanup(asked.join, 5)
        # Hermes delivers both hooks off the prompt's path, in order, on its own worker thread.
        deadline = time.monotonic() + 10
        while len(self.sent) < 3:
            self.assertLess(time.monotonic(), deadline, f"pushes so far: {self.sent}")
            time.sleep(0.01)
        secret = next(frame for frame in list(viewer.frames) if frame.get("method") == "secret")
        self.assertTrue(session_server_requests.resolve({"jsonrpc": "2.0", "id": secret["id"], "result": {"value": ""}}))
        asked.join(5)
        self.adapter._delivery_queue.join()

        self.assertEqual([(p["kind"], p["hermes"]["request_id"]) for p in self.sent],
                         [("approval", pending.data["request_id"]), ("clarify", "clar-1"), ("secret", secret["id"])])
        # The ids answer them on the owner: the approval and the question are open under them.
        self.assertEqual({p["prompt_id"] for p in session.controls.snapshot(live, generation)},
                         {pending.data["request_id"], "clar-1"})
        self.assertEqual(self.sent[0]["body"], "Reason: Delete the build\nCommand: rm -rf build")
        self.assertEqual((self.sent[1]["body"], self.sent[1]["hermes"]["answer_mode"]),
                         ("Which format?\nOptions: PDF, Markdown", "text"))
        self.assertEqual(self.sent[2]["body"], "Enter the API key")
        for push in self.sent:
            self.assertEqual((push["hermes"]["session_id"], push["hermes"]["profile"], push["thread_id"]),
                             (live, "research", live))
        # The secret settled; the approval and the question still hold the Live Activity.
        self.assertTrue(tracker._sessions[live]["attention"])

    def test_a_gateway_wait_notifies_once_whatever_the_process_platform(self):
        from gateway.session_context import clear_session_vars, set_session_vars
        from tools.human_input_hooks import human_input_request
        live, route = "20261102_100000_live02", "agent:main:local:dm:20261102_100000_live02"
        # Hermes names on_human_input_request's platform from HERMES_PLATFORM before the session's platform.
        for process in ["desktop", "cli"]:
            with self.subTest(HERMES_PLATFORM=process), patch.dict(os.environ, {"HERMES_PLATFORM": process}):
                self.sent.clear()
                tokens = set_session_vars(platform="local", session_id=live, session_key=route, profile="research")
                try:
                    self.invoke_hook("pre_api_request", session_id=live, turn_id="turn-1", platform="desktop")
                    with human_input_request("approval", prompt="rm -rf build") as human:
                        human.outcome = "once"
                finally:
                    clear_session_vars(tokens)
                self.invoke_hook("on_session_prompt", session_id=live, kind="approval", prompt_id=f"appr-{process}",
                                 execution_generation=1, params={"command": "rm -rf build", "description": "",
                                                                 "choices": ["once", "deny"]})
                self.adapter._delivery_queue.join()
                self.assertEqual([(p["kind"], p["hermes"].get("request_id")) for p in self.sent],
                                 [("approval", f"appr-{process}")])


if __name__ == "__main__":
    unittest.main()
