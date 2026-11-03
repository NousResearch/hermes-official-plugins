import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from concurrent.futures import ThreadPoolExecutor
import pytest
from hermes_security.store import SecurityStore
from hermes_security.errors import Conflict, NotFound, SealedError, ValidationError


def scan(s):
    return s.create_scan(
        mode="standard",
        safety_level="static",
        target={"root": "/repo", "repoKey": "repo", "snapshotDigest": "sha256:abc"},
        inventory=[{"path": "a.py", "lines": 3}],
        options={},
    )["scan_id"]


def candidate(i="c"):
    return {
        "candidateId": i,
        "ruleId": "r",
        "identity": {"anchor": "a"},
        "locations": [{"path": "a.py", "startLine": 1}],
        "evidenceState": "candidate",
    }


def test_migrations_and_rollback(tmp_path):
    s = SecurityStore(tmp_path)
    a = scan(s)
    with s.transaction() as c:
        assert c.execute("pragma foreign_keys").fetchone()[0] == 1
        assert c.execute("pragma journal_mode").fetchone()[0] == "wal"
        assert c.execute("pragma user_version").fetchone()[0] == 3
    with pytest.raises(RuntimeError):
        with s.transaction() as c:
            c.execute("update scans set status='failed' where scan_id=?", (a,))
            raise RuntimeError("crash")
    assert s.get_scan(a)["status"] == "created"
    assert scan(s) != a
    s.close()
    s = SecurityStore(tmp_path)
    assert s.list_scans()["total"] == 2
    assert s.inventory_paths(a) == {"a.py"}
    assert s.inventory(a, limit=1)[0]["lines"] == 3
    assert s.list_scans(q="/repo", repo_key="repo", status="created")["total"] == 2
    s.close()


def test_concurrency_idempotency_and_late(tmp_path):
    s, t = SecurityStore(tmp_path), SecurityStore(tmp_path)
    a, b = scan(s), scan(s)
    assert s.record_attempt(a, "att_a", "baseline") == s.record_attempt(
        a, "att_a", "baseline"
    )
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(
                lambda i: (s if i % 2 else t).upsert_candidates(
                    a, "att_a", [candidate()]
                ),
                range(24),
            )
        )
    assert sum(r["inserted"] for r in results) == 1
    assert sum(r["duplicates"] for r in results) == 23
    assert len(s.list_candidates(a)) == 1
    with pytest.raises(Conflict):
        s.upsert_candidates(b, "att_a", [candidate()])
    s.finish_attempt(a, "att_a", "finished")
    with pytest.raises(Conflict):
        s.upsert_candidates(a, "att_a", [candidate("late")])
    with pytest.raises(Conflict):
        s.record_attempt(b, "att_a", "baseline")
    s.close()
    t.close()


def test_secret_rejection_atomic_batch(tmp_path):
    s = SecurityStore(
        tmp_path, redactor=lambda text: (text.replace("RAWSECRET", "[redacted]"), [])
    )
    a = scan(s)
    s.record_attempt(a, "att_a", "baseline")
    for extra in ({"secret": {"value": "bad"}}, {"nested": {"text": "RAWSECRET"}}):
        with pytest.raises(ValidationError):
            s.upsert_candidates(
                a, "att_a", [candidate(), dict(candidate("bad"), **extra)]
            )
        assert s.list_candidates(a) == []
    safe = dict(
        candidate(),
        secret={
            "type": "token",
            "fingerprint": "sha256:abc",
            "line": 1,
            "path": "a.py",
        },
    )
    assert s.upsert_candidates(a, "att_a", [safe])["inserted"] == 1


