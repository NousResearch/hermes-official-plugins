"""
Feishu/Lark drive document comment handling.

Processes ``drive.notice.comment_add_v1`` events against the Drive v1/v2 comment
APIs, kept separate from the main adapter so comment logic can evolve independently.

Flow: parse event -> access check -> OK reaction -> parallel fetch (doc meta + comment)
-> timeline (whole-doc comments or local thread replies) -> prompt -> AIAgent with
feishu_doc + feishu_drive tools -> deliver reply (whole -> add_whole_comment;
local -> reply_to_comment, falling back to add_whole_comment on 1069302).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# --- Lark SDK helpers (lazy-imported) ---

async def _exec_request(client, method, uri, paths=None, queries=None, body=None):
    """Execute a lark API request (tenant token) and return (code, msg, data_dict)."""
    logger.info("[Feishu-Comment] API >>> %s %s paths=%s queries=%s body=%s",
                method, uri, paths, queries,
                json.dumps(body, ensure_ascii=False)[:500] if body else None)
    from lark_oapi import AccessTokenType
    from lark_oapi.core.enum import HttpMethod
    from lark_oapi.core.model.base_request import BaseRequest
    http_method = HttpMethod.GET if method == "GET" else HttpMethod.POST
    builder = BaseRequest.builder().http_method(http_method).uri(uri).token_types({AccessTokenType.TENANT})
    if paths:
        builder = builder.paths(paths)
    if queries:
        builder = builder.queries(queries)
    if body is not None:
        builder = builder.body(body)
    response = await asyncio.to_thread(client.request, builder.build())
    code = getattr(response, "code", None)
    msg = getattr(response, "msg", "")
    data: dict = {}
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
    logger.info("[Feishu-Comment] API <<< %s %s code=%s msg=%s data_keys=%s",
                method, uri, code, msg, list(data.keys()) if data else "empty")
    if code != 0:
        raw_content = ""
        if raw and hasattr(raw, "content"):
            raw_content = raw.content[:500] if isinstance(raw.content, (str, bytes)) else str(raw.content)[:500]
        logger.warning("[Feishu-Comment] API FAIL raw response: %s", raw_content)
    return code, msg, data


# --- Event parsing ---

def _as_dict(obj: Any) -> dict:
    """Coerce a dict or SDK object (via ``vars()``) into a dict; anything else -> {}."""
    if isinstance(obj, dict):
        return obj
    return vars(obj) if hasattr(obj, "__dict__") else {}


def parse_drive_comment_event(data: Any) -> Optional[Dict[str, Any]]:
    """Extract a flat field dict from a ``drive.notice.comment_add_v1`` payload.

    *data* may be a ``CustomizedEvent`` (WebSocket) whose ``.event`` is a dict,
    or a ``SimpleNamespace`` (Webhook) built from the full JSON body.
    Returns ``None`` when the payload is malformed.
    """
    logger.debug("[Feishu-Comment] parse_drive_comment_event: data type=%s", type(data).__name__)
    event = getattr(data, "event", None)
    if event is None:
        logger.debug("[Feishu-Comment] parse_drive_comment_event: no .event attribute, returning None")
        return None
    evt = _as_dict(event)
    logger.debug("[Feishu-Comment] parse_drive_comment_event: evt keys=%s", list(evt.keys()))
    notice_meta = _as_dict(evt.get("notice_meta") or {})
    from_user = _as_dict(notice_meta.get("from_user_id") or {})
    to_user = _as_dict(notice_meta.get("to_user_id") or {})
    return {
        "event_id": str(evt.get("event_id") or ""),
        "comment_id": str(evt.get("comment_id") or ""),
        "reply_id": str(evt.get("reply_id") or ""),
        "is_mentioned": bool(evt.get("is_mentioned")),
        "timestamp": str(evt.get("timestamp") or ""),
        "file_token": str(notice_meta.get("file_token") or ""),
        "file_type": str(notice_meta.get("file_type") or ""),
        "notice_type": str(notice_meta.get("notice_type") or ""),
        "from_open_id": str(from_user.get("open_id") or ""),
        "to_open_id": str(to_user.get("open_id") or ""),
    }


# --- Drive comment API ---

_REACTION_URI = "/open-apis/drive/v2/files/:file_token/comments/reaction"
_BATCH_QUERY_META_URI = "/open-apis/drive/v1/metas/batch_query"
_BATCH_QUERY_COMMENT_URI = "/open-apis/drive/v1/files/:file_token/comments/batch_query"
_LIST_COMMENTS_URI = "/open-apis/drive/v1/files/:file_token/comments"
_REPLIES_URI = "/open-apis/drive/v1/files/:file_token/comments/:comment_id/replies"
_ADD_COMMENT_URI = "/open-apis/drive/v1/files/:file_token/new_comments"
_WIKI_GET_NODE_URI = "/open-apis/wiki/v2/spaces/get_node"

_COMMENT_RETRY_LIMIT = 6
_COMMENT_RETRY_DELAY_S = 1.0
_MAX_PAGES = 5  # 5 x page_size 100

_REACTION_VERBS = {"add": "added", "delete": "deleted"}


async def update_comment_reaction(
    client: Any, action: str, *, file_token: str, file_type: str, reply_id: str, reaction_type: str = "OK",
) -> bool:
    """Add (``action="add"``) or remove (``"delete"``) an emoji reaction on a comment reply (Drive v2).

    Best-effort: returns ``True`` on success, ``False`` on failure (errors are logged).
    """
    if action == "add":  # the add path is the first SDK touch per event: surface a missing lark_oapi cleanly
        try:
            from lark_oapi import AccessTokenType  # noqa: F401
        except ImportError:
            logger.error("[Feishu-Comment] lark_oapi not available")
            return False
    code, msg, _ = await _exec_request(
        client, "POST", _REACTION_URI, paths={"file_token": file_token},
        queries=[("file_type", file_type)],
        body={"action": action, "reply_id": reply_id, "reaction_type": reaction_type},
    )
    if code == 0:
        logger.info("[Feishu-Comment] Reaction '%s' %s: file=%s:%s reply=%s",
                    reaction_type, _REACTION_VERBS[action], file_type, file_token, reply_id)
    else:
        logger.warning("[Feishu-Comment] Reaction API failed: code=%s msg=%s file=%s:%s reply=%s",
                       code, msg, file_type, file_token, reply_id)
    return code == 0


async def query_document_meta(client: Any, file_token: str, file_type: str) -> Dict[str, Any]:
    """Fetch ``{"title", "url", "doc_type"}`` via the batch_query meta API; empty dict on failure."""
    body = {"request_docs": [{"doc_token": file_token, "doc_type": file_type}], "with_url": True}
    logger.debug("[Feishu-Comment] query_document_meta: file_token=%s file_type=%s", file_token, file_type)
    code, msg, data = await _exec_request(client, "POST", _BATCH_QUERY_META_URI, body=body)
    if code != 0:
        logger.warning("[Feishu-Comment] Meta batch_query failed: code=%s msg=%s", code, msg)
        return {}
    metas = data.get("metas", [])
    logger.debug("[Feishu-Comment] query_document_meta: raw metas type=%s value=%s",
                 type(metas).__name__, str(metas)[:300])
    if metas:
        meta = metas[0] if isinstance(metas, list) else {}
    elif isinstance(metas, dict):  # alternate response shape: keyed by token
        meta = metas.get(file_token, {})
    else:
        logger.debug("[Feishu-Comment] query_document_meta: no metas found")
        return {}
    result = {"title": meta.get("title", ""), "url": meta.get("url", ""), "doc_type": meta.get("doc_type", file_type)}
    logger.info("[Feishu-Comment] query_document_meta: title=%s url=%s",
                result["title"], result["url"][:80] if result["url"] else "")
    return result


async def batch_query_comment(client: Any, file_token: str, file_type: str, comment_id: str) -> Dict[str, Any]:
    """Fetch one comment's details (``is_whole``, ``quote``, ``reply_list``...); empty dict on failure.

    Retries up to ``_COMMENT_RETRY_LIMIT`` times: the comment may not be queryable
    yet when the notice arrives (eventual consistency).
    """
    logger.debug("[Feishu-Comment] batch_query_comment: file_token=%s comment_id=%s", file_token, comment_id)
    for attempt in range(_COMMENT_RETRY_LIMIT):
        code, msg, data = await _exec_request(
            client, "POST", _BATCH_QUERY_COMMENT_URI, paths={"file_token": file_token},
            queries=[("file_type", file_type), ("user_id_type", "open_id")],
            body={"comment_ids": [comment_id]},
        )
        if code == 0:
            break
        if attempt < _COMMENT_RETRY_LIMIT - 1:
            logger.info("[Feishu-Comment] batch_query_comment retry %d/%d: code=%s msg=%s",
                        attempt + 1, _COMMENT_RETRY_LIMIT, code, msg)
            await asyncio.sleep(_COMMENT_RETRY_DELAY_S)
        else:
            logger.warning("[Feishu-Comment] batch_query_comment failed after %d attempts: code=%s msg=%s",
                           _COMMENT_RETRY_LIMIT, code, msg)
            return {}
    items = data.get("items", [])
    logger.debug("[Feishu-Comment] batch_query_comment: got %d items", len(items) if isinstance(items, list) else 0)
    if items and isinstance(items, list):
        item = items[0]
        logger.info("[Feishu-Comment] batch_query_comment: is_whole=%s quote=%s reply_count=%s",
                    item.get("is_whole"), (item.get("quote", "") or "")[:60],
                    len(item.get("reply_list", {}).get("replies", [])) if isinstance(item.get("reply_list"), dict) else "?")
        return item
    logger.warning("[Feishu-Comment] batch_query_comment: empty items, raw data keys=%s", list(data.keys()))
    return {}


async def _list_all_pages(
    client: Any, uri: str, paths: dict, queries: list, *, fail_msg: str, page_msg: str = "",
) -> Tuple[List[Dict[str, Any]], bool]:
    """GET up to ``_MAX_PAGES`` pages of ``items``; returns ``(items, fetch_ok)``.

    *fail_msg* is logged with ``(code, msg)`` on failure; *page_msg* (optional) at debug with ``(page_n, total)``.
    """
    items_out: List[Dict[str, Any]] = []
    page_token = ""
    for _ in range(_MAX_PAGES):
        page_queries = queries + ([("page_token", page_token)] if page_token else [])
        code, msg, data = await _exec_request(client, "GET", uri, paths=paths, queries=page_queries)
        if code != 0:
            logger.warning(fail_msg, code, msg)
            return items_out, False
        items = data.get("items", [])
        if isinstance(items, list):
            items_out.extend(items)
            if page_msg:
                logger.debug(page_msg, len(items), len(items_out))
        if not data.get("has_more"):
            break
        page_token = data.get("page_token", "")
        if not page_token:
            break
    return items_out, True


async def list_whole_comments(client: Any, file_token: str, file_type: str) -> List[Dict[str, Any]]:
    """List all whole-document comments (paginated, up to 500)."""
    logger.debug("[Feishu-Comment] list_whole_comments: file_token=%s", file_token)
    all_comments, _ = await _list_all_pages(
        client, _LIST_COMMENTS_URI, {"file_token": file_token},
        [("file_type", file_type), ("is_whole", "true"), ("page_size", "100"), ("user_id_type", "open_id")],
        fail_msg="[Feishu-Comment] List whole comments failed: code=%s msg=%s",
        page_msg="[Feishu-Comment] list_whole_comments: page got %d items, total=%d",
    )
    logger.info("[Feishu-Comment] list_whole_comments: total %d whole comments fetched", len(all_comments))
    return all_comments


async def list_comment_replies(
    client: Any, file_token: str, file_type: str, comment_id: str, *, expect_reply_id: str = "",
) -> List[Dict[str, Any]]:
    """List all replies in a comment thread (paginated, up to 500).

    If *expect_reply_id* is set and absent from the fetched thread, retries up to
    ``_COMMENT_RETRY_LIMIT`` times (the new reply may not be listed yet).
    """
    logger.debug("[Feishu-Comment] list_comment_replies: file_token=%s comment_id=%s", file_token, comment_id)
    for attempt in range(_COMMENT_RETRY_LIMIT):
        all_replies, fetch_ok = await _list_all_pages(
            client, _REPLIES_URI, {"file_token": file_token, "comment_id": comment_id},
            [("file_type", file_type), ("page_size", "100"), ("user_id_type", "open_id")],
            fail_msg="[Feishu-Comment] List replies failed: code=%s msg=%s",
        )
        if not expect_reply_id or not fetch_ok:
            break
        if any(r.get("reply_id") == expect_reply_id for r in all_replies):
            break
        if attempt < _COMMENT_RETRY_LIMIT - 1:
            logger.info("[Feishu-Comment] list_comment_replies: reply_id=%s not found, retry %d/%d",
                        expect_reply_id, attempt + 1, _COMMENT_RETRY_LIMIT)
            await asyncio.sleep(_COMMENT_RETRY_DELAY_S)
        else:
            logger.warning("[Feishu-Comment] list_comment_replies: reply_id=%s not found after %d attempts",
                           expect_reply_id, _COMMENT_RETRY_LIMIT)
    logger.info("[Feishu-Comment] list_comment_replies: total %d replies fetched", len(all_replies))
    return all_replies


def _sanitize_comment_text(text: str) -> str:
    """Escape characters not allowed in Feishu comment text_run content."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


