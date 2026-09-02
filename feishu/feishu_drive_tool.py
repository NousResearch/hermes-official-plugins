"""Feishu Drive Tools -- document comment operations via Feishu/Lark API.

List / reply-to / add document comments through the generic BaseRequest path (lazy SDK
import), sharing client/request plumbing with feishu_doc_tool via ``tools.feishu_lark``.
The lark client is injected per-thread by the feishu_comment event handler.
"""

import logging

from tools.feishu_lark import (  # noqa: F401  (set_client/get_client are imported by feishu_comment)
    build_request,
    _check_feishu,
    get_client,
    response_data,
    set_client,
)
from tools.registry import registry, tool_error, tool_result

logger = logging.getLogger(__name__)


def _do_request(client, method, uri, paths=None, queries=None, body=None):
    """Build and execute a BaseRequest, return (code, msg, data_dict)."""
    # Tool handlers run synchronously in a worker thread (no running event
    # loop), so call the blocking lark client directly.
    response = client.request(build_request(method, uri, paths, queries, body))
    return getattr(response, "code", None), getattr(response, "msg", ""), response_data(response)


def _prepare(args: dict, keys: tuple, missing_msg: str):
    """Client check first, then required fields (stripped). Returns (client, values, error|None)."""
    client = get_client()
    values = tuple(args.get(k, "").strip() for k in keys)
    if client is None:
        return None, values, tool_error("Feishu client not available")
    if not all(values):
        return client, values, tool_error(missing_msg)
    return client, values, None


def _file_type(args: dict) -> str:
    return args.get("file_type", "docx") or "docx"


def _paged_queries(args: dict) -> list:
    """Query params shared by the comment/reply listing endpoints."""
    return [
        ("file_type", _file_type(args)),
        ("user_id_type", "open_id"),
        ("page_size", str(args.get("page_size", 100))),
    ]


def _with_page_token(queries: list, args: dict) -> list:
    """Append page_token last (after any is_whole) so the query order stays as before."""
    page_token = args.get("page_token", "")
    if page_token:
        queries.append(("page_token", page_token))
    return queries


_FILE_TOKEN_PROP = {"type": "string", "description": "The document file token."}
_FILE_TYPE_PROP = {"type": "string", "description": "File type (default: docx).", "default": "docx"}
_PAGE_TOKEN_PROP = {"type": "string", "description": "Pagination token for next page."}
_COMMENTS_URI = "/open-apis/drive/v1/files/:file_token/comments"
_REPLIES_URI = "/open-apis/drive/v1/files/:file_token/comments/:comment_id/replies"
_ADD_COMMENT_URI = "/open-apis/drive/v1/files/:file_token/new_comments"


# ---------------------------------------------------------------------------
# feishu_drive_list_comments
# ---------------------------------------------------------------------------

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
    client, (file_token,), err = _prepare(args, ("file_token",), "file_token is required")
    if err:
        return err

    queries = _paged_queries(args)
    if args.get("is_whole", False):
        queries.append(("is_whole", "true"))
    _with_page_token(queries, args)

    code, msg, data = _do_request(
        client, "GET", _COMMENTS_URI, paths={"file_token": file_token}, queries=queries,
    )
    if code != 0:
        return tool_error(f"List comments failed: code={code} msg={msg}")
    return tool_result(data)


# ---------------------------------------------------------------------------
# feishu_drive_list_comment_replies
# ---------------------------------------------------------------------------

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
    client, (file_token, comment_id), err = _prepare(
        args, ("file_token", "comment_id"), "file_token and comment_id are required"
    )
    if err:
        return err

    code, msg, data = _do_request(
        client, "GET", _REPLIES_URI,
        paths={"file_token": file_token, "comment_id": comment_id},
        queries=_with_page_token(_paged_queries(args), args),
    )
    if code != 0:
        return tool_error(f"List replies failed: code={code} msg={msg}")
    return tool_result(data)


# ---------------------------------------------------------------------------
# feishu_drive_reply_comment
# ---------------------------------------------------------------------------

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
    client, (file_token, comment_id, content), err = _prepare(
        args, ("file_token", "comment_id", "content"), "file_token, comment_id, and content are required"
    )
    if err:
        return err

    # Replies use the rich "content.elements[text_run]" body shape; file_type is a query param.
    code, msg, data = _do_request(
        client, "POST", _REPLIES_URI,
        paths={"file_token": file_token, "comment_id": comment_id},
        queries=[("file_type", _file_type(args))],
        body={"content": {"elements": [{"type": "text_run", "text_run": {"text": content}}]}},
    )
    if code != 0:
        return tool_error(f"Reply comment failed: code={code} msg={msg}")
    return tool_result(success=True, data=data)


# ---------------------------------------------------------------------------
# feishu_drive_add_comment
# ---------------------------------------------------------------------------

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
    client, (file_token, content), err = _prepare(
        args, ("file_token", "content"), "file_token and content are required"
    )
    if err:
        return err

    # new_comments takes the flat "reply_elements[text]" shape with file_type in the body.
    code, msg, data = _do_request(
        client, "POST", _ADD_COMMENT_URI,
        paths={"file_token": file_token},
        body={"file_type": _file_type(args), "reply_elements": [{"type": "text", "text": content}]},
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
