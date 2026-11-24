"""How a message addressed to push (cron, webhook routes, send_message) reaches the relay."""
import asyncio
import contextvars
import dataclasses
import io
import json
import sys
import tempfile
import threading
import types
import unittest
import unittest.mock
import urllib.error
from pathlib import Path
from unittest.mock import patch

from push_plugin import finish_turn, load_module, load_push_adapter


def _hermes_send_path():
    try:
        from gateway.platform_registry import PlatformEntry, platform_registry
        from tools import send_message_tool
        return PlatformEntry, platform_registry, send_message_tool
    except Exception:
        return None


class RegistrationTests(unittest.TestCase):
    def registration(self):
        adapter = load_module("adapter.py", _gateway_stubs())
        registered = {}
        adapter.register(types.SimpleNamespace(register_platform=lambda **kwargs: registered.update(kwargs)))
        return registered

    def test_a_push_is_never_split_into_chunks(self):
        # 0 is the registry's "no limit": core sends the whole message once.
        self.assertEqual(self.registration()["max_message_length"], 0)

    def test_the_adapter_takes_long_cron_output_whole(self):
        adapter = load_module("adapter.py", _gateway_stubs())
        self.assertTrue(adapter.PushAdapter.splits_long_messages)


@unittest.skipUnless(_hermes_send_path(), "needs a Hermes checkout on PYTHONPATH (run on the test server)")
class HermesSendPathTests(unittest.TestCase):
    """The registration against Hermes' real registry and send_message routing."""

    def test_a_long_cron_output_goes_out_as_one_push(self):
        PlatformEntry, platform_registry, send_message_tool = _hermes_send_path()
        from gateway.config import Platform, PlatformConfig
        adapter = load_module("adapter.py")
        scope = platform_registry.current_scope_key()
        previous = platform_registry.snapshot_registration("push", scope=scope)
        adapter.register(types.SimpleNamespace(register_platform=lambda **kwargs: platform_registry.register(
            PlatformEntry(source="plugin", **kwargs), scope=scope)))
        current = platform_registry.snapshot_registration("push", scope=scope)
        self.addCleanup(platform_registry.restore_registration, "push", current, previous, scope=scope)
        sent = []

        async def send_via_adapter(platform, pconfig, chat_id, chunk, **kwargs):
            sent.append(chunk)
            return {"success": True}
        original = send_message_tool._send_via_adapter
        send_message_tool._send_via_adapter = send_via_adapter
        self.addCleanup(setattr, send_message_tool, "_send_via_adapter", original)
        # About 6 KB: four times the old 1,500-character limit.
        report = "Cronjob Response: Weekly digest\n" + "line of the report\n" * 330
        result = asyncio.run(send_message_tool._send_to_platform(
            Platform("push"), PlatformConfig(enabled=True), "device", report))
        self.assertEqual(result, {"success": True})
        self.assertEqual(sent, [report])

    def test_the_gateway_hands_long_cron_output_over_uncut(self):
        from gateway.delivery import MAX_PLATFORM_OUTPUT, DeliveryRouter
        adapter = load_module("adapter.py")
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        report = "x" * (MAX_PLATFORM_OUTPUT * 2)
        with patch.dict("os.environ", {"HERMES_HOME": home.name}):
            delivered = DeliveryRouter.__new__(DeliveryRouter)._cap_oversized_output(
                adapter.PushAdapter, report, "job1")
        self.assertEqual(delivered, report)


class WebhookSilenceTests(unittest.TestCase):
    """A webhook run that ends on a silence marker notifies nobody."""

    def setUp(self):
        filters = types.ModuleType("gateway.response_filters")
        filters.is_autonomous_silence_response = lambda text: text.strip().startswith("[SILENT]")
        modules = patch.dict(sys.modules, {"gateway": types.ModuleType("gateway"), "gateway.response_filters": filters})
        modules.start()
        self.addCleanup(modules.stop)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.adapter, _ = load_push_adapter(Path(directory.name))
        self.sent = []
        self.adapter._deliver_in_background = self.sent.append
        self.adapter._origin_metadata = lambda fields: {}
        self.adapter._report_activity = lambda *args: None

    def end(self, platform, body):
        finish_turn(self.adapter, "webhook-run", body, platform)

    def test_the_plugin_skips_a_silent_webhook_run(self):
        for platform in ["webhook", "msgraph_webhook"]:
            with self.subTest(platform=platform):
                self.end(platform, "[SILENT] Nothing new in the repository.")
                self.end(platform, "")
        self.assertEqual(self.sent, [])
        self.end("webhook", "The build failed on main.")
        self.assertEqual([(p["kind"], p["body"]) for p in self.sent], [("reply", "The build failed on main.")])

