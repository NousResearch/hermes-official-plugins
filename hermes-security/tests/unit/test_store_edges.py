import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor
import sqlite3
import threading
import pytest
from hermes_security.store import SecurityStore
from hermes_security.store.migrations import migrate
from hermes_security.errors import Conflict, NotFound, SealedError, ValidationError


def new_scan(s):
    return s.create_scan(
        mode="standard",
        safety_level="static",
        target={"root": "/repo", "repoKey": "r"},
        inventory=[{"path": "x", "lines": 1}],
        options={},
    )["scan_id"]


def _process_writer(args):
    directory, sid, number = args
    s = SecurityStore(directory)
    for i in range(8):
        s.add_event(sid, "process", f"{number}:{i}")
    s.close()
    return True


def test_process_writers(tmp_path):
    s = SecurityStore(tmp_path)
    sid = new_scan(s)
    with ProcessPoolExecutor(4) as pool:
        assert all(pool.map(_process_writer, [(tmp_path, sid, i) for i in range(4)]))
    events = s.events(sid)
    assert len(events) == 32 and len({e["message"] for e in events}) == 32


def test_migration_reentry_and_future_version():
    c = sqlite3.connect(":memory:")
    c.execute("BEGIN IMMEDIATE")
    migrate(c)
    c.commit()
    c.execute("BEGIN IMMEDIATE")
    migrate(c)
    c.commit()
    assert c.execute("pragma user_version").fetchone()[0] == 3
    c.execute("pragma user_version=4")
    with pytest.raises(Conflict):
        migrate(c)
    c.close()


def test_version_1_database_upgrades_in_place():
    from hermes_security.store.migrations import DDL
    c = sqlite3.connect(":memory:")
    for statement in DDL:
        c.execute(statement)
    c.execute("pragma user_version=1")
    c.execute("BEGIN IMMEDIATE")
    migrate(c)
    c.commit()
    assert c.execute("pragma user_version").fetchone()[0] == 3
    assert c.execute("select count(*) from dispatches").fetchone()[0] == 0
    c.close()


def test_database_from_before_the_detector_removal_can_index_findings(tmp_path):
    """Profiles created before the detector backends were removed still carry
    findings.detectors (NOT NULL) and detector_runs. The v1 DDL was edited without
    a migration, so finalize failed indexing any finding on those databases."""
    from hermes_security.store.migrations import DDL, DDL_V2
    old = [s.replace("owasp TEXT NOT NULL,", "owasp TEXT NOT NULL, detectors TEXT NOT NULL,") for s in DDL]
    old.append("CREATE TABLE detector_runs (scan_id TEXT REFERENCES scans(scan_id), receipt_id TEXT NOT NULL, "
               "data TEXT NOT NULL, PRIMARY KEY(scan_id,receipt_id))")
    c = sqlite3.connect(tmp_path / "security.db")
    for statement in old + DDL_V2:
        c.execute(statement)
    c.execute("pragma user_version=2")
    c.commit()
    c.execute("BEGIN IMMEDIATE")
    migrate(c)
    c.commit()
    assert c.execute("pragma user_version").fetchone()[0] == 3
    assert "detectors" not in [r[1] for r in c.execute("pragma table_info(findings)")]
    assert c.execute("select count(*) from sqlite_master where name='detector_runs'").fetchone()[0] == 0
    c.execute("INSERT INTO repositories VALUES ('r','/repo')")
    c.execute("INSERT INTO scans(scan_id,repo_key,root,mode,safety_level,status,created_at,target,options) "
              "VALUES ('s','r','/repo','standard','static','awaiting_analysis','t','{}','{}')")
    c.execute("INSERT INTO findings(scan_id,occurrence_id,finding_id,severity,evidence_state,title,repo_key,root,path,"
              "category,cwe,owasp,data) VALUES ('s','o','f','low','source_supported','t','r','/repo','p','c','[]','[]','{}')")
    c.close()


def test_canonical_columns_and_foreign_keys(tmp_path):
    s = SecurityStore(tmp_path)
    sid = new_scan(s)
    s.add_event(sid, "note", "unicode", {"z": "é", "a": [1]})
    with s.transaction() as c:
        assert c.execute("SELECT data FROM events").fetchone()[0] == '{"a":[1],"z":"é"}'
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("INSERT INTO chains VALUES('foreign','{}')")
    with pytest.raises(ValidationError):
        s.add_event(sid, "x", "x", {"x": float("nan")})
    assert len(s.events(sid)) == 1


