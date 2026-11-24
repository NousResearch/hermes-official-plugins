"""Run the shipped notification formatting hooks without Hermes or a live relay."""
import base64
import hashlib
import json
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from push_plugin import (
    PUSH,
    finish_turn,
    load_module,
    load_privacy,
    load_push_adapter,
    registered_hooks,
)

ORIGIN = {"profile": "research", "source": "ios", "session_id": "20261002_101500_abcdef"}
RELAY = ("https://relay.example", "install-key")


class AdapterTestCase(unittest.TestCase):
    """Imports a fresh copy of the whole adapter for each test, homed in a temporary directory."""

    def load_adapter(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name)
        self.adapter, self.constants = load_push_adapter(self.home)
        return self.adapter


class ServerRequestNotificationTests(AdapterTestCase):
    """Every server→client request a person answers becomes one push."""

    def setUp(self):
        self.sent = []
        self.reported = []
        self.open = {}
        self.env = {
            "HERMES_UI_SESSION_ID": "live-1", "HERMES_SESSION_KEY": "20261002_101500_abcdef",
            "HERMES_SESSION_PROFILE": "research", "HERMES_SESSION_PLATFORM": "ios",
        }
        context = types.ModuleType("gateway.session_context")
        context.get_session_env = lambda name, default="": self.env.get(name, default)
        registry = types.ModuleType("tui_gateway.server_requests")
        registry.open_requests = lambda sid: list(self.open.get(sid, []))
        modules = patch.dict(sys.modules, {
            "gateway": types.ModuleType("gateway"), "gateway.session_context": context,
            "tui_gateway": types.ModuleType("tui_gateway"), "tui_gateway.server_requests": registry,
        })
        modules.start()
        self.addCleanup(modules.stop)
        self.registry = registry
        adapter = self.load_adapter()
        adapter._request_worker = threading.current_thread()  # Alive: the tests poll themselves.
        adapter._relay_settings = lambda: RELAY
        adapter._deliver_in_background = lambda payload, relay=None: self.sent.append((payload, relay))
        adapter._report_activity = lambda *args: self.reported.append(args)
        adapter._origin_metadata = lambda fields: {
            key: fields[key] for key in ("profile", "source", "session_id") if fields.get(key)}

    def payload(self, method, params, request_id="srq-0123456789ab"):
        return self.adapter._server_request_payload(method, request_id, params, dict(ORIGIN))

    def poll(self):
        before = len(self.sent)
        self.adapter._notify_open_requests(self.registry.open_requests)
        return [payload for payload, _ in self.sent[before:]]

    def test_long_description_cannot_displace_command(self):
        result = self.payload("approval", {"request_id": "r", "description": "Why " * 400, "command": "echo " * 300})
        self.assertIn("…\nCommand: echo", result["body"])
        self.assertTrue(result["body"].endswith("…"))
        self.assertLessEqual(len(result["body"]), 600)

    def test_identical_description_and_command_are_not_duplicated(self):
        result = self.payload("approval", {"request_id": "r", "description": "swift test", "command": "swift test"})
        self.assertEqual(result["body"], "swift test")

    def test_unaddressable_questions_still_open_the_chat(self):
        for params in [
            {"questions": [{"question": "Missing question identity"}]},
            {"questions": "invalid"},
            {"questions": [{"qid": "q1", "question": "Done?"}], "answers": {"q1": "yes"}},
        ]:
            with self.subTest(params=params):
                result = self.payload("clarify", params)
                self.assertNotIn("answer_mode", result["hermes"])
                self.assertEqual(result["hermes"]["session_id"], "20261002_101500_abcdef")

    def test_secret_shows_its_prompt_but_never_gets_a_text_answer(self):
        result = self.payload("secret", {"env_var": "OPENAI_API_KEY", "prompt": "Enter your OpenAI API key"})
        self.assertEqual((result["kind"], result["title"]), ("secret", "Secret needed"))
        self.assertEqual(result["body"], "Enter your OpenAI API key")
        self.assertNotIn("answer_mode", result["hermes"])
        self.assertEqual(self.payload("secret", {"env_var": "TOKEN", "prompt": ""})["body"], "TOKEN")

    def test_vault_prompts_ask_for_input(self):
        cases = {
            "vault.unlock_prompt": ({"backend": "bitwarden", "display_name": "Bitwarden"}, "Unlock Bitwarden to continue."),
            "vault.code": ({"site": "github.com"}, "Hermes needs a one-time code for github.com."),
            "vault.save_login": ({"origin": "https://example.com", "site": "example.com"}, "Save a login for example.com?"),
        }
        for method, (params, body) in cases.items():
            with self.subTest(method=method):
                result = self.payload(method, params)
                self.assertEqual((result["kind"], result["title"], result["body"]), ("input", "Input needed", body))

    def test_window_bridges_are_not_for_people(self):
        for method in ["terminal.read", "preview.read", "preview.act", "window.read", "tour"]:
            self.assertIsNone(self.payload(method, {}))

    def test_a_watched_turn_notifies_each_request_once_with_its_turns_relay(self):
        self.adapter._watch_requests({"session_id": "ignored", "platform": "ignored"})
        self.assertTrue(self.adapter._requests_watched("live-1"))
        self.open["live-1"] = [
            {"id": "srq-1", "method": "clarify", "params": {"session_id": "live-1", "questions": [
                {"qid": "q1", "question": "Which format?"}]}},
            {"id": "srq-2", "method": "window.read", "params": {"session_id": "live-1"}},
        ]
        pushes = self.poll()
        self.assertEqual([(p["kind"], p["hermes"]["request_id"]) for p in pushes], [("clarify", "srq-1")])
        self.assertEqual(pushes[0]["hermes"]["session_id"], "20261002_101500_abcdef")
        self.assertEqual(self.sent[0][1], RELAY)
        # Still open on the next pass: not announced twice.
        self.assertEqual(self.poll(), [])
        self.open["live-1"].append({"id": "srq-3", "method": "sudo", "params": {"session_id": "live-1"}})
        self.assertEqual([p["kind"] for p in self.poll()], ["sudo"])

    def test_an_open_question_holds_the_live_activity_until_it_closes(self):
        self.adapter._watch_requests({"session_id": "20261002_101500_abcdef"})
        self.poll()
        self.open["live-1"] = [{"id": "srq-1", "method": "window.read", "params": {}}]
        self.poll()
        self.open["live-1"].append({"id": "srq-2", "method": "approval", "params": {"request_id": "a1"}})
        self.poll()
        self.open["live-1"] = []
        self.poll()
        self.assertEqual(self.reported, [
            ("attention", "20261002_101500_abcdef", False), ("attention", "20261002_101500_abcdef", False),
            ("attention", "20261002_101500_abcdef", True), ("attention", "20261002_101500_abcdef", False)])

    def test_an_approval_hook_marks_the_turn_waiting_unless_smart_decides(self):
        self.adapter._HUMAN_INPUT_HOOKS = False  # Hermes before 0.22.1
        hook = self.adapter._on_approval_request
        self.adapter._origin_metadata = lambda fields: {}
        hook(command="ls", description="List", session_key="x", surface="smart", session_id="s1")
        hook(command="ls", description="List", session_key="x", surface="gateway", session_id="s1")
        self.assertEqual(self.reported, [("attention", "s1", True)])

    def test_turn_end_and_staleness_stop_the_watch(self):
        self.adapter._watch_requests({})
        self.adapter._unwatch_requests("20261002_101500_abcdef")
        self.assertFalse(self.adapter._requests_watched("live-1"))
        self.env["HERMES_UI_SESSION_ID"] = ""
        self.adapter._watch_requests({})
        self.assertEqual(self.adapter._request_watches, {})
        self.env["HERMES_UI_SESSION_ID"] = "live-2"
        self.adapter._watch_requests({})
        self.adapter._request_watches["live-2"]["touched"] -= self.adapter.REQUEST_WATCH_TTL_SECONDS + 1
        self.open["live-2"] = [{"id": "srq-9", "method": "sudo", "params": {}}]
        self.assertEqual(self.poll(), [])
        self.assertEqual(self.adapter._request_watches, {})

    def test_messaging_and_old_hermes_sessions_are_not_watched(self):
        self.adapter._relay_settings = lambda: ("", "")
        self.adapter._watch_requests({})
        self.adapter._relay_settings = lambda: RELAY
        with patch.dict(sys.modules, {"tui_gateway.server_requests": None}):
            self.assertIsNone(self.adapter._open_requests_reader())
            self.adapter._watch_requests({})
        self.assertEqual(self.adapter._request_watches, {})

    def test_a_broken_reader_or_request_never_raises(self):
        self.adapter._watch_requests({})
        self.adapter._notify_open_requests(lambda sid: 1 / 0)
        self.open["live-1"] = ["junk", {"id": "srq-x", "method": "approval", "params": None}]
        self.assertEqual([p["kind"] for p in self.poll()], ["approval"])

    def test_app_session_approval_hook_defers_to_its_request(self):
        self.adapter._HUMAN_INPUT_HOOKS = False  # Hermes before 0.22.1
        hook = self.adapter._on_approval_request
        self.adapter._origin_metadata = lambda fields: {"profile": "research"}
        self.adapter._watch_requests({})
        hook(command="rm -rf build", description="Delete", session_key="20261002_101500_abcdef", surface="gateway")
        self.assertEqual(self.sent, [])
        # A messaging platform's approval is asked in that platform, not by request.
        self.env["HERMES_UI_SESSION_ID"] = ""
        hook(command="rm -rf build", description="Delete", session_key="agent:main:telegram:dm:1", surface="gateway")
        self.assertEqual(self.sent[-1][0]["hermes"], {
            "profile": "research", "kind": "approval", "session_key": "agent:main:telegram:dm:1"})
        hook(command="ls", description="List", session_key="x", surface="smart")
        self.assertEqual(len(self.sent), 1)

    def privacy(self):
        privacy = load_privacy()
        privacy.load_key = lambda: bytes(range(32))
        return privacy

    def open_sealed(self, protected, install_key):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        box = base64.b64decode(protected["hermes"]["sealed"]["data"])
        aad = ("hermes-preview-v1:" + hashlib.sha256(install_key.encode()).hexdigest()).encode()
        return AESGCM(bytes(range(32))).decrypt(box[:12], box[12:], aad)

    def test_every_kind_seals_its_routing_without_public_leakage(self):
        install_key = "ab" * 32
        cases = {
            "approval": {"request_id": "queue-private", "command": "rm -rf private-dir"},
            "clarify": {"questions": [{"qid": "q-private", "question": "Which private format?"}]},
            "sudo": {"command": "sudo private-tool"},
            "secret": {"env_var": "PRIVATE_KEY", "prompt": "Private key please"},
        }
        for method, params in cases.items():
            with self.subTest(method=method):
                protected = self.privacy().protect(self.payload(method, params), install_key)
                public = json.dumps(protected)
                for private in ["rivate", "research", "srq-0123456789ab", "queue-", "answer_mode"]:
                    self.assertNotIn(private, public)
                self.assertEqual(protected["kind"], method)
                self.assertEqual(protected["hermes"]["session_id"], "20261002_101500_abcdef")
                self.assertEqual(protected["hermes"]["source"], "ios")
                clear = json.loads(self.open_sealed(protected, install_key))
                self.assertEqual(clear["hermes"]["kind"], method)
                self.assertEqual(clear["hermes"]["profile"], "research")
                self.assertEqual(clear["thread_id"], "20261002_101500_abcdef")
                expected_request = "queue-private" if method == "approval" else "srq-0123456789ab"
                self.assertEqual(clear["hermes"]["request_id"], expected_request)
                if method == "clarify":
                    self.assertEqual(clear["hermes"]["question_id"], "q-private")
                    self.assertEqual(clear["hermes"]["answer_mode"], "text")

    def test_unicode_preview_stays_in_budget_and_marks_truncation(self):
        install_key = "ab" * 32
        payload = self.payload("approval", {"description": "理由" * 200, "command": "🙂" * 600, "request_id": "r"})
        clear = self.open_sealed(self.privacy().protect(payload, install_key), install_key)
        self.assertLessEqual(len(clear), 2000)
        self.assertTrue(json.loads(clear)["body"].endswith("…"))


