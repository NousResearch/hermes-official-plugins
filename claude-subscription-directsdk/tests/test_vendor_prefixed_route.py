"""#377: a vendor-prefixed model id from Hermes core must route to the bare native id.

Hermes core can pass an OpenRouter-style canonical id (`anthropic/claude-sonnet-5`) even when the
config names `claude-sonnet-5`. The plugin used to send it through verbatim; the native CLI 404s,
and because a 404 is not an auth error `_allows_auth_retry()` denies every retry. The turn then
silently falls back to another provider.
"""

from __future__ import annotations

import unittest

from model_catalog import _route_base, accepts_thinking_disable, native_model


class VendorPrefixedRoute(unittest.TestCase):
    def test_anthropic_prefix_stripped_and_alias_resolved(self):
        self.assertEqual(_route_base('anthropic/claude-sonnet-5'), 'claude-sonnet-5')
        self.assertEqual(native_model('anthropic/claude-sonnet-5'), 'claude-sonnet-5[1m]')

    def test_openrouter_prefix_stripped(self):
        self.assertEqual(_route_base('openrouter/anthropic/claude-opus-5'), 'claude-opus-5')
        self.assertEqual(native_model('openrouter/claude-opus-5'), 'claude-opus-5[1m]')

    def test_dot_and_bracket_survive_the_prefix(self):
        self.assertEqual(native_model('anthropic/claude-sonnet-5.5'), 'claude-sonnet-5-5[1m]')
        self.assertEqual(native_model('anthropic/claude-sonnet-5[1m]'), 'claude-sonnet-5[1m]')

    def test_thinking_policy_uses_the_stripped_route(self):
        # Sonnet 5.5 rejects `thinking: disabled`; the prefixed id must not evade that check.
        self.assertFalse(accepts_thinking_disable('anthropic/claude-sonnet-5-5'))
        self.assertTrue(accepts_thinking_disable('anthropic/claude-haiku-4-5'))

    def test_unknown_vendor_is_left_alone(self):
        # Only the vendors core actually emits are ours to rewrite; a foreign namespace still
        # reaches the CLI and fails there rather than being silently guessed at.
        self.assertEqual(_route_base('custom/claude-sonnet-5'), 'custom/claude-sonnet-5')
        self.assertEqual(_route_base('claude-sonnet-5'), 'claude-sonnet-5')


if __name__ == '__main__':
    unittest.main()
