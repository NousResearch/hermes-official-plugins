"""Waits on a person in a gateway session, as the gateway reports them (``on_session_prompt`` /
``on_session_prompt_settled``), with Hermes' modules stubbed and the contract's keyword arguments."""
import json
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from push_plugin import PUSH, load_push_adapter

RELAY = {"HERMES_MOBILE_PUSH_RELAY_URL": "https://relay.example", "HERMES_MOBILE_PUSH_INSTALL_KEY": "install-key"}
STORED = "20261102_101500_abcdef"
LIVE = "20261102_100000_live01"
ROUTE = "agent:main:local:dm:" + LIVE


class GatewayPromptTestCase(unittest.TestCase):
    """The plugin as register() hooks it up, in a profile's home, on a Hermes with the session
    prompt hooks; the session context is the gateway's for an app session (platform ``local``)."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name) / "profiles" / "research"
        self.home.mkdir(parents=True)
        self.env = {"HERMES_SESSION_PLATFORM": "local", "HERMES_SESSION_ID": STORED,
                    "HERMES_SESSION_KEY": ROUTE, "HERMES_SESSION_PROFILE": "research"}
        context = types.ModuleType("gateway.session_context")
        context.get_session_env = lambda name, default="": self.env.get(name, default)
        redactor = types.ModuleType("agent.redact")
        redactor.redact_sensitive_text = lambda text, *, force: text.replace("sk-live-123", "[REDACTED]")
        self.adapter, constants = load_push_adapter(self.home)
        modules = patch.dict(sys.modules, {
            "hermes_constants": constants, "gateway": types.ModuleType("gateway"),
            "gateway.session_context": context,
            "agent": types.ModuleType("agent"), "agent.redact": redactor,
            "hermes_state": None,
        })
        modules.start()
        self.addCleanup(modules.stop)
        environment = patch.dict(os.environ, RELAY)
        environment.start()
        self.addCleanup(environment.stop)
        self.sent = []
        self.reported = []
        for name, value in {"_deliver_in_background": lambda payload, relay=None: self.sent.append((payload, relay)),
                            "_report_activity": lambda *args: self.reported.append(args),
                            # The Live Activity's own reporter posts to the relay; not under test here.
                            "_activity_begin": lambda fields: None,
                            "_HUMAN_INPUT_HOOKS": True, "_SESSION_PROMPT_HOOKS": True}.items():
            patcher = patch.object(self.adapter, name, value, create=True)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.hooks = {}
        ctx = types.SimpleNamespace(register_platform=lambda **kwargs: None,
                                    on_unload=lambda callback: self.hooks.setdefault("unload", callback),
                                    register_hook=lambda name, callback: self.hooks.setdefault(name, callback),
                                    session_source=lambda: "gui",
                                    session_surface=lambda: "desktop",
                                    in_delegated_child=lambda include_subprocesses=False: False)
        self.adapter.register(ctx)

    def fire(self, name, **kwargs):
        self.assertIn(name, self.hooks, f"the plugin does not listen to {name}")
        return self.hooks[name](**kwargs)

    def turn(self, session_id=STORED, platform="desktop"):
        """``pre_api_request``: the turn's LLM call starts, in the turn's own session context."""
        self.fire("pre_api_request", session_id=session_id, turn_id="turn-1", platform=platform)

    def prompt(self, kind, prompt_id, params, session_id=LIVE, execution_generation=None):
        """``on_session_prompt``, as the gateway fires it: off the turn, with none of its context."""
        saved, self.env = self.env, {}
        try:
            self.fire("on_session_prompt", session_id=session_id, kind=kind, prompt_id=prompt_id,
                      execution_generation=execution_generation, params=params)
        finally:
            self.env = saved
        return [payload for payload, _ in self.sent]

    def settle(self, kind, prompt_id, session_id=LIVE, reason="settled"):
        self.fire("on_session_prompt_settled", session_id=session_id, kind=kind, prompt_id=prompt_id,
                  execution_generation=None, reason=reason)


APPROVAL = {"kind": "approval", "prompt_id": "appr-1", "execution_generation": 3,
            "command": "rm -rf build", "description": "Delete the build", "choices": ["once", "deny", "session"]}
CLARIFY = {"kind": "clarify", "prompt_id": "clar-1", "execution_generation": 3, "question": "Which format?",
           "choices": ["PDF", "Markdown"], "multi_select": True}
ORIGIN = {"profile": "research", "source": "gui", "session_id": STORED}


class SessionPromptNotificationTests(GatewayPromptTestCase):
    def test_an_approval_is_answerable_by_its_prompt_id(self):
        self.turn()
        [push] = self.prompt("approval", "appr-1", APPROVAL, execution_generation=3)
        self.assertEqual(push, {
            "title": "Approval needed", "subtitle": "Waiting on you", "kind": "approval", "thread_id": STORED,
            "body": "Reason: Delete the build\nCommand: rm -rf build",
            "hermes": {**ORIGIN, "kind": "approval", "request_id": "appr-1"}})

    def test_a_question_is_answerable_by_text_with_its_prompt_id(self):
        self.turn()
        [push] = self.prompt("clarify", "clar-1", CLARIFY, execution_generation=3)
        self.assertEqual((push["kind"], push["title"], push["subtitle"], push["thread_id"]),
                         ("clarify", "Question", "Waiting on you", STORED))
        self.assertEqual(push["body"], "Which format?\nOptions: PDF, Markdown\nYou can choose more than one.")
        self.assertEqual(push["hermes"], {**ORIGIN, "kind": "clarify", "answer_mode": "text", "request_id": "clar-1"})

    def test_a_question_batch_frame_addresses_its_first_unanswered_question(self):
        self.turn()
        [push] = self.prompt("clarify", "srq-0123456789ab", {"session_id": LIVE, "questions": [
            {"qid": "q1", "question": "First?"}, {"qid": "q2", "question": "Second?", "choices": ["a", "b"]}],
            "answers": {"q1": "done"}})
        self.assertEqual(push["body"], "Second?\nOptions: a, b")
        self.assertEqual(push["hermes"], {**ORIGIN, "kind": "clarify", "question_id": "q2", "answer_mode": "text",
                                          "request_id": "srq-0123456789ab"})

    def test_password_secret_and_vault_frames_notify_as_the_request_watch_does(self):
        self.turn()
        cases = [
            ("sudo", {"command": "sudo apt install ffmpeg"}, "sudo", "Password needed",
             "Hermes needs your administrator password to continue.\nCommand: sudo apt install ffmpeg"),
            ("secret", {"env_var": "OPENAI_API_KEY", "prompt": "Enter your OpenAI API key"}, "secret",
             "Secret needed", "Enter your OpenAI API key"),
            ("vault.unlock_prompt", {"backend": "bitwarden", "display_name": "Bitwarden"}, "input", "Input needed",
             "Unlock Bitwarden to continue."),
            ("vault.code", {"site": "github.com", "hint": "Check the authenticator"}, "input", "Input needed",
             "Hermes needs a one-time code for github.com.\nCheck the authenticator"),
            ("vault.save_login", {"site": "example.com"}, "input", "Input needed", "Save a login for example.com?"),
        ]
        for number, (method, params, kind, title, body) in enumerate(cases):
            with self.subTest(method=method):
                prompt_id = f"srq-00000000000{number}"
                push = self.prompt(method, prompt_id, {"session_id": LIVE, **params})[-1]
                self.assertEqual((push["kind"], push["title"], push["body"], push["thread_id"]),
                                 (kind, title, body, STORED))
                self.assertEqual(push["hermes"], {**ORIGIN, "kind": kind, "request_id": prompt_id})
        self.assertEqual(len(self.sent), len(cases))

    def test_prompts_a_window_answers_send_nothing(self):
        self.turn()
        for kind in ["theme.apply", "tour", "preview.act", "window.read", "terminal.read", "input", ""]:
            self.prompt(kind, "srq-window00000", {"session_id": LIVE})
        self.prompt("approval", "", APPROVAL)
        self.assertEqual((self.sent, self.reported), ([], []))

    def test_a_prompt_delivered_twice_notifies_once(self):
        self.turn()
        self.prompt("approval", "appr-1", APPROVAL)
        self.prompt("approval", "appr-1", APPROVAL)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.reported, [("attention", STORED, True)])

    def test_the_push_uses_the_relay_of_the_turn_it_waits_in(self):
        self.turn()
        with patch.dict(os.environ, {"HERMES_MOBILE_PUSH_RELAY_URL": "https://other.example"}):
            self.prompt("approval", "appr-1", APPROVAL)
        self.assertEqual(self.sent[0][1], ("https://relay.example", "install-key"))

    def test_credentials_in_a_command_are_masked_and_routing_stays_sealed(self):
        self.turn()
        [push] = self.prompt("approval", "appr-private", {**APPROVAL, "command": "curl -H 'X: sk-live-123'",
                                                          "description": ""})
        self.assertEqual(push["body"], "curl -H 'X: [REDACTED]'")
        [_, sudo] = self.prompt("sudo", "srq-sudo0000000", {"command": "sudo env TOKEN=sk-live-123 make"})
        self.assertEqual(sudo["body"].splitlines()[1], "Command: sudo env TOKEN=[REDACTED] make")
        public = json.dumps(self.adapter._private_payload(push, "ab" * 32))
        for private in ["appr-private", "research", "curl", "REDACTED"]:
            self.assertNotIn(private, public)

    def test_a_hook_on_another_thread_is_addressed_from_the_turn(self):
        self.turn()
        worker = threading.Thread(target=self.prompt, args=("clarify", "clar-1", CLARIFY))
        worker.start()
        worker.join()
        self.assertEqual([p["hermes"]["session_id"] for p, _ in self.sent], [STORED])


class SessionLookupTests(GatewayPromptTestCase):
    """The hook names the gateway's live session; the push names the turn's stored one."""

    def test_the_stored_id_finds_the_turn(self):
        self.env["HERMES_SESSION_KEY"] = "agent:main:local:dm:somewhere-else"
        self.turn()
        self.prompt("approval", "appr-1", APPROVAL, session_id=STORED)
        self.assertEqual(self.sent[0][0]["hermes"]["session_id"], STORED)
        self.assertEqual(self.sent[0][1], ("https://relay.example", "install-key"))

    def test_after_compression_every_id_finds_the_rotated_turn_until_it_ends(self):
        self.turn(STORED)
        rotated = "20261102_103000_rotate"
        self.turn(rotated)  # Compression gave the turn a new stored id; the gateway session keeps its own.
        for number, session_id in enumerate([STORED, rotated, LIVE]):
            self.prompt("approval", f"appr-{number}", APPROVAL, session_id=session_id)
        self.assertEqual([p["hermes"]["session_id"] for p, _ in self.sent], [rotated] * 3)
        self.assertEqual([p["thread_id"] for p, _ in self.sent], [rotated] * 3)
        self.assertEqual(self.reported, [("attention", rotated, True)] * 3)
        self.fire("on_session_end", session_id=rotated, turn_id="turn-1", completed=False, platform="desktop")
        self.prompt("approval", "appr-after", APPROVAL, session_id=STORED)
        self.assertEqual(self.sent[-1][0]["hermes"]["session_id"], STORED)
        self.assertNotIn("source", self.sent[-1][0]["hermes"], "addressed from the hook, not the ended turn")

    def test_a_miss_is_still_announced_from_the_hooks_own_session(self):
        with patch.dict(os.environ, {"HERMES_MOBILE_PUSH_RELAY_URL": "https://now.example"}):
            [push] = self.prompt("secret", "srq-secret00000", {"prompt": "API key"}, session_id="unknown-1")
        self.assertEqual(push["hermes"], {"profile": "research", "session_id": "unknown-1", "kind": "secret",
                                          "request_id": "srq-secret00000"})
        self.assertEqual(push["thread_id"], "unknown-1")
        self.assertEqual(self.sent[0][1], ("https://now.example", "install-key"))
        self.assertEqual(self.reported, [("attention", "unknown-1", True)])

    def test_an_ended_turn_is_forgotten(self):
        self.turn()
        self.fire("on_session_end", session_id=STORED, turn_id="turn-1", completed=False, platform="desktop")
        self.prompt("approval", "appr-1", APPROVAL, session_id=LIVE)
        self.assertEqual(self.sent[0][0]["hermes"]["session_id"], LIVE)

    def test_a_turn_that_ends_under_another_id_is_forgotten_by_its_session_key(self):
        self.turn()
        self.fire("on_session_end", session_id="20261102_110000_unseen", turn_id="turn-1", completed=False,
                  platform="desktop")
        self.prompt("approval", "appr-1", APPROVAL, session_id=STORED)
        self.assertNotIn("source", self.sent[0][0]["hermes"], "addressed from the hook, not the ended turn")

    def test_unloading_forgets_every_turn(self):
        self.turn()
        self.hooks["unload"]()
        self.prompt("approval", "appr-1", APPROVAL, session_id=LIVE)
        self.assertEqual(self.sent[0][0]["hermes"]["session_id"], LIVE)

    def test_without_the_prompt_hooks_a_turn_is_not_noted(self):
        self.adapter._SESSION_PROMPT_HOOKS = False
        self.turn()
        self.adapter._SESSION_PROMPT_HOOKS = True
        self.prompt("approval", "appr-1", APPROVAL, session_id=LIVE)
        self.assertEqual(self.sent[0][0]["hermes"]["session_id"], LIVE)

    def test_a_stale_turn_is_forgotten(self):
        self.turn()
        later = time.monotonic() + self.adapter.REQUEST_WATCH_TTL_SECONDS + 1
        with patch.object(self.adapter.time, "monotonic", lambda: later):
            self.prompt("approval", "appr-1", APPROVAL, session_id=LIVE)
        self.assertEqual(self.sent[0][0]["hermes"]["session_id"], LIVE)

    def note_turn(self, stored, live, now):
        """A turn noted at *now*, whose end is never reported."""
        saved = self.env["HERMES_SESSION_KEY"]
        self.env["HERMES_SESSION_KEY"] = "agent:main:local:dm:" + live
        try:
            with patch.object(self.adapter.time, "monotonic", lambda: now):
                self.turn(stored)
        finally:
            self.env["HERMES_SESSION_KEY"] = saved

    def test_abandoned_turns_are_forgotten_after_the_ttl_without_a_lookup(self):
        start = time.monotonic()
        for number in range(100):
            self.note_turn(f"stored-{number}", f"live-{number}", start)
        # A turn that a compression renamed: its old and new stored ids and its live id.
        self.note_turn("stored-old", "live-renamed", start)
        self.note_turn("stored-renamed", "live-renamed", start)
        self.note_turn(STORED, LIVE, start + self.adapter.REQUEST_WATCH_TTL_SECONDS + 1)
        self.assertEqual(set(self.adapter._prompt_watches), {STORED, LIVE})

    def test_the_noted_turns_are_bounded(self):
        limit = 256  # Turns noted at once.
        start = time.monotonic()
        for number in range(limit + 40):
            self.note_turn(f"stored-{number}", f"live-{number}", start + number)
        self.assertEqual(len(self.adapter._prompt_watches), 2 * limit)
        # The least recently noted go with every id they were noted under; the latest stay.
        for number in [0, 39]:
            self.assertNotIn(f"stored-{number}", self.adapter._prompt_watches)
            self.assertNotIn(f"live-{number}", self.adapter._prompt_watches)
        with patch.object(self.adapter.time, "monotonic", lambda: start + limit + 40):
            self.prompt("approval", "appr-1", APPROVAL, session_id="live-40")
        self.assertEqual(self.sent[0][0]["hermes"], {**ORIGIN, "session_id": "stored-40", "kind": "approval",
                                                      "request_id": "appr-1"})

    def test_a_running_turn_survives_the_sweep(self):
        start = time.monotonic()
        ttl = self.adapter.REQUEST_WATCH_TTL_SECONDS
        self.note_turn(STORED, LIVE, start)
        self.note_turn("stored-abandoned", "live-abandoned", start)
        self.note_turn(STORED, LIVE, start + ttl - 10)  # Its next LLM call notes it again.
        self.note_turn("stored-new", "live-new", start + ttl + 5)
        self.assertEqual(set(self.adapter._prompt_watches), {STORED, LIVE, "stored-new", "live-new"})
        with patch.object(self.adapter.time, "monotonic", lambda: start + ttl + 5):
            self.prompt("approval", "appr-1", APPROVAL, session_id=LIVE)
        self.assertEqual(self.sent[0][0]["hermes"], {**ORIGIN, "kind": "approval", "request_id": "appr-1"})

    def test_a_subagent_neither_replaces_nor_ends_its_parents_turn(self):
        self.turn()
        self.turn("subagent_child", platform="subagent")
        self.fire("on_session_end", session_id="subagent_child", turn_id="turn-1", completed=True,
                  platform="subagent")
        self.prompt("approval", "appr-1", APPROVAL, session_id=LIVE)
        self.assertEqual(self.sent[0][0]["hermes"]["session_id"], STORED)


class SessionPromptActivityTests(GatewayPromptTestCase):
    def test_the_turn_waits_until_its_last_prompt_settles(self):
        self.turn()
        self.prompt("approval", "appr-1", APPROVAL)
        self.prompt("clarify", "clar-1", CLARIFY)
        self.settle("approval", "appr-1")
        self.assertEqual(self.reported, [("attention", STORED, True), ("attention", STORED, True)])
        self.settle("clarify", "clar-1")
        self.settle("clarify", "clar-1")
        self.assertEqual(self.reported[2:], [("attention", STORED, False)])

    def test_a_prompt_in_another_session_does_not_hold_this_one(self):
        self.turn()
        self.prompt("approval", "appr-1", APPROVAL)
        self.prompt("approval", "appr-other", APPROVAL, session_id="other-session")
        self.settle("approval", "appr-1")
        self.assertEqual(self.reported[-1], ("attention", STORED, False))

    def test_a_settle_without_an_announced_prompt_changes_nothing(self):
        self.turn()
        self.settle("approval", "never-opened", reason="timeout")
        self.prompt("tour", "srq-tour0000000", {})
        self.settle("tour", "srq-tour0000000", reason="resolved")
        self.assertEqual(self.reported, [])

    def test_prompts_of_one_turn_under_different_ids_hold_it_until_the_last_settles(self):
        self.turn()
        self.prompt("approval", "appr-1", APPROVAL, session_id=STORED)
        self.prompt("secret", "srq-secret00000", {"prompt": "API key"}, session_id=LIVE)
        self.settle("approval", "appr-1", session_id=STORED)
        self.assertEqual(self.reported, [("attention", STORED, True)] * 2)
        self.settle("secret", "srq-secret00000", session_id=LIVE, reason="resolved")
        self.assertEqual(self.reported[2:], [("attention", STORED, False)])

    def test_a_prompt_whose_settle_never_came_does_not_hold_the_next_turn(self):
        for forget in ["end", "unload"]:
            with self.subTest(forget=forget):
                self.reported.clear()
                self.turn()
                self.prompt("approval", f"appr-lost-{forget}", APPROVAL)
                if forget == "end":
                    self.fire("on_session_end", session_id=STORED, turn_id="turn-1", completed=False,
                              platform="desktop")
                else:
                    self.hooks["unload"]()
                self.turn()
                self.prompt("approval", f"appr-next-{forget}", APPROVAL)
                self.reported.clear()
                self.settle("approval", f"appr-next-{forget}")
                self.assertEqual(self.reported, [("attention", STORED, False)])

    def test_an_answered_approval_leaves_the_turn_waiting_on_an_open_question(self):
        self.turn()
        self.prompt("approval", "appr-1", APPROVAL)
        self.prompt("clarify", "clar-1", CLARIFY)
        self.reported.clear()
        self.fire("post_approval_response", session_id=STORED, choice="once")
        self.settle("approval", "appr-1")
        self.assertEqual(self.reported, [])
        self.settle("clarify", "clar-1")
        self.assertEqual(self.reported, [("attention", STORED, False)])
        self.fire("post_approval_response", session_id=STORED, choice="once")
        self.assertEqual(self.reported[1:], [("attention", STORED, False)])

    def test_open_prompts_are_bounded(self):
        self.turn()
        for number in range(514):
            self.prompt("approval", f"appr-{number}", APPROVAL, session_id=f"s-{number}")
        self.reported.clear()
        self.settle("approval", "appr-0", session_id="s-0")
        self.settle("approval", "appr-1", session_id="s-1")
        self.settle("approval", "appr-2", session_id="s-2")
        self.assertEqual(self.reported, [("attention", "s-2", False)])


class SessionOwnershipTests(GatewayPromptTestCase):
    """Each wait notifies from exactly one hook."""

    def human_input(self, kind, platform="local", session_id=STORED, prompt="rm -rf build"):
        fields = {"kind": kind, "request_id": "hook-" + kind, "session_id": session_id, "session_key": ROUTE,
                  "platform": platform, "prompt": prompt}
        self.fire("on_human_input_request", **fields)
        self.fire("on_human_input_resolved", **fields, outcome="answered")

    def test_a_gateway_session_notifies_from_the_prompt_not_the_human_input_hook(self):
        self.turn()
        for kind in ["approval", "clarify", "sudo"]:
            self.human_input(kind)
        self.assertEqual((self.sent, self.reported), ([], []))
        self.prompt("approval", "appr-1", APPROVAL)
        self.prompt("clarify", "clar-1", CLARIFY)
        self.prompt("sudo", "srq-sudo0000000", {"command": "sudo make install"})
        self.assertEqual([(p["kind"], p["hermes"]["request_id"]) for p, _ in self.sent],
                         [("approval", "appr-1"), ("clarify", "clar-1"), ("sudo", "srq-sudo0000000")])
        # On a Hermes without the prompt hooks the human-input hook notifies, exactly as before.
        self.sent.clear()
        self.reported.clear()
        self.adapter._SESSION_PROMPT_HOOKS = False
        for kind in ["approval", "clarify", "sudo"]:
            self.human_input(kind)
        self.assertEqual([p["kind"] for p, _ in self.sent], ["approval", "clarify", "sudo"])
        self.assertEqual([p["hermes"].get("request_id") for p, _ in self.sent], [None] * 3)
        self.assertEqual(self.reported, [("attention", STORED, True), ("attention", STORED, False)] * 3)

    def test_messaging_and_cli_waits_keep_the_human_input_hook(self):
        self.env["HERMES_SESSION_PLATFORM"] = "telegram"
        self.env["HERMES_SESSION_KEY"] = "agent:main:telegram:dm:42"
        self.turn("tg-1", platform="telegram")
        self.human_input("approval", platform="telegram", session_id="tg-1")
        self.human_input("clarify", platform="cli", session_id="cli-1")
        self.assertEqual([(p["kind"], p["subtitle"]) for p, _ in self.sent],
                         [("approval", "Waiting on you · Telegram")])
        # The gateway's own prompt for that messaging turn is left to the hook that already announced it.
        self.prompt("approval", "appr-tg", APPROVAL, session_id="tg-1")
        self.assertEqual(len(self.sent), 1)

    def test_a_compressed_messaging_turn_is_found_by_its_lineage_root(self):
        """The gateway names a messaging session's prompts by the root of its compression lineage."""
        lineages = {"tg-tip": ["tg-root", "tg-tip"]}

        class SessionDB:
            def __init__(self, read_only=False):
                assert read_only

            def get_compression_lineage(self, session_id):
                return lineages.get(session_id, [])

            def close(self):
                pass

        state = types.ModuleType("hermes_state")
        state.SessionDB = SessionDB
        self.env["HERMES_SESSION_PLATFORM"] = "telegram"
        self.env["HERMES_SESSION_KEY"] = "agent:main:telegram:dm:42"
        with patch.dict(sys.modules, {"hermes_state": state}):
            self.turn("tg-tip", platform="telegram")
            self.prompt("approval", "appr-tg", APPROVAL, session_id="tg-root")
        self.assertEqual((self.sent, self.reported), ([], []))

    def hermes_platform(self):
        """``on_human_input_request``'s ``platform`` as Hermes derives it (``tools/human_input_hooks.py``
        ``_identity``): the process's ``HERMES_PLATFORM`` first, then the session's platform and source."""
        return (os.getenv("HERMES_PLATFORM") or self.env.get("HERMES_SESSION_PLATFORM")
                or self.env.get("HERMES_SESSION_SOURCE") or "cli")

    def test_another_process_platform_does_not_announce_a_gateway_wait_twice(self):
        for process in ["desktop", "cli"]:
            with self.subTest(HERMES_PLATFORM=process), patch.dict(os.environ, {"HERMES_PLATFORM": process}):
                self.sent.clear()
                self.reported.clear()
                self.turn()
                self.human_input("approval", platform=self.hermes_platform())
                self.prompt("approval", f"appr-{process}", APPROVAL)
                self.assertEqual([(p["kind"], p["hermes"].get("request_id")) for p, _ in self.sent],
                                 [("approval", f"appr-{process}")])
                self.assertEqual(self.reported, [("attention", STORED, True)])

    def test_a_messaging_wait_is_announced_whatever_the_process_platform(self):
        self.env["HERMES_SESSION_PLATFORM"] = "telegram"
        self.env["HERMES_SESSION_KEY"] = "agent:main:telegram:dm:42"
        with patch.dict(os.environ, {"HERMES_PLATFORM": "local"}):
            self.turn("tg-1", platform="telegram")
            self.human_input("approval", platform=self.hermes_platform(), session_id="tg-1")
            self.prompt("approval", "appr-tg", APPROVAL, session_id="tg-1")
        self.assertEqual([(p["kind"], p["hermes"].get("request_id"), p["thread_id"]) for p, _ in self.sent],
                         [("approval", None, "tg-1")])

    def test_a_previously_announced_prompt_is_not_announced_again(self):
        self.turn()
        self.adapter._notified_requests["srq-seen0000000"] = None
        self.prompt("sudo", "srq-seen0000000", {})
        self.assertEqual(self.sent, [])

    def test_the_new_hooks_are_registered_and_declared(self):
        import yaml
        manifest = yaml.safe_load((PUSH / "plugin.yaml").read_text())
        for hook in ["on_session_prompt", "on_session_prompt_settled"]:
            self.assertIn(hook, self.hooks)
            self.assertIn(hook, manifest["provides_hooks"])



class CapabilityTests(unittest.TestCase):
    def test_the_prompt_hooks_are_detected_by_the_gateway_module(self):
        import importlib.machinery
        hooks = types.ModuleType("gateway.session_prompt_hooks")
        hooks.__spec__ = importlib.machinery.ModuleSpec("gateway.session_prompt_hooks", None)
        with tempfile.TemporaryDirectory() as home:
            # Whichever Hermes is importable here: present, then absent (None in sys.modules: not importable).
            with patch.dict(sys.modules, {"gateway.session_prompt_hooks": hooks}):
                adapter, _ = load_push_adapter(Path(home))
            self.assertIs(getattr(adapter, "_SESSION_PROMPT_HOOKS", None), True)
            with patch.dict(sys.modules, {"gateway.session_prompt_hooks": None}):
                adapter, _ = load_push_adapter(Path(home))
            self.assertIs(getattr(adapter, "_SESSION_PROMPT_HOOKS", None), False)


if __name__ == "__main__":
    unittest.main()
