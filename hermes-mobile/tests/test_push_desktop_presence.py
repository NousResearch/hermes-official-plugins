"""What a Hermes with the plugin session accessors adds to a finished turn's reply push: the client
that submitted the turn as its source (``turn_client()``), and whether the user is active at
a computer (``session_viewers()``)."""
import asyncio
import base64
import hashlib
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from push_plugin import finish_turn, load_module, load_privacy, load_push_adapter

DESKTOP = {"name": "hermes-desktop", "version": "1.4.0", "kind": "desktop"}
WEBAPP = {"name": "hermes-webapp", "kind": "webapp"}
IPHONE = {"name": "hermes-mobile-ios", "version": "2.3.0", "kind": "mobile"}
ANDROID = {"name": "hermes-mobile-android", "kind": "mobile"}


class PresenceTestCase(unittest.TestCase):
    """A fresh adapter on a Hermes whose ``ctx`` has the session accessors, in an app or
    Desktop session the unified runtime created as ``gui``."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name)
        self.adapter, self.constants = load_push_adapter(self.home)
        self.client = None
        self.viewers = []
        self.failing = set()
        self.reads = []
        self.policy = types.SimpleNamespace(source="gui", platform="desktop")
        self.delegated = False
        self.ctx = types.SimpleNamespace(
            turn_client=lambda: self.read("turn_client", self.client),
            session_viewers=lambda: self.read("session_viewers", self.viewers),
            session_source=lambda: getattr(self.policy, "source", "") if self.policy is not None else "",
            session_surface=lambda: getattr(self.policy, "platform", "") if self.policy is not None else "",
            in_delegated_child=lambda include_subprocesses=False: self.delegated,
            current_cron_execution=lambda: None,
            cron_delivery_platforms=lambda job_id=None: (),
        )
        self.adapter._ctx = self.ctx
        context = types.ModuleType("gateway.session_context")
        context.get_session_env = lambda name, default="": default
        modules = patch.dict(sys.modules, {
            "hermes_constants": self.constants, "gateway": types.ModuleType("gateway"),
            "gateway.session_context": context,
            "persistence_assembly": None, "tools": None, "agent": None,
            "gateway.response_filters": None, "cron": None,
        })
        modules.start()
        self.addCleanup(modules.stop)
        self.sent = []
        self.reported = []
        self.adapter._deliver_in_background = lambda payload, relay=None: self.sent.append(payload)
        self.adapter._report_activity = lambda *args: self.reported.append(args)
        self.adapter._HUMAN_INPUT_HOOKS = True
        self.adapter._SESSION_PROMPT_HOOKS = False
        self.profile = self.home.name if self.home.parent.name == "profiles" else "default"

    def read(self, name, value):
        self.reads.append(name)
        if name in self.failing:
            raise RuntimeError(f"{name} failed")
        return value

    def reply(self, session_id="local-1", platform="desktop", body="Done."):
        before = len(self.sent)
        finish_turn(self.adapter, session_id, body, platform)
        pushes = self.sent[before:]
        self.assertEqual(len(pushes), 1, pushes)
        return pushes[0]


class TurnSourceTests(PresenceTestCase):
    def test_the_reply_names_the_client_that_sent_the_turn(self):
        table = [
            (DESKTOP, "desktop"), (WEBAPP, "desktop"), (IPHONE, "ios"), (ANDROID, "android"),
            ({"name": "hermes-mobile-watch", "kind": "mobile"}, "mobile"), ({"kind": "mobile"}, "mobile"),
            # No client, a client of another kind or without one, a malformed answer: today's label.
            (None, "gui"), ({"name": "hermes-tui", "kind": "tui"}, "gui"), ({"name": "hermes-desktop"}, "gui"),
            ({"name": "hermes-desktop", "kind": "Desktop"}, "gui"), ("desktop", "gui"), ([], "gui"),
        ]
        for client, source in table:
            with self.subTest(client=client):
                self.client = client
                payload = self.reply()
                self.assertEqual(payload["hermes"]["source"], source)
                self.assertEqual(payload["hermes"]["platform"], "desktop")
                self.assertEqual(payload["hermes"]["kind"], "agent_end")

    def test_without_the_accessor_or_when_it_fails_the_reply_keeps_todays_label(self):
        self.client = DESKTOP
        self.failing.add("turn_client")
        with self.assertLogs(self.adapter.logger, "DEBUG") as logs:
            self.assertEqual(self.reply()["hermes"]["source"], "gui", "the push still goes, with today's label")
        self.assertEqual([record.levelname for record in logs.records], ["DEBUG"])
        self.failing.clear()
        del self.ctx.turn_client  # A Hermes whose PluginContext predates the accessor.
        with self.assertNoLogs(self.adapter.logger, "DEBUG"):
            self.assertEqual(self.reply()["hermes"]["source"], "gui")
            self.ctx.turn_client = "not callable"
            self.assertEqual(self.reply()["hermes"]["source"], "gui")
            del self.ctx.turn_client
            del self.ctx.session_viewers
            self.assertEqual(self.reply()["hermes"]["source"], "gui")

    def test_a_delegated_child_keeps_its_own_label(self):
        self.client = DESKTOP
        payload = self.reply("subagent_1", "subagent")
        self.assertEqual((payload["hermes"]["source"], payload["hermes"]["is_subagent"]), ("subagent", True))
        self.delegated = True
        try:
            self.assertEqual(self.reply("local-1", "desktop")["hermes"]["source"], "gui")
        finally:
            self.delegated = False

    def test_messaging_turns_keep_their_platform(self):
        self.policy = None
        payload = self.reply("s1", "homeassistant")
        self.assertEqual((payload["hermes"]["source"], payload["subtitle"]), ("homeassistant", "Homeassistant"))

    def test_a_client_of_another_shape_keeps_todays_label_and_the_reply_goes(self):
        # Not an answer the accessor gives: a kind or a name that is neither a string nor None.
        for client in [{"kind": []}, {"kind": {}}, {"kind": 1}, {"kind": "desktop", "name": []},
                       {"kind": "webapp", "name": {"n": 1}}, {"kind": "mobile", "name": 5}]:
            with self.subTest(client=client):
                self.client = client
                payload = self.reply()
                self.assertEqual(payload["hermes"]["source"], "gui")
                self.assertNotIn("desktop_active", payload["hermes"])

    def test_webhook_and_delegated_replies_carry_todays_payload(self):
        """Whatever the accessors say, a webhook run's reply and a delegated child's completion are the
        pushes a Hermes without them sends."""
        self.client = DESKTOP
        self.viewers = [{"name": "hermes-desktop", "kind": "desktop", "idle_seconds": 1.0, "basis": "system",
                         "can_alert": True}]
        cases = [("s-hook", "webhook", False), ("s-graph", "msgraph_webhook", False),
                 ("subagent_1", "subagent", False), ("local-1", "desktop", True)]
        with patch.object(self.adapter, "_webhook_route_delivers_to_push", lambda: False):
            for session_id, platform, delegated in cases:
                with self.subTest(platform=platform, delegated=delegated):
                    self.delegated = delegated
                    try:
                        with patch.object(self.ctx, "turn_client", None), \
                                patch.object(self.ctx, "session_viewers", None):
                            control = self.reply(session_id, platform)
                        self.assertEqual(self.reply(session_id, platform), control)
                    finally:
                        self.delegated = False

    def test_the_notification_text_is_the_same_whichever_client_sent_the_turn(self):
        shown = set()
        for client in [None, DESKTOP, WEBAPP, IPHONE, ANDROID]:
            self.client = client
            payload = self.reply(body="**The draft** is ready.")
            shown.add((payload["title"], payload.get("subtitle"), payload["body"], payload["thread_id"]))
            privacy = load_privacy()
            privacy._home = lambda: self.home
            privacy.load_key = lambda: bytes(range(32))
            protected = privacy.protect(payload, "ab" * 32)
            shown.add((protected["title"], protected.get("subtitle"), protected["body"]))
        self.assertEqual(shown, {("Hermes", None, "**The draft** is ready.", "local-1"),
                                 ("Hermes Agent", None, "New activity. Open Hermes Agent to view.")})

    def test_the_relay_sees_every_client_label(self):
        privacy = load_privacy()
        privacy._home = lambda: self.home
        privacy.load_key = lambda: bytes(range(32))
        public = {}
        for client in [DESKTOP, WEBAPP, IPHONE, ANDROID, {"name": "hermes-mobile-watch", "kind": "mobile"}]:
            self.client = client
            public[client["name"]] = privacy.protect(self.reply(), "ab" * 32)["hermes"].get("source")
        self.assertEqual(public, {"hermes-desktop": "desktop", "hermes-webapp": "desktop", "hermes-mobile-ios": "ios",
                                  "hermes-mobile-android": "android", "hermes-mobile-watch": "mobile"})
        # A label outside the list stays sealed.
        self.policy, self.client = None, None
        self.assertNotIn("source", privacy.protect(self.reply("s1", "homeassistant"), "ab" * 32)["hermes"])

    def test_waits_on_the_person_and_cron_runs_keep_todays_label(self):
        self.client = DESKTOP
        self.adapter._on_human_input_request(kind="approval", request_id="r1", session_id="local-1",
                                             session_key="agent:main:local:dm:local-1", platform="local",
                                             prompt="rm -rf build")
        self.assertEqual((self.sent[-1]["kind"], self.sent[-1]["hermes"]["source"]), ("approval", "gui"))
        self.ctx.current_cron_execution = lambda: types.SimpleNamespace(job_id="job-1", job_name="Morning brief")
        self.ctx.cron_delivery_platforms = lambda job_id=None: ()
        redactor = types.ModuleType("agent.redact")
        redactor.redact_sensitive_text = lambda text, *, force: text
        with patch.dict(sys.modules, {"agent": types.ModuleType("agent"), "agent.redact": redactor}):
            payload = self.reply("local-cron", "cron")
        self.assertEqual((payload["kind"], payload["hermes"]["source"]), ("delivery", "cron"))
        self.assertNotIn("turn_client", self.reads, "only a reply push asks for the turn's client")


def viewer(kind="desktop", idle_seconds=12.0, basis="system", can_alert=True, name=None):
    names = {"desktop": "hermes-desktop", "webapp": "hermes-webapp", "mobile": "hermes-mobile-ios"}
    return {"name": name or names.get(kind), "kind": kind, "idle_seconds": idle_seconds, "basis": basis,
            "can_alert": can_alert}


def _hermes_presence_accessors():
    """Hermes' ``plugin_session`` when it has the accessors (a Hermes checkout on ``PYTHONPATH``)."""
    try:
        from hermes_cli import plugin_session
    except Exception:
        return None
    if callable(getattr(plugin_session, "turn_client", None)) and callable(getattr(plugin_session, "session_viewers", None)):
        return plugin_session
    return None