async def reply_to_comment(
    client: Any, file_token: str, file_type: str, comment_id: str, text: str,
) -> Tuple[bool, int]:
    """Post a reply to a local comment thread. Returns ``(success, code)``."""
    text = _sanitize_comment_text(text)
    logger.info("[Feishu-Comment] reply_to_comment: comment_id=%s text=%s", comment_id, text[:100])
    code, msg, _ = await _exec_request(
        client, "POST", _REPLIES_URI, paths={"file_token": file_token, "comment_id": comment_id},
        queries=[("file_type", file_type)],
        body={"content": {"elements": [{"type": "text_run", "text_run": {"text": text}}]}},
    )
    if code != 0:
        logger.warning("[Feishu-Comment] reply_to_comment FAILED: code=%s msg=%s comment_id=%s", code, msg, comment_id)
    else:
        logger.info("[Feishu-Comment] reply_to_comment OK: comment_id=%s", comment_id)
    return code == 0, code


async def add_whole_comment(client: Any, file_token: str, file_type: str, text: str) -> bool:
    """Add a new whole-document comment. Returns ``True`` on success."""
    text = _sanitize_comment_text(text)
    logger.info("[Feishu-Comment] add_whole_comment: file_token=%s text=%s", file_token, text[:100])
    code, msg, _ = await _exec_request(
        client, "POST", _ADD_COMMENT_URI, paths={"file_token": file_token},
        body={"file_type": file_type, "reply_elements": [{"type": "text", "text": text}]},
    )
    if code != 0:
        logger.warning("[Feishu-Comment] add_whole_comment FAILED: code=%s msg=%s", code, msg)
    else:
        logger.info("[Feishu-Comment] add_whole_comment OK")
    return code == 0


