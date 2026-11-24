"""Sealed reply and delivery previews reach the lock screen without markdown syntax."""
import tempfile
import unittest
from pathlib import Path

from push_plugin import load_push_adapter


class NotificationTextTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.adapter, _ = load_push_adapter(Path(directory.name))
        self.protected = []
        self.adapter.privacy.protect = lambda payload, key: self.protected.append(payload) or payload

    def test_markup_goes_and_the_words_and_lines_stay(self):
        text = self.adapter._plain_notification_text(
            "# The Fox Who Learned to Sail\n\nThe estuary knew **Fenris** before _anyone_ else.\n"
            "- one\n* two\n> quoted\n---\n```bash\necho hi\n```\nSee [the docs](https://example.com) and `ls -la`.")
        self.assertEqual(text, "The Fox Who Learned to Sail\n\nThe estuary knew Fenris before anyone else.\n"
                               "• one\n• two\nquoted\necho hi\nSee the docs and ls -la.")

    def test_symbols_that_are_not_markup_stay(self):
        self.assertEqual(self.adapter._plain_notification_text("snake_case_name and 2*3*4 stay"),
                         "snake_case_name and 2*3*4 stay")

    def test_replies_and_deliveries_are_cleaned_before_sealing(self):
        for kind in ("reply", "delivery"):
            self.adapter._private_payload({"title": "Hermes", "body": "## Done\n**Shipped**", "kind": kind}, "k" * 32)
        self.assertEqual([p["body"] for p in self.protected], ["Done\nShipped", "Done\nShipped"])

    def test_approvals_keep_their_command_verbatim(self):
        self.adapter._private_payload({"title": "Approval needed", "body": "rm *.tmp **/cache", "kind": "approval"}, "k" * 32)
        self.assertEqual(self.protected[-1]["body"], "rm *.tmp **/cache")


class RoutineWrapperTests(unittest.TestCase):
    """Hermes's cron wrapper (job name, id, stop hint) never reaches the lock screen."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.adapter, _ = load_push_adapter(Path(directory.name))

    def wrapped(self, name, body, footer=True):
        text = f"Cronjob Response: {name}\n(job_id: d37d2cfcb38f)\n-------------\n\n{body}"
        if footer:
            text += f'\n\nTo stop or manage this job, send me a new message (e.g. "stop reminder {name}").'
        return text

    def test_the_job_name_becomes_the_subtitle_and_the_body_is_the_output(self):
        for footer in (True, False):
            payload = self.adapter._build_payload(self.wrapped("Morning brief", "Three meetings today.", footer))
            self.assertEqual(payload["body"], "Three meetings today.")
            self.assertEqual(payload["subtitle"], "Morning brief")
            self.assertEqual(payload["kind"], "delivery")

    def test_output_that_reads_like_a_gateway_notice_stays_a_delivery(self):
        payload = self.adapter._build_payload(self.wrapped("Uptime", "Gateway online since 9:00."))
        self.assertEqual(payload["kind"], "delivery")

    def test_device_access_results_keep_their_label(self):
        name = "hermes-mobile-device-access-" + "a" * 32
        payload = self.adapter._build_payload(self.wrapped(name, "Calendar: two events."))
        self.assertEqual(payload["body"], "Calendar: two events.")
        self.assertEqual(payload["subtitle"], "Device update")

    def test_unwrapped_messages_are_unchanged(self):
        payload = self.adapter._build_payload("Gateway online")
        self.assertEqual(payload["body"], "Gateway online")
        self.assertEqual(payload["kind"], "system")
        self.assertNotIn("subtitle", payload)


class AddressedKindTests(unittest.TestCase):
    """The apps file addressed messages by their sealed kind, so it is always there."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.adapter, _ = load_push_adapter(Path(directory.name))
        self.adapter._store_delivery = lambda content, meta: {"session_id": "push_delivery_x", "source": "push"}

    def test_gateway_shutdown_and_restart_notices_are_system_and_sealed_as_such(self):
        for text in ("⚠️ Hermes is shutting down — your current task will be interrupted.",
                     "⚠️ Hermes is restarting — your current task will be interrupted."):
            payload = self.adapter._build_payload(text, chat_id="push", metadata={"_interim_send": True})
            self.assertEqual(payload["kind"], "system")
            self.assertEqual(payload["hermes"]["kind"], "system")

    def test_deliveries_carry_their_kind_and_no_internal_markers(self):
        payload = self.adapter._build_payload("Report ready.", metadata={"_interim_send": True, "job_name": "Digest"})
        self.assertEqual(payload["hermes"]["kind"], "delivery")
        self.assertNotIn("_interim_send", payload["hermes"])

    def test_an_explicit_kind_is_kept(self):
        payload = self.adapter._build_payload("Hi", metadata={"kind": "reply"})
        self.assertEqual(payload["hermes"]["kind"], "reply")


if __name__ == "__main__":
    unittest.main()
