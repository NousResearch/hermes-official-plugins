"""WeCom plugin registration: both platforms' ``PlatformEntry`` kwargs and ``register``.

One plugin, two platforms: ``wecom`` (AI Bot over the ``openws`` WebSocket) and ``wecom_callback``
(self-built apps over an HTTP callback endpoint with AES message crypto).
"""

import dataclasses
import importlib
import importlib.machinery
import logging
from typing import Any

from gateway.platforms._shared import get_scoped_secret

# Relative imports: the loader runs this package as ``hermes_plugins.<slug>``, so the registry, the
# standalone sender and the tests all share one ``WeComAdapter`` / ``WecomCallbackAdapter`` class object.
from . import gateway_env
from .adapter import (
    PLATFORM_NAME,
    WeComAdapter,
    _standalone_send,
    check_wecom_requirements,
    interactive_setup,
)
from .callback_adapter import DEFAULT_PORT as CALLBACK_DEFAULT_PORT
from .callback_adapter import PLATFORM_NAME as CALLBACK_PLATFORM_NAME
from .callback_adapter import (
    WecomCallbackAdapter,
    check_wecom_callback_requirements,
    ensure_wecom_callback_requirements,
)

logger = logging.getLogger(__name__)

_INSTALL_HINT = "Run `hermes setup` to install WeCom support."

# System-prompt platform hint (PlatformEntry.platform_hint); cached-prompt bytes, keep stable.
_WECOM_PLATFORM_HINT = (
    "You are on WeCom (\u4f01\u4e1a\u5fae\u4fe1). Markdown is supported. "
    "You can send files natively: write MEDIA:/absolute/path/to/file in your response. "
    "Images (.jpg, .png, .webp) send as photos (\u226410 MB), other files as documents (\u226420 MB), videos "
    "(.mp4) play inline. Voice messages must be AMR \u2014 other audio formats send as file attachments. Image "
    "URLs via ![alt](url) are downloaded and sent as photos. Never claim you lack file-sending."
)


def _secret(name: str) -> str:
    return str(get_scoped_secret(name, "") or "").strip()


def _config_has_bot(config) -> bool:
    """``validate_config``: the merged config carries a ``bot_id`` (env credentials arrive via the seed)."""
    return bool((getattr(config, "extra", {}) or {}).get("bot_id"))


def _config_has_callback_app(config) -> bool:
    """``validate_config``: ``corp_id`` or a multi-app ``apps`` block in the merged config."""
    extra = getattr(config, "extra", {}) or {}
    return bool(extra.get("corp_id") or extra.get("apps"))


def _is_connected(config) -> bool:
    """Configured = a ``bot_id`` in config extras, or WECOM_BOT_ID + WECOM_SECRET in env."""
    return _config_has_bot(config) or all(_secret(name) for name in gateway_env.ENV_ENABLE_CREDENTIALS)


def _callback_is_connected(config) -> bool:
    """Callback mode: ``corp_id`` / ``apps`` in extras, or the corp ID + corp secret in env."""
    return _config_has_callback_app(config) or all(
        _secret(name) for name in gateway_env.CALLBACK_ENV_ENABLE_CREDENTIALS)


def _build_callback_adapter(config):
    return WecomCallbackAdapter(config)


def _supported_entry_fields() -> set:
    from gateway.platform_registry import PlatformEntry
    return {f.name for f in dataclasses.fields(PlatformEntry)}


def _standalone_send_policy():
    """Text sends go straight to ``standalone_sender_fn``; media needs the running gateway's adapter (core's
    ``media_via_adapter`` seam, which replaced its hardcoded ``wecom`` route). Older cores without the field
    keep their own built-in route."""
    from gateway.platforms.send_targets import StandaloneSendPolicy
    fields = {f.name for f in dataclasses.fields(StandaloneSendPolicy)}
    if "media_via_adapter" in fields:
        return StandaloneSendPolicy(text_direct=True, media_via_adapter=True)
    return StandaloneSendPolicy(text_direct=True)


def _with_optional(kwargs: dict[str, Any], optional: dict[str, Any], supported: set) -> dict[str, Any]:
    # PlatformEntry raises TypeError on unknown keys, so fields newer than the running core are dropped.
    for key, value in optional.items():
        if key in supported:
            kwargs[key] = value
        else:
            logger.debug("wecom: core PlatformEntry has no %r field; not passing it", key)
    return kwargs