@unittest.skipUnless(_hermes_presence_accessors(), "needs a Hermes checkout with plugin_session.turn_client on PYTHONPATH")
class HermesPresenceAccessorTests(unittest.TestCase):
    """The plugin against Hermes' own accessors: found, and read as the contract describes them."""

    def setUp(self):
        self.adapter = load_module("adapter.py")
        ps = _hermes_presence_accessors()
        self.adapter._ctx = types.SimpleNamespace(
            turn_client=ps.turn_client,
            session_viewers=ps.session_viewers,
            session_surface=ps.session_surface,
            **({"session_source": ps.session_source} if callable(getattr(ps, "session_source", None)) else {}),
            **({"in_delegated_child": ps.in_delegated_child} if callable(getattr(ps, "in_delegated_child", None)) else {}),
        )
        self.sent = []
        self.adapter._deliver_in_background = lambda payload, relay=None: self.sent.append(payload)
        self.adapter._report_activity = lambda *args: None

    def test_outside_a_gateway_turn_there_is_no_client_and_nobody_watching(self):
        self.assertIsNone(self.adapter._plugin_session_fact("turn_client"))
        self.assertEqual(self.adapter._plugin_session_fact("session_viewers"), [])
        finish_turn(self.adapter, "20261120_101500_abcdef", "Done.", "cli")
        [payload] = self.sent
        self.assertEqual(payload["hermes"]["source"], self.adapter._creation_source("cli"))
        self.assertNotIn("desktop_active", payload["hermes"], "the relay reads it only on a Desktop reply")