def test_misc_records_and_seal(tmp_path):
    s = SecurityStore(tmp_path)
    a = scan(s)
    s.record_attempt(a, "att_a", "baseline")
    s.upsert_candidates(a, "att_a", [candidate()])
    assert (
        s.set_candidate_state(a, "c", "rejected", reason="not reachable")[
            "evidenceState"
        ]
        == "rejected"
    )
    assert len(s.list_candidates(a, states=["rejected"])) == 1
    s.record_coverage(a, [{"unit": "file:a.py", "state": "reviewed"}], source="att_a")
    s.record_coverage(a, [{"unitId": "file:a.py", "state": "deferred"}], source="att_a")
    assert len(s.coverage_units(a)) == 1
    assert s.coverage_units(a)[0]["state"] == "deferred"
    v = {"receiptId": "v", "candidateId": "c", "status": "passed"}
    assert s.record_validation(a, v) == v
    assert s.validations(a, "c") == [v]
    assert s.validations(a, "no") == []
    assert s.get_chains(a) is None
    doc = {"chains": []}
    assert s.record_chains(a, doc) == doc
    assert s.get_chains(a) == doc
    g = {"grantId": "g", "origins": ["https://example.test"]}
    s.grant_validation(a, g)
    assert s.get_grant("g")["origins"] == g["origins"]
    s.revoke_grant("g")
    assert s.get_grant("g")["revoked"] is True
    s.set_scan_status(a, "completed")
    assert not s.is_sealed(a)
    s.seal(a, "sha256:abc", str(tmp_path / "scans" / a))
    assert s.is_sealed(a)
    mutations = [
        lambda: s.set_scan_status(a, "failed"),
        lambda: s.record_attempt(a, "att_b", "baseline"),
        lambda: s.finish_attempt(a, "att_a", "finished"),
        lambda: s.upsert_candidates(a, "att_a", []),
        lambda: s.set_candidate_state(a, "c", "candidate", reason="x"),
        lambda: s.record_coverage(a, [], source="x"),
        lambda: s.record_validation(a, v),
        lambda: s.record_chains(a, doc),
        lambda: s.upsert_finding_index(a, []),
        lambda: s.grant_validation(a, g),
        lambda: s.revoke_grant("g"),
        lambda: s.lease(a, "owner", 10),
        lambda: s.release(a, "owner"),
        lambda: s.seal(a, "other", "other"),
    ]
    for mutate in mutations:
        with pytest.raises(SealedError):
            mutate()
    assert s.set_scan_status(a, "completed")["status"] == "completed"
    event = s.add_event(a, "note", "post-seal", {"x": 1})
    assert s.events(a, after_id=event["id"] - 1, limit=1)[0]["message"] == "post-seal"
    with pytest.raises(NotFound):
        s.get_scan("missing")
    with pytest.raises(ValidationError):
        s.set_scan_status(a, "nonsense")


def finding(i):
    return {
        "findingId": f"f{i}",
        "occurrenceId": f"o{i}",
        "title": f"Ownership {i}",
        "severity": {"level": "high" if i % 2 else "low"},
        "evidenceState": "source_supported",
        "locations": [{"path": "a.py"}],
        "taxonomy": {"category": "auth", "cwe": ["CWE-639"], "owasp": ["A01:2025"]},
        "provenance": {},
        "codeEvidence": [{"code": "BIG EVIDENCE"}],
    }


def test_finding_index_and_triage(tmp_path):
    s = SecurityStore(tmp_path)
    a = scan(s)
    s.upsert_finding_index(a, [finding(i) for i in range(5)])
    s.upsert_finding_index(a, [finding(0)])
    assert s.list_findings()["total"] == 5
    result = s.list_findings(
        q="CWE-639",
        severity="high",
        evidence_state="source_supported",
        repo_key="repo",
        owasp="A01:2025",
        chained=False,
        scan_id=a,
        limit=1,
        offset=1,
    )
    assert result["total"] == 2 and len(result["items"]) == 1
    assert "codeEvidence" not in result["items"][0]
    assert s.get_finding("f0")["codeEvidence"][0]["code"] == "BIG EVIDENCE"
    s.record_chains(
        a,
        {
            "chains": [
                {
                    "findingIds": ["f0"],
                    "title": "Escalation route",
                    "summary": "chain text",
                }
            ]
        },
    )
    assert s.list_findings(chained=True, q="Escalation")["total"] == 1
    s.seal(a, "digest", str(tmp_path / "scans" / a))
    s.set_triage("f0", "closed", note="done")
    assert s.get_finding("f0", scan_id=a)["triage"]["state"] == "closed"
    assert s.list_findings(triage="open")["total"] == 4
    repo = s.list_repositories()["items"][0]
    assert repo["unresolved_count"] == 4 and repo["last_scan_id"] == a
    assert s.list_repositories(limit=1, offset=1) == {"items": [], "total": 1}
    with pytest.raises(ValidationError):
        s.set_triage("f0", "invalid")
    with pytest.raises(NotFound):
        s.get_finding("missing")
    with pytest.raises(NotFound):
        s.set_triage("missing", "open")


def test_lease_expiry_recovery(tmp_path, monkeypatch):
    s = SecurityStore(tmp_path)
    t = SecurityStore(tmp_path)
    a = scan(s)
    import hermes_security.store.leases as leases

    clock = [100.0]
    monkeypatch.setattr(leases.time, "time", lambda: clock[0])
    assert s.lease(a, "one", 10)
    assert not t.lease(a, "two", 10)
    t.release(a, "two")
    assert not t.lease(a, "two", 10)
    clock[0] = 111
    assert t.lease(a, "two", 10)
    s.release(a, "one")
    assert not s.lease(a, "one", 10)
    t.close()
    clock[0] = 122
    t = SecurityStore(tmp_path)
    assert t.lease(a, "recovered", 10)
    t.release(a, "recovered")
    assert s.lease(a, "one", 10)
    with pytest.raises(ValidationError):
        s.lease(a, "x", -1)
