"""Claude Subscription WebSearch: ``web_search`` through the user's Claude subscription (opt-in, default off)."""

from __future__ import annotations

# Dual import: the Hermes loader imports this directory as a package; the flat test path does not.
try:
    from .native_search import ClaudeSubscriptionWebSearchProvider
except ImportError:
    from native_search import ClaudeSubscriptionWebSearchProvider


def register(ctx) -> None:
    ctx.register_web_search_provider(ClaudeSubscriptionWebSearchProvider())
