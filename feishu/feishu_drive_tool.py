"""Feishu Drive Tools -- document comment operations via Feishu/Lark API.

List / reply-to / add document comments through the generic BaseRequest path (lazy SDK
import). The lark client is injected per-thread by the feishu_comment event handler.
"""

import json
import logging
import threading

from tools.registry import registry, tool_error, tool_result

logger = logging.getLogger(__name__)

# Thread-local storage for the lark client injected by feishu_comment handler.
_local = threading.local()


def set_client(client):
    """Store a lark client for the current thread (called by feishu_comment)."""
    _local.client = client


def get_client():
    """Return the lark client for the current thread, or None."""
    return getattr(_local, "client", None)


def _check_feishu():
    # find_spec avoids executing lark_oapi's ~5s __init__ at every hermes startup;
    # the handlers do the real import when invoked.
    import importlib.util
    try:
        return importlib.util.find_spec("lark_oapi") is not None
    except (ImportError, ValueError):
        return False


def _do_request(client, method, uri, paths=None, queries=None, body=None):
    """Build and execute a BaseRequest, return (code, msg, data_dict)."""
    from lark_oapi import AccessTokenType
    from lark_oapi.core.enum import HttpMethod
    from lark_oapi.core.model.base_request import BaseRequest

    builder = (
        BaseRequest.builder()
        .http_method(HttpMethod.GET if method == "GET" else HttpMethod.POST)
        .uri(uri)
        .token_types({AccessTokenType.TENANT})
    )
    if paths:
        builder = builder.paths(paths)
    if queries:
        builder = builder.queries(queries)
    if body is not None:
        builder = builder.body(body)

    # Tool handlers run synchronously in a worker thread (no running event
    # loop), so call the blocking lark client directly.
    response = client.request(builder.build())

    code = getattr(response, "code", None)
    msg = getattr(response, "msg", "")

    # Prefer the raw JSON body's "data"; fall back to the typed response.data.
    data = {}
    raw = getattr(response, "raw", None)
    if raw and hasattr(raw, "content"):
        try:
            data = json.loads(raw.content).get("data", {})
        except (json.JSONDecodeError, AttributeError):
            pass
    if not data:
        resp_data = getattr(response, "data", None)
        if isinstance(resp_data, dict):
            data = resp_data
        elif resp_data and hasattr(resp_data, "__dict__"):
            data = vars(resp_data)

    return code, msg, data


def _paged_queries(args: dict) -> list:
    """Common query params for comment/reply listing endpoints."""
    return [
        ("file_type", args.get("file_type", "docx") or "docx"),
        ("user_id_type", "open_id"),
        ("page_size", str(args.get("page_size", 100))),
    ]


_FILE_TOKEN_PROP = {"type": "string", "description": "The document file token."}
_FILE_TYPE_PROP = {"type": "string", "description": "File type (default: docx).", "default": "docx"}
_PAGE_TOKEN_PROP = {"type": "string", "description": "Pagination token for next page."}


# ---------------------------------------------------------------------------
# feishu_drive_list_comments
# ---------------------------------------------------------------------------

_LIST_COMMENTS_URI = "/open-apis/drive/v1/files/:file_token/comments"

FEISHU_DRIVE_LIST_COMMENTS_SCHEMA = {
    "name": "feishu_drive_list_comments",
    "description": (
        "List comments on a Feishu document. "
        "Use is_whole=true to list whole-document comments only."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "file_token": _FILE_TOKEN_PROP,
            "file_type": _FILE_TYPE_PROP,
            "is_whole": {
                "type": "boolean",
                "description": "If true, only return whole-document comments.",
                "default": False,
            },
            "page_size": {
                "type": "integer",
                "description": "Number of comments per page (max 100).",
                "default": 100,
            },
            "page_token": _PAGE_TOKEN_PROP,
        },
        "required": ["file_token"],
    },
}


def _handle_list_comments(args: dict, **kwargs) -> str:
    client = get_client()
    if client is None:
        return tool_error("Feishu client not available")

    file_token = args.get("file_token", "").strip()
    if not file_token:
        return tool_error("file_token is required")

    queries = _paged_queries(args)
    if args.get("is_whole", False):
        queries.append(("is_whole", "true"))
    page_token = args.get("page_token", "")
    if page_token:
        queries.append(("page_token", page_token))

    code, msg, data = _do_request(
        client, "GET", _LIST_COMMENTS_URI,
        paths={"file_token": file_token},
        queries=queries,
    )
    if code != 0:
        return tool_error(f"List comments failed: code={code} msg={msg}")

    return tool_result(data)


# ---------------------------------------------------------------------------
# feishu_drive_list_comment_replies
# ---------------------------------------------------------------------------

_LIST_REPLIES_URI = "/open-apis/drive/v1/files/:file_token/comments/:comment_id/replies"

FEISHU_DRIVE_LIST_REPLIES_SCHEMA = {
    "name": "feishu_drive_list_comment_replies",
    "description": "List all replies in a comment thread on a Feishu document.",
    "parameters": {
        "type": "object",
        "properties": {
            "file_token": _FILE_TOKEN_PROP,
            "comment_id": {
                "type": "string",
                "description": "The comment ID to list replies for.",
            },
            "file_type": _FILE_TYPE_PROP,
            "page_size": {
                "type": "integer",
                "description": "Number of replies per page (max 100).",
                "default": 100,
            },
            "page_token": _PAGE_TOKEN_PROP,
        },
        "required": ["file_token", "comment_id"],
    },
}


