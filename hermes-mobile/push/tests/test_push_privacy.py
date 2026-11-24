"""What protect() leaves readable to the relay."""
import base64
import hashlib
import json
import unittest

from push_plugin import load_privacy

INSTALL_KEY = "ab" * 32
SESSION_KEY = "agent:main:signal:dm:+15551234567"


class PublicSessionTests(unittest.TestCase):
    def protect(self, hermes):
        privacy = load_privacy()
        privacy.load_key = lambda: bytes(range(32))
        payload = {"title": "Approval needed", "body": "Run ls?", "kind": "approval", "hermes": hermes}
        if hermes.get("session_id") or hermes.get("session_key"):
            payload["thread_id"] = hermes.get("session_id") or hermes.get("session_key")
        return privacy.protect(payload, INSTALL_KEY)

    def open_sealed(self, protected):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        box = base64.b64decode(protected["hermes"]["sealed"]["data"])
        aad = ("hermes-preview-v1:" + hashlib.sha256(INSTALL_KEY.encode()).hexdigest()).encode()
        return json.loads(AESGCM(bytes(range(32))).decrypt(box[:12], box[12:], aad))

    def test_a_gateway_session_key_never_reaches_the_relay(self):
        protected = self.protect({"profile": "work", "kind": "approval", "session_key": SESSION_KEY})
        public = json.dumps(protected)
        for private in ["15551234567", "signal", "agent:main"]:
            self.assertNotIn(private, public)
        self.assertNotIn("session_id", protected["hermes"])
        self.assertNotIn("session_key", protected["hermes"])
        # The relay still groups and dedupes by the keyed thread token.
        self.assertRegex(protected["thread_id"], "^[0-9a-f]{64}$")
        # The app still routes by the key, from the sealed part.
        clear = self.open_sealed(protected)
        self.assertEqual(clear["hermes"]["session_key"], SESSION_KEY)
        self.assertEqual(clear["thread_id"], SESSION_KEY)

    def test_a_stored_session_id_stays_public_for_mutes(self):
        protected = self.protect({"profile": "work", "session_id": "20261002_101500_abcdef",
                                  "session_key": SESSION_KEY})
        self.assertEqual(protected["hermes"]["session_id"], "20261002_101500_abcdef")
        self.assertNotIn("15551234567", json.dumps(protected))

    def test_a_subagent_session_id_is_still_marked(self):
        protected = self.protect({"session_id": "subagent_123"})
        self.assertIs(protected["hermes"]["is_subagent"], True)

    def test_the_platform_and_source_reach_the_app_sealed(self):
        protected = self.protect({"profile": "work", "session_id": "20261002_101500_abcdef", "platform": "telegram",
                                  "source": "telegram", "tool_output": "secret"})
        self.assertNotIn("telegram", json.dumps(protected))
        clear = self.open_sealed(protected)["hermes"]
        self.assertEqual(clear["platform"], "telegram")
        self.assertEqual(clear["source"], "telegram")
        self.assertNotIn("tool_output", clear)

    def test_the_platform_and_source_are_bounded_strings(self):
        clear = self.open_sealed(self.protect({"platform": "x" * 500, "source": 7}))["hermes"]
        self.assertEqual(clear["platform"], "x" * 64)
        self.assertEqual(clear["source"], "7")


if __name__ == "__main__":
    unittest.main()
