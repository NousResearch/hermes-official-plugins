"""weixin — Weixin (personal WeChat) gateway platform for Hermes Agent (official plugin).

Formerly built into hermes-agent core (``plugins/platforms/weixin``). Everything a migrated install relies on
is unchanged: platform name ``weixin``, the ``WEIXIN_*`` env vars, the ``platforms.weixin`` config keys, the
QR login's ``weixin/accounts`` files and ``hermes gateway setup`` → Weixin.

Wiring goes only through the public plugin API (``ctx.register_platform``), and ``register()`` does nothing
on a core that still bundles Weixin.
"""

from .adapter import register

__all__ = ["register"]
