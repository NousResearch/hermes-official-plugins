"""Feishu Document Tool -- read document content via Feishu/Lark API.

Provides ``feishu_doc_read`` for reading document content as plain text.
Uses the same lazy-import + BaseRequest pattern as feishu_comment.py. Registered through the plugin API
(``register_tools(ctx)`` -> ``ctx.register_tool``), not ``tools.registry``.
"""

from .lark_client import (
    DEFAULT_PLATFORMS,
    _check_feishu,
    build_request,
    get_client,
    raw_body)
from tools.registry import tool_error, tool_result

TOOLSET = "feishu_doc"

_RAW_CONTENT_URI = "/open-apis/docx/v1/documents/:document_id/raw_content"

FEISHU_DOC_READ_SCHEMA = {
    "name": "feishu_doc_read",
    "description": (
        "Read the full content of a Feishu/Lark document as plain text. "
        "Useful when you need more context beyond the quoted text in a comment."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "doc_token": {
                "type": "string",
                "description": "The document token (from the document URL or comment context).",
            },
        },
        "required": ["doc_token"],
    },
}


def _handle_feishu_doc_read(args: dict, **kwargs) -> str:
    doc_token = args.get("doc_token", "").strip()
    if not doc_token:
        return tool_error("doc_token is required")
    client = get_client()
    if client is None:
        return tool_error("Feishu client not available (not in a Feishu comment context)")
    try:
        request = build_request("GET", _RAW_CONTENT_URI, paths={"document_id": doc_token})
    except ImportError:
        return tool_error("lark_oapi not installed")

    # Handlers run synchronously in a worker thread (no event loop): call the blocking client.
    response = client.request(request)
    code = getattr(response, "code", None)
    if code != 0:
        return tool_error(f"Failed to read document: code={code} msg={getattr(response, 'msg', 'unknown error')}")

    body = raw_body(response)
    if body is not None:
        try:
            return tool_result(success=True, content=body.get("data", {}).get("content", ""))
        except AttributeError:
            pass
    data = getattr(response, "data", None)  # fallback: the typed response.data
    if data:
        content = data.get("content", "") if isinstance(data, dict) else getattr(data, "content", str(data))
        return tool_result(success=True, content=content)
    return tool_error("No content returned from document API")


def register_tools(ctx) -> None:
    """Register ``feishu_doc_read`` in the ``feishu_doc`` toolset via the plugin API."""
    ctx.register_tool(
        name="feishu_doc_read", toolset=TOOLSET, schema=FEISHU_DOC_READ_SCHEMA, handler=_handle_feishu_doc_read,
        check_fn=_check_feishu, requires_env=[], is_async=False, description="Read Feishu document content",
        emoji="\U0001f4c4", default_platforms=DEFAULT_PLATFORMS)