class PreviewKeyLockTests(unittest.TestCase):
    """The preview key is created under a cross-process lock on every platform."""

    def load_privacy(self, modules, home):
        privacy = load_privacy(modules)
        privacy._home = lambda: home
        return privacy

    def test_windows_uses_msvcrt_and_releases_the_lock(self):
        calls = []
        msvcrt = types.ModuleType("msvcrt")
        msvcrt.LK_LOCK, msvcrt.LK_UNLCK = 1, 0
        msvcrt.locking = lambda fd, mode, size: calls.append((mode, size))
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            privacy = self.load_privacy({"fcntl": None, "msvcrt": msvcrt}, home)
            self.assertIsNone(privacy.fcntl)
            key = privacy.load_key(create=True)
            self.assertEqual(len(key), 32)
            self.assertEqual(calls, [(1, 1), (0, 1)])
            self.assertEqual(privacy.load_key(create=True), key)

    def test_posix_keeps_flock(self):
        with tempfile.TemporaryDirectory() as root:
            privacy = self.load_privacy({}, Path(root))
            self.assertIsNotNone(privacy.fcntl)
            self.assertEqual(len(privacy.load_key(create=True)), 32)


class SurfaceLabelTests(AdapterTestCase):
    """The line under a reply's title names where the turn ran, unless it ran in the app."""

    def setUp(self):
        self.label = self.load_adapter()._surface_label

    def test_a_reply_in_the_app_carries_no_surface_line(self):
        for platform in [None, "", "ios", "IOS", "android", "Android", "gui", "web", "desktop", "cli", "tui", "local"]:
            with self.subTest(platform=platform):
                self.assertEqual(self.label(platform), "")

    def test_other_surfaces_are_named_as_they_spell_themselves(self):
        self.assertEqual(self.label("telegram"), "Telegram")
        self.assertEqual(self.label("whatsapp"), "WhatsApp")
        self.assertEqual(self.label("bluebubbles"), "iMessage")
        self.assertEqual(self.label("home_assistant"), "Home Assistant")