def test_bounded_busy_retry(tmp_path, monkeypatch):
    s = SecurityStore(tmp_path)
    sid = new_scan(s)
    blocker = sqlite3.connect(s.path, isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    original = s._connection

    @contextmanager
    def quick_connection():
        with original() as c:
            c.execute("PRAGMA busy_timeout=1")
            yield c

    monkeypatch.setattr(s, "_connection", quick_connection)
    with pytest.raises(Conflict, match="busy"):
        s.add_event(sid, "x", "blocked")
    blocker.rollback()
    blocker.close()
    assert s.events(sid) == []
    s.add_event(sid, "x", "retry works")
    assert len(s.events(sid)) == 1


def test_concurrent_lease_and_seal_race(tmp_path):
    a = SecurityStore(tmp_path)
    b = SecurityStore(tmp_path)
    sid = new_scan(a)
    barrier = threading.Barrier(2)

    def claim(pair):
        store, owner = pair
        barrier.wait()
        return store.lease(sid, owner, 60)

    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(claim, [(a, "a"), (b, "b")])) == [False, True]
    a.record_attempt(sid, "att", "baseline")
    barrier = threading.Barrier(2)

    def seal():
        barrier.wait()
        a.seal(sid, "digest", "artifacts")

    def submit():
        barrier.wait()
        try:
            return b.upsert_candidates(sid, "att", [{"candidateId": "c"}])
        except SealedError:
            return "sealed"

    with ThreadPoolExecutor(2) as pool:
        one = pool.submit(seal)
        two = pool.submit(submit)
        one.result()
        result = two.result()
    assert a.is_sealed(sid)
    assert len(a.list_candidates(sid)) == (0 if result == "sealed" else 1)
    with pytest.raises(SealedError):
        b.upsert_candidates(sid, "att", [{"candidateId": "later"}])


def test_occurrences_latest_and_coverage(tmp_path):
    s = SecurityStore(tmp_path)
    a = new_scan(s)
    f = {
        "findingId": "f",
        "occurrenceId": "o1",
        "title": "first",
        "locations": [{"path": "x"}],
    }
    s.upsert_finding_index(a, [f, dict(f, occurrenceId="o2")])
    assert s.list_findings()["total"] == 2
    s.set_triage("f", "accepted_risk")
    assert s.list_findings(triage="accepted_risk")["total"] == 2
    b = new_scan(s)
    s.upsert_finding_index(b, [dict(f, occurrenceId="o3", title="latest")])
    assert s.get_finding("f")["title"] == "latest"
    assert s.get_finding("f", scan_id=a)["title"] == "first"
    assert s.list_repositories()["items"][0]["coverage_completeness"] == "partial"
    units = [
        {"unit": f"lane:A{i:02}:2025", "state": "not_applicable"} for i in range(1, 11)
    ]
    units.append({"unit": "file:x", "state": "reviewed"})
    s.record_coverage(b, units, source="review")
    assert s.list_repositories()["items"][0]["coverage_completeness"] == "complete"
    s.record_coverage(b, [{"unit": "file:y", "state": "deferred"}], source="review")
    assert s.list_repositories()["items"][0]["coverage_completeness"] == "partial"
    assert s.list_findings(q="' OR 1=1 --")["total"] == 0


def test_errors_and_atomic_batches(tmp_path):
    s = SecurityStore(tmp_path)
    sid = new_scan(s)
    with pytest.raises(ValidationError):
        s.list_findings(limit=-1)
    with pytest.raises(ValidationError):
        s.list_scans(offset=-1)
    with pytest.raises(ValidationError):
        s.create_scan(
            mode="bad", safety_level="static", target={}, inventory=[], options={}
        )
    with pytest.raises(NotFound):
        s.get_grant("absent")
    with pytest.raises(NotFound):
        s.revoke_grant("absent")
    with pytest.raises(NotFound):
        s.set_candidate_state(sid, "absent", "rejected", reason="x")
    with pytest.raises(ValidationError):
        s.record_coverage(
            sid,
            [
                {"unit": "file:x", "state": "reviewed"},
                {"unit": "bad", "state": "invalid"},
            ],
            source="worker",
        )
    assert s.coverage_units(sid) == [] and s.events(sid) == []
    s.record_attempt(sid, "att", "baseline")
    with pytest.raises(Conflict):
        s.record_attempt(sid, "att", "other")
    with pytest.raises(Conflict):
        s.upsert_candidates(sid, "att", [{"candidateId": "c", "scanId": "other"}])
    with pytest.raises(ValidationError):
        s.upsert_candidates(sid, "att", [{}])
    c = {
        "ruleId": "rule",
        "identity": {"anchor": "anchor"},
        "locations": [{"path": "x", "startLine": 1}],
    }
    assert s.upsert_candidates(sid, "att", [c]) == {"inserted": 1, "duplicates": 0}
    assert s.upsert_candidates(sid, "att", [c]) == {"inserted": 0, "duplicates": 1}
    s.finish_attempt(sid, "att", "rejected", error="no")
    s.finish_attempt(sid, "att", "rejected", error="no")
    with pytest.raises(Conflict):
        s.upsert_candidates(sid, "att", [c])
    with pytest.raises(Conflict):
        s.finish_attempt(sid, "att", "finished")
    s.close()
    s.close()
    with pytest.raises(Conflict):
        s.list_scans()
