"""hermes-telegram-business — observe-with-approval Telegram Business Mode.

Turns a Hermes Telegram bot into a secretary for a Telegram Business
account: customers message the owner, the plugin drafts a reply via the
host LLM, and the OWNER approves every message before it goes out via
[✓ Send] [✎ Edit] [✕ Discard] inline buttons. There is no auto-send.

Wiring (all through the public Hermes plugin surface — zero core edits):

* ``ctx.register_telegram_handler`` — PTB handlers for BusinessConnection
  updates, business_message / edited_business_message updates, a
  pattern-scoped ``bd:`` CallbackQueryHandler for the draft buttons, an
  owner-DM text handler for edit-capture, and a ``/biz`` CommandHandler.
* ``ctx.llm`` — host-owned drafting (user's active model + auth).
* Plugin-owned SQLite state at ``<HERMES_HOME>/telegram-business/state.db``.

Requires Hermes Agent with ``register_telegram_handler`` support
(hermes-agent PR #59159) and python-telegram-bot >= 21.1 (ships with the
Telegram platform plugin).
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

try:  # Normal package import (Hermes plugin loader, tests via conftest alias)
    from .manager import (
        BusinessDeliveryNotAttempted,
        BusinessModeManager,
        CALLBACK_PREFIX,
    )
    from .state import BusinessStateDB
except ImportError:  # pragma: no cover - loaded as a bare module (no package)
    import importlib.util as _ilu
    import sys as _sys

    def _load(_name: str):
        _path = Path(__file__).resolve().parent / f"{_name}.py"
        _spec = _ilu.spec_from_file_location(f"telegram_business_{_name}", _path)
        _mod = _ilu.module_from_spec(_spec)
        _sys.modules[_spec.name] = _mod
        _spec.loader.exec_module(_mod)
        return _mod

    _manager_mod = _load("manager")
    _state_mod = _load("state")
    BusinessModeManager = _manager_mod.BusinessModeManager
    BusinessDeliveryNotAttempted = _manager_mod.BusinessDeliveryNotAttempted
    CALLBACK_PREFIX = _manager_mod.CALLBACK_PREFIX
    BusinessStateDB = _state_mod.BusinessStateDB

logger = logging.getLogger(__name__)

_DEFAULT_PERSONA = (
    "You are drafting a short, friendly reply on behalf of the account "
    "owner. Match the tone of a personal message — warm, direct, and "
    "concise. Do not introduce yourself as an assistant or AI. Reply in "
    "the same language the customer used."
)


def _hermes_home() -> Path:
    home = os.environ.get("HERMES_HOME")
    if home:
        return Path(home)
    return Path.home() / ".hermes"


def _plugin_config(ctx: Any) -> dict:
    """Read this plugin's config block from config.yaml (best-effort).

    Users configure under ``plugins.entries.telegram-business.*``:

        plugins:
          entries:
            telegram-business:
              debounce_seconds: 8
              draft_ttl_hours: 24
              max_customer_text_chars: 4000
              owner_persona: "..."
    """
    try:
        import yaml
        cfg_path = _hermes_home() / "config.yaml"
        if cfg_path.exists():
            raw = yaml.safe_load(cfg_path.read_text()) or {}
            entries = ((raw.get("plugins") or {}).get("entries") or {})
            block = entries.get("telegram-business") or {}
            if isinstance(block, dict):
                return block
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("telegram-business: config read failed: %s", exc)
    return {}


def register(ctx: Any) -> None:
    cfg = _plugin_config(ctx)
    debounce = float(cfg.get("debounce_seconds", 8.0))
    ttl_hours = float(cfg.get("draft_ttl_hours", 24.0))
    max_chars = int(cfg.get("max_customer_text_chars", 4000))
    persona = str(cfg.get("owner_persona") or _DEFAULT_PERSONA)

    # Deferred singletons — constructed on first connect so import stays light.
    _state: dict = {"db": None, "manager": None}

    def _get_manager(adapter: Any) -> BusinessModeManager:
        if _state["manager"] is not None:
            return _state["manager"]

        db = BusinessStateDB(_hermes_home() / "telegram-business" / "state.db")

        async def _send(**kwargs):
            kwargs.setdefault("parse_mode", None)
            kwargs.setdefault("disable_web_page_preview", True)
            bot = getattr(adapter, "_bot", None) or getattr(adapter, "bot", None)
            if bot is None:
                raise BusinessDeliveryNotAttempted("Telegram bot is not connected")
            return await bot.send_message(**kwargs)

        system_prompt = (
            f"{persona}\n\n"
            "Output only the message text. No preamble, no quotes, no "
            "markdown headers. Keep it under 4 sentences unless the "
            "customer asked something that genuinely needs a longer reply."
        )

        async def _draft(customer_text: str, customer_chat_id: str) -> str:
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": customer_text},
            ]
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                None,
                lambda: ctx.llm.complete(
                    messages,
                    max_tokens=500,
                    timeout=60.0,
                    purpose="telegram-business draft",
                ),
            )
            return (result.text or "").strip()

        try:
            manager = BusinessModeManager(
                session_db=db,
                send_message=_send,
                draft_generator=_draft,
                debounce_seconds=debounce,
                draft_ttl_hours=ttl_hours,
                max_customer_text_chars=max_chars,
            )
        except BaseException:
            try:
                db.close()
            except BaseException:
                pass
            raise

        _state["db"] = db
        _state["manager"] = manager
        return manager

    # ------------------------------------------------------------------
    # The handler factory the Telegram adapter invokes at connect() time.
    # PTB imports live inside so register() works without PTB installed.
    # ------------------------------------------------------------------

    def _wire(application: Any, adapter: Any) -> None:
        from telegram.ext import (
            BusinessConnectionHandler,
            CallbackQueryHandler,
            CommandHandler,
            MessageHandler,
            filters,
        )

        manager = _get_manager(adapter)

        async def _on_business_connection(update, context):
            conn = getattr(update, "business_connection", None)
            if conn is None:
                return
            try:
                await manager.handle_connection_update(conn)
            except Exception as exc:
                logger.exception("telegram-business: connection handler failed: %s", exc)

        async def _on_business_message(update, context):
            message = (
                getattr(update, "business_message", None)
                or getattr(update, "edited_business_message", None)
            )
            if message is None:
                return
            try:
                await manager.handle_business_message(message)
            except Exception as exc:
                logger.exception("telegram-business: message handler failed: %s", exc)

        async def _on_draft_button(update, context):
            query = update.callback_query
            if not query or not query.data:
                return
            caller_id = str(getattr(query.from_user, "id", "")) or None
            try:
                await manager.handle_callback(
                    data=query.data,
                    caller_user_id=caller_id,
                    answer=query.answer,
                    edit_message_text=query.edit_message_text,
                )
            except Exception as exc:
                logger.exception("telegram-business: draft callback failed: %s", exc)
                try:
                    await query.answer(text="⚠️ Action failed.")
                except Exception:
                    pass

        async def _on_owner_dm(update, context):
            """Edit-capture: the owner's next DM after tapping Edit.

            Registered in group -1 (runs before the default group where
            the core adapter's handlers live). When the message is
            consumed as an edit override we raise ApplicationHandlerStop
            so the core text pipeline never sees it; otherwise we return
            normally and PTB continues into group 0 unchanged.
            """
            from telegram.ext import ApplicationHandlerStop

            message = getattr(update, "message", None)
            chat = getattr(message, "chat", None)
            if message is None or chat is None:
                return
            try:
                consumed = await manager.maybe_handle_edit_capture(
                    owner_chat_id=str(chat.id),
                    text=getattr(message, "text", "") or "",
                )
            except Exception as exc:
                logger.exception("telegram-business: edit capture failed: %s", exc)
                consumed = False
            if consumed:
                raise ApplicationHandlerStop

        async def _on_biz_command(update, context):
            message = getattr(update, "message", None)
            if message is None:
                return
            user = getattr(message, "from_user", None)
            chat = getattr(message, "chat", None)
            text = (getattr(message, "text", "") or "").strip()
            args = text.split()[1:]
            try:
                reply = await manager.handle_biz_command(
                    owner_user_id=str(getattr(user, "id", "")),
                    owner_chat_id=str(getattr(chat, "id", "")),
                    args=args,
                )
            except Exception as exc:
                logger.exception("telegram-business: /biz failed: %s", exc)
                reply = f"⚠️ /biz failed: {exc}"
            try:
                bot = getattr(adapter, "_bot", None)
                if bot is not None and chat is not None:
                    await bot.send_message(
                        chat_id=chat.id, text=reply,
                        disable_web_page_preview=True,
                    )
            except Exception:
                logger.debug("telegram-business: /biz reply send failed", exc_info=True)

        # Connection lifecycle (established / edited / ended).
        application.add_handler(BusinessConnectionHandler(_on_business_connection))
        # Incoming + edited customer messages (text only for v1).
        application.add_handler(MessageHandler(
            filters.UpdateType.BUSINESS_MESSAGE & filters.TEXT,
            _on_business_message,
        ))
        application.add_handler(MessageHandler(
            filters.UpdateType.EDITED_BUSINESS_MESSAGE & filters.TEXT,
            _on_business_message,
        ))
        # Draft approval buttons — pattern-scoped so every other callback
        # falls through to the core adapter's CallbackQueryHandler.
        application.add_handler(CallbackQueryHandler(
            _on_draft_button, pattern=rf"^{CALLBACK_PREFIX}",
        ))
        # /biz command (owner-only state, adapter-level — no agent loop).
        application.add_handler(CommandHandler("biz", _on_biz_command))
        # Owner-DM edit capture in group -1 — runs before the core text
        # handler (default group 0); raises ApplicationHandlerStop only
        # when it consumes the message as an edit override.
        application.add_handler(MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
            _on_owner_dm,
        ), group=-1)

        logger.info("telegram-business: Business Mode handlers wired")

    ctx.register_telegram_handler(_wire)
