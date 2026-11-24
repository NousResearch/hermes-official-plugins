"""QQ (Official Bot API v2) env -> gateway config bridge (``PlatformEntry.env_enablement_fn``).

Core carried this as a ``gateway.platform_env.EnvCred`` row. That row needs a ``Platform`` member at
import time, which a plugin platform only has once it is registered, so the plugin seeds the same keys
through the public ``env_enablement_fn`` seam instead (the gateway enables the platform when
``is_connected`` sees App ID + Client Secret, then merges this seed into ``platforms.qqbot.extra`` and
lifts ``home_channel`` into a ``HomeChannel``).
"""

import logging

from gateway.platforms._shared import get_scoped_secret

# Logger name parity with the origin module (gateway.config_env): records stay under "gateway.config".
logger = logging.getLogger("gateway.config")

ENV_ENABLE_CREDENTIALS = ("QQ_APP_ID", "QQ_CLIENT_SECRET")


def _env(name: str, default: str = "") -> str:
    return str(get_scoped_secret(name, default) or default)


def _home_channel() -> dict | None:
    """``QQBOT_HOME_CHANNEL``, else the pre-rename ``QQ_HOME_CHANNEL`` (with a deprecation warning)."""
    qq_home = _env("QQBOT_HOME_CHANNEL").strip()
    name_env = "QQBOT_HOME_CHANNEL_NAME"
    if not qq_home and (qq_home := _env("QQ_HOME_CHANNEL").strip()):
        name_env = "QQ_HOME_CHANNEL_NAME"
        logger.warning(
            "QQ_HOME_CHANNEL is deprecated; rename to QQBOT_HOME_CHANNEL "
            "in your .env for consistency with the platform key."
        )
    if not qq_home:
        return None
    return {
        "chat_id": qq_home,
        "name": _env("QQBOT_HOME_CHANNEL_NAME") or _env(name_env, "Home"),
        "thread_id": _env("QQBOT_HOME_CHANNEL_THREAD_ID") or _env("QQ_HOME_CHANNEL_THREAD_ID") or None,
    }


def env_enablement() -> dict:
    """``platforms.qqbot.extra`` seed from env once QQ_APP_ID and QQ_CLIENT_SECRET are set."""
    app_id = _env("QQ_APP_ID")
    client_secret = _env("QQ_CLIENT_SECRET")
    if not (app_id and client_secret):
        return {}
    seed: dict = {"app_id": app_id, "client_secret": client_secret}
    for key, env in (("allow_from", "QQ_ALLOWED_USERS"), ("group_allow_from", "QQ_GROUP_ALLOWED_USERS")):
        if value := _env(env).strip():
            seed[key] = value
    if home := _home_channel():
        seed["home_channel"] = home
    return seed


# Authorization env names (``PlatformEntry`` fields). QQ's dm/group policy is YAML-only (no env var):
# ``own_policy_env`` of two blank names still marks QQ as an own-policy platform, so the gateway's
# open-policy startup refusal reads ``platforms.qqbot.extra`` and wants QQ_ALLOW_ALL_USERS.
AUTHZ = {
    "allowed_users_env": "QQ_ALLOWED_USERS",
    "allow_all_env": "QQ_ALLOW_ALL_USERS",
    "own_policy_env": ("", ""),
    "group_allowed_chats_env": "QQ_GROUP_ALLOWED_USERS",
    "allowlist_suppresses_pairing": True,
}
