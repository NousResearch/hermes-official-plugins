"""Shared lark_oapi plumbing for the feishu_doc / feishu_drive tool modules (core's ``tools/feishu_lark.py``).

Holds the client handle of the comment being answered, the cheap availability probe, and the generic
``BaseRequest`` builder + response unpacking (same pattern as feishu_comment.py).
"""

import contextvars
import json

from .adapter import PLATFORM_NAME, _lark_sdk_installed, app_credentials

# The lark client of the comment being answered: feishu_comment sets it around the agent run.
# A context variable, because each tool call runs on a worker thread that gets a copy of the
# agent's context (tools/thread_context.py) and none of its thread-locals. Concurrent comment
# events each run in their own context, so they never see each other's client.
current_client: contextvars.ContextVar = contextvars.ContextVar("feishu_lark_client", default=None)


def get_client():
    """Return the lark client of the comment being answered, or None outside a comment run."""
    return current_client.get()


def _profile_extra() -> dict:
    """``platforms.feishu`` settings of the active profile's config.yaml, from the blocks the gateway
    merges (``gateway.platforms.feishu``, ``platforms.feishu``, ``gateway.feishu``; later wins), with
    top-level keys folded into ``extra`` as ``PlatformConfig.from_dict`` does."""
    from gateway.config import PlatformConfig
    from hermes_cli.config import load_config_readonly

    config = load_config_readonly() or {}
    gateway = config.get("gateway") if isinstance(config.get("gateway"), dict) else {}
    blocks = ((gateway.get("platforms") or {}).get(PLATFORM_NAME) if isinstance(gateway.get("platforms"), dict) else None,
              (config.get("platforms") or {}).get(PLATFORM_NAME) if isinstance(config.get("platforms"), dict) else None,
              gateway.get(PLATFORM_NAME))
    extra: dict = {}
    for block in blocks:
        if isinstance(block, dict):
            extra.update(PlatformConfig.from_dict(block).extra)
    return extra


def _check_feishu() -> bool:
    """The feishu_doc / feishu_drive tools' ``check_fn``: offered only once Feishu is set up for the
    active profile, i.e. lark-oapi is installed and the adapter would have an App ID and App Secret
    (read exactly as it reads them: the profile's scoped env, then its ``platforms.feishu`` config).
    An installed but unconfigured plugin adds nothing to the model's tool list.

    The registry caches the verdict per profile for its 30 s TTL, and the tool-list memo expires with
    it, so credentials added later count on the next session (in a running gateway, within 30 s)."""
    return _lark_sdk_installed() and all(app_credentials(_profile_extra()))


def build_request(method, uri, paths=None, queries=None, body=None):
    """Build a tenant-token BaseRequest. Raises ImportError if lark_oapi is missing."""
    from lark_oapi import AccessTokenType
    from lark_oapi.core.enum import HttpMethod
    from lark_oapi.core.model.base_request import BaseRequest

    builder = (
        BaseRequest.builder()
        .http_method(HttpMethod.GET if method == "GET" else HttpMethod.POST)
        .uri(uri)
        .token_types({AccessTokenType.TENANT}))
    if paths:
        builder = builder.paths(paths)
    if queries:
        builder = builder.queries(queries)
    if body is not None:
        builder = builder.body(body)
    return builder.build()


def lark_call(client, method, uri, paths=None, queries=None, body=None):
    """Build + execute a BaseRequest; returns (code, msg, data_dict).

    Tool handlers run synchronously in a worker thread (no running event loop), so the
    blocking lark client is called directly.
    """
    response = client.request(build_request(method, uri, paths, queries, body))
    return getattr(response, "code", None), getattr(response, "msg", ""), response_data(response)


def raw_body(response):
    """Parsed JSON object of the raw HTTP body, or None when absent/unparseable/not a dict."""
    raw = getattr(response, "raw", None)
    try:
        body = json.loads(raw.content) if raw and hasattr(raw, "content") else None
    except (json.JSONDecodeError, AttributeError):
        return None
    return body if isinstance(body, dict) else None


def response_data(response) -> dict:
    """``data`` of a lark response: prefer the raw JSON body, fall back to typed .data."""
    body = raw_body(response)
    data = body.get("data", {}) if body is not None else {}
    if data:
        return data
    resp_data = getattr(response, "data", None)
    if isinstance(resp_data, dict):
        return resp_data
    return vars(resp_data) if resp_data and hasattr(resp_data, "__dict__") else data