class DesktopActiveTests(PresenceTestCase):
    """``hermes.desktop_active``: the user is at a computer, by ``session_viewers()``."""

    def active(self, **reply):
        return self.reply(**reply)["hermes"].get("desktop_active", "omitted")

    def test_a_computer_counts_while_its_input_is_newer_than_the_threshold(self):
        self.client = DESKTOP
        for idle, expected in [(0, True), (12.5, True), (299.999, True), (300, False), (300.5, False), (86400, False),
                               (None, False), ("12", False), (True, False)]:
            with self.subTest(idle=idle):
                self.viewers = [viewer(idle_seconds=idle)]
                self.assertIs(self.active(), expected)

    def test_only_a_computer_that_can_get_the_users_attention_counts(self):
        self.client = DESKTOP
        for can_alert, expected in [(True, True), (None, True), (False, False)]:
            with self.subTest(can_alert=can_alert):
                self.viewers = [viewer(can_alert=can_alert)]
                self.assertIs(self.active(), expected)

    def test_a_webapp_counts_as_a_computer_and_a_phone_does_not(self):
        for client in [DESKTOP, WEBAPP]:
            for kind, expected in [("desktop", True), ("webapp", True), ("mobile", False), ("tui", False), (None, False)]:
                with self.subTest(client=client["name"], viewer=kind):
                    self.client = client
                    self.viewers = [viewer(kind=kind, basis="page")]
                    self.assertIs(self.active(), expected)

    def test_any_active_computer_among_the_viewers_counts(self):
        self.client = DESKTOP
        self.viewers = [viewer(kind="mobile", idle_seconds=1), viewer(idle_seconds=900), "junk",
                        viewer(kind="webapp", idle_seconds=4, can_alert=None)]
        self.assertIs(self.active(), True)
        self.viewers = self.viewers[:3]
        self.assertIs(self.active(), False)

    def test_a_turn_a_phone_sent_is_not_the_computers_even_while_the_computer_is_active(self):
        self.viewers = [viewer(idle_seconds=2)]
        for client in [IPHONE, ANDROID, None, {"name": "hermes-tui", "kind": "tui"}]:
            with self.subTest(client=client):
                self.client = client
                self.assertEqual(self.active(), "omitted")
        self.policy = None  # A session no client created: "desktop" is the platform's own label.
        self.client = None
        self.assertIs(self.active(), False)

    def test_only_a_reply_labelled_desktop_carries_it(self):
        """The relay reads ``desktop_active`` only beside ``source: desktop``: a phone, CLI or messaging
        turn's reply carries none, to the delivery queue or the relay."""
        privacy = load_privacy()
        privacy._home = lambda: self.home
        privacy.load_key = lambda: bytes(range(32))
        self.viewers = [viewer(idle_seconds=2)]
        turns = [("iphone", IPHONE, "desktop"), ("android", ANDROID, "desktop"), ("no client", None, "desktop"),
                 ("cli", None, "cli"), ("messaging", None, "homeassistant")]
        for name, client, platform in turns:
            with self.subTest(name):
                self.client = client
                if platform != "desktop":
                    self.policy = None
                payload = self.reply(session_id="s1", platform=platform)
                self.assertNotEqual(payload["hermes"]["source"], "desktop")
                self.assertNotIn("desktop_active", payload["hermes"])
                self.policy = types.SimpleNamespace(source="gui", platform="desktop")
        for source in ["ios", "gui", "cli", "telegram", ""]:
            with self.subTest(sealed_source=source):
                protected = privacy.protect({"kind": "reply", "hermes": {"session_id": "s", "source": source,
                                                                         "desktop_active": False}}, "ab" * 32)
                self.assertNotIn("desktop_active", protected["hermes"])
        self.client = DESKTOP
        for viewers, expected in [([viewer()], True), ([], False)]:
            self.viewers = viewers
            payload = self.reply()
            self.assertIs(payload["hermes"]["desktop_active"], expected)
            self.assertIs(privacy.protect(payload, "ab" * 32)["hermes"]["desktop_active"], expected)

    def test_no_viewers_is_not_active(self):
        self.client = DESKTOP
        self.viewers = []
        self.assertIs(self.active(), False)
        self.policy = None
        self.client = None
        self.assertEqual(self.active(session_id="s1", platform="homeassistant"), "omitted")

    def test_unknown_without_the_accessors_or_when_one_fails(self):
        self.client = DESKTOP
        self.viewers = [viewer()]
        for failing in ["turn_client", "session_viewers"]:
            with self.subTest(failing=failing):
                self.failing = {failing}
                with self.assertLogs(self.adapter.logger, "DEBUG") as logs:
                    payload = self.reply()
                self.assertNotIn("desktop_active", payload["hermes"])
                self.assertEqual(payload["hermes"]["source"], "gui", "both facts or neither")
                self.assertEqual(len(logs.records), 1)
        self.failing = set()
        self.viewers = None  # Not an answer the accessor gives.
        self.assertEqual(self.active(), "omitted")
        self.viewers = [viewer()]
        del self.ctx.session_viewers
        self.assertEqual(self.active(), "omitted")
        self.assertEqual(self.reply()["hermes"]["source"], "gui")
        self.ctx.session_viewers = lambda: self.viewers
        del self.ctx.turn_client
        self.assertEqual(self.active(), "omitted")

    def test_an_answer_hermes_does_not_give_means_todays_payload(self):
        """Both facts or neither: a missing, failing or malformed accessor answer sends exactly the push
        a Hermes without the accessors sends."""
        with patch.object(self.ctx, "turn_client", None), patch.object(self.ctx, "session_viewers", None):
            control = self.reply()
        self.assertEqual((control["hermes"]["source"], "desktop_active" in control["hermes"]), ("gui", False))
        clients = [{"kind": []}, {"kind": {}}, {"kind": "desktop", "name": 5}, {"kind": "webapp", "name": []},
                   "desktop", ["desktop"], 1, True]
        for client in clients:
            with self.subTest(client=client):
                self.client, self.viewers = client, [viewer()]
                self.assertEqual(self.reply(), control)
        for viewers in [None, "desktop", {"kind": "desktop"}, 5, viewer()]:
            with self.subTest(viewers=viewers):
                self.client, self.viewers = DESKTOP, viewers
                self.assertEqual(self.reply(), control)
        self.client, self.viewers = DESKTOP, [viewer()]
        for failing in ["turn_client", "session_viewers"]:
            with self.subTest(failing=failing):
                self.failing = {failing}
                self.assertEqual(self.reply(), control)
        self.failing = set()
        for missing in ["turn_client", "session_viewers"]:
            with self.subTest(missing=missing), patch.object(self.ctx, missing, None):
                self.assertEqual(self.reply(), control)
        # A Desktop session labelled "desktop" by its platform on such a Hermes says nothing either.
        self.policy = None
        with patch.object(self.ctx, "turn_client", None), patch.object(self.ctx, "session_viewers", None):
            payload = self.reply()
        self.assertEqual((payload["hermes"]["source"], "desktop_active" in payload["hermes"]), ("desktop", False))

    def test_no_client_and_an_unknown_kind_are_answers(self):
        # Well formed, naming no computer: today's label, and no Desktop reply to say it of.
        self.viewers = [viewer()]
        for client in [None, {}, {"kind": None, "name": None}, {"name": "hermes-cli", "kind": "cli"},
                       {"kind": "Desktop"}]:
            with self.subTest(client=client):
                self.client = client
                payload = self.reply()
                self.assertEqual((payload["hermes"]["source"], "desktop_active" in payload["hermes"]), ("gui", False))
        self.client = {"kind": "mobile", "name": None}
        payload = self.reply()
        self.assertEqual((payload["hermes"]["source"], "desktop_active" in payload["hermes"]), ("mobile", False))

    def test_a_viewer_hermes_would_not_report_is_skipped(self):
        """A malformed viewer does not count, and does not make the rest of the answer unknown."""
        self.client = DESKTOP
        nan, inf = float("nan"), float("inf")
        malformed = [{"kind": {}}, {"kind": []}, {"kind": ["desktop"], "idle_seconds": 1, "can_alert": True},
                     viewer(idle_seconds=-1), viewer(idle_seconds=-0.001), viewer(idle_seconds=nan),
                     viewer(idle_seconds=inf), viewer(idle_seconds=-inf), viewer(idle_seconds=10**400),
                     viewer(idle_seconds=False), viewer(idle_seconds=[1]),
                     viewer(can_alert="false"), viewer(can_alert="true"), viewer(can_alert=0), viewer(can_alert=1),
                     viewer(can_alert=[]), "junk", None, ["desktop", 1]]
        for entry in malformed:
            with self.subTest(viewer=entry):
                self.viewers = [entry]
                payload = self.reply()
                self.assertEqual((payload["hermes"]["source"], payload["hermes"]["desktop_active"]), ("desktop", False))
                self.viewers = [entry, viewer(kind="webapp", idle_seconds=3, can_alert=None)]
                self.assertIs(self.reply()["hermes"]["desktop_active"], True)
                self.viewers = (entry, viewer(idle_seconds=0.0))
                self.assertIs(self.reply()["hermes"]["desktop_active"], True)

    def test_webhook_and_delegated_replies_never_read_the_accessors(self):
        self.client, self.viewers = DESKTOP, [viewer()]
        with patch.object(self.adapter, "_webhook_route_delivers_to_push", lambda: False):
            self.reply("s-hook", "webhook")
            self.reply("s-graph", "msgraph_webhook")
        self.reply("subagent_1", "subagent")
        self.delegated = True
        try:
            self.reply()
        finally:
            self.delegated = False
        self.assertEqual(self.reads, [])
        self.reply()
        self.assertEqual(self.reads, ["turn_client", "session_viewers"])

    def test_a_caller_cannot_send_it(self):
        """Only the reply the turn-end hook builds carries ``desktop_active``; an addressed send's metadata
        never puts it in what ``POST /notify`` carries, sealed or not."""
        relay = self.adapter.PushAdapter(types.SimpleNamespace(
            extra={"relay_url": "https://relay.example", "install_key": "ab" * 32}))
        self.assertEqual((relay.relay_url, relay.install_key), ("https://relay.example", "ab" * 32))
        posted = []
        response = MagicMock(status=200)
        response.__enter__.return_value = response
        response.read.return_value = b'{"status": "sent"}'

        def urlopen(request, timeout):
            posted.append(json.loads(request.data))
            return response

        keys = {"sealed": lambda: bytes(range(32)), "content-free": lambda: None}
        for sealing, load_key in keys.items():
            for kind in ["system", "turn_error", "delivery", "reply"]:
                with self.subTest(sealing=sealing, kind=kind), \
                        patch.object(self.adapter.privacy, "load_key", load_key), \
                        patch.object(self.adapter.privacy, "_home", lambda: self.home), \
                        patch.object(self.adapter.urllib.request, "urlopen", urlopen):
                    posted.clear()
                    metadata = {"kind": kind, "source": "gui", "session_id": "s", "desktop_active": True}
                    asyncio.run(relay.send("chat", "Hello", metadata=metadata))
                    [body] = posted
                    self.assertEqual(body["kind"], kind)
                    self.assertNotIn("desktop_active", body["hermes"])
                    self.assertEqual("sealed" in body["hermes"], sealing == "sealed")
                    self.assertNotIn("desktop_active", self.adapter._build_payload("Hello", metadata=metadata)["hermes"])
        # Sealing shows it only on a reply.
        privacy = load_privacy()
        privacy._home = lambda: self.home
        privacy.load_key = lambda: bytes(range(32))
        for kind in ["system", "turn_error", "delivery", "approval"]:
            protected = privacy.protect({"kind": kind, "hermes": {"session_id": "s", "desktop_active": True}}, "k")
            self.assertNotIn("desktop_active", protected["hermes"], kind)

    def test_the_accessors_are_read_once_at_the_end_of_a_turn_that_notifies(self):
        self.client = DESKTOP
        self.viewers = [viewer()]
        self.adapter._on_turn_reply(session_id="local-1", turn_id="turn-1", assistant_response="Done.", platform="desktop")
        self.adapter._on_stream_start(session_id="local-1", turn_id="turn-1")
        self.assertEqual(self.reads, [])
        finish_turn(self.adapter, "local-1", None, "desktop", failed=True)
        self.assertEqual(self.reads, [])
        self.reply()
        self.assertEqual(self.reads, ["turn_client", "session_viewers"])

    def test_other_pushes_never_carry_it(self):
        self.client = DESKTOP
        self.viewers = [viewer()]
        child = self.reply("subagent_1", "subagent")
        self.assertNotIn("desktop_active", child["hermes"])
        self.delegated = True
        try:
            self.assertNotIn("desktop_active", self.reply()["hermes"])
        finally:
            self.delegated = False
        with patch.object(self.adapter, "_webhook_route_delivers_to_push", lambda: False):
            for platform in ["webhook", "msgraph_webhook"]:
                self.assertNotIn("desktop_active", self.reply("s-hook", platform)["hermes"])
        before = list(self.sent)
        self.adapter._on_human_input_request(kind="approval", request_id="r1", session_id="local-1",
                                             session_key="route", platform="local", prompt="rm -rf build")
        self.adapter._on_human_input_request(kind="clarify", request_id="r2", session_id="local-1",
                                             session_key="route", platform="local", prompt="Which?")
        payload = self.adapter._server_request_payload("sudo", "srq-1", {}, {"session_id": "local-1", "source": "gui"})
        self.ctx.current_cron_execution = lambda: types.SimpleNamespace(job_id="job-1", job_name="Morning brief")
        self.ctx.cron_delivery_platforms = lambda job_id=None: ()
        redactor = types.ModuleType("agent.redact")
        redactor.redact_sensitive_text = lambda text, *, force: text
        with patch.dict(sys.modules, {"agent": types.ModuleType("agent"), "agent.redact": redactor}):
            finish_turn(self.adapter, "local-cron", "Brief ready.", "cron")
        delivery = self.adapter._build_payload("Gateway online", metadata={"kind": "system"})
        finish_turn(self.adapter, "local-1", "Partial.", "desktop", failed=True)
        others = self.sent[len(before):] + [payload, delivery]
        self.assertEqual([p["kind"] for p in others], ["approval", "clarify", "delivery", "sudo", "system"])
        for other in others:
            self.assertNotIn("desktop_active", other["hermes"])

    def test_the_relay_sees_it_on_the_reply(self):
        privacy = load_privacy()
        privacy._home = lambda: self.home
        privacy.load_key = lambda: bytes(range(32))
        self.client = DESKTOP
        for viewers, expected in [([viewer()], True), ([], False)]:
            self.viewers = viewers
            protected = privacy.protect(self.reply(), "ab" * 32)
            self.assertIs(protected["hermes"]["desktop_active"], expected)
            self.assertEqual(sorted(protected["hermes"]), ["desktop_active", "event_id", "sealed", "session_id", "source"])
        # Only a boolean shows; anything else stays behind, as other metadata does.
        for value in ["true", 1, None]:
            protected = privacy.protect({"kind": "reply", "hermes": {"session_id": "s", "desktop_active": value}}, "k")
            self.assertNotIn("desktop_active", protected["hermes"])

    def test_a_sealed_reply_shows_it_beside_the_source_and_seals_the_same(self):
        """The relay reads ``desktop_active`` from the unsealed routing fields, next to ``source``; the sealed
        preview the device opens and the event id the relay dedupes by are those of the same reply without it."""
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        privacy = load_privacy()
        privacy._home = lambda: self.home
        privacy.load_key = lambda: bytes(range(32))
        install_key = "ab" * 32
        aad = ("hermes-preview-v1:" + hashlib.sha256(install_key.encode()).hexdigest()).encode()

        def opened(protected):
            box = base64.b64decode(protected["hermes"]["sealed"]["data"])
            clear = json.loads(AESGCM(bytes(range(32))).decrypt(box[:12], box[12:], aad))
            clear.pop("issued_at")
            return clear

        self.client = DESKTOP
        self.viewers = [viewer()]
        reply = self.reply()
        without = {**reply, "hermes": {k: v for k, v in reply["hermes"].items() if k != "desktop_active"}}
        sealed, control = privacy.protect(reply, install_key), privacy.protect(without, install_key)
        self.assertEqual(list(sealed["hermes"]), ["session_id", "source", "desktop_active", "sealed", "event_id"])
        self.assertEqual((sealed["hermes"]["source"], sealed["hermes"]["desktop_active"]), ("desktop", True))
        self.assertEqual({k: v for k, v in sealed["hermes"].items() if k not in ("desktop_active", "sealed")},
                         {k: v for k, v in control["hermes"].items() if k != "sealed"})
        self.assertEqual({k: v for k, v in sealed.items() if k != "hermes"}, {k: v for k, v in control.items() if k != "hermes"})
        self.assertEqual(opened(sealed), opened(control))
        self.assertNotIn("desktop_active", opened(sealed)["hermes"])

    def test_one_reply_before_and_after(self):
        """The body ``POST /notify`` carries for a Desktop turn while the user is at the computer, sealed as
        ``_post_notify`` sends it: on a Hermes without the accessors, and on one with them."""
        privacy = load_privacy()
        privacy._home = lambda: self.home
        privacy.load_key = lambda: bytes(range(32))
        self.client = DESKTOP
        self.viewers = [viewer(idle_seconds=8.0)]
        with patch.object(self.ctx, "turn_client", None), patch.object(self.ctx, "session_viewers", None):
            before = self.reply()
        after = self.reply()
        sealed = [privacy.protect(push, "ab" * 32) for push in (before, after)]
        for push in sealed:
            push["hermes"].update(sealed={"v": 1, "data": "…"}, event_id="…")
        self.assertEqual([push["hermes"] for push in sealed], [
            {"session_id": "local-1", "source": "gui", "sealed": {"v": 1, "data": "…"}, "event_id": "…"},
            {"session_id": "local-1", "source": "desktop", "desktop_active": True, "sealed": {"v": 1, "data": "…"},
             "event_id": "…"}])
        self.assertEqual({k: v for k, v in after.items() if k != "hermes"}, {k: v for k, v in before.items() if k != "hermes"})