class TurnReplyTests(AdapterTestCase):
    """A finished turn notifies its final reply once, from post_llm_call, when on_session_end says it completed."""

    def setUp(self):
        adapter = self.load_adapter()
        modules = patch.dict(sys.modules, {"hermes_constants": self.constants})
        modules.start()
        self.addCleanup(modules.stop)
        self.sent = []
        adapter._deliver_in_background = self.sent.append
        adapter._report_activity = lambda *args: None
        adapter.NATIVELY_NOTIFYING = {"telegram", "discord"}

    def test_a_completed_turn_notifies_its_final_reply(self):
        finish_turn(self.adapter, "20261002_101500_abcdef", "  The draft is ready.  ", "desktop")
        self.assertEqual(len(self.sent), 1)
        payload = self.sent[0]
        self.assertEqual((payload["kind"], payload["body"], payload["thread_id"]),
                         ("reply", "The draft is ready.", "20261002_101500_abcdef"))
        self.assertEqual((payload["hermes"]["kind"], payload["hermes"]["turn_id"]), ("agent_end", "turn-1"))
        self.assertNotIn("subtitle", payload)
        self.assertEqual(self.adapter._replies, {})

    def test_only_a_completed_turn_with_its_own_reply_notifies(self):
        for outcome in [{"failed": True}, {"interrupted": True}, {"completed": False}, {"turn_id": "turn-2"}]:
            with self.subTest(outcome=outcome):
                finish_turn(self.adapter, "s1", "Partial answer.", "desktop", **outcome)
        finish_turn(self.adapter, "s1", None, "desktop")
        self.assertEqual(self.sent, [])
        self.assertEqual(self.adapter._replies, {})

    def test_a_turn_that_ends_on_a_bare_silence_marker_notifies_nobody(self):
        """A background process or subagent finished and the agent chose not to answer."""
        with patch.dict(sys.modules, {"gateway.response_filters": None}):
            for body in ["[SILENT]", " [silent] ", "NO_REPLY", "No  reply", "SILENT"]:
                with self.subTest(body=body):
                    finish_turn(self.adapter, "s1", body, "desktop")
            finish_turn(self.adapter, "s1", "[SILENT] is the marker for silence.", "desktop")
        self.assertEqual([p["body"] for p in self.sent], ["[SILENT] is the marker for silence."])

    def test_the_reply_is_judged_by_hermes_silence_rules_when_it_has_them(self):
        filters = types.ModuleType("gateway.response_filters")
        filters.is_intentional_silence_response = lambda text: text.strip() == "QUIET"
        filters.strip_trailing_silence_marker = lambda text: text.removesuffix("\n\nQUIET")
        with patch.dict(sys.modules, {"gateway.response_filters": filters}):
            finish_turn(self.adapter, "s1", "QUIET", "desktop")
            finish_turn(self.adapter, "s1", "[SILENT]", "desktop")
            finish_turn(self.adapter, "s1", "The tests pass.\n\nQUIET", "desktop")
        self.assertEqual([p["body"] for p in self.sent], ["[SILENT]", "The tests pass."])

    def test_natively_notifying_platforms_notify_only_when_listed(self):
        for platform in ["telegram", "discord", "homeassistant"]:
            finish_turn(self.adapter, "s-" + platform, "Done.", platform)
        self.assertEqual([p["subtitle"] for p in self.sent], ["Homeassistant"])
        with patch.dict("os.environ", {"HERMES_MOBILE_PUSH_HOOK_PLATFORMS": " Telegram, custom"}):
            for platform in ["telegram", "discord", "custom"]:
                finish_turn(self.adapter, "s-" + platform, "Done.", platform)
        self.assertEqual([p["subtitle"] for p in self.sent], ["Homeassistant", "Telegram", "Custom"])

    def test_listed_platforms_are_the_only_gateway_platforms_that_notify(self):
        """HERMES_MOBILE_PUSH_HOOK_PLATFORMS is an exact allowlist for gateway platforms, as the
        retired gateway hook read it; the app's, the desktop's and the CLI's own turns notify regardless."""
        with patch.dict("os.environ", {"HERMES_MOBILE_PUSH_HOOK_PLATFORMS": "telegram"}):
            for platform in ["telegram", "discord", "homeassistant", "webhook", "api_server",
                             "desktop", "local", "cli", ""]:
                finish_turn(self.adapter, "s-" + platform, "Done.", platform)
        self.assertEqual([p["hermes"]["platform"] for p in self.sent],
                         ["telegram", "desktop", "local", "cli", ""])


