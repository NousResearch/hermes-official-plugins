"""A numeric epoch in resets_at is a supported wire form, not a missing reset."""
from datetime import datetime

import extra_usage as eu

ISO = "2026-11-25T00:00:00Z"
EPOCH = datetime.fromisoformat(ISO.replace("Z", "+00:00")).timestamp()


def _limits(resets_at):
    return {"extra_usage": {"is_enabled": True},
            "limits": [{"kind": "session", "percent": 100, "resets_at": resets_at}]}


def test_iso_and_numeric_resets_agree():
    assert eu.observe_usage("k-iso", _limits(ISO))[1] == EPOCH
    assert eu.observe_usage("k-sec", _limits(EPOCH))[1] == EPOCH
    assert eu.observe_usage("k-ms", _limits(int(EPOCH * 1000)))[1] == EPOCH
