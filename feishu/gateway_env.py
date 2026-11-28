"""Feishu/Lark env -> gateway config bridge (``PlatformEntry.env_enablement_fn``).

Core carried this as a ``gateway.platform_env.EnvCred`` row. That row needs a ``Platform`` member at
import time, which a plugin platform only has once it is registered, so the plugin seeds the same keys
through the public ``env_enablement_fn`` seam instead (the gateway enables the platform when
``is_connected`` sees ``app_id``, then merges this seed into ``platforms.feishu.extra``).
"""

from gateway.platforms._shared import get_scoped_secret

ENV_ENABLE_CREDENTIALS = ("FEISHU_APP_ID", "FEISHU_APP_SECRET")

# Always written once the credentials are set (core's ``fixed``), with core's defaults.
_FIXED = (
    ("app_id", "FEISHU_APP_ID", ""), ("app_secret", "FEISHU_APP_SECRET", ""),
    ("domain", "FEISHU_DOMAIN", "feishu"), ("connection_mode", "FEISHU_CONNECTION_MODE", "websocket"),
)
# Written only when set (core's ``optional``).
_OPTIONAL = (("encrypt_key", "FEISHU_ENCRYPT_KEY"), ("verification_token", "FEISHU_VERIFICATION_TOKEN"))
_HOME_ENV = "FEISHU_HOME_CHANNEL"


def _env(name: str, default: str = "") -> str:
    value = get_scoped_secret(name, None)
    return default if value is None else str(value)


def _home_channel() -> dict:
    # Core's ``env_home_channel``: chat id as set, ``_NAME`` (default "Home"), optional ``_THREAD_ID``.
    chat_id = _env(_HOME_ENV)
    if not chat_id:
        return {}
    home = {"chat_id": chat_id, "name": _env(f"{_HOME_ENV}_NAME", "Home")}
    thread_id = _env(f"{_HOME_ENV}_THREAD_ID")
    return {"home_channel": {**home, "thread_id": thread_id} if thread_id else home}


def env_enablement() -> dict:
    """``platforms.feishu.extra`` seed from env once App ID and App Secret are set.

    ``FEISHU_HOME_CHANNEL`` (+ ``_NAME`` / ``_THREAD_ID``) becomes the home channel for cron
    ``deliver=feishu``.
    """
    if not all(_env(name) for name in ENV_ENABLE_CREDENTIALS):
        return {}
    seed = {key: _env(env, default) for key, env, default in _FIXED}
    seed.update({key: value for key, env in _OPTIONAL if (value := _env(env))})
    return {**seed, **_home_channel()}


# Authorization env names (``PlatformEntry`` fields).
AUTHZ = {
    "allowed_users_env": "FEISHU_ALLOWED_USERS",
    "allow_all_env": "FEISHU_ALLOW_ALL_USERS",
    "allow_bots_env": "FEISHU_ALLOW_BOTS",
    "allowlist_suppresses_pairing": True,
}
