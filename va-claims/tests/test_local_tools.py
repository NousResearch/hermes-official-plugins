"""Local-first behavior: intent-to-file, benefit tracking and the guide work
with zero credentials and zero network. The 12-month effective-date clock and
the store layout are the contract users depend on."""

from __future__ import annotations

import json

import pytest

import va_claims_plugin


class CfgCtx:
    def __init__(self, config=None):
        self._config = config or {}

    def get_config(self):
        return dict(self._config)


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    for var in ("VA_CLAIMS_ICN", "VA_CLAIMS_ENV", "VA_REFERENCE_API_KEY",
                "VA_CLAIMS_CLIENT_ID", "VA_CLAIMS_PRIVATE_KEY"):
        monkeypatch.delenv(var, raising=False)


def call(tool, args):
    return json.loads(getattr(va_claims_plugin, tool)(CfgCtx(), args))


def test_intent_to_file_starts_the_12_month_clock():
    out = call("va_intent_to_file", {"icn": "ICN-TEST-1", "program": "disability",
                                     "conditions": ["tinnitus", "knee"]})
    assert out["success"]
    assert out["recorded"]["conditions"] == ["tinnitus", "knee"]
    assert out["recorded"]["expires_on"]  # effective-date clock is recorded


def test_benefit_track_and_summary_round_trip():
    add = call("va_benefit_track", {"icn": "ICN-TEST-2", "program": "education",
                                    "status": "active", "expires_on": "2033-01-01"})
    assert add["success"]
    assert add["record"]["status"] == "active"
    summary = call("va_benefit_summary", {"icn": "ICN-TEST-2"})
    assert summary["success"]
    assert summary["benefits"]["education"]["status"] == "active"
    # the expiry registered a deadline countdown
    assert any(d.get("program") == "education" for d in summary["deadlines"])


def test_benefit_track_rejects_unknown_program():
    out = call("va_benefit_track", {"icn": "ICN-TEST-3", "program": "not-a-program",
                                    "status": "active"})
    assert not out["success"]
    assert "Valid:" in out["error"]


def test_guide_cites_federal_standards():
    text = va_claims_plugin.va_claim_guide(CfgCtx(), {"topic": "file"})
    assert "38 CFR" in text


def test_claim_status_without_credentials_returns_hint_not_crash():
    out = call("va_claim_status", {"icn": "ICN-TEST-4"})
    assert not out["success"]
    assert "client_id" in out["error"]
    assert out["hint"]