_REPLY_CHUNK_SIZE = 4000


def _chunk_text(text: str, limit: int = _REPLY_CHUNK_SIZE) -> List[str]:
    """Split text into chunks for delivery, preferring line breaks."""
    if len(text) <= limit:
        return [text]
    chunks = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return chunks


async def deliver_comment_reply(
    client: Any, file_token: str, file_type: str, comment_id: str, text: str, is_whole: bool,
) -> bool:
    """Route the agent reply to the right API, chunking long text.

    Whole comment -> add_whole_comment. Local comment -> reply_to_comment; on
    1069302 (reply not allowed) fall back to add_whole_comment for this and all
    later chunks.
    """
    chunks = _chunk_text(text)
    logger.info("[Feishu-Comment] deliver_comment_reply: is_whole=%s comment_id=%s text_len=%d chunks=%d",
                is_whole, comment_id, len(text), len(chunks))
    for i, chunk in enumerate(chunks):
        if len(chunks) > 1:
            logger.info("[Feishu-Comment] deliver_comment_reply: sending chunk %d/%d (%d chars)",
                        i + 1, len(chunks), len(chunk))
        if is_whole:
            ok = await add_whole_comment(client, file_token, file_type, chunk)
        else:
            ok, code = await reply_to_comment(client, file_token, file_type, comment_id, chunk)
            if not ok and code == 1069302:
                logger.info("[Feishu-Comment] Reply not allowed (1069302), falling back to add_whole_comment")
                ok = await add_whole_comment(client, file_token, file_type, chunk)
                is_whole = True
        if not ok:
            return False
    return True