class CronCompletionTests(AdapterTestCase):
    def setUp(self):
        self.sent = []
        self.job = {"name": "Daily report", "deliver": "telegram"}
        self.redactor = types.ModuleType("agent.redact")
        self.redactor.redact_sensitive_text = lambda text, *, force: text.replace("secret-token", "[REDACTED]")
        self.filters = types.ModuleType("gateway.response_filters")
        self.filters.is_autonomous_silence_response = lambda text: text.strip() == "NO_REPLY"
        modules = patch.dict(sys.modules, {
            "agent.redact": self.redactor,
            "gateway.response_filters": self.filters,
        })
        modules.start()
        self.addCleanup(modules.stop)
        self.reported = []
        adapter = self.load_adapter()
        adapter._deliver_in_background = self.sent.append
        adapter._origin_metadata = lambda fields: {"profile": "research"}
        adapter._cron_job = lambda job_id: self.job if job_id == "job" else None
        adapter._unwatch_requests = lambda session_id="": None
        adapter._report_activity = lambda *args: self.reported.append(args)
        adapter.MAX_MESSAGE_LENGTH = 400
        adapter.NATIVELY_NOTIFYING = {"telegram"}

    def complete(self, session_id="cron_job_20260921_090000", body="The daily report is ready.", **flags):
        finish_turn(self.adapter, session_id, body, "cron", **flags)

    def test_telegram_cron_completion_routes_to_its_persisted_run(self):
        self.complete()
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0]["kind"], "delivery")
        self.assertEqual(self.sent[0]["subtitle"], "Daily report")
        self.assertEqual(self.sent[0]["hermes"], {
            "profile": "research", "kind": "delivery", "source": "cron",
            "session_id": "cron_job_20260921_090000",
        })

    def test_addressed_push_delivery_is_not_duplicated(self):
        for delivery in ["push", "telegram,push:device", ["telegram", "push"], "origin"]:
            with self.subTest(delivery=delivery):
                self.job = {"deliver": delivery, "origin": {"platform": "push", "chat_id": "device"}}
                self.complete()
        self.assertEqual(self.sent, [])

    def test_intentionally_silent_cron_does_not_notify(self):
        self.complete(body="NO_REPLY")
        self.assertEqual(self.sent, [])

    def test_cron_redacts_body_and_job_name_before_truncation(self):
        self.job["name"] = "Report secret-token"
        self.complete(body="x" * 390 + "secret-token")
        self.assertEqual(self.sent[0]["subtitle"], "Report [REDACTED]")
        self.assertEqual(self.sent[0]["body"], "x" * 390 + "[REDACTED]")

    def test_redaction_failure_never_sends_original_cron_text(self):
        def fail(text, *, force):
            raise RuntimeError("redactor unavailable")
        self.redactor.redact_sensitive_text = fail
        self.job["name"] = "secret-token"
        self.complete(body="secret-token")
        self.assertEqual(self.sent[0]["body"], "[REDACTED - redaction failed]")
        self.assertEqual(self.sent[0]["subtitle"], "Scheduled task")

    def test_older_hermes_still_suppresses_original_silent_marker(self):
        with patch.dict(sys.modules, {"gateway.response_filters": None}):
            self.complete(body="[SILENT] No changes")
        self.assertEqual(self.sent, [])

    def test_failed_interrupted_and_kanban_runs_do_not_emit_success(self):
        self.complete(failed=True)
        self.complete(interrupted=True)
        self.complete(completed=False)
        finish_turn(self.adapter, "kanban_job", "Done.", "cli")
        self.assertEqual(self.sent, [])


def _hermes_server_requests():
    try:
        from tui_gateway import server_requests
        return server_requests
    except Exception:
        return None


@unittest.skipUnless(_hermes_server_requests(), "needs a Hermes checkout on PYTHONPATH (run on the test server)")
class HermesServerRequestTests(unittest.TestCase):
    """The watch against Hermes' real request registry, contracts and session context."""

    def setUp(self):
        self.adapter = load_module("adapter.py")
        self.sent = []
        self.adapter._deliver_in_background = lambda payload, relay=None: self.sent.append(payload)
        self.adapter._relay_settings = lambda: RELAY
        self.registry = _hermes_server_requests()
        self.registry.reset_for_tests()
        self.addCleanup(self.registry.reset_for_tests)
        self.frames = []
        sinks = patch.multiple(self.registry, _write=self.frames.append, _answerable=lambda sid: True)
        sinks.start()
        self.addCleanup(sinks.stop)

    def test_each_human_request_notifies_once_with_its_id(self):
        from gateway.session_context import clear_session_vars, set_session_vars
        sid = "live-test"
        tokens = set_session_vars(session_key="20261002_101500_abcdef", session_id="20261002_101500_abcdef",
                                  source="ios", profile="research", ui_session_id=sid)
        try:
            self.adapter._on_api_request(session_id="20261002_101500_abcdef", platform="ios")
        finally:
            clear_session_vars(tokens)
        self.assertTrue(self.adapter._requests_watched(sid))
        settles = [
            self.registry.send_async("clarify", sid, {"questions": [
                {"qid": "q1", "question": "Which format?", "choices": ["PDF", "Markdown"], "multi_select": False}]},
                lambda result: None),
            self.registry.send_async("sudo", sid, {"command": "sudo true"}, lambda result: None),
            self.registry.send_async("secret", sid, {"env_var": "API_KEY", "prompt": "API key"}, lambda result: None),
            self.registry.send_async("approval", sid, {
                "request_id": "queue-1", "command": "rm -rf build", "description": "Delete build",
                "choices": ["once", "deny"]}, lambda result: None),
            self.registry.send_async("window.read", sid, {}, lambda result: None),
        ]
        self.adapter._notify_open_requests(self.registry.open_requests)
        self.adapter._notify_open_requests(self.registry.open_requests)
        for settle in settles:
            settle("resolved")
        self.assertEqual([push["kind"] for push in self.sent], ["clarify", "sudo", "secret", "approval"])
        frame_ids = [frame["id"] for frame in self.frames]
        self.assertEqual([push["hermes"]["request_id"] for push in self.sent[:3]], frame_ids[:3])
        self.assertTrue(all(request_id.startswith("srq-") for request_id in frame_ids))
        self.assertEqual(self.sent[0]["hermes"]["question_id"], "q1")
        self.assertEqual(self.sent[3]["hermes"]["request_id"], "queue-1")
        for push in self.sent:
            self.assertEqual(push["hermes"]["session_id"], "20261002_101500_abcdef")
            self.assertEqual(push["hermes"]["profile"], "research")
        self.adapter._on_session_end(session_id="20261002_101500_abcdef", completed=True)
        self.assertFalse(self.adapter._requests_watched(sid))


