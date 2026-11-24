"""Feishu / Lark plugin registration: the ``PlatformEntry`` kwargs, the inert guard, ``register``."""

import dataclasses
import importlib
import importlib.machinery
import logging
from typing import Any

# Relative imports: the loader runs this package as ``hermes_plugins.<slug>``, so ``.adapter`` is the one
# module (and the one ``FeishuAdapter`` class object) the registry, the comment handler and the tools share.
from . import gateway_env
from .adapter import (
    PLATFORM_NAME,
    FeishuAdapter,
    _apply_yaml_config,
    _is_connected,
    _standalone_send,
    check_feishu_requirements,
    feishu_deps_present,
    interactive_setup,
)
from .adapter_hint import PLATFORM_HINT
from .dashboard_card import DASHBOARD_CARD
from .feishu_doc_tool import register_tools as register_doc_tools
from .feishu_drive_tool import register_tools as register_drive_tools
from .send_targets import SEND_ENTRY_KWARGS

logger = logging.getLogger(__name__)

_INSTALL_HINT = "Feishu: lark-oapi is not installed. Reinstall the plugin: hermes plugins install feishu"


def _binds_port(extra) -> bool:
    """Webhook mode listens on ``webhook_port``; WebSocket mode binds nothing."""
    return str((extra or {}).get("connection_mode", "websocket")).strip().lower() == "webhook"


# PlatformEntry fields newer than some cores this plugin may load on (PlatformEntry raises TypeError on
# unknown keys), so each is passed only when the running core has it. ``binds_port_fn`` / ``listen_port``
# / ``thread_reply_anchor`` / ``opus_voice`` replace the Feishu special cases core dropped with the
# bundled adapter.
_OPTIONAL_ENTRY_KWARGS: dict[str, Any] = {
    **DASHBOARD_CARD,
    "display_tier": "medium",
    "env_enable_credentials": gateway_env.ENV_ENABLE_CREDENTIALS,
    "binds_port_fn": _binds_port,
    "listen_port": ("webhook_port", 8765),
    "thread_reply_anchor": True,
    "opus_voice": True,
    **SEND_ENTRY_KWARGS,
    **gateway_env.AUTHZ,
}


def _supported_entry_fields() -> set:
    from gateway.platform_registry import PlatformEntry
    return {f.name for f in dataclasses.fields(PlatformEntry)}


def platform_kwargs() -> dict[str, Any]:
    kwargs: dict[str, Any] = dict(
        name=PLATFORM_NAME, label="Feishu / Lark", adapter_factory=FeishuAdapter,
        check_fn=feishu_deps_present, ensure_deps_fn=check_feishu_requirements,
        is_connected=_is_connected, validate_config=_is_connected,
        required_env=["FEISHU_APP_ID", "FEISHU_APP_SECRET"], install_hint=_INSTALL_HINT,
        setup_fn=interactive_setup, apply_yaml_config_fn=_apply_yaml_config,
        cron_deliver_env_var="FEISHU_HOME_CHANNEL", standalone_sender_fn=_standalone_send,
        max_message_length=8000, emoji="🪽", allow_update_command=True, platform_hint=PLATFORM_HINT,
        env_enablement_fn=gateway_env.env_enablement,
    )
    supported = _supported_entry_fields()
    for key, value in _OPTIONAL_ENTRY_KWARGS.items():
        if key in supported:
            kwargs[key] = value
        else:
            logger.debug("feishu: core PlatformEntry has no %r field; not passing it", key)
    return kwargs


def core_ships_feishu() -> bool:
    """True on a Hermes core that still bundles Feishu (``plugins/platforms/feishu`` or
    ``tools/feishu_doc_tool.py``, removed together when core dropped it).

    Looks only inside the imported packages' own ``__path__``: a plain ``find_spec`` also consults meta-path
    finders (e.g. an editable install of a different checkout) and can report a module the running core
    does not have."""
    def _ships(package: str, module: str) -> bool:
        try:
            search = list(getattr(importlib.import_module(package), "__path__", None) or ())
        except ImportError:
            return False
        spec = importlib.machinery.PathFinder.find_spec(module, search) if search else None
        # ``origin`` is None for a namespace match: a directory left behind holding only ``__pycache__``
        # after an update deleted the bundled files is not a core that ships Feishu.
        return spec is not None and spec.origin is not None

    return _ships("plugins.platforms", "plugins.platforms.feishu") or _ships("tools", "tools.feishu_doc_tool")


def register(ctx) -> None:
    """Plugin entry point — called by the Hermes plugin loader."""
    if core_ships_feishu():
        # Registering on top of the built-in would shadow core's feishu_* tools and replace its adapter.
        logger.info("feishu: this Hermes core still ships Feishu built in; "
                    "the plugin stays inactive until core drops it")
        return
    ctx.register_platform(**platform_kwargs())
    register_doc_tools(ctx)
    register_drive_tools(ctx)
