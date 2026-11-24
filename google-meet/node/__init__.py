"""Remote 'node host': run the Meet bot on a different machine than the gateway.

    gateway (Linux)  ── ws://mac.local:18789 ──▶  node server (Mac) → process_manager → meet_bot

Why: Google sign-in + Chrome profile live on the user's laptop; running the bot there reuses
that profile without shipping credentials. NodeClient (gateway RPC), NodeServer (hosts the
bot), NodeRegistry (approved nodes: name → url+token), protocol (envelope helpers).
"""

from __future__ import annotations

from . import protocol
from .client import NodeClient
from .protocol import (
    VALID_REQUEST_TYPES, decode, encode, make_error, make_request, make_response, validate_request)
from .registry import NodeRegistry
from .server import NodeServer

__all__ = [
    "VALID_REQUEST_TYPES",
    "NodeClient",
    "NodeRegistry",
    "NodeServer",
    "decode",
    "encode",
    "make_error",
    "make_request",
    "make_response",
    "protocol",
    "validate_request",
]