class PushOriginTests(unittest.TestCase):
    def test_profile_is_captured_before_background_worker_loses_context(self):
        adapter, constants = load_push_adapter(Path("/srv/hermes/profiles/research"))
        with patch.dict(sys.modules, {"hermes_constants": constants}):
            metadata = adapter._origin_metadata({"session_id": "cron_job", "platform": "cron"})
            constants.get_hermes_home = lambda: Path("/srv/hermes")
            self.assertEqual(metadata["profile"], "research")
            self.assertEqual(adapter._origin_metadata({"profile": "explicit", "platform": "ios"})["profile"], "explicit")


class DeliveryHistoryTests(AdapterTestCase):
    def setUp(self):
        self.rows = {}
        self.closed = False
        owner = self
        class DB:
            def create_session(self, session_id, source):
                owner.rows[session_id] = {"source": source}
            def append_message(self, session_id, role, content):
                owner.rows[session_id].update(role=role, content=content)
            def set_session_title(self, session_id, title):
                # A rejected title still leaves a usable saved transcript.
                raise ValueError("duplicate title")
            def close(self):
                owner.closed = True
        state = types.ModuleType("hermes_state")
        state.SessionDB = DB
        modules = patch.dict(sys.modules, {"hermes_state": state})
        modules.start()
        self.addCleanup(modules.stop)
        adapter = self.load_adapter()
        adapter.MAX_MESSAGE_LENGTH = 40
        adapter._origin_metadata = lambda fields, **kwargs: {"profile": "research", **fields}

    def test_unaddressed_cron_retains_full_text_and_routes_despite_title_failure(self):
        content = "Cronjob Response: Daily report\n" + "details " * 50
        payload = self.adapter._build_payload(content)
        row = self.rows[payload["hermes"]["session_id"]]
        self.assertEqual(row, {"source": "cron", "role": "assistant", "content": content})
        self.assertEqual(payload["hermes"]["profile"], "research")
        self.assertEqual(payload["body"], content[:40])
        self.assertTrue(self.closed)

    def test_live_cron_job_metadata_identifies_scheduled_history(self):
        payload = self.adapter._build_payload("Unwrapped result", metadata={"job_id": "job-1"})
        self.assertEqual(self.rows[payload["hermes"]["session_id"]]["source"], "cron")

    def test_standalone_unwrapped_delivery_is_still_saved(self):
        payload = self.adapter._build_payload("Unwrapped result")
        self.assertEqual(self.rows[payload["hermes"]["session_id"]]["content"], "Unwrapped result")

    def test_existing_session_and_system_events_never_create_history(self):
        payload = self.adapter._build_payload("Answer", metadata={"session_id": "existing"})
        self.assertEqual(payload["hermes"]["session_id"], "existing")
        self.adapter._build_payload("Gateway online", metadata={"kind": "system"})
        self.assertEqual(self.rows, {})


class PushHardeningTests(AdapterTestCase):
    """Ported from hermes-mobile-push-relay tests/test_hardening.py when the relay's plugin copy was retired."""

    def setUp(self):
        self.load_adapter()
        modules = patch.dict(sys.modules, {"hermes_constants": self.constants})
        modules.start()
        self.addCleanup(modules.stop)

    def test_registered_hooks_match_the_manifest_on_old_and_new_hook_registries(self):
        import yaml
        manifest = yaml.safe_load((PUSH / "plugin.yaml").read_text())
        for extra in [set(), {"gateway_client_event"}]:
            with self.subTest(extra=extra):
                hooks = registered_hooks(self.adapter, self.constants, set(manifest["provides_hooks"]) | extra)
                self.assertEqual(sorted(hooks), sorted(manifest["provides_hooks"]))
                self.assertEqual(len(hooks), len(set(hooks)))

    def test_scoped_secrets_are_preferred_over_the_environment(self):
        shared = types.ModuleType("gateway.platforms._shared")
        shared.get_scoped_secret = {
            "HERMES_MOBILE_PUSH_RELAY_URL": "https://scoped.example/", "HERMES_MOBILE_PUSH_INSTALL_KEY": "scoped-key"}.get
        with patch.dict(sys.modules, {"gateway": types.ModuleType("gateway"),
                                      "gateway.platforms": types.ModuleType("gateway.platforms"),
                                      "gateway.platforms._shared": shared}), \
                patch.dict("os.environ", {"HERMES_MOBILE_PUSH_RELAY_URL": "https://env.example",
                                          "HERMES_MOBILE_PUSH_INSTALL_KEY": "env-key"}):
            self.assertEqual(self.adapter._relay_settings(), ("https://scoped.example", "scoped-key"))

    def test_a_burst_is_bounded_and_drops_the_oldest(self):
        import queue
        with patch.object(self.adapter, "_delivery_worker", types.SimpleNamespace(is_alive=lambda: True)), \
                patch.object(self.adapter, "_delivery_queue", queue.Queue(maxsize=3)), \
                patch.object(self.adapter, "_relay_settings", return_value=RELAY):
            for number in range(10):
                self.adapter._deliver_in_background({"number": number})
            pending = self.adapter._delivery_queue
            self.assertEqual(pending.qsize(), 3)
            self.assertEqual([pending.get_nowait()[2]["number"] for _ in range(3)], [7, 8, 9])

    def test_legacy_preview_key_is_copied_once_and_kept(self):
        privacy = load_privacy()
        privacy._home = lambda: self.home
        legacy = self.home / "plugins/hermes-mobile-push/preview_key"
        legacy.parent.mkdir(parents=True)
        key = bytes(range(32))
        legacy.write_bytes(base64.b64encode(key))
        self.assertEqual(privacy.load_key(), key)
        self.assertEqual(privacy.key_path().read_bytes(), legacy.read_bytes())
        self.assertTrue(legacy.is_file())
        self.assertEqual(privacy.load_key(create=True), key)