def _handle_list_replies(args: dict, **kwargs) -> str:
    client = get_client()
    if client is None:
        return tool_error("Feishu client not available")

    file_token = args.get("file_token", "").strip()
    comment_id = args.get("comment_id", "").strip()
    if not file_token or not comment_id:
        return tool_error("file_token and comment_id are required")

    queries = _paged_queries(args)
    page_token = args.get("page_token", "")
    if page_token:
        queries.append(("page_token", page_token))

    code, msg, data = _do_request(
        client, "GET", _LIST_REPLIES_URI,
        paths={"file_token": file_token, "comment_id": comment_id},
        queries=queries,
    )
    if code != 0:
        return tool_error(f"List replies failed: code={code} msg={msg}")

    return tool_result(data)


# ---------------------------------------------------------------------------
# feishu_drive_reply_comment
# ---------------------------------------------------------------------------

_REPLY_COMMENT_URI = "/open-apis/drive/v1/files/:file_token/comments/:comment_id/replies"

FEISHU_DRIVE_REPLY_SCHEMA = {
    "name": "feishu_drive_reply_comment",
    "description": (
        "Reply to a local comment thread on a Feishu document. "
        "Use this for local (quoted-text) comments. "
        "For whole-document comments, use feishu_drive_add_comment instead."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "file_token": _FILE_TOKEN_PROP,
            "comment_id": {
                "type": "string",
                "description": "The comment ID to reply to.",
            },
            "content": {
                "type": "string",
                "description": "The reply text content (plain text only, no markdown).",
            },
            "file_type": _FILE_TYPE_PROP,
        },
        "required": ["file_token", "comment_id", "content"],
    },
}


def _handle_reply_comment(args: dict, **kwargs) -> str:
    client = get_client()
    if client is None:
        return tool_error("Feishu client not available")

    file_token = args.get("file_token", "").strip()
    comment_id = args.get("comment_id", "").strip()
    content = args.get("content", "").strip()
    if not file_token or not comment_id or not content:
        return tool_error("file_token, comment_id, and content are required")

    file_type = args.get("file_type", "docx") or "docx"
    body = {"content": {"elements": [{"type": "text_run", "text_run": {"text": content}}]}}

    code, msg, data = _do_request(
        client, "POST", _REPLY_COMMENT_URI,
        paths={"file_token": file_token, "comment_id": comment_id},
        queries=[("file_type", file_type)],
        body=body,
    )
    if code != 0:
        return tool_error(f"Reply comment failed: code={code} msg={msg}")

    return tool_result(success=True, data=data)


# ---------------------------------------------------------------------------
# feishu_drive_add_comment
# ---------------------------------------------------------------------------

_ADD_COMMENT_URI = "/open-apis/drive/v1/files/:file_token/new_comments"

FEISHU_DRIVE_ADD_COMMENT_SCHEMA = {
    "name": "feishu_drive_add_comment",
    "description": (
        "Add a new whole-document comment on a Feishu document. "
        "Use this for whole-document comments or as a fallback when "
        "reply_comment fails with code 1069302."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "file_token": _FILE_TOKEN_PROP,
            "content": {
                "type": "string",
                "description": "The comment text content (plain text only, no markdown).",
            },
            "file_type": _FILE_TYPE_PROP,
        },
        "required": ["file_token", "content"],
    },
}


def _handle_add_comment(args: dict, **kwargs) -> str:
    client = get_client()
    if client is None:
        return tool_error("Feishu client not available")

    file_token = args.get("file_token", "").strip()
    content = args.get("content", "").strip()
    if not file_token or not content:
        return tool_error("file_token and content are required")

    file_type = args.get("file_type", "docx") or "docx"
    body = {"file_type": file_type, "reply_elements": [{"type": "text", "text": content}]}

    code, msg, data = _do_request(
        client, "POST", _ADD_COMMENT_URI,
        paths={"file_token": file_token},
        body=body,
    )
    if code != 0:
        return tool_error(f"Add comment failed: code={code} msg={msg}")

    return tool_result(success=True, data=data)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

for _name, _schema, _handler, _desc, _emoji in (
    ("feishu_drive_list_comments", FEISHU_DRIVE_LIST_COMMENTS_SCHEMA, _handle_list_comments,
     "List document comments", "\U0001f4ac"),
    ("feishu_drive_list_comment_replies", FEISHU_DRIVE_LIST_REPLIES_SCHEMA, _handle_list_replies,
     "List comment replies", "\U0001f4ac"),
    ("feishu_drive_reply_comment", FEISHU_DRIVE_REPLY_SCHEMA, _handle_reply_comment,
     "Reply to a document comment", "\u2709\ufe0f"),
    ("feishu_drive_add_comment", FEISHU_DRIVE_ADD_COMMENT_SCHEMA, _handle_add_comment,
     "Add a whole-document comment", "\u2709\ufe0f"),
):
    registry.register(
        name=_name,
        toolset="feishu_drive",
        schema=_schema,
        handler=_handler,
        check_fn=_check_feishu,
        requires_env=[],
        is_async=False,
        description=_desc,
        emoji=_emoji,
    )
