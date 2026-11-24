"""Weixin (personal WeChat via iLink Bot API) env -> gateway config bridge (``PlatformEntry.env_overrides``).

Core carried the same ``EnvCred`` row. ``Platform("weixin")`` resolves only once the plugin registered the
platform, so the row is built when the gateway runs it, never at import.
"""

import logging

from gateway.platform_env import EnvCred, strip_slash

logger = logging.getLogger("gateway.config")  # core logged this refusal under gateway.config

ENV_ENABLE_CREDENTIALS = ("WEIXIN_TOKEN", "WEIXIN_ACCOUNT_ID")


def _weixin_row(config) -> None:
    from gateway.config import Platform
    EnvCred(
        Platform("weixin"), (("WEIXIN_TOKEN", "WEIXIN_ACCOUNT_ID"),), token="WEIXIN_TOKEN",
        optional=(("account_id", "WEIXIN_ACCOUNT_ID"),),
        optional_stripped=(
            ("base_url", "WEIXIN_BASE_URL", strip_slash), ("cdn_base_url", "WEIXIN_CDN_BASE_URL", strip_slash),
            ("dm_policy", "WEIXIN_DM_POLICY", str.lower), ("group_policy", "WEIXIN_GROUP_POLICY", str.lower),
            ("allow_from", "WEIXIN_ALLOWED_USERS"), ("group_allow_from", "WEIXIN_GROUP_ALLOWED_USERS"),
            ("split_multiline_messages", "WEIXIN_SPLIT_MULTILINE_MESSAGES"),
        ),
        home="WEIXIN_HOME_CHANNEL", home_strip=True,
    )(config)
    _refuse_placeholder_token(config, Platform("weixin"))


def _refuse_placeholder_token(config, platform) -> None:
    """Core's startup refusal, kept: an enabled Weixin whose token is a placeholder or under 4 characters
    is turned off with the same error (``gateway.config._validate_gateway_config`` while Weixin was core)."""
    from .adapter import usable_token
    pconfig = config.platforms.get(platform)
    token = getattr(pconfig, "token", None)
    if pconfig is None or not pconfig.enabled or token is None or not token.strip() or usable_token(token):
        return
    logger.error("weixin is enabled but WEIXIN_TOKEN is set to a placeholder value ('%s'). Set a real bot token "
                 "before starting the gateway. The adapter will NOT be started.", token.strip()[:6] + "...")
    pconfig.enabled = False
    # The registry enable pass that runs next re-enables a disabled platform whose is_connected passes,
    # unless config.yaml disabled it explicitly; this refusal counts as that (the loader scrubs the marker).
    pconfig.extra["_enabled_explicit"] = True


ENV_OVERRIDES = (_weixin_row,)

# Authorization env names (``PlatformEntry`` fields).
AUTHZ = {
    "allowed_users_env": "WEIXIN_ALLOWED_USERS",
    "allow_all_env": "WEIXIN_ALLOW_ALL_USERS",
    "own_policy_env": ("WEIXIN_DM_POLICY", "WEIXIN_GROUP_POLICY"),
    "allowlist_suppresses_pairing": True,
}