class UnifiedRuntimeTests(AdapterTestCase):
    """Hermes 0.22.1+ runs app sessions through the gateway: the store says "local", the agent
    "desktop" and only the launch policy keeps the creation label ("gui"); the plugin context
    names the cron run a turn belongs to (``ctx.current_cron_execution()``)."""

    def setUp(self):
        self.load_adapter()
        self.cron = None
        self.jobs = {}
        self.policy = None
        self.env = {}
        session_policy = types.ModuleType("gateway.session_policy")
        session_policy.active_policy = lambda: self.policy
        context = types.ModuleType("gateway.session_context")
        context.get_session_env = lambda name, default="": self.env.get(name, default)
        jobs = types.ModuleType("cron.jobs")
        jobs.get_job = self.jobs.get
        modules = patch.dict(sys.modules, {
            "hermes_constants": self.constants, "gateway": types.ModuleType("gateway"),
            "gateway.session_policy": session_policy, "gateway.session_context": context,
            "cron": types.ModuleType("cron"), "cron.jobs": jobs,
            "hermes_state": None, "tools": None, "agent": None, "agent.delegation_context": None,
        })
        modules.start()
        self.addCleanup(modules.stop)
        self.sent = []
        self.reported = []
        context = types.SimpleNamespace(current_cron_execution=lambda: self.cron)
        for name, value in {"_deliver_in_background": lambda payload, relay=None: self.sent.append(payload),
                            "_report_activity": lambda *args: self.reported.append(args),
                            "_HUMAN_INPUT_HOOKS": True, "_ctx": context,
                            # A Hermes without the session prompt hooks, whichever Hermes is importable;
                            # ``with_prompt_hooks`` turns them on.
                            "_SESSION_PROMPT_HOOKS": False}.items():
            patcher = patch.object(self.adapter, name, value, create=name == "_SESSION_PROMPT_HOOKS")
            patcher.start()
            self.addCleanup(patcher.stop)

    def launch(self, source, platform):
        self.policy = types.SimpleNamespace(source=source, platform=platform)

    def with_prompt_hooks(self, stored, route):
        """A Hermes with the session prompt hooks; a gateway app session's turn, *stored* in the store
        and *route* the gateway's session key, starts its LLM call (``pre_api_request``)."""
        self.assertTrue(hasattr(self.adapter, "_on_session_prompt"), "the plugin does not listen to on_session_prompt")
        for name, value in {"_SESSION_PROMPT_HOOKS": True, "_activity_begin": lambda fields: None}.items():
            patcher = patch.object(self.adapter, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.adapter._prompt_watches.clear)
        self.env = {"HERMES_SESSION_PLATFORM": "local", "HERMES_SESSION_ID": stored, "HERMES_SESSION_KEY": route}
        self.adapter._on_api_request(session_id=stored, turn_id="turn-1", platform="desktop")

    def gateway_prompt(self, session_id, kind, prompt_id, params, settled=False):
        """The gateway opens (or settles) a prompt, off the turn: none of its session context."""
        saved, self.env = self.env, {}
        try:
            if settled:
                self.adapter._on_session_prompt_settled(session_id=session_id, kind=kind, prompt_id=prompt_id,
                                                        execution_generation=None, reason="resolved")
            else:
                self.adapter._on_session_prompt(session_id=session_id, kind=kind, prompt_id=prompt_id,
                                                execution_generation=None, params=params)
        finally:
            self.env = saved

    def end(self, session_id, platform, body="Done."):
        finish_turn(self.adapter, session_id, body, platform)

    def test_an_app_session_is_its_own_surface_labelled_gui(self):
        self.launch("gui", "desktop")
        self.end("local-1", "desktop")
        payload = self.sent[-1]
        self.assertNotIn("subtitle", payload)
        # Not "desktop": the relay's Desktop filter would silence the phone's own sessions.
        self.assertEqual((payload["kind"], payload["hermes"]["source"], payload["hermes"]["platform"]),
                         ("reply", "gui", "desktop"))
        # A safe-mode session's managed worker builds its agent under the label itself.
        self.end("local-2", "gui")
        self.assertEqual((self.sent[-1]["hermes"]["source"], self.sent[-1].get("subtitle")), ("gui", None))

    def test_a_delegated_child_keeps_its_own_label(self):
        self.launch("gui", "desktop")
        self.end("subagent_1", "subagent")
        self.assertEqual(self.sent[-1]["hermes"]["source"], "subagent")
        self.assertTrue(self.sent[-1]["hermes"]["is_subagent"])

    def test_an_unaddressed_cron_run_notifies_with_the_name_of_its_run(self):
        self.cron = types.SimpleNamespace(job_id="job-1", job_name="Morning brief")
        self.jobs["job-1"] = {"name": "Stored name", "deliver": "local"}
        redactor = types.ModuleType("agent.redact")
        redactor.redact_sensitive_text = lambda text, *, force: text
        with patch.dict(sys.modules, {"agent.redact": redactor, "gateway.response_filters": None}):
            self.end("local-cron", "cron", body="Brief ready.")
        payload = self.sent[-1]
        self.assertEqual((payload["kind"], payload["subtitle"], payload["body"], payload["hermes"]["source"]),
                         ("delivery", "Morning brief", "Brief ready.", "cron"))

    def test_a_cron_run_that_delivers_to_push_is_not_announced_twice(self):
        self.cron = types.SimpleNamespace(job_id="job-1", job_name="Morning brief")
        self.jobs["job-1"] = {"name": "Morning brief", "deliver": "push"}
        self.end("local-cron", "cron")
        self.assertEqual(self.sent, [])

    def test_kanban_workers_are_unattended(self):
        with patch.dict("os.environ", {"HERMES_KANBAN_TASK": "task-1"}):
            self.end("local-kanban", "cli")
        self.end("local-kanban-2", "kanban")
        self.assertEqual(self.sent, [])

    def test_the_creation_label_replaces_the_gateway_platform(self):
        self.launch("gui", "desktop")
        origin = self.adapter._origin_metadata({"platform": "local", "session_id": "local-1", "profile": "p"})
        self.assertEqual(origin, {"profile": "p", "source": "gui", "session_id": "local-1"})
        self.policy = None  # Outside the turn, without a readable store, "local" says nothing.
        origin = self.adapter._origin_metadata({"platform": "local", "session_id": "local-1", "profile": "p"})
        self.assertEqual(origin, {"profile": "p", "session_id": "local-1"})

    def test_an_approval_a_person_is_asked_names_the_stored_session_not_the_route(self):
        self.launch("gui", "desktop")
        self.adapter._on_human_input_request(kind="approval", request_id="r1", session_id="local-1",
                                             session_key="agent:main:local:dm:local-1", platform="local",
                                             prompt="rm -rf build")
        payload = self.sent[-1]
        self.assertEqual((payload["title"], payload["subtitle"], payload["body"], payload["thread_id"]),
                         ("Approval needed", "Waiting on you", "rm -rf build", "local-1"))
        self.assertEqual((payload["hermes"]["session_id"], payload["hermes"]["source"], payload["hermes"]["session_key"]),
                         ("local-1", "gui", "agent:main:local:dm:local-1"))
        self.assertNotIn("request_id", payload["hermes"], "the hook's request id answers nothing the app can")
        self.assertEqual(self.reported, [("attention", "local-1", True)])

    def test_with_the_prompt_hooks_an_approval_names_the_stored_session_not_the_route(self):
        self.launch("gui", "desktop")
        self.with_prompt_hooks("local-1", "agent:main:local:dm:live-1")
        self.adapter._on_human_input_request(kind="approval", request_id="r1", session_id="local-1",
                                             session_key="agent:main:local:dm:live-1", platform="local",
                                             prompt="rm -rf build")
        self.assertEqual((self.sent, self.reported), ([], []), "the gateway's prompt announces it, with its id")
        self.gateway_prompt("live-1", "approval", "appr-1", {
            "kind": "approval", "prompt_id": "appr-1", "execution_generation": 1, "command": "rm -rf build",
            "description": "", "choices": ["once", "deny"]})
        [payload] = self.sent
        self.assertEqual((payload["title"], payload["subtitle"], payload["body"], payload["thread_id"]),
                         ("Approval needed", "Waiting on you", "rm -rf build", "local-1"))
        self.assertEqual((payload["hermes"]["session_id"], payload["hermes"]["source"], payload["hermes"]["request_id"]),
                         ("local-1", "gui", "appr-1"))
        self.assertEqual(self.reported, [("attention", "local-1", True)])

    def test_a_messaging_approval_names_its_platform(self):
        self.adapter._on_human_input_request(kind="approval", request_id="r1", session_id="s1",
                                             session_key="agent:main:telegram:dm:1", platform="telegram", prompt="")
        payload = self.sent[-1]
        self.assertEqual((payload["subtitle"], payload["body"]), ("Waiting on you · Telegram",
                                                                  "The agent is waiting for approval."))
        self.assertEqual(payload["hermes"]["source"], "telegram")

    def test_pre_approval_request_is_left_to_the_human_input_hook(self):
        self.adapter._on_approval_request(command="ls", description="List", session_key="x", surface="gateway",
                                          session_id="s1")
        self.assertEqual((self.sent, self.reported), ([], []))

    def test_questions_in_app_sessions_notify_and_hold_the_live_activity(self):
        self.launch("gui", "desktop")
        fields = {"kind": "clarify", "request_id": "r1", "session_id": "local-1", "session_key": "route",
                  "platform": "local", "prompt": "Which format?"}
        self.adapter._on_human_input_request(**fields)
        self.adapter._on_human_input_resolved(**fields, outcome="answered")
        self.adapter._on_human_input_request(**{**fields, "kind": "sudo", "prompt": "sudo make install"})
        self.assertEqual([(p["kind"], p["title"], p["body"], p["thread_id"]) for p in self.sent], [
            ("clarify", "Question", "Which format?", "local-1"),
            ("sudo", "Password needed", ("Hermes needs your administrator password to continue.\n"
                                         "Command: sudo make install"), "local-1")])
        self.assertEqual(self.sent[0]["hermes"], {"profile": self.home.name if self.home.parent.name == "profiles" else "default",
                                                  "source": "gui", "session_id": "local-1", "kind": "clarify",
                                                  "session_key": "route"})
        self.assertEqual(self.reported, [("attention", "local-1", True), ("attention", "local-1", False),
                                         ("attention", "local-1", True)])

    def test_with_the_prompt_hooks_questions_in_app_sessions_notify_and_hold_the_live_activity(self):
        self.launch("gui", "desktop")
        self.with_prompt_hooks("local-1", "agent:main:local:dm:local-1")
        fields = {"kind": "clarify", "request_id": "r1", "session_id": "local-1", "session_key": "route",
                  "platform": "local", "prompt": "Which format?"}
        self.adapter._on_human_input_request(**fields)
        self.gateway_prompt("local-1", "clarify", "clar-1", {
            "kind": "clarify", "prompt_id": "clar-1", "execution_generation": 1, "question": "Which format?",
            "choices": [], "multi_select": False})
        self.adapter._on_human_input_resolved(**fields, outcome="answered")
        self.gateway_prompt("local-1", "clarify", "clar-1", None, settled=True)
        self.adapter._on_human_input_request(**{**fields, "kind": "sudo", "prompt": "sudo make install"})
        self.gateway_prompt("local-1", "sudo", "srq-0123456789ab", {"session_id": "local-1",
                                                                     "command": "sudo make install"})
        self.assertEqual([(p["kind"], p["title"], p["body"], p["thread_id"]) for p in self.sent], [
            ("clarify", "Question", "Which format?", "local-1"),
            ("sudo", "Password needed", ("Hermes needs your administrator password to continue.\n"
                                         "Command: sudo make install"), "local-1")])
        self.assertEqual(self.sent[0]["hermes"], {"profile": self.home.name if self.home.parent.name == "profiles" else "default",
                                                  "source": "gui", "session_id": "local-1", "kind": "clarify",
                                                  "answer_mode": "text", "request_id": "clar-1"})
        self.assertEqual(self.reported, [("attention", "local-1", True), ("attention", "local-1", False),
                                         ("attention", "local-1", True)])

    def test_questions_elsewhere_and_watched_sessions_keep_their_own_paths(self):
        base = {"request_id": "r1", "session_id": "s1", "prompt": "?"}
        self.adapter._on_human_input_request(**base, kind="clarify", platform="cli")
        self.adapter._on_human_input_request(**base, kind="clarify", platform="telegram")
        self.adapter._on_human_input_request(**base, kind="vault", platform="local")
        self.env["HERMES_UI_SESSION_ID"] = "live-1"
        self.adapter._request_watches["live-1"] = {"session_id": "s1", "touched": time.monotonic()}
        self.addCleanup(self.adapter._request_watches.clear)
        for kind in ["approval", "clarify"]:
            self.adapter._on_human_input_request(**base, kind=kind, platform="local")  # the watch owns it
        self.assertEqual((self.sent, self.reported), ([], []))

    def test_the_relay_sees_the_gui_label(self):
        privacy = load_privacy()
        privacy._home = lambda: self.home
        sealed = privacy.protect({"kind": "reply", "hermes": {"source": "gui", "session_id": "local-1"}}, "key")
        self.assertEqual(sealed["hermes"]["source"], "gui")


