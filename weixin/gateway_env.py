"""Weixin (personal WeChat via iLink Bot API) env -> gateway config bridge (``PlatformEntry.env_overrides``).

Core carried the same ``EnvCred`` row. ``Platform("weixin")`` resolves only once the plugin registered the
platform, so the row is built when the gateway runs it, never at import.
"""

from gateway.platform_env import EnvCred, strip_slash

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


ENV_OVERRIDES = (_weixin_row,)

# Authorization env names (``PlatformEntry`` fields).
AUTHZ = {
    "allowed_users_env": "WEIXIN_ALLOWED_USERS",
    "allow_all_env": "WEIXIN_ALLOW_ALL_USERS",
    "own_policy_env": ("WEIXIN_DM_POLICY", "WEIXIN_GROUP_POLICY"),
    "allowlist_suppresses_pairing": True,
}
