"""Forward-only SQLite schema. Caller holds BEGIN IMMEDIATE during migration."""

from ..errors import Conflict

VERSION = 3
DDL = [
    """CREATE TABLE repositories (repo_key TEXT PRIMARY KEY, root TEXT NOT NULL)""",
    """CREATE TABLE scans (scan_id TEXT PRIMARY KEY, repo_key TEXT NOT NULL REFERENCES repositories(repo_key), root TEXT NOT NULL,
       mode TEXT NOT NULL, safety_level TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL,
       target TEXT NOT NULL, options TEXT NOT NULL, snapshot_digest TEXT, reason TEXT, sealed_at TEXT)""",
    """CREATE TABLE snapshots (scan_id TEXT PRIMARY KEY REFERENCES scans(scan_id), digest TEXT, data TEXT NOT NULL)""",
    """CREATE TABLE inventory_files (scan_id TEXT REFERENCES scans(scan_id), path TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY(scan_id,path))""",
    """CREATE TABLE worker_attempts (attempt_id TEXT PRIMARY KEY, scan_id TEXT NOT NULL REFERENCES scans(scan_id), role TEXT NOT NULL,
       packet_id TEXT, provider TEXT, model TEXT, status TEXT NOT NULL, created_at TEXT NOT NULL, finished_at TEXT, result_digest TEXT, error TEXT,
       UNIQUE(scan_id,attempt_id))""",
    """CREATE TABLE candidates (scan_id TEXT NOT NULL, candidate_id TEXT NOT NULL, attempt_id TEXT NOT NULL, state TEXT NOT NULL, data TEXT NOT NULL,
       PRIMARY KEY(scan_id,candidate_id,attempt_id), FOREIGN KEY(scan_id,attempt_id) REFERENCES worker_attempts(scan_id,attempt_id))""",
    """CREATE TABLE evidence (scan_id TEXT REFERENCES scans(scan_id), evidence_id TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY(scan_id,evidence_id))""",
    """CREATE TABLE validations (scan_id TEXT REFERENCES scans(scan_id), receipt_id TEXT NOT NULL, candidate_id TEXT, data TEXT NOT NULL, PRIMARY KEY(scan_id,receipt_id))""",
    """CREATE TABLE validation_grants (grant_id TEXT PRIMARY KEY, scan_id TEXT NOT NULL REFERENCES scans(scan_id), data TEXT NOT NULL)""",
    """CREATE TABLE chains (scan_id TEXT PRIMARY KEY REFERENCES scans(scan_id), data TEXT NOT NULL)""",
    """CREATE TABLE coverage_units (scan_id TEXT REFERENCES scans(scan_id), unit_id TEXT NOT NULL, source TEXT NOT NULL, state TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY(scan_id,unit_id))""",
    """CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, scan_id TEXT NOT NULL REFERENCES scans(scan_id), kind TEXT NOT NULL, message TEXT NOT NULL, data TEXT NOT NULL, created_at TEXT NOT NULL)""",
    """CREATE TABLE seals (scan_id TEXT PRIMARY KEY REFERENCES scans(scan_id), manifest_digest TEXT NOT NULL, artifact_dir TEXT NOT NULL, sealed_at TEXT NOT NULL)""",
    """CREATE TABLE findings (scan_id TEXT REFERENCES scans(scan_id), occurrence_id TEXT NOT NULL, finding_id TEXT NOT NULL,
       severity TEXT NOT NULL, evidence_state TEXT NOT NULL, title TEXT NOT NULL, repo_key TEXT NOT NULL, root TEXT NOT NULL,
       path TEXT NOT NULL, category TEXT NOT NULL, cwe TEXT NOT NULL, owasp TEXT NOT NULL,
       chained INTEGER NOT NULL DEFAULT 0, chain_text TEXT NOT NULL DEFAULT '', sealed_at TEXT, data TEXT NOT NULL,
       PRIMARY KEY(scan_id,occurrence_id))""",
    """CREATE TABLE triage (finding_id TEXT PRIMARY KEY, state TEXT NOT NULL, note TEXT, updated_at TEXT NOT NULL)""",
    """CREATE TABLE leases (scan_id TEXT PRIMARY KEY REFERENCES scans(scan_id), owner TEXT NOT NULL, expires_at REAL NOT NULL)""",
    "CREATE INDEX scans_repo_time ON scans(repo_key,created_at)",
    "CREATE INDEX findings_filter ON findings(severity,evidence_state,repo_key,scan_id)",
    "CREATE INDEX findings_identity ON findings(finding_id)",
    "CREATE INDEX events_scan ON events(scan_id,id)",
]
# v2: delegate dispatches seen through host hooks (worker tracking), and a cheap
# lookup of open scans by their chat session for automatic continue.
DDL_V2 = [
    """CREATE TABLE dispatches (scan_id TEXT NOT NULL REFERENCES scans(scan_id), attempt_id TEXT NOT NULL, packet_id TEXT,
       role TEXT, candidate_ids TEXT NOT NULL DEFAULT '[]', parent_session_id TEXT, child_session_id TEXT,
       status TEXT NOT NULL, dispatched_at TEXT NOT NULL, finished_at TEXT, detail TEXT, PRIMARY KEY(scan_id,attempt_id))""",
    "CREATE INDEX dispatches_child ON dispatches(child_session_id)",
    "CREATE INDEX scans_open ON scans(sealed_at,status)",
]


def migrate(connection):
    """Apply missing versions atomically within the caller's transaction."""
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version > VERSION:
        raise Conflict("database schema is newer than this application")
    if version < 1:
        for statement in DDL:
            connection.execute(statement)
        connection.execute("PRAGMA user_version=1")
    if version < 2:
        for statement in DDL_V2:
            connection.execute(statement)
        connection.execute("PRAGMA user_version=2")
    if version < 3:
        # v3: the detector backends were removed by editing the v1 DDL in place, so
        # databases created before that still have findings.detectors (NOT NULL, which
        # makes every finding insert fail) and the unused detector_runs table.
        columns = [row[1] for row in connection.execute("PRAGMA table_info(findings)")]
        if "detectors" in columns:
            connection.execute("ALTER TABLE findings DROP COLUMN detectors")
        connection.execute("DROP TABLE IF EXISTS detector_runs")
        connection.execute("PRAGMA user_version=3")