def platform_kwargs() -> dict[str, Any]:
    """``wecom``: the AI Bot (WebSocket) platform."""
    from gateway.platforms.send_targets import parse_nonempty

    kwargs: dict[str, Any] = dict(
        name=PLATFORM_NAME, label="WeCom (Enterprise WeChat)", adapter_factory=WeComAdapter,
        check_fn=check_wecom_requirements, is_connected=_is_connected, validate_config=_config_has_bot,
        required_env=["WECOM_BOT_ID", "WECOM_SECRET"], install_hint=_INSTALL_HINT, setup_fn=interactive_setup,
        cron_deliver_env_var="WECOM_HOME_CHANNEL", standalone_sender_fn=_standalone_send, max_message_length=4000,
        emoji="💼", allow_update_command=True, platform_hint=_WECOM_PLATFORM_HINT, display_tier="low",
        # Non-editable, but its native "stream" msgtype gives a typing animation + cumulative updates.
        display_overrides={"streaming": True},
        target_ref_syntax_fn=parse_nonempty, standalone_send=_standalone_send_policy(),
        dashboard_label="WeCom (group bot)", dashboard_description="Send-only WeCom group bot via webhook.",
        docs_url="https://developer.work.weixin.qq.com/document/path/91770",
        dashboard_env_vars=("WECOM_BOT_ID", "WECOM_SECRET"), dashboard_required_env=("WECOM_BOT_ID",),
        env_enablement_fn=gateway_env.env_enablement,
        env_enable_credentials=gateway_env.ENV_ENABLE_CREDENTIALS, **gateway_env.AUTHZ,
    )
    return _with_optional(kwargs, {}, _supported_entry_fields())


def callback_platform_kwargs() -> dict[str, Any]:
    """``wecom_callback``: self-built apps over the HTTP callback endpoint (binds a host port)."""
    kwargs: dict[str, Any] = dict(
        name=CALLBACK_PLATFORM_NAME, label="WeCom Callback (self-built apps)", adapter_factory=_build_callback_adapter,
        check_fn=check_wecom_callback_requirements, ensure_deps_fn=ensure_wecom_callback_requirements,
        is_connected=_callback_is_connected, validate_config=_config_has_callback_app,
        required_env=["WECOM_CALLBACK_CORP_ID", "WECOM_CALLBACK_CORP_SECRET"], install_hint=_INSTALL_HINT,
        emoji="💼", allow_update_command=True, display_tier="low",
        dashboard_label="WeCom (app)", dashboard_description="Two-way WeCom integration via callback app.",
        docs_url="https://developer.work.weixin.qq.com/document/path/90930",
        dashboard_env_vars=(
            "WECOM_CALLBACK_CORP_ID", "WECOM_CALLBACK_CORP_SECRET", "WECOM_CALLBACK_AGENT_ID",
            "WECOM_CALLBACK_TOKEN", "WECOM_CALLBACK_ENCODING_AES_KEY",
        ),
        dashboard_required_env=("WECOM_CALLBACK_CORP_ID", "WECOM_CALLBACK_CORP_SECRET", "WECOM_CALLBACK_AGENT_ID"),
        env_enablement_fn=gateway_env.callback_env_enablement,
        env_enable_credentials=gateway_env.CALLBACK_ENV_ENABLE_CREDENTIALS, **gateway_env.CALLBACK_AUTHZ,
    )
    # ``(extra key, default)`` of the host TCP port the adapter binds, for core's port-collision checks.
    return _with_optional(kwargs, {"listen_port": ("port", CALLBACK_DEFAULT_PORT)}, _supported_entry_fields())


def core_ships_wecom() -> bool:
    """True on a Hermes core that still bundles WeCom (``plugins/platforms/wecom``).

    Looks only inside the imported package's own ``__path__``: a plain ``find_spec`` also consults meta-path
    finders (e.g. an editable install of a different checkout) and can report a module the running core
    does not have."""
    try:
        search = list(getattr(importlib.import_module("plugins.platforms"), "__path__", None) or ())
    except ImportError:
        return False
    spec = importlib.machinery.PathFinder.find_spec("plugins.platforms.wecom", search) if search else None
    # ``origin`` is None for a namespace match: a directory left behind holding only ``__pycache__``
    # after an update deleted the bundled files is not a core that ships WeCom.
    return spec is not None and spec.origin is not None


def register(ctx) -> None:
    """Plugin entry point — called by the Hermes plugin loader."""
    if core_ships_wecom():
        # Registering on top of the built-in would replace core's adapters with a second copy.
        logger.info("wecom: this Hermes core still ships WeCom built in; "
                    "the plugin stays inactive until core drops it")
        return
    ctx.register_platform(**platform_kwargs())
    ctx.register_platform(**callback_platform_kwargs())