# --- Comment content extraction helpers ---

def _extract_reply_text(reply: Dict[str, Any], *, semantic: bool = False, self_open_id: str = "") -> str:
    """Plain text of a reply's content (text_run / docs_link / person elements).

    Person mentions render as ``@<user_id>``. In *semantic* mode (for the prompt's
    "current text"), the self @mention is dropped (it is routing, not content), an
    unknown mention renders as ``@`` and whitespace is collapsed.
    """
    content = reply.get("content", {})
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            return content
    missing_uid = "" if semantic else "unknown"
    parts = []
    for elem in content.get("elements", []):
        etype = elem.get("type")
        if etype == "text_run":
            parts.append(elem.get("text_run", {}).get("text", ""))
        elif etype == "docs_link":
            parts.append(elem.get("docs_link", {}).get("url", ""))
        elif etype == "person":
            uid = elem.get("person", {}).get("user_id", missing_uid)
            if semantic and self_open_id and uid == self_open_id:
                continue
            parts.append(f"@{uid}")
    text = "".join(parts)
    return " ".join(text.split()).strip() if semantic else text


def _get_reply_user_id(reply: Dict[str, Any]) -> str:
    """Extract user_id from a reply dict."""
    user_id = reply.get("user_id", "")
    if isinstance(user_id, dict):
        return user_id.get("open_id", "") or user_id.get("user_id", "")
    return str(user_id)


def _extract_semantic_text(reply: Dict[str, Any], self_open_id: str = "") -> str:
    """Semantic text of a reply: self @mention stripped, whitespace collapsed."""
    return _extract_reply_text(reply, semantic=True, self_open_id=self_open_id)


