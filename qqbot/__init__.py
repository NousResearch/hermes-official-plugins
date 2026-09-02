"""QQBot platform package.

Re-exports the adapter symbols from ``adapter.py`` (the original ``qqbot.py``)
so all existing import paths remain unchanged, e.g.
``from gateway.platforms.qqbot import QQAdapter, check_qq_requirements``.

Sub-modules: ``constants``, ``utils`` (User-Agent, config helpers), ``crypto``
(AES-256-GCM), ``onboard`` (QR scan-to-configure), ``chunked_upload``, ``keyboards``.
"""

from .adapter import (  # noqa: F401
    QQAdapter,
    QQCloseError,
    check_qq_requirements,
    _coerce_list,
    _ssrf_redirect_guard,
)
from .onboard import BindStatus, build_connect_url, qr_register  # noqa: F401
from .crypto import decrypt_secret, generate_bind_key  # noqa: F401
from .utils import build_user_agent, get_api_headers, coerce_list  # noqa: F401
from .chunked_upload import (  # noqa: F401
    ChunkedUploader,
    UploadDailyLimitExceededError,
    UploadFileTooLargeError,
)
from .keyboards import (  # noqa: F401
    ApprovalRequest,
    InlineKeyboard,
    InteractionEvent,
    build_approval_keyboard,
    build_approval_text,
    build_update_prompt_keyboard,
    parse_approval_button_data,
    parse_interaction_event,
    parse_update_prompt_button_data,
)

__all__ = [
    "QQAdapter", "QQCloseError", "check_qq_requirements", "_coerce_list", "_ssrf_redirect_guard",
    "BindStatus", "build_connect_url", "qr_register",
    "decrypt_secret", "generate_bind_key",
    "build_user_agent", "get_api_headers", "coerce_list",
    "ChunkedUploader", "UploadDailyLimitExceededError", "UploadFileTooLargeError",
    "ApprovalRequest", "InlineKeyboard", "InteractionEvent",
    "build_approval_keyboard", "build_approval_text", "build_update_prompt_keyboard",
    "parse_approval_button_data", "parse_interaction_event", "parse_update_prompt_button_data",
]
