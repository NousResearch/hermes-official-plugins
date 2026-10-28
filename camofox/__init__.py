"""hermes-camofox: the Camofox anti-detection browser (camofox-browser REST server) for Hermes Agent.

Formerly built into hermes-agent core (``tools/browser_camofox.py`` and the Camofox branches of the
built-in browser tools). What a migrated install relies on is unchanged: the ``browser_*`` tool names
and schemas skills call, ``CAMOFOX_URL`` / ``CAMOFOX_API_KEY`` / ``CAMOFOX_USER_ID`` /
``CAMOFOX_SESSION_KEY`` / ``CAMOFOX_ADOPT_EXISTING_TAB`` / ``CAMOFOX_REWRITE_LOOPBACK_URLS`` /
``CAMOFOX_LOOPBACK_HOST_ALIAS``, the ``browser.camofox.*`` config keys, and the managed-persistence
identity (the same profile-scoped Camofox ``userId``, so saved logins survive the move).

Wiring goes through the public plugin API: ``ctx.register_tool``, ``ctx.register_hook``,
``ctx.register_auxiliary_task`` + ``ctx.llm`` and ``ctx.register_cli_command``.
"""

from __future__ import annotations

import importlib.machinery
import logging

logger = logging.getLogger(__name__)

TOOLSET = "camofox"


def core_bundles_camofox() -> bool:
    """True while the running Hermes core still ships its own Camofox backend.

    ``PathFinder`` searches only ``tools.__path__``: ``importlib.util.find_spec`` would also consult
    an editable dev install's meta-path finder and could answer for a different checkout."""
    import tools
    return importlib.machinery.PathFinder.find_spec("tools.browser_camofox", list(tools.__path__)) is not None


def register(ctx) -> None:
    """Plugin entry point, called by the Hermes plugin loader.

    On a core that still bundles Camofox the built-in ``browser_*`` tools already route to Camofox
    when it is selected, so this copy registers nothing: two owners of the same tool names would
    collide."""
    if core_bundles_camofox():
        logger.info("camofox plugin inactive: this Hermes core still bundles the Camofox browser backend")
        return

    from . import camofox_schemas as s
    from . import camofox_tools as t
    from .camofox_cli import handle_cli, setup_cli

    ctx.register_auxiliary_task(
        t.VISION_TASK, display_name="Camofox browser vision",
        description="Screenshot analysis for browser_vision on the Camofox backend",
        inherit_from="vision")

    tools = (
        (s.BROWSER_NAVIGATE, t.handle_navigate, "🌐"),
        (s.BROWSER_SNAPSHOT, t.handle_snapshot, "📸"),
        (s.BROWSER_CLICK, t.handle_click, "👆"),
        (s.BROWSER_TYPE, t.handle_type, "⌨️"),
        (s.BROWSER_SCROLL, t.handle_scroll, "📜"),
        (s.BROWSER_BACK, t.handle_back, "◀️"),
        (s.BROWSER_PRESS, t.handle_press, "⌨️"),
        (s.BROWSER_GET_IMAGES, t.handle_get_images, "🖼️"),
        (s.BROWSER_VISION, t.make_vision_handler(lambda: ctx.llm), "👁️"),
        (s.BROWSER_CONSOLE, t.handle_console, "🖥️"),
    )
    for schema, handler, emoji in tools:
        ctx.register_tool(name=schema["name"], toolset=TOOLSET, schema=schema, handler=handler,
                          check_fn=t.check_camofox_configured, emoji=emoji)
    # Offered only with CAMOFOX_API_KEY set: the server's cookie route checks that bearer token.
    ctx.register_tool(name=s.BROWSER_IMPORT_COOKIES["name"], toolset=TOOLSET, schema=s.BROWSER_IMPORT_COOKIES,
                      handler=t.handle_import_cookies, check_fn=t.check_cookie_import_available, emoji="🍪")

    # Core cleaned the task's Camofox session after every turn (cleanup_task_resources), except with
    # browser.headed on, where someone watches the window; a teardown without a final turn (CLI quit,
    # /new) still gets one through on_session_finalize.
    def _on_session_end(task_id=None, session_id=None, **_):
        if (task_id or session_id) and not t.headed_mode():
            t.cleanup_task(task_id or session_id)

    def _on_session_finalize(session_id=None, **_):
        if session_id:
            t.cleanup_task(session_id)

    # browser_vault fills the Camofox tab through this; a core without the seam keeps its old behaviour.
    # Under plugins.isolation: host the core refuses this registration (the script carries the
    # credential); the browser tools must still load there.
    if hasattr(ctx, "register_browser_page_backend"):
        from .camofox_vault import make_page_backend
        try:
            ctx.register_browser_page_backend(make_page_backend())
        except Exception as exc:  # noqa: BLE001 -- vault fill unavailable in this mode; tools still work
            logger.info("camofox: browser_vault fill unavailable here (%s)", exc)

    ctx.register_hook("on_session_end", _on_session_end)
    ctx.register_hook("on_session_finalize", _on_session_finalize)
    ctx.register_cli_command(
        name="camofox", help="Set up and check the Camofox browser server",
        description="Camofox browser backend: point Hermes at a camofox-browser server and check it.",
        setup_fn=setup_cli, handler_fn=handle_cli)


__all__ = ["register", "core_bundles_camofox"]