class PresenceSettingsTests(PresenceTestCase):
    """``active_seconds`` and ``quiet_while_active``, from the push platform's config (its ``extra``)."""

    def configure(self, **extra):
        self.adapter.PushAdapter(types.SimpleNamespace(extra=extra))

    def test_active_seconds_is_a_positive_number_else_300(self):
        cases = [({}, 300), ({"active_seconds": 120}, 120), ({"active_seconds": 45.5}, 45.5),
                 ({"active_seconds": "90"}, 90), ({"active_seconds": 0}, 300), ({"active_seconds": -5}, 300),
                 ({"active_seconds": "soon"}, 300), ({"active_seconds": None}, 300), ({"active_seconds": True}, 300),
                 ({"active_seconds": float("inf")}, 300), ({"active_seconds": float("nan")}, 300),
                 ({"active_seconds": [60]}, 300), ({"active_seconds": 10**400}, 300), ({"active_seconds": "1e400"}, 300),
                 ({"active_seconds": {"seconds": 60}}, 300)]
        for extra, seconds in cases:
            with self.subTest(extra=extra):
                self.configure(**extra)
                self.assertEqual(self.adapter._active_seconds(), seconds)

    def test_the_threshold_decides_desktop_active(self):
        self.client = DESKTOP
        self.configure(active_seconds=60)
        for idle, expected in [(59.9, True), (60, False), (100, False)]:
            with self.subTest(idle=idle):
                self.viewers = [viewer(idle_seconds=idle)]
                self.assertIs(self.reply()["hermes"]["desktop_active"], expected)
        self.configure(active_seconds=600)
        self.viewers = [viewer(idle_seconds=500)]
        self.assertIs(self.reply()["hermes"]["desktop_active"], True)

    def test_quiet_while_active_is_off_unless_switched_on(self):
        for value, quiet in [(None, False), (False, False), (True, True), ("true", True), (" Yes ", True),
                             ("on", True), ("1", True), ("false", False), ("no", False), (1, True), (0, False),
                             (2, False), (-1, False), (1.0, False), ("0", False), ("2", False),
                             ([True], False), ({"on": True}, False), (10**400, False)]:
            with self.subTest(value=value):
                self.configure(**({} if value is None else {"quiet_while_active": value}))
                self.assertIs(self.adapter._quiet_while_active(), quiet)

    def test_an_unusable_active_seconds_still_lets_the_reply_go(self):
        self.client, self.viewers = DESKTOP, [viewer(idle_seconds=12)]
        for value in [10**400, "1e400", -1, "soon", True]:
            with self.subTest(active_seconds=value):
                self.configure(active_seconds=value, quiet_while_active=False)
                payload = self.reply()
                self.assertEqual((payload["hermes"]["source"], payload["hermes"]["desktop_active"]), ("desktop", True))
                self.viewers = [viewer(idle_seconds=300)]
                self.assertIs(self.reply()["hermes"]["desktop_active"], False, "the default, 300")
                self.viewers = [viewer(idle_seconds=12)]

    def test_an_unusable_active_seconds_is_warned_once_per_home_without_its_value(self):
        self.client, self.viewers = DESKTOP, [viewer(idle_seconds=5)]
        for value in [None, 120, "90"]:  # Unset or usable: no warning.
            with self.subTest(value=value), self.assertNoLogs(self.adapter.logger, "WARNING"):
                self.configure(active_seconds=value)
                self.reply()
        for name, value in [("work", "s3cr3t-token"), ("play", -4242)]:
            with self.subTest(name):
                home = self.home / "profiles" / name
                self.constants.get_hermes_home = lambda home=home: home
                self.configure(active_seconds=value)
                with self.assertLogs(self.adapter.logger, "WARNING") as logs:
                    self.reply()
                    self.configure(active_seconds="1e400")
                    self.reply()
                [record] = logs.records
                self.assertEqual(record.levelname, "WARNING")
                message = record.getMessage().replace(str(self.home), "")
                self.assertIn("active_seconds", message)
                for shown in [str(value), "1e400"]:
                    self.assertNotIn(shown, message)
        self.adapter._on_unload()
        self.configure(active_seconds="soon")
        with self.assertLogs(self.adapter.logger, "WARNING"):
            self.reply()  # Unloading forgets what was warned.

    def test_settings_belong_to_the_home_whose_adapter_was_configured(self):
        other = self.home / "profiles" / "work"
        self.configure(active_seconds=30, quiet_while_active=True)
        self.assertEqual((self.adapter._active_seconds(), self.adapter._quiet_while_active()), (30, True))
        self.constants.get_hermes_home = lambda: other
        self.assertEqual((self.adapter._active_seconds(), self.adapter._quiet_while_active()), (300, False),
                         "a home that configured nothing uses the defaults")
        self.configure(active_seconds=900)
        self.assertEqual((self.adapter._active_seconds(), self.adapter._quiet_while_active()), (900, False))
        self.constants.get_hermes_home = lambda: self.home
        self.assertEqual((self.adapter._active_seconds(), self.adapter._quiet_while_active()), (30, True))
        self.constants.get_hermes_home = lambda: self.home / "profiles" / "unconfigured"
        self.assertEqual((self.adapter._active_seconds(), self.adapter._quiet_while_active()), (300, False))

    def test_a_home_that_configured_nothing_never_borrows_another_homes_quiet(self):
        self.client, self.viewers = DESKTOP, [viewer(idle_seconds=5)]
        self.configure(quiet_while_active=True)
        before = len(self.sent)
        finish_turn(self.adapter, "local-1", "Done.", "desktop")
        self.assertEqual(self.sent[before:], [], "home A asked for quiet")
        self.constants.get_hermes_home = lambda: self.home / "profiles" / "new"
        payload = self.reply()
        self.assertEqual((payload["kind"], payload["hermes"]["desktop_active"], payload["hermes"]["profile"]),
                         ("reply", True, "new"))

    def test_a_home_without_settings_while_another_has_some_is_logged_once(self):
        self.client, self.viewers = DESKTOP, [viewer(idle_seconds=5000)]
        with self.assertNoLogs(self.adapter.logger, "DEBUG"):
            self.reply()  # No home stored any: nothing to fall back from.
        self.configure(active_seconds=4242, quiet_while_active="TRUE")
        with self.assertNoLogs(self.adapter.logger, "DEBUG"):
            self.reply()  # The configured home.
        for name in ["work", "play"]:
            with self.subTest(name):
                self.constants.get_hermes_home = lambda name=name: self.home / "profiles" / name
                with self.assertLogs(self.adapter.logger, "DEBUG") as logs:
                    self.reply()
                    self.reply()
                [record] = logs.records
                self.assertEqual(record.levelname, "DEBUG")
                message = record.getMessage()
                self.assertIn(name, message)
                for value in ["4242", "TRUE"]:  # No setting's value.
                    self.assertNotIn(value, message.replace(str(self.home), ""))
        self.constants.get_hermes_home = lambda: self.home / "profiles" / "fresh"
        self.configure()  # A home whose adapter set nothing has settings: the defaults, with no log.
        with self.assertNoLogs(self.adapter.logger, "DEBUG"):
            self.reply()
        self.adapter._on_unload()
        self.configure(active_seconds=4242)
        self.constants.get_hermes_home = lambda: self.home / "profiles" / "work"
        with self.assertLogs(self.adapter.logger, "DEBUG"):
            self.reply()  # Unloading forgets what was logged with the settings.

    def test_an_unknown_home_shares_no_settings(self):
        self.client, self.viewers = DESKTOP, [viewer(idle_seconds=40)]
        self.configure(active_seconds=30, quiet_while_active=True)

        def failing():
            raise RuntimeError("no home")

        for unknown in ["raises", "unavailable"]:
            with self.subTest(unknown):
                if unknown == "raises":
                    self.constants.get_hermes_home = failing
                else:
                    del self.constants.get_hermes_home
                self.assertEqual((self.adapter._active_seconds(), self.adapter._quiet_while_active()), (300, False))
                self.configure(active_seconds=60, quiet_while_active=True)  # Remembers nothing.
                self.assertEqual((self.adapter._active_seconds(), self.adapter._quiet_while_active()), (300, False))
                if unknown == "unavailable":  # The hook itself, where Hermes names no home.
                    self.viewers = [viewer(idle_seconds=5)]
                    self.assertIs(self.reply()["hermes"]["desktop_active"], True)
                self.constants.get_hermes_home = lambda: self.home
                self.assertEqual((self.adapter._active_seconds(), self.adapter._quiet_while_active()), (30, True))
                self.assertEqual(list(self.adapter._platform_settings), [str(self.home)])

    def test_unloading_forgets_the_settings(self):
        self.client, self.viewers = DESKTOP, [viewer(idle_seconds=5)]
        self.configure(active_seconds=30, quiet_while_active=True)
        self.adapter._on_unload()
        self.assertEqual(self.adapter._platform_settings, {})
        self.assertEqual((self.adapter._active_seconds(), self.adapter._quiet_while_active()), (300, False))
        self.assertIs(self.reply()["hermes"]["desktop_active"], True)
        self.configure(quiet_while_active=True)  # A gateway that loads the plugin again configures it again.
        self.assertIs(self.adapter._quiet_while_active(), True)

    def test_the_settings_keep_no_secret(self):
        self.configure(relay_url="https://relay.example", install_key="secret-key", quiet_while_active=True)
        self.assertNotIn("secret-key", repr(self.adapter._platform_settings))


