"""register(ctx) wiring: the seven tools go through the public plugin API with
the exact names, toolset and schemas the catalog entry advertises."""

from __future__ import annotations

import va_claims_plugin

TOOL_NAMES = [
    "va_claim_status",
    "va_claim_track",
    "va_disability_lookup",
    "va_intent_to_file",
    "va_benefit_track",
    "va_benefit_summary",
    "va_claim_guide",
]


class RecordingCtx:
    def __init__(self):
        self.tools = []

    def register_tool(self, **kwargs):
        self.tools.append(kwargs)


def test_registers_the_seven_tools_in_the_va_claims_toolset():
    ctx = RecordingCtx()
    va_claims_plugin.register(ctx)
    assert [t["name"] for t in ctx.tools] == TOOL_NAMES
    for tool in ctx.tools:
        assert tool["toolset"] == "va_claims"
        assert tool["schema"]["name"] == tool["name"]
        assert tool["schema"]["parameters"]["type"] == "object"