def _reply_list_replies(whole_comment: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return the ``reply_list.replies`` of a whole comment (``reply_list`` may be a JSON string)."""
    reply_list = whole_comment.get("reply_list", {})
    if isinstance(reply_list, str):
        try:
            reply_list = json.loads(reply_list)
        except (json.JSONDecodeError, TypeError):
            reply_list = {}
    return reply_list.get("replies", [])


# --- Document link parsing and wiki resolution ---

# Matches feishu/lark document URLs and extracts doc_type + token
_FEISHU_DOC_URL_RE = re.compile(
    r"(?:feishu\.cn|larkoffice\.com|larksuite\.com|lark\.suite\.com)"
    r"/(?P<doc_type>wiki|doc|docx|sheet|sheets|slides|mindnote|bitable|base|file)"
    r"/(?P<token>[A-Za-z0-9_-]{10,40})"
)


def _extract_docs_links(replies: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """Extract unique ``{"url", "doc_type", "token"}`` document links from comment replies."""
    seen_tokens = set()
    links = []
    for reply in replies:
        content = reply.get("content", {})
        if isinstance(content, str):
            try:
                content = json.loads(content)
            except (json.JSONDecodeError, TypeError):
                continue
        for elem in content.get("elements", []):
            if elem.get("type") not in {"docs_link", "link"}:
                continue
            url = (elem.get("docs_link") or elem.get("link") or {}).get("url", "")
            m = _FEISHU_DOC_URL_RE.search(url) if url else None
            if not m or m.group("token") in seen_tokens:
                continue
            seen_tokens.add(m.group("token"))
            links.append({"url": url, "doc_type": m.group("doc_type"), "token": m.group("token")})
    return links


async def _reverse_lookup_wiki_token(client: Any, obj_type: str, obj_token: str) -> Optional[str]:
    """Return the wiki node_token owning *obj_token*, or None if not a wiki doc / API failure."""
    code, msg, data = await _exec_request(
        client, "GET", _WIKI_GET_NODE_URI, queries=[("token", obj_token), ("obj_type", obj_type)],
    )
    if code == 0:
        return data.get("node", {}).get("node_token", "") or None
    logger.warning("[Feishu-Comment] Wiki reverse lookup failed: code=%s msg=%s obj=%s:%s", code, msg, obj_type, obj_token)
    return None


async def _resolve_wiki_nodes(client: Any, links: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """Annotate wiki links in-place with ``resolved_type``/``resolved_token``; non-wiki links untouched."""
    for link in links:
        if link["doc_type"] != "wiki":
            continue
        wiki_token = link["token"]
        code, msg, data = await _exec_request(client, "GET", _WIKI_GET_NODE_URI, queries=[("token", wiki_token)])
        if code != 0:
            logger.warning("[Feishu-Comment] Wiki resolve failed: code=%s msg=%s token=%s", code, msg, wiki_token)
            continue
        node = data.get("node", {})
        resolved_type, resolved_token = node.get("obj_type", ""), node.get("obj_token", "")
        if resolved_type and resolved_token:
            logger.info("[Feishu-Comment] Wiki resolved: %s -> %s:%s", wiki_token, resolved_type, resolved_token)
            link["resolved_type"] = resolved_type
            link["resolved_token"] = resolved_token
        else:
            logger.warning("[Feishu-Comment] Wiki resolve returned empty: %s", wiki_token)
    return links


def _format_referenced_docs(links: List[Dict[str, str]], current_file_token: str = "") -> str:
    """Format resolved document links for prompt embedding."""
    if not links:
        return ""
    lines = ["", "Referenced documents in comments:"]
    for link in links:
        rtype = link.get("resolved_type", link["doc_type"])
        rtoken = link.get("resolved_token", link["token"])
        suffix = " (same as current document)" if rtoken == current_file_token else ""
        lines.append(f"- {rtype}:{rtoken}{suffix} ({link['url'][:80]})")
    return "\n".join(lines)


async def _referenced_docs_text(client: Any, replies: List[Dict[str, Any]], file_token: str) -> str:
    """Extract, wiki-resolve and format the document links found in *replies*."""
    doc_links = _extract_docs_links(replies)
    if doc_links:
        doc_links = await _resolve_wiki_nodes(client, doc_links)
    return _format_referenced_docs(doc_links, file_token)


# --- Prompt construction ---

_PROMPT_TEXT_LIMIT = 220
_LOCAL_TIMELINE_LIMIT = 20
_WHOLE_TIMELINE_LIMIT = 12

Timeline = List[Tuple[str, str, bool]]  # [(user_id, text, is_self)]


def _truncate(text: str, limit: int = _PROMPT_TEXT_LIMIT) -> str:
    """Truncate text for prompt embedding."""
    return text if len(text) <= limit else text[:limit] + "..."


def _select_timeline(timeline: Timeline, limit: int, center: int, pinned: Tuple[int, ...] = ()) -> Timeline:
    """Select up to *limit* entries: *pinned* + *center*, then expand outward from *center*.

    Out-of-range indices are ignored; if nothing is selectable, falls back to the last *limit* entries.
    """
    if len(timeline) <= limit:
        return timeline
    n = len(timeline)
    selected = {i for i in (*pinned, center) if 0 <= i < n}
    budget = limit - len(selected)
    lo, hi = center - 1, center + 1
    while budget > 0 and (lo >= 0 or hi < n):
        if lo >= 0 and lo not in selected:
            selected.add(lo)
            budget -= 1
        lo -= 1
        if budget > 0 and hi < n and hi not in selected:
            selected.add(hi)
            budget -= 1
        hi += 1
    if not selected:
        return timeline[-limit:]
    return [timeline[i] for i in sorted(selected)]


_COMMON_INSTRUCTIONS = """
This is a Feishu document comment thread, not an IM chat.
Do NOT call feishu_drive_add_comment or feishu_drive_reply_comment yourself.
Your reply will be posted automatically. Just output the reply text.
Use the thread timeline above as the main context.
If the quoted content is not enough, use feishu_doc_read to read nearby context.
The quoted content is your primary anchor — insert/summarize/explain requests are about it.
Do not guess document content you haven't read.
Reply in the same language as the user's comment unless they request otherwise.
Use plain text only. Do not use Markdown, headings, bullet lists, tables, or code blocks.
Do not show your reasoning process. Do not start with "I will", "Let me", or "I'll first".
Output only the final user-facing reply.
If no reply is needed, output exactly NO_REPLY.
""".strip()


def _finish_prompt(lines: List[str], selected: Timeline, referenced_docs: str) -> str:
    """Append the timeline entries, referenced docs and common instructions."""
    for user_id, text, is_self in selected:
        lines.append(f"[{user_id}] {_truncate(text)}{' <-- YOU' if is_self else ''}")
    if referenced_docs:
        lines.append(referenced_docs)
    lines += ["", _COMMON_INSTRUCTIONS]
    return "\n".join(lines)


def build_local_comment_prompt(
    *, doc_title: str, doc_url: str, file_token: str, file_type: str, comment_id: str, quote_text: str,
    root_comment_text: str, target_reply_text: str, timeline: Timeline, self_open_id: str,
    target_index: int = -1, referenced_docs: str = "",
) -> str:
    """Build the prompt for a local (quoted-text) comment."""
    selected = _select_timeline(timeline, _LOCAL_TIMELINE_LIMIT, target_index, pinned=(0, len(timeline) - 1))
    lines = [
        f'The user added a reply in "{doc_title}".',
        f'Current user comment text: "{_truncate(target_reply_text)}"',
        f'Original comment text: "{_truncate(root_comment_text)}"',
        f'Quoted content: "{_truncate(quote_text, 500)}"',
        "This comment mentioned you (@mention is for routing, not task content).",
        f"Document link: {doc_url}",
        "Current commented document:",
        f"- file_type={file_type}",
        f"- file_token={file_token}",
        f"- comment_id={comment_id}",
        "",
        f"Current comment card timeline ({len(selected)}/{len(timeline)} entries):",
    ]
    return _finish_prompt(lines, selected, referenced_docs)


def build_whole_comment_prompt(
    *, doc_title: str, doc_url: str, file_token: str, file_type: str, comment_text: str, timeline: Timeline,
    self_open_id: str, current_index: int = -1, nearest_self_index: int = -1, referenced_docs: str = "",
) -> str:
    """Build the prompt for a whole-document comment."""
    selected = _select_timeline(timeline, _WHOLE_TIMELINE_LIMIT, current_index, pinned=(nearest_self_index,))
    lines = [
        f'The user added a comment in "{doc_title}".',
        f'Current user comment text: "{_truncate(comment_text)}"',
        "This is a whole-document comment.",
        "This comment mentioned you (@mention is for routing, not task content).",
        f"Document link: {doc_url}",
        "Current commented document:",
        f"- file_type={file_type}",
        f"- file_token={file_token}",
        "",
        f"Whole-document comment timeline ({len(selected)}/{len(timeline)} entries):",
    ]
    return _finish_prompt(lines, selected, referenced_docs)


# --- Agent execution ---

def _resolve_model_and_runtime() -> Tuple[str, dict]:
    """Resolve model and provider credentials, same as gateway message handling."""
    from gateway.run import _load_gateway_config, _resolve_gateway_model, _resolve_runtime_agent_kwargs
    model = _resolve_gateway_model(_load_gateway_config())
    runtime_kwargs = _resolve_runtime_agent_kwargs()
    if not model and runtime_kwargs.get("provider"):  # fall back to the provider's default model
        try:
            from hermes_cli.models import get_default_model_for_provider
            model = get_default_model_for_provider(runtime_kwargs["provider"])
        except Exception:
            pass
    return model, runtime_kwargs


# Session cache for cross-card memory within the same document.
_SESSION_MAX_MESSAGES = 50  # keep last N messages per document session
_SESSION_TTL_S = 3600       # expire sessions after 1 hour of inactivity

_session_cache_lock = threading.Lock()
_session_cache: Dict[str, Dict] = {}  # key -> {"messages": [...], "last_access": float}


def _session_key(file_type: str, file_token: str) -> str:
    return f"comment-doc:{file_type}:{file_token}"


def _load_session_history(key: str) -> List[Dict[str, Any]]:
    """Load conversation history for a document session (expires after ``_SESSION_TTL_S``)."""
    with _session_cache_lock:
        entry = _session_cache.get(key)
        if entry is None:
            return []
        if time.time() - entry["last_access"] > _SESSION_TTL_S:
            del _session_cache[key]
            logger.info("[Feishu-Comment] Session expired: %s", key)
            return []
        entry["last_access"] = time.time()
        return list(entry["messages"])


def _save_session_history(key: str, messages: List[Dict[str, Any]]) -> None:
    """Save the last N user/assistant messages (system messages and tool internals stripped)."""
    cleaned = [m for m in messages if m.get("role") in {"user", "assistant"} and m.get("content")]
    cleaned = cleaned[-_SESSION_MAX_MESSAGES:]
    with _session_cache_lock:
        _session_cache[key] = {"messages": cleaned, "last_access": time.time()}
        logger.info("[Feishu-Comment] Session saved: %s (%d messages)", key, len(cleaned))


def _run_comment_agent(prompt: str, client: Any, session_key: str = "") -> str:
    """Create an AIAgent with feishu tools and run the prompt; empty string on failure.

    *session_key*, if given, loads/saves history for cross-card memory in the same document.
    """
    from run_agent import AIAgent
    logger.info("[Feishu-Comment] _run_comment_agent: injecting lark client into tool thread-locals")
    from tools import feishu_doc_tool, feishu_drive_tool
    tool_mods = (feishu_doc_tool, feishu_drive_tool)
    for mod in tool_mods:
        mod.set_client(client)
    try:
        model, runtime_kwargs = _resolve_model_and_runtime()
        logger.info("[Feishu-Comment] _run_comment_agent: model=%s provider=%s base_url=%s",
                    model, runtime_kwargs.get("provider"), (runtime_kwargs.get("base_url") or "")[:50])
        history = _load_session_history(session_key) if session_key else []
        if history:
            logger.info("[Feishu-Comment] _run_comment_agent: loaded %d history messages from session %s",
                        len(history), session_key)
        agent = AIAgent(
            model=model,
            **{k: runtime_kwargs.get(k) for k in ("base_url", "api_key", "provider", "api_mode", "credential_pool")},
            quiet_mode=True, skip_context_files=True, skip_memory=True, max_iterations=15,
            enabled_toolsets=["feishu_doc", "feishu_drive"],
        )
        logger.info("[Feishu-Comment] _run_comment_agent: calling run_conversation (prompt=%d chars, history=%d)",
                    len(prompt), len(history))
        result = agent.run_conversation(prompt, conversation_history=history or None)
        response = (result.get("final_response") or "").strip()
        logger.info("[Feishu-Comment] _run_comment_agent: done api_calls=%d response_len=%d response=%s",
                    result.get("api_calls", 0), len(response), response[:200])
        if session_key and result.get("messages", []):
            _save_session_history(session_key, result["messages"])
        return response
    except Exception as e:
        logger.exception("[Feishu-Comment] _run_comment_agent: agent failed: %s", e)
        return ""
    finally:
        for mod in tool_mods:
            mod.set_client(None)


# --- Event handler entry point ---

_NO_REPLY_SENTINEL = "NO_REPLY"
_ALLOWED_NOTICE_TYPES = {"add_comment", "add_reply"}


def _last_index_where(timeline: Timeline, pred) -> Optional[Tuple[str, int]]:
    """Return ``(text, index)`` of the last timeline entry matching *pred*, or None."""
    for i in range(len(timeline) - 1, -1, -1):
        if pred(timeline[i]):
            return timeline[i][1], i
    return None


def _timeline_entry(r: Dict[str, Any], self_open_id: str) -> Tuple[str, str, bool]:
    uid = _get_reply_user_id(r)
    return uid, _extract_reply_text(r), (uid == self_open_id) if self_open_id else False


async def _whole_comment_prompt(client: Any, from_open_id: str, doc: dict) -> str:
    """Build the prompt for a whole-document comment from all whole comments on the doc.

    *doc* = build_*_prompt's shared kwargs (doc_title, doc_url, file_token, file_type, self_open_id).
    """
    file_token, file_type, self_open_id = doc["file_token"], doc["file_type"], doc["self_open_id"]
    logger.info("[Feishu-Comment] Fetching whole-document comments for timeline...")
    whole_comments = await list_whole_comments(client, file_token, file_type)
    timeline: Timeline = []
    all_raw_replies: List[Dict[str, Any]] = []
    current_text, current_index, nearest_self_index = "", -1, -1
    for wc in whole_comments:
        replies = _reply_list_replies(wc)
        all_raw_replies.extend(replies)
        for r in replies:
            uid, _, is_self = entry = _timeline_entry(r, self_open_id)
            idx = len(timeline)
            timeline.append(entry)
            if uid == from_open_id:
                current_text, current_index = _extract_semantic_text(r, self_open_id), idx
            if is_self:
                nearest_self_index = idx
    if not current_text and (found := _last_index_where(timeline, lambda e: not e[2])):
        current_text, current_index = found
    logger.info("[Feishu-Comment] Whole timeline: %d entries, current_idx=%d, self_idx=%d, text=%s",
                len(timeline), current_index, nearest_self_index, current_text[:80] if current_text else "(empty)")
    return build_whole_comment_prompt(
        comment_text=current_text, timeline=timeline, current_index=current_index, nearest_self_index=nearest_self_index,
        referenced_docs=await _referenced_docs_text(client, all_raw_replies, file_token), **doc,
    )


async def _local_comment_prompt(
    client: Any, comment_id: str, reply_id: str, from_open_id: str, quote_text: str, doc: dict,
) -> str:
    """Build the prompt for a local comment from its thread replies (*doc* as in _whole_comment_prompt)."""
    file_token, file_type, self_open_id = doc["file_token"], doc["file_type"], doc["self_open_id"]
    logger.info("[Feishu-Comment] Fetching comment thread replies...")
    replies = await list_comment_replies(client, file_token, file_type, comment_id, expect_reply_id=reply_id)
    timeline: Timeline = [_timeline_entry(r, self_open_id) for r in replies]
    root_text = _extract_semantic_text(replies[0], self_open_id) if replies else ""
    target_text, target_index = "", -1
    for i, r in enumerate(replies):
        rid = r.get("reply_id", "")
        if rid and rid == reply_id:
            target_text, target_index = _extract_semantic_text(r, self_open_id), i
    if not target_text and (found := _last_index_where(timeline, lambda e: e[0] == from_open_id)):
        target_text, target_index = found
    logger.info("[Feishu-Comment] Local timeline: %d entries, target_idx=%d, quote=%s root=%s target=%s",
                len(timeline), target_index, quote_text[:60] if quote_text else "(empty)",
                root_text[:60] if root_text else "(empty)",
                target_text[:60] if target_text else "(empty)")
    return build_local_comment_prompt(
        comment_id=comment_id, quote_text=quote_text, root_comment_text=root_text, target_reply_text=target_text,
        timeline=timeline, target_index=target_index,
        referenced_docs=await _referenced_docs_text(client, replies, file_token), **doc,
    )


async def handle_drive_comment_event(client: Any, data: Any, *, self_open_id: str = "") -> None:
    """Full orchestration for a drive comment event.

    Parse + filter (self-reply, receiver, notice_type) -> access rules -> OK reaction
    -> parallel fetch (doc meta + comment) -> build timeline/prompt by is_whole
    -> run agent -> deliver reply -> remove OK reaction.
    """
    logger.info("[Feishu-Comment] ========== handle_drive_comment_event START ==========")
    parsed = parse_drive_comment_event(data)
    if parsed is None:
        logger.warning("[Feishu-Comment] Dropping malformed drive comment event")
        return
    logger.info("[Feishu-Comment] [Step 0/5] Event parsed successfully")

    file_token, file_type, comment_id, reply_id, from_open_id, to_open_id, notice_type = (
        parsed[k] for k in ("file_token", "file_type", "comment_id", "reply_id", "from_open_id", "to_open_id", "notice_type")
    )

    if from_open_id and self_open_id and from_open_id == self_open_id:
        logger.debug("[Feishu-Comment] Skipping self-authored event: from=%s", from_open_id)
        return
    if not to_open_id or (self_open_id and to_open_id != self_open_id):
        logger.debug("[Feishu-Comment] Skipping event not addressed to self: to=%s", to_open_id or "(empty)")
        return
    if notice_type and notice_type not in _ALLOWED_NOTICE_TYPES:
        logger.debug("[Feishu-Comment] Skipping notice_type=%s", notice_type)
        return
    if not file_token or not file_type or not comment_id:
        logger.warning("[Feishu-Comment] Missing required fields, skipping")
        return
    logger.info("[Feishu-Comment] Event: notice=%s file=%s:%s comment=%s from=%s",
                notice_type, file_type, file_token, comment_id, from_open_id)

    # Access control. Wiki-hosted docs report their underlying obj token, so when no
    # exact rule matched and the config has wiki: keys, reverse-lookup the wiki node.
    from plugins.platforms.feishu.feishu_comment_rules import load_config, resolve_rule, is_user_allowed, has_wiki_keys

    comments_cfg = load_config()
    rule = resolve_rule(comments_cfg, file_type, file_token)
    if rule.match_source in {"wildcard", "top"} and has_wiki_keys(comments_cfg):
        wiki_token = await _reverse_lookup_wiki_token(client, file_type, file_token)
        if wiki_token:
            rule = resolve_rule(comments_cfg, file_type, file_token, wiki_token=wiki_token)
    if not rule.enabled:
        logger.info("[Feishu-Comment] Comments disabled for %s:%s, skipping", file_type, file_token)
        return
    if not is_user_allowed(rule, from_open_id):
        logger.info("[Feishu-Comment] User %s denied (policy=%s, rule=%s)", from_open_id, rule.policy, rule.match_source)
        return
    logger.info("[Feishu-Comment] Access granted: user=%s policy=%s rule=%s", from_open_id, rule.policy, rule.match_source)

    reaction_kwargs = dict(file_token=file_token, file_type=file_type, reply_id=reply_id, reaction_type="OK")
    if reply_id:
        asyncio.ensure_future(update_comment_reaction(client, "add", **reaction_kwargs))

    logger.info("[Feishu-Comment] [Step 2/5] Parallel fetch: doc meta + comment batch_query")
    doc_meta, comment_detail = await asyncio.gather(
        asyncio.ensure_future(query_document_meta(client, file_token, file_type)),
        asyncio.ensure_future(batch_query_comment(client, file_token, file_type, comment_id)),
    )
    doc_title = doc_meta.get("title", "Untitled")
    doc_url = doc_meta.get("url", "")
    is_whole = bool(comment_detail.get("is_whole"))
    logger.info("[Feishu-Comment] Comment context: title=%s is_whole=%s", doc_title, is_whole)

    logger.info("[Feishu-Comment] [Step 3/5] Building timeline (is_whole=%s)", is_whole)
    doc = dict(doc_title=doc_title, doc_url=doc_url, file_token=file_token, file_type=file_type, self_open_id=self_open_id)
    if is_whole:
        prompt = await _whole_comment_prompt(client, from_open_id, doc)
    else:
        prompt = await _local_comment_prompt(client, comment_id, reply_id, from_open_id, comment_detail.get("quote", ""), doc)
    logger.info("[Feishu-Comment] [Step 4/5] Prompt built (%d chars), running agent...", len(prompt))
    logger.debug("[Feishu-Comment] Full prompt:\n%s", prompt)

    # run_conversation is synchronous -> thread. Session key groups all comment cards on one doc.
    sess_key = _session_key(file_type, file_token)
    response = await asyncio.get_running_loop().run_in_executor(None, _run_comment_agent, prompt, client, sess_key)

    if not response or _NO_REPLY_SENTINEL in response:
        logger.info("[Feishu-Comment] Agent returned NO_REPLY, skipping delivery")
    else:
        logger.info("[Feishu-Comment] Agent response (%d chars): %s", len(response), response[:200])
        logger.info("[Feishu-Comment] [Step 5/5] Delivering reply (is_whole=%s, comment_id=%s)", is_whole, comment_id)
        if await deliver_comment_reply(client, file_token, file_type, comment_id, response, is_whole):
            logger.info("[Feishu-Comment] Reply delivered successfully")
        else:
            logger.error("[Feishu-Comment] Failed to deliver reply")

    if reply_id:  # best-effort cleanup of the OK reaction
        await update_comment_reaction(client, "delete", **reaction_kwargs)
    logger.info("[Feishu-Comment] ========== handle_drive_comment_event END ==========")