class QuietWhileActiveTests(PresenceTestCase):
    """``quiet_while_active: true``: no reply push for a Desktop turn while the user is active at a
    computer; everything else about the turn and every other push stays as it is."""

    def setUp(self):
        super().setUp()
        self.client = DESKTOP
        self.viewers = [viewer(idle_seconds=5)]
        self.adapter.PushAdapter(types.SimpleNamespace(extra={"quiet_while_active": True}))

    def end(self, **end):
        before = len(self.sent), len(self.reported)
        finish_turn(self.adapter, "local-1", "Done.", "desktop", **end)
        return self.sent[before[0]:], self.reported[before[1]:]

    def test_only_the_reply_the_user_is_at_the_computer_to_see_stays_quiet(self):
        pushes, reported = self.end()
        self.assertEqual(pushes, [])
        # The turn's end is reported and its reply taken, as when the push goes.
        self.assertEqual(reported, [("end", "local-1", "turn-1")])
        self.assertEqual(self.adapter._replies, {})
        self.adapter.PushAdapter(types.SimpleNamespace(extra={}))
        pushes, sent_reported = self.end()
        self.assertEqual([p["hermes"]["desktop_active"] for p in pushes], [True])
        self.assertEqual(sent_reported, reported)
        self.assertEqual(self.adapter._replies, {})

    def test_replies_the_user_is_not_at_the_computer_for_still_go(self):
        cases = [("idle", lambda: setattr(self, "viewers", [viewer(idle_seconds=301)])),
                 ("phone turn", lambda: setattr(self, "client", IPHONE)),
                 ("no viewers", lambda: setattr(self, "viewers", [])),
                 ("accessor failed", lambda: self.failing.add("session_viewers")),
                 ("client of another shape", lambda: setattr(self, "client", {"kind": []})),
                 ("viewer of another shape", lambda: setattr(self, "viewers", [{"kind": {}}])),
                 ("negative idle", lambda: setattr(self, "viewers", [viewer(idle_seconds=-1)])),
                 ("NaN idle", lambda: setattr(self, "viewers", [viewer(idle_seconds=float("nan"))])),
                 ("can_alert a string", lambda: setattr(self, "viewers", [viewer(idle_seconds=5, can_alert="false")]))]
        for name, change in cases:
            with self.subTest(name):
                self.client, self.viewers, self.failing = DESKTOP, [viewer(idle_seconds=5)], set()
                change()
                pushes, _ = self.end()
                self.assertEqual([p["kind"] for p in pushes], ["reply"])
        self.failing = set()
        self.client, self.viewers = DESKTOP, [viewer(idle_seconds=5)]
        with patch.object(self.ctx, "turn_client", None), patch.object(self.ctx, "session_viewers", None):
            pushes, _ = self.end()
        self.assertEqual([("desktop_active" in p["hermes"]) for p in pushes], [False])

    def test_waits_on_the_person_and_other_pushes_are_never_quieted(self):
        self.adapter._on_human_input_request(kind="approval", request_id="r1", session_id="local-1",
                                             session_key="route", platform="local", prompt="rm -rf build")
        self.adapter._on_human_input_request(kind="clarify", request_id="r2", session_id="local-1",
                                             session_key="route", platform="local", prompt="Which?")
        self.assertEqual(self.end(failed=True)[0], [])
        self.reply("subagent_1", "subagent")  # A sub-agent's completion.
        self.assertEqual([p["kind"] for p in self.sent], ["approval", "clarify", "reply"])


if __name__ == "__main__":
    unittest.main()
