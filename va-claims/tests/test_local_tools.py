"""Local-first behavior: intent-to-file, benefit tracking and the guide work
with zero credentials and zero network. Local planning must never masquerade
as a VA submission or a protected effective-date deadline."""

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


@pytest.mark.parametrize("environment", ["sandbox", "production"])
def test_intent_to_file_only_saves_local_plan(environment, monkeypatch):
    def no_network(*args, **kwargs):
        pytest.fail("Local intent planning must not call the VA API")
    monkeypatch.setattr(va_claims_plugin, "_http", no_network)
    monkeypatch.setattr(va_claims_plugin, "_ccg_token", no_network)
    ctx = CfgCtx({"environment": environment, "client_id": "configured",
                  "private_key_path": "/unused/key.pem"})
    args = {"icn": "ICN-TEST-1", "conditions": ["tinnitus", "knee"]}
    for _ in range(2):
        out = json.loads(va_claims_plugin.va_intent_to_file(ctx, args))
        assert out["success"]
        assert out["operation"] == "local_record_saved"
        assert out["submitted_to_va"] is False
        assert "NOT submitted to VA" in out["reminder"]
        assert out["recorded"]["conditions"] == ["tinnitus", "knee"]
        assert out["recorded"]["recorded_on"]
        assert "filed_on" not in out["recorded"]
        assert "expires_on" not in out["recorded"]
    summary = call("va_benefit_summary", {"icn": "ICN-TEST-1"})
    assert len(summary["intents"]) == 2
    assert summary["deadlines"] == []
    assert "NOT submitted to VA" in summary["intent_notice"]


def test_legacy_intent_deadline_is_not_presented_as_va_receipt():
    store = va_claims_plugin.store
    icn = "ICN-LEGACY"
    data = store.load(icn)
    data["intent_to_file"] = [{"filed_on": "2026-01-01", "expires_on": "2027-01-01",
                              "conditions": ["knee"], "note": "Keep my evidence plan"}]
    generated = {"label": "Complete claim for intent filed 2026-01-01", "due": "2027-01-01",
                 "program": "disability", "note": "38 CFR 3.155(b): miss this and the effective date is lost"}
    manual = {"label": "Call VA to verify receipt", "due": "2026-01-02", "program": "disability"}
    # An explicitly edited/manual deadline must not be silently hidden.
    edited = {**generated, "note": "Confirmed separately; retain my reminder"}
    data["deadlines"] = [generated, manual, edited]
    path = store.save(icn, data)
    from pathlib import Path
    before = Path(path).read_bytes()
    summary = call("va_benefit_summary", {"icn": icn})
    intent = summary["intents"][0]
    assert intent["legacy_record"]
    assert intent["recorded_on"] == "2026-01-01"
    assert intent["note"] == "Keep my evidence plan"
    assert intent["status"] == "local_plan_only"
    assert "filed_on" not in intent and "expires_on" not in intent
    assert [d["label"] for d in summary["deadlines"]] == [manual["label"], edited["label"]]
    assert store.list_people()[0]["deadlines"] == 2
    assert Path(path).read_bytes() == before  # historical record is preserved


def test_schema_does_not_offer_production_submission():
    description = va_claims_plugin.schemas.VA_INTENT_TO_FILE["description"]
    assert "NEVER submits" in description
    assert "even with credentials" in description


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