class OlderHermesPayloadTests(AdapterTestCase):
    """On a Hermes without the plugin session accessors (``turn_client``, ``session_viewers``) every push
    is the one the plugin has always sent, byte for byte: whether ``hermes_cli.plugin_session`` is missing
    or predates them."""

    setUp, launch, end = UnifiedRuntimeTests.setUp, UnifiedRuntimeTests.launch, UnifiedRuntimeTests.end

    def older_hermes(self):
        session = types.ModuleType("hermes_cli.plugin_session")
        session.session_surface = lambda: "desktop"
        session.session_client = lambda: {"name": "hermes-desktop"}
        yield {"hermes_cli": types.ModuleType("hermes_cli"), "hermes_cli.plugin_session": session}
        yield {"hermes_cli": None, "hermes_cli.plugin_session": None}

    def pushes(self):
        """A Desktop or app reply, a delegated child's, a messaging platform's and a wait on the person."""
        self.sent.clear()
        self.launch("gui", "desktop")
        self.end("local-1", "desktop", body="The draft is ready.")
        self.end("subagent_1", "subagent", body="Child done.")
        self.policy = None
        self.end("s-home", "homeassistant", body="Lights off.")
        self.launch("gui", "desktop")
        self.adapter._on_human_input_request(kind="approval", request_id="r1", session_id="local-1",
                                             session_key="agent:main:local:dm:local-1", platform="local",
                                             prompt="rm -rf build")
        return list(self.sent)

    def test_every_push_is_todays(self):
        self.maxDiff = None
        profile = self.home.name if self.home.parent.name == "profiles" else "default"
        expected = [
            {"title": "Hermes", "body": "The draft is ready.", "kind": "reply", "thread_id": "local-1",
             "hermes": {"profile": profile, "source": "gui", "session_id": "local-1", "kind": "agent_end",
                        "platform": "desktop", "turn_id": "turn-1"}},
            {"title": "Hermes", "subtitle": "Subagent", "body": "Child done.", "kind": "reply", "thread_id": "subagent_1",
             "hermes": {"profile": profile, "source": "subagent", "session_id": "subagent_1", "is_subagent": True,
                        "kind": "agent_end", "platform": "subagent", "turn_id": "turn-1"}},
            {"title": "Hermes", "subtitle": "Homeassistant", "body": "Lights off.", "kind": "reply",
             "thread_id": "s-home",
             "hermes": {"profile": profile, "source": "homeassistant", "session_id": "s-home", "kind": "agent_end",
                        "platform": "homeassistant", "turn_id": "turn-1"}},
            {"title": "Approval needed", "subtitle": "Waiting on you", "body": "rm -rf build", "kind": "approval",
             "hermes": {"profile": profile, "source": "gui", "session_id": "local-1", "kind": "approval",
                        "session_key": "agent:main:local:dm:local-1"},
             "thread_id": "local-1"},
        ]
        for modules in self.older_hermes():
            with self.subTest(plugin_session=modules["hermes_cli.plugin_session"]), patch.dict(sys.modules, modules):
                pushes = self.pushes()
                self.assertEqual(pushes, expected)
                self.assertEqual([json.dumps(p) for p in pushes], [json.dumps(p) for p in expected])

    def test_the_relay_sees_what_it_always_saw(self):
        privacy = load_privacy()
        privacy._home = lambda: self.home
        privacy.load_key = lambda: bytes(range(32))
        for modules in self.older_hermes():
            with self.subTest(plugin_session=modules["hermes_cli.plugin_session"]), patch.dict(sys.modules, modules):
                public = [privacy.protect(push, "ab" * 32) for push in self.pushes()]
                self.assertEqual([sorted(p["hermes"]) for p in public], [
                    ["event_id", "sealed", "session_id", "source"],
                    ["event_id", "is_subagent", "sealed", "session_id", "source"],
                    ["event_id", "sealed", "session_id"],
                    ["event_id", "sealed", "session_id", "source"]])
                self.assertEqual([p["hermes"].get("source") for p in public], ["gui", "subagent", None, "gui"])


if __name__ == "__main__":
    unittest.main()
