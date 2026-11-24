"""WeCom (bot + callback) env -> gateway config bridge (``PlatformEntry.env_enablement_fn``).

Core carried these as ``gateway.platform_env.EnvCred`` rows. Those rows need a ``Platform`` member at import
time, which a plugin platform only has once it is registered, so the plugin seeds the same keys through the
public ``env_enablement_fn`` seam instead: the gateway enables a platform when ``is_connected`` sees both
credentials, then merges the seed into ``platforms.<name>.extra``. No credentials, no seed: a home without
WeCom env never enables ``wecom_callback`` (which binds port 8645).
"""

from gateway.platforms._shared import get_scoped_secret, seed_extra_from_env

ENV_ENABLE_CREDENTIALS = ("WECOM_BOT_ID", "WECOM_SECRET")
CALLBACK_ENV_ENABLE_CREDENTIALS = ("WECOM_CALLBACK_CORP_ID", "WECOM_CALLBACK_CORP_SECRET")
_CALLBACK_DEFAULT_PORT = 8645


def _raw(name: str) -> str:
    # Core's EnvCred read these unstripped (``_getenv_str``); stripping only gates the credential check.
    return str(get_scoped_secret(name, "") or "")


def _port(raw: str) -> int:
    """``int(raw.strip(), 10)``, the default port on blank or malformed input (core's ``int_or(8645)``)."""
    try:
        return int(str(raw).strip(), 10)
    except (TypeError, ValueError):
        return _CALLBACK_DEFAULT_PORT


def _gate(names) -> bool:
    return all(_raw(name) for name in names)


def env_enablement() -> dict:
    """``platforms.wecom.extra`` seed once WECOM_BOT_ID and WECOM_SECRET are both set.

    ``WECOM_WEBSOCKET_URL`` is optional; ``WECOM_HOME_CHANNEL`` (+ ``_NAME``, ``_THREAD_ID``) becomes the home channel for
    cron ``deliver=wecom``.
    """
    if not _gate(ENV_ENABLE_CREDENTIALS):
        return {}
    seed = {"bot_id": _raw("WECOM_BOT_ID"), "secret": _raw("WECOM_SECRET")}
    if url := _raw("WECOM_WEBSOCKET_URL"):
        seed["websocket_url"] = url
    seed.update(seed_extra_from_env((), home_env="WECOM_HOME_CHANNEL"))
    if "home_channel" in seed and (thread_id := _raw("WECOM_HOME_CHANNEL_THREAD_ID")):
        seed["home_channel"]["thread_id"] = thread_id
    return seed


def callback_env_enablement() -> dict:
    """``platforms.wecom_callback.extra`` seed once the callback corp ID and corp secret are both set.

    Every key is always written once enabled (blank when unset), as core's row did. No host default: a falsy
    ``extra.host`` lets the adapter's dual-stack ``DEFAULT_HOST=None`` bind v4+v6.
    """
    if not _gate(CALLBACK_ENV_ENABLE_CREDENTIALS):
        return {}
    return {
        "corp_id": _raw("WECOM_CALLBACK_CORP_ID"), "corp_secret": _raw("WECOM_CALLBACK_CORP_SECRET"),
        "agent_id": _raw("WECOM_CALLBACK_AGENT_ID"), "token": _raw("WECOM_CALLBACK_TOKEN"),
        "encoding_aes_key": _raw("WECOM_CALLBACK_ENCODING_AES_KEY"),
        "host": _raw("WECOM_CALLBACK_HOST"), "port": _port(_raw("WECOM_CALLBACK_PORT")),
    }


# Authorization env names (``PlatformEntry`` fields). Core reads them from this file when the
# live entry lacks them: a user plugin overriding this platform, safe mode, the plugin disabled.
AUTHZ = {
    "allowed_users_env": "WECOM_ALLOWED_USERS",
    "allow_all_env": "WECOM_ALLOW_ALL_USERS",
    "own_policy_env": ("WECOM_DM_POLICY", "WECOM_GROUP_POLICY"),
    "allowlist_suppresses_pairing": True,
}
CALLBACK_AUTHZ = {
    "allowed_users_env": "WECOM_CALLBACK_ALLOWED_USERS",
    "allow_all_env": "WECOM_CALLBACK_ALLOW_ALL_USERS",
    "allowlist_suppresses_pairing": True,
}
