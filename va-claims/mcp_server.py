#!/usr/bin/env python3
"""Run the va-claims tools as a standalone MCP server (stdio).

The same seven tools the Hermes plugin registers, exposed over MCP so any
MCP client (Claude Desktop, Codex, other agents) can use them without
Hermes. Handlers, schemas, guide text and the local store are imported
from the plugin package unchanged; only the transport differs.

Configuration comes from environment variables (the plugin's settings
equivalents):
  VA_CLAIMS_ICN           default veteran ICN
  VA_CLAIMS_ENV           sandbox | production
  VA_REFERENCE_API_KEY    Benefits Reference Data API key
  VA_CLAIMS_CLIENT_ID     Lighthouse OAuth client ID (claims status/track)
  VA_CLAIMS_PRIVATE_KEY   path to the RSA private key PEM
  HERMES_HOME             where plugins-data/va-claims/ lives (default ~/.hermes)

Run:  python3 mcp_server.py            (mcp>=2, stdio)
Client config example:
  {"mcpServers": {"va-claims": {"command": "python3", "args": ["/path/to/mcp_server.py"]}}}
"""

from __future__ import annotations

import inspect
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import __init__ as va  # noqa: E402  (the plugin package itself)
import schemas  # noqa: E402

from mcp.server.mcpserver import MCPServer  # noqa: E402


class EnvCtx:
    """Stands in for the Hermes plugin ctx: config arrives via env vars."""

    def get_config(self) -> dict:
        return {}


CTX = EnvCtx()

server = MCPServer(
    name="va-claims",
    instructions=(
        "VA benefits claim tracking and filing guidance grounded in 38 CFR "
        "Parts 3, 4, 14 and 38 USC 5107/5901-5905. Local-first: no PII "
        "leaves the machine except the ICN sent to api.va.gov."
    ),
)

_TOOLS = [
    (schemas.VA_CLAIM_STATUS, lambda args: va.va_claim_status(CTX, args)),
    (schemas.VA_CLAIM_TRACK, lambda args: va.va_claim_track(CTX, args)),
    (schemas.VA_DISABILITY_LOOKUP, lambda args: va.va_disability_lookup(CTX, args)),
    (schemas.VA_INTENT_TO_FILE, lambda args: va.va_intent_to_file(CTX, args)),
    (schemas.VA_BENEFIT_TRACK, lambda args: va.va_benefit_track(CTX, args)),
    (schemas.VA_BENEFIT_SUMMARY, lambda args: va.va_benefit_summary(CTX, args)),
    (schemas.VA_CLAIM_GUIDE, lambda args: va.va_claim_guide(CTX, args)),
]

_TYPE_MAP = {"string": str, "integer": int, "number": float, "boolean": bool, "array": list, "object": dict}


def _make_handler(schema: dict, call):
    """Build a real Python signature from the plugin's JSON-Schema so the
    MCP SDK can validate arguments; the body just forwards to the plugin."""
    name = schema["name"]
    props = schema["parameters"]["properties"]
    required = set(schema["parameters"].get("required", []))

    params = []
    annotations = {}
    for key, spec in props.items():
        py_type = _TYPE_MAP.get(spec.get("type", "string"), str)
        default = inspect.Parameter.empty if key in required else None
        params.append(inspect.Parameter(key, inspect.Parameter.POSITIONAL_OR_KEYWORD, default=default))
        annotations[key] = py_type
    annotations["return"] = str

    def tool(*args, **kwargs) -> str:
        return call(dict(kwargs))

    tool.__name__ = name
    tool.__qualname__ = name
    tool.__doc__ = schema["description"]
    tool.__annotations__ = annotations
    tool.__signature__ = inspect.Signature(parameters=params)
    return tool


for _schema, _call in _TOOLS:
    server.add_tool(_make_handler(_schema, _call), name=_schema["name"], description=_schema["description"])


if __name__ == "__main__":
    server.run(transport="stdio")