class EventLoopTests(unittest.TestCase):
    """An addressed message is built (session store reads and writes) off the gateway's event loop."""

    def test_the_payload_is_built_on_a_worker_thread_in_the_callers_context(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        adapter, _ = load_push_adapter(Path(directory.name))
        session = contextvars.ContextVar("session", default="")
        built, posted = [], []

        def build(content, **kwargs):
            built.append((threading.current_thread() is threading.main_thread(), session.get()))
            return {"body": content}
        adapter._build_payload = build
        adapter._post_notify = lambda relay_url, install_key, payload: posted.append(payload) or (200, "")
        adapter.SendResult = lambda **kwargs: kwargs
        config = types.SimpleNamespace(extra={"relay_url": "https://relay.example", "install_key": "key"})

        async def deliver():
            session.set("cron_job_20261002_090000")
            await adapter.PushAdapter(config).send("device", "From the adapter")
            await adapter._standalone_send(config, "device", "Standalone")
        asyncio.run(deliver())
        self.assertEqual(built, [(False, "cron_job_20261002_090000")] * 2)
        self.assertEqual(posted, [{"body": "From the adapter"}, {"body": "Standalone"}])


class NotifyOutcomeTests(unittest.TestCase):
    """A 200 from /notify that reached no device is a failed send; a held-back one is not."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.adapter, _ = load_push_adapter(Path(directory.name))
        self.adapter._build_payload = lambda content, **kwargs: {"body": content}
        self.adapter.SendResult = lambda **kwargs: kwargs
        self.config = types.SimpleNamespace(extra={"relay_url": "https://relay.example", "install_key": "key"})

    def deliver(self, answer):
        self.adapter._post_notify = lambda relay_url, install_key, payload: (200, json.dumps(answer))

        async def both():
            return (await self.adapter.PushAdapter(self.config).send("device", "Hello"),
                    await self.adapter._standalone_send(self.config, "device", "Hello"))
        return asyncio.run(both())

    def test_a_send_no_device_received_fails(self):
        with self.assertLogs(self.adapter.logger, "WARNING"):
            sent, standalone = self.deliver({"status": "failed", "sent": 0, "failed": 2, "pruned": 0, "filtered": 0})
        self.assertFalse(sent["success"])
        self.assertIn("delivery failed", sent["error"])
        self.assertIn("delivery failed", standalone["error"])

    def test_a_muted_filtered_or_duplicate_send_succeeds(self):
        for status in ["sent", "muted", "filtered", "skipped"]:
            with self.subTest(status=status):
                sent, standalone = self.deliver({"status": status, "sent": int(status == "sent"),
                                                 "failed": 0, "pruned": 0, "filtered": 0})
                self.assertTrue(sent["success"])
                self.assertTrue(standalone["success"])


class BackgroundDeliveryTests(unittest.TestCase):
    """A hook's notification the relay turns away is logged in one line, without the key."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.adapter, _ = load_push_adapter(Path(directory.name))

    def test_an_install_without_devices_logs_one_line(self):
        def no_devices(relay_url, install_key, payload):
            raise urllib.error.HTTPError(f"{relay_url}/notify", 404, "Not Found", {}, io.BytesIO(b""))
        self.adapter._post_notify = no_devices
        with self.assertLogs(self.adapter.logger, "INFO") as logs:
            self.adapter._deliver_in_background({"kind": "reply"}, relay=("https://relay.example", "secret-key"))
            self.adapter._delivery_queue.join()
        self.assertEqual(len(logs.records), 1)
        self.assertIn("no device paired", logs.output[0])
        self.assertIsNone(logs.records[0].exc_info)
        self.assertNotIn("secret-key", logs.output[0])


def _hermes_redactor():
    try:
        from agent.redact import redact_sensitive_text
        return redact_sensitive_text
    except Exception:
        return None


class PreviewRedactionTests(unittest.TestCase):
    """A command shown on the lock screen is force-redacted, like cron output."""

    def setUp(self):
        self.redactor = types.ModuleType("agent.redact")
        self.redactor.redact_sensitive_text = lambda text, *, force: text.replace("sk-live-123", "***")
        modules = patch.dict(sys.modules, {"agent": types.ModuleType("agent"), "agent.redact": self.redactor})
        modules.start()
        self.addCleanup(modules.stop)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.adapter, _ = load_push_adapter(Path(directory.name))

    def payload(self, method, params):
        return self.adapter._server_request_payload(method, "srq-1", params, {"session_id": "s"})

    def test_approval_and_password_prompts_mask_credentials(self):
        approval = self.payload("approval", {"request_id": "a", "description": "Call the API with sk-live-123",
                                             "command": "curl -H 'Authorization: Bearer sk-live-123' https://api"})
        self.assertEqual(approval["body"], "Reason: Call the API with ***\n"
                                           "Command: curl -H 'Authorization: Bearer ***' https://api")
        sudo = self.payload("sudo", {"command": "sudo env TOKEN=sk-live-123 make install"})
        self.assertEqual(sudo["body"], "Hermes needs your administrator password to continue.\n"
                                       "Command: sudo env TOKEN=*** make install")

    def test_a_failing_redactor_never_shows_the_command(self):
        def fail(text, *, force):
            raise RuntimeError("redactor unavailable")
        self.redactor.redact_sensitive_text = fail
        approval = self.payload("approval", {"request_id": "a", "command": "export TOKEN=sk-live-123"})
        self.assertNotIn("sk-live-123", approval["body"])
        self.assertNotIn("sk-live-123", self.payload("sudo", {"command": "sudo TOKEN=sk-live-123 true"})["body"])


@unittest.skipUnless(_hermes_redactor(), "needs a Hermes checkout on PYTHONPATH (run on the test server)")
class HermesRedactionTests(unittest.TestCase):
    def test_hermes_redactor_masks_a_bearer_token_in_an_approval(self):
        adapter = load_module("adapter.py")
        token = "sk-abcdefghijklmnopqrstuvwxyz123456"
        body = adapter._approval_preview({"command": f"curl -H 'Authorization: Bearer {token}' https://api"})
        self.assertNotIn(token, body)
        self.assertIn("curl", body)


class ScopedSettingsTests(unittest.TestCase):
    """The plugin reads the profile's own gate, not another profile's env."""

    def setUp(self):
        shared = types.ModuleType("gateway.platforms._shared")
        shared.get_scoped_secret = lambda name, default=None: {
            "HERMES_MOBILE_PUSH_RELAY_URL": "https://scoped.example/",
            "HERMES_MOBILE_PUSH_INSTALL_KEY": "scoped-key"}.get(name, default)
        shared.platform_gate_env = lambda name, default="": {"HERMES_MOBILE_PUSH_HOOK_PLATFORMS": "custom"}.get(name, default)
        modules = patch.dict(sys.modules, {"gateway": types.ModuleType("gateway"),
                                           "gateway.platforms": types.ModuleType("gateway.platforms"),
                                           "gateway.platforms._shared": shared})
        modules.start()
        self.addCleanup(modules.stop)
        environment = patch.dict("os.environ", {
            "HERMES_MOBILE_PUSH_RELAY_URL": "https://env.example", "HERMES_MOBILE_PUSH_INSTALL_KEY": "env-key",
            "HERMES_MOBILE_PUSH_HOOK_PLATFORMS": "other"})
        environment.start()
        self.addCleanup(environment.stop)

    def test_the_plugin_reads_the_hook_platforms_gate_per_profile(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        adapter, _ = load_push_adapter(Path(directory.name))
        self.assertEqual(adapter._gate("HERMES_MOBILE_PUSH_HOOK_PLATFORMS"), "custom")


class RelayTransportTests(unittest.TestCase):
    """The install key rides in every request's Authorization header, so it only goes over HTTPS (or to this machine)."""

    URLS = {  # noqa: RUF012 -- a read-only table of cases
        "https://relay.example": True, "https://relay.example:8443/base": True,
        "http://127.0.0.1:8787": True, "http://localhost:8787": True, "http://[::1]:8787": True,
        "http://relay.example": False, "http://127.0.0.1.example": False, "ftp://relay.example": False,
        "relay.example": False, "https://": False, "http://[::1": False,
    }

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.adapter, _ = load_push_adapter(Path(directory.name))

    def test_the_plugin_refuses_a_plain_http_relay(self):
        for url, allowed in self.URLS.items():
            with self.subTest(url=url):
                relay = self.adapter._relay_settings({"relay_url": url, "install_key": "key"})
                self.assertEqual(relay, (url.rstrip("/") if allowed else "", "key"))
        with self.assertLogs(self.adapter.logger, "WARNING") as logs:
            config = types.SimpleNamespace(extra={"relay_url": "http://push.example/", "install_key": "key"})
            self.assertFalse(self.adapter.validate_config(config))
            result = asyncio.run(self.adapter._standalone_send(config, "device", "Hello"))
        self.assertIn("error", result)
        # Said once per URL, naming the rule but neither the URL nor the key.
        self.assertEqual(len(logs.output), 1)
        self.assertIn("must start with https://", logs.output[0])
        self.assertNotIn("push.example", logs.output[0])

    def test_the_install_key_rides_in_the_authorization_header(self):
        sent = []
        response = unittest.mock.MagicMock(status=200)
        response.__enter__.return_value = response
        response.read.return_value = b'{"status": "sent"}'
        with patch.object(self.adapter.urllib.request, "urlopen", lambda request, timeout: sent.append(request) or response):
            self.assertEqual(self.adapter._post_notify("https://relay.example", "key", {"body": "Hi"}),
                             (200, '{"status": "sent"}'))
        self.assertEqual(sent[0].full_url, "https://relay.example/notify")
        self.assertEqual(sent[0].get_header("Authorization"), "Bearer key")

    def test_the_adapter_names_the_https_rule_when_it_refuses_to_connect(self):
        config = types.SimpleNamespace(extra={"relay_url": "http://push.example", "install_key": "key"})
        push = self.adapter.PushAdapter(config)
        with self.assertLogs(self.adapter.logger, "WARNING") as logs:
            self.assertFalse(asyncio.run(push.connect()))
        self.assertTrue(any("must start with https://" in line for line in logs.output), logs.output)
        self.assertFalse(any("not configured" in line for line in logs.output), logs.output)

    def test_the_dashboard_rule_is_registered_only_where_hermes_knows_it(self):
        registered = {}
        context = types.SimpleNamespace(register_platform=lambda **kwargs: registered.update(kwargs))
        entry = types.ModuleType("gateway.platform_registry")
        for fields in [{"name": str}, {"name": str, "env_value_rules": dict}]:
            registered.clear()
            entry.PlatformEntry = dataclasses.make_dataclass("PlatformEntry", list(fields.items()))
            with self.subTest(fields=list(fields)), patch.dict(sys.modules, {"gateway.platform_registry": entry}):
                self.adapter.register(context)
                rules = registered.get("env_value_rules")
                if "env_value_rules" not in fields:
                    self.assertIsNone(rules)
                    continue
                accepts, detail = rules["HERMES_MOBILE_PUSH_RELAY_URL"]
                self.assertTrue(accepts("https://relay.example"))
                self.assertFalse(accepts("http://relay.example"))
                self.assertIn("https://", detail)


def _gateway_stubs():
    base = types.ModuleType("gateway.platforms.base")
    base.BasePlatformAdapter = type("BasePlatformAdapter", (), {})
    base.SendResult = type("SendResult", (), {})
    config = types.ModuleType("gateway.config")
    config.Platform = lambda value: value
    config.PlatformConfig = type("PlatformConfig", (), {})
    return {"gateway": types.ModuleType("gateway"), "gateway.platforms": types.ModuleType("gateway.platforms"),
            "gateway.platforms.base": base, "gateway.config": config}


if __name__ == "__main__":
    unittest.main()
