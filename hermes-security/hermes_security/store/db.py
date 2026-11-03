"""Profile-local workbench storage with process-safe, bounded write transactions."""

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid

from ..canonical import canonical_json, stable_id, utcnow
from ..errors import Conflict, NotFound, SealedError, ValidationError
from .migrations import migrate
from . import leases
from ..fsutil import is_link, set_private

STATUSES = {
    "created",
    "running",
    "awaiting_analysis",
    "finalizing",
    "completed",
    "partial",
    "canceled",
    "interrupted",
    "failed",
}
STATES = {
    "candidate",
    "source_supported",
    "runtime_confirmed",
    "rejected",
    "inconclusive",
}
TRIAGE = {"open", "closed", "accepted_risk", "false_positive"}
COVERAGE = {
    "reviewed",
    "not_applicable",
    "deferred",
    "unsupported",
    "unknown",
    "failed",
}


class SecurityStore:
    """Each operation opens its own connection; no connections cross threads/forks."""

    def __init__(self, data_dir: Path, *, redactor=None):
        self.data_dir = Path(data_dir).absolute()
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        set_private(self.data_dir, 0o700)
        self.path = self.data_dir / "security.db"
        if is_link(self.path):
            raise ValidationError("database may not be a symlink")
        scans = self.data_dir / "scans"
        if is_link(scans):
            raise ValidationError("scans directory may not be a symlink")
        scans.mkdir(exist_ok=True, mode=0o700)
        set_private(scans, 0o700)
        # Create privately before sqlite opens it (also constrains WAL/SHM modes).
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.close(fd)
        set_private(self.path, 0o600)
        self.redactor = redactor
        self._closed = False
        with self.transaction() as c:
            migrate(c)

    @contextmanager
    def _connection(self):
        if self._closed:
            raise Conflict("store is closed")
        c = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        c.row_factory = sqlite3.Row
        try:
            c.execute("PRAGMA busy_timeout=5000")
            c.execute("PRAGMA foreign_keys=ON")
            c.execute("PRAGMA journal_mode=WAL")
            yield c
        finally:
            c.close()

    @contextmanager
    def transaction(self):
        """Serialize check-and-write; rollback all exceptions, bounded busy retry."""
        with self._connection() as c:
            for attempt in range(3):
                try:
                    c.execute("BEGIN IMMEDIATE")
                    break
                except sqlite3.OperationalError as exc:
                    if (getattr(exc, "sqlite_errorcode", 0) & 255) not in (
                        sqlite3.SQLITE_BUSY,
                        sqlite3.SQLITE_LOCKED,
                    ):
                        raise
                    if attempt == 2:
                        raise Conflict("database is busy; retry the operation") from exc
                    time.sleep(0.02 * (attempt + 1))
            try:
                yield c
                c.commit()
            except BaseException:
                c.rollback()
                raise

    def _json(self, data):
        def check(value):
            if isinstance(value, dict):
                secret = value.get("secret")
                if secret is not None and (
                    not isinstance(secret, dict)
                    or set(secret) - {"type", "fingerprint", "line", "path"}
                ):
                    raise ValidationError("secret metadata contains forbidden fields")
                for key, v in value.items():
                    check(key)
                    check(v)
            elif isinstance(value, (list, tuple)):
                for v in value:
                    check(v)
            elif isinstance(value, str) and self.redactor:
                result = self.redactor(value)
                redacted = result[0] if isinstance(result, tuple) else result
                if redacted != value:
                    raise ValidationError("literal secret rejected")

        check(data)
        try:
            return canonical_json(data)
        except (TypeError, ValueError) as exc:
            raise ValidationError("value is not canonical JSON") from exc

    @staticmethod
    def _scan(c, scan_id, mutable=False):
        row = c.execute("SELECT * FROM scans WHERE scan_id=?", (scan_id,)).fetchone()
        if row is None:
            raise NotFound("scan not found")
        if mutable and row["sealed_at"]:
            raise SealedError("scan is sealed")
        result = dict(row)
        for key in ("target", "options"):
            result[key] = json.loads(result[key])
        return result

    @staticmethod
    def _page(limit, offset):
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or limit < 0
            or not isinstance(offset, int)
            or offset < 0
        ):
            raise ValidationError("pagination must use nonnegative integers")

    def create_scan(self, *, mode, safety_level, target, inventory, options):
        if mode not in {"standard", "deep", "diff", "validate"} or safety_level not in {
            "static",
            "local-safe",
            "active-authorized",
        }:
            raise ValidationError("invalid scan mode or safety level")
        if not isinstance(target, dict) or not target.get("root"):
            raise ValidationError("target requires root")
        root = target["root"]
        repo = target.get("repoKey") or stable_id("repo", root)
        stamp = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        scan_id = stable_id(
            "scan", root, target.get("snapshotDigest"), mode, stamp + uuid.uuid4().hex
        )
        with self.transaction() as c:
            c.execute(
                "INSERT INTO repositories VALUES(?,?) ON CONFLICT(repo_key) DO UPDATE SET root=excluded.root",
                (repo, root),
            )
            c.execute(
                """INSERT INTO scans(scan_id,repo_key,root,mode,safety_level,status,created_at,target,options,snapshot_digest)
                VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    scan_id,
                    repo,
                    root,
                    mode,
                    safety_level,
                    "created",
                    utcnow(),
                    self._json(target),
                    self._json(options),
                    target.get("snapshotDigest"),
                ),
            )
            c.execute(
                "INSERT INTO snapshots VALUES(?,?,?)",
                (scan_id, target.get("snapshotDigest"), self._json(target)),
            )
            self._insert_inventory(c, scan_id, inventory)
            return self._scan(c, scan_id)

    def _insert_inventory(self, c, scan_id, inventory):
        for item in inventory:
            path = item.get("path")
            if (
                not isinstance(path, str)
                or not path
                or "\x00" in path
                or Path(path).is_absolute()
                or ".." in Path(path).parts
            ):
                raise ValidationError("invalid inventory path")
            c.execute(
                "INSERT INTO inventory_files VALUES(?,?,?)",
                (scan_id, path, self._json(item)),
            )

    def get_scan(self, scan_id):
        with self._connection() as c:
            return self._scan(c, scan_id)

    def list_scans(self, *, q=None, status=None, repo_key=None, limit=50, offset=0):
        self._page(limit, offset)
        terms, args = [], []
        for key, value in [("status", status), ("repo_key", repo_key)]:
            if value is not None:
                terms.append(key + "=?")
                args.append(value)
        if q:
            terms.append("(root LIKE ? OR scan_id LIKE ?)")
            args.extend(["%" + q + "%"] * 2)
        where = " WHERE " + " AND ".join(terms) if terms else ""
        with self._connection() as c:
            total = c.execute("SELECT count(*) FROM scans" + where, args).fetchone()[0]
            rows = c.execute(
                "SELECT scan_id FROM scans"
                + where
                + " ORDER BY rowid DESC LIMIT ? OFFSET ?",
                args + [limit, offset],
            ).fetchall()
            return {"items": [self._scan(c, r[0]) for r in rows], "total": total}

    def set_scan_status(self, scan_id, status, *, reason=None):
        if status not in STATUSES:
            raise ValidationError("invalid scan status")
        self._json(reason)
        with self.transaction() as c:
            row = self._scan(c, scan_id)
            if row["sealed_at"]:
                if status == row["status"] and (
                    reason is None or reason == row["reason"]
                ):
                    return row
                raise SealedError("scan is sealed")
            c.execute(
                "UPDATE scans SET status=?,reason=? WHERE scan_id=?",
                (status, reason, scan_id),
            )
            return self._scan(c, scan_id)

    def update_scan_options(self, scan_id, values):
        with self.transaction() as c:
            row = self._scan(c, scan_id)
            c.execute("UPDATE scans SET options=? WHERE scan_id=?", (self._json({**row["options"], **values}), scan_id))
            return self._scan(c, scan_id)

    def set_inventory(self, scan_id, *, target, inventory, options):
        """Fill in a scan saved before its files were read: snapshot, inventory and the options derived from it."""
        with self.transaction() as c:
            row = self._scan(c, scan_id, True)
            digest = target.get("snapshotDigest")
            c.execute("UPDATE scans SET target=?,options=?,snapshot_digest=? WHERE scan_id=?",
                      (self._json(target), self._json({**row["options"], **options}), digest, scan_id))
            c.execute("UPDATE snapshots SET digest=?,data=? WHERE scan_id=?", (digest, self._json(target), scan_id))
            c.execute("DELETE FROM inventory_files WHERE scan_id=?", (scan_id,))
            self._insert_inventory(c, scan_id, inventory)
            return self._scan(c, scan_id)

    def set_scan_option(self, scan_id, key, value):
        """Attach UI metadata (such as the chat session running the scan) to a scan."""
        with self.transaction() as c:
            row = self._scan(c, scan_id)
            options = dict(row["options"])
            options[key] = value
            c.execute("UPDATE scans SET options=? WHERE scan_id=?", (self._json(options), scan_id))
            return self._scan(c, scan_id)

    def update_scan_option(self, scan_id, key, update):
        """Atomic read-modify-write of one option: ``update(copy_of_old) -> new`` (None removes it)."""
        with self.transaction() as c:
            row = self._scan(c, scan_id)
            options = dict(row["options"])
            old = options.get(key)
            new = update(json.loads(json.dumps(old)))
            if new == old:
                return row
            if new is None:
                options.pop(key, None)
            else:
                options[key] = new
            c.execute("UPDATE scans SET options=? WHERE scan_id=?", (self._json(options), scan_id))
            return self._scan(c, scan_id)

    def open_scans_for_session(self, session_id, *, limit=20):
        """Unfinished, not-canceled scans linked to one chat session (newest first)."""
        with self._connection() as c:
            rows = c.execute(
                "SELECT scan_id FROM scans WHERE sealed_at IS NULL AND status!='canceled' AND "
                "(json_extract(options,'$.session.sessionId')=? OR json_extract(options,'$.session.storedSessionId')=?) "
                "ORDER BY rowid DESC LIMIT ?",
                (session_id, session_id, limit),
            ).fetchall()
            return [self._scan(c, r[0]) for r in rows]

    def progress_key(self, scan_id, *, ignore=()):
        """Latest event id, skipping bookkeeping kinds that do not mean the scan moved forward."""
        with self._connection() as c:
            sql, args = "SELECT max(id) FROM events WHERE scan_id=?", [scan_id]
            if ignore:
                sql += " AND kind NOT IN (" + ",".join("?" for _ in ignore) + ")"
                args.extend(ignore)
            return c.execute(sql, args).fetchone()[0] or 0

    def last_event(self, scan_id, *, ignore=()):
        """Latest event (kind, message, created_at) outside ``ignore``, or ``None``."""
        with self._connection() as c:
            sql, args = "SELECT kind,message,created_at FROM events WHERE scan_id=?", [scan_id]
            if ignore:
                sql += " AND kind NOT IN (" + ",".join("?" for _ in ignore) + ")"
                args.extend(ignore)
            row = c.execute(sql + " ORDER BY id DESC LIMIT 1", args).fetchone()
            return dict(zip(("kind", "message", "created_at"), row)) if row else None

    @staticmethod
    def _dispatch_row(row):
        item = dict(row)
        item["candidate_ids"] = json.loads(item["candidate_ids"] or "[]")
        return item

    @staticmethod
    def _answered(c, scan_id, attempt_id):
        """True when a worker result for this dispatch (or an incremental _N submit) was accepted."""
        rows = c.execute(
            "SELECT attempt_id FROM worker_attempts WHERE scan_id=? AND status='accepted'", (scan_id,)
        ).fetchall()
        return any(r[0] == attempt_id or r[0].startswith(attempt_id + "_") for r in rows)

    def record_dispatch(self, scan_id, attempt_id, *, packet_id=None, role=None, candidate_ids=(),
                        parent_session_id=None, child_session_id=None):
        """Remember a delegate started for an attempt. Returns the row, or None when already answered."""
        self._json([attempt_id, packet_id, role, list(candidate_ids), parent_session_id, child_session_id])
        with self.transaction() as c:
            self._scan(c, scan_id, True)
            if self._answered(c, scan_id, attempt_id):
                return None
            old = c.execute("SELECT * FROM dispatches WHERE scan_id=? AND attempt_id=?", (scan_id, attempt_id)).fetchone()
            if old and old["status"] == "running" and old["child_session_id"] == child_session_id:
                return self._dispatch_row(old)
            if old:
                c.execute(
                    "UPDATE dispatches SET packet_id=COALESCE(?,packet_id),role=COALESCE(?,role),candidate_ids=?,"
                    "parent_session_id=?,child_session_id=?,status='running',dispatched_at=?,finished_at=NULL,detail=NULL "
                    "WHERE scan_id=? AND attempt_id=?",
                    (packet_id, role, self._json(list(candidate_ids) or json.loads(old["candidate_ids"])),
                     parent_session_id, child_session_id, utcnow(), scan_id, attempt_id),
                )
            else:
                c.execute(
                    "INSERT INTO dispatches(scan_id,attempt_id,packet_id,role,candidate_ids,parent_session_id,"
                    "child_session_id,status,dispatched_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (scan_id, attempt_id, packet_id, role, self._json(list(candidate_ids)), parent_session_id,
                     child_session_id, "running", utcnow()),
                )
            return self._dispatch_row(c.execute(
                "SELECT * FROM dispatches WHERE scan_id=? AND attempt_id=?", (scan_id, attempt_id)).fetchone())

    def finish_dispatch(self, scan_id, attempt_id, *, detail=None):
        """Close a running dispatch: 'finished' if its result was accepted, else 'missing'.

        Idempotent: a dispatch that is no longer running is left alone (late hooks)."""
        self._json(detail)
        with self.transaction() as c:
            self._scan(c, scan_id, True)
            row = c.execute("SELECT * FROM dispatches WHERE scan_id=? AND attempt_id=?", (scan_id, attempt_id)).fetchone()
            if row is None or row["status"] != "running":
                return None
            status = "finished" if self._answered(c, scan_id, attempt_id) else "missing"
            c.execute("UPDATE dispatches SET status=?,finished_at=?,detail=? WHERE scan_id=? AND attempt_id=?",
                      (status, utcnow(), detail, scan_id, attempt_id))
            return self._dispatch_row(c.execute(
                "SELECT * FROM dispatches WHERE scan_id=? AND attempt_id=?", (scan_id, attempt_id)).fetchone())

    def dispatch_for_child(self, child_session_id):
        """Newest dispatch started for this child session on an unfinished scan, or None."""
        with self._connection() as c:
            row = c.execute(
                "SELECT d.* FROM dispatches d JOIN scans s ON s.scan_id=d.scan_id WHERE d.child_session_id=? "
                "AND s.sealed_at IS NULL ORDER BY d.rowid DESC LIMIT 1", (child_session_id,)).fetchone()
            return self._dispatch_row(row) if row else None

    def dispatches(self, scan_id):
        with self._connection() as c:
            return [self._dispatch_row(r) for r in c.execute(
                "SELECT * FROM dispatches WHERE scan_id=? ORDER BY rowid", (scan_id,))]

    def inventory(self, scan_id, *, limit=None, offset=0):
        self._page(limit if limit is not None else 0, offset)
        with self._connection() as c:
            self._scan(c, scan_id)
            rows = c.execute(
                "SELECT data FROM inventory_files WHERE scan_id=? ORDER BY path LIMIT ? OFFSET ?",
                (scan_id, -1 if limit is None else limit, offset),
            )
            return [json.loads(r[0]) for r in rows]

    def inventory_paths(self, scan_id):
        return {item["path"] for item in self.inventory(scan_id)}

    def record_attempt(
        self, scan_id, attempt_id, role, *, packet_id=None, provider=None, model=None
    ):
        self._json([attempt_id, role, packet_id, provider, model])
        with self.transaction() as c:
            self._scan(c, scan_id, True)
            old = c.execute(
                "SELECT * FROM worker_attempts WHERE attempt_id=?", (attempt_id,)
            ).fetchone()
            if old:
                if old["scan_id"] != scan_id or any(
                    old[k] != v
                    for k, v in [
                        ("role", role),
                        ("packet_id", packet_id),
                        ("provider", provider),
                        ("model", model),
                    ]
                ):
                    raise Conflict(
                        "attempt identity already belongs to another submission"
                    )
                return dict(old)
            c.execute(
                """INSERT INTO worker_attempts(attempt_id,scan_id,role,packet_id,provider,model,status,created_at)
                VALUES(?,?,?,?,?,?,?,?)""",
                (
                    attempt_id,
                    scan_id,
                    role,
                    packet_id,
                    provider,
                    model,
                    "running",
                    utcnow(),
                ),
            )
            return dict(
                c.execute(
                    "SELECT * FROM worker_attempts WHERE attempt_id=?", (attempt_id,)
                ).fetchone()
            )

    @staticmethod
    def _attempt(c, scan_id, attempt_id):
        row = c.execute(
            "SELECT * FROM worker_attempts WHERE attempt_id=? AND scan_id=?",
            (attempt_id, scan_id),
        ).fetchone()
        if row is None:
            raise Conflict("attempt is not registered for this scan")
        return row

    def finish_attempt(
        self, scan_id, attempt_id, status, *, result_digest=None, error=None
    ):
        if status not in {
            "finished",
            "accepted",
            "rejected",
            "failed",
            "canceled",
            "late",
            "missing",
        }:
            raise ValidationError("invalid terminal attempt status")
        self._json([result_digest, error])
        with self.transaction() as c:
            self._scan(c, scan_id, True)
            row = self._attempt(c, scan_id, attempt_id)
            if row["status"] != "running":
                if (
                    row["status"] == status
                    and row["result_digest"] == result_digest
                    and row["error"] == error
                ):
                    return dict(row)
                raise Conflict("attempt already finished")
            c.execute(
                "UPDATE worker_attempts SET status=?,result_digest=?,error=?,finished_at=? WHERE attempt_id=?",
                (status, result_digest, error, utcnow(), attempt_id),
            )
            return dict(
                c.execute(
                    "SELECT * FROM worker_attempts WHERE attempt_id=?", (attempt_id,)
                ).fetchone()
            )

    def upsert_candidates(self, scan_id, attempt_id, candidates):
        with self.transaction() as c:
            self._scan(c, scan_id, True)
            if self._attempt(c, scan_id, attempt_id)["status"] != "running":
                raise Conflict("attempt no longer accepts submissions")
            inserted = 0
            for candidate in candidates:
                item = dict(candidate)
                if (
                    item.get("scanId", scan_id) != scan_id
                    or item.get("attemptId", attempt_id) != attempt_id
                ):
                    raise Conflict("candidate belongs to another scan or attempt")
                cid = item.get("candidateId")
                if not cid:
                    try:
                        cid = stable_id(
                            "cand",
                            scan_id,
                            item["ruleId"],
                            item["identity"]["anchor"],
                            item["locations"][0]["path"],
                            item["locations"][0]["startLine"],
                        )
                    except (KeyError, IndexError, TypeError) as exc:
                        raise ValidationError(
                            "candidate identity is incomplete"
                        ) from exc
                    item["candidateId"] = cid
                state = item.get("evidenceState", "candidate")
                if state not in STATES - {"runtime_confirmed"}:
                    raise ValidationError("invalid candidate evidence state")
                item["evidenceState"] = state
                data = self._json(item)
                inserted += c.execute(
                    "INSERT INTO candidates VALUES(?,?,?,?,?) ON CONFLICT(scan_id,candidate_id,attempt_id) DO NOTHING",
                    (scan_id, cid, attempt_id, state, data),
                ).rowcount
            return {"inserted": inserted, "duplicates": len(candidates) - inserted}

    def list_candidates(self, scan_id, *, states=None):
        with self._connection() as c:
            self._scan(c, scan_id)
            sql, args = "SELECT data FROM candidates WHERE scan_id=?", [scan_id]
            if states is not None:
                sql += " AND state IN (" + ",".join("?" for _ in states) + ")"
                args.extend(states)
            return [
                json.loads(r[0])
                for r in c.execute(sql + " ORDER BY candidate_id,attempt_id", args)
            ]

    def set_candidate_state(self, scan_id, candidate_id, state, *, reason):
        if state not in STATES:
            raise ValidationError("invalid evidence state")
        with self.transaction() as c:
            self._scan(c, scan_id, True)
            rows = c.execute(
                "SELECT attempt_id,data FROM candidates WHERE scan_id=? AND candidate_id=?",
                (scan_id, candidate_id),
            ).fetchall()
            if not rows:
                raise NotFound("candidate not found")
            for row in rows:
                item = json.loads(row["data"])
                item.update(evidenceState=state, dispositionReason=reason)
                c.execute(
                    "UPDATE candidates SET state=?,data=? WHERE scan_id=? AND candidate_id=? AND attempt_id=?",
                    (state, self._json(item), scan_id, candidate_id, row["attempt_id"]),
                )
            return item

    def _event(self, c, scan_id, kind, message, data=None):
        self._json([kind, message])
        cur = c.execute(
            "INSERT INTO events(scan_id,kind,message,data,created_at) VALUES(?,?,?,?,?)",
            (scan_id, kind, message, self._json(data), utcnow()),
        )
        row = dict(
            c.execute("SELECT * FROM events WHERE id=?", (cur.lastrowid,)).fetchone()
        )
        row["data"] = json.loads(row["data"])
        return row

    def add_event(self, scan_id, kind, message, data=None):
        with self.transaction() as c:
            self._scan(c, scan_id)
            return self._event(c, scan_id, kind, message, data)

    def events(self, scan_id, *, after_id=0, limit=200):
        self._page(limit, after_id)
        with self._connection() as c:
            self._scan(c, scan_id)
            rows = [
                dict(r)
                for r in c.execute(
                    "SELECT * FROM events WHERE scan_id=? AND id>? ORDER BY id LIMIT ?",
                    (scan_id, after_id, limit),
                )
            ]
            for r in rows:
                r["data"] = json.loads(r["data"])
            return rows

    def record_coverage(self, scan_id, units, *, source, monotonic=False):
        """Upsert coverage. ``monotonic`` keeps a reviewed/not_applicable unit from being downgraded."""
        guard = (" WHERE coverage_units.state NOT IN ('reviewed','not_applicable')"
                 " OR excluded.state IN ('reviewed','not_applicable')") if monotonic else ""
        with self.transaction() as c:
            self._scan(c, scan_id, True)
            for unit in units:
                uid = unit.get("unitId") or unit.get("unit")
                if not uid or unit.get("state") not in COVERAGE:
                    raise ValidationError("invalid coverage unit")
                # Keep every review that closed a unit, not only the latest one.
                if unit["state"] in ("reviewed", "not_applicable"):
                    old = c.execute("SELECT source,state,data FROM coverage_units WHERE scan_id=? AND unit_id=?", (scan_id, uid)).fetchone()
                    prior = (json.loads(old[2]).get("closedBy") or [old[0]]) if old and old[1] in ("reviewed", "not_applicable") else []
                    unit = {**unit, "closedBy": sorted(set(prior) | {source})}
                c.execute(
                    "INSERT INTO coverage_units VALUES(?,?,?,?,?) ON CONFLICT(scan_id,unit_id) DO UPDATE SET source=excluded.source,state=excluded.state,data=excluded.data" + guard,
                    (scan_id, uid, source, unit["state"], self._json(unit)),
                )
                self._event(
                    c,
                    scan_id,
                    "coverage.updated",
                    "Coverage updated",
                    {"source": source, "unit": unit},
                )

    def _documents(self, table, scan_id, *, candidate_id=None):
        with self._connection() as c:
            self._scan(c, scan_id)
            sql, args = f"SELECT data FROM {table} WHERE scan_id=?", [scan_id]
            if candidate_id is not None:
                sql += " AND candidate_id=?"
                args.append(candidate_id)
            return [json.loads(r[0]) for r in c.execute(sql + " ORDER BY rowid", args)]

    def coverage_units(self, scan_id):
        return self._documents("coverage_units", scan_id)

    def _receipt(self, table, scan_id, receipt):
        rid = receipt.get("receiptId")
        if not rid:
            raise ValidationError("receiptId required")
        with self.transaction() as c:
            self._scan(c, scan_id, True)
            c.execute(
                "INSERT INTO validations VALUES(?,?,?,?) ON CONFLICT(scan_id,receipt_id) DO UPDATE SET candidate_id=excluded.candidate_id,data=excluded.data",
                (scan_id, rid, receipt.get("candidateId"), self._json(receipt)),
            )
            return json.loads(self._json(receipt))

    def record_validation(self, scan_id, receipt):
        return self._receipt("validations", scan_id, receipt)

    def validations(self, scan_id, candidate_id=None):
        return self._documents("validations", scan_id, candidate_id=candidate_id)

    def record_chains(self, scan_id, chains_doc):
        with self.transaction() as c:
            self._scan(c, scan_id, True)
            c.execute(
                "INSERT INTO chains VALUES(?,?) ON CONFLICT(scan_id) DO UPDATE SET data=excluded.data",
                (scan_id, self._json(chains_doc)),
            )
            c.execute(
                "UPDATE findings SET chained=0,chain_text='' WHERE scan_id=?",
                (scan_id,),
            )
            for chain in chains_doc.get("chains", []):
                for fid in chain.get("findingIds", []):
                    c.execute(
                        "UPDATE findings SET chained=1,chain_text=chain_text||? WHERE scan_id=? AND finding_id=?",
                        (" " + self._json(chain), scan_id, fid),
                    )
            return json.loads(self._json(chains_doc))

    def get_chains(self, scan_id):
        docs = self._documents("chains", scan_id)
        return docs[0] if docs else None

    def seal(self, scan_id, manifest_digest, artifact_dir):
        self._json([manifest_digest, artifact_dir])
        with self.transaction() as c:
            self._scan(c, scan_id, True)
            stamp = utcnow()
            c.execute(
                "INSERT INTO seals VALUES(?,?,?,?)",
                (scan_id, manifest_digest, artifact_dir, stamp),
            )
            c.execute("UPDATE scans SET sealed_at=? WHERE scan_id=?", (stamp, scan_id))
            c.execute(
                "UPDATE findings SET sealed_at=? WHERE scan_id=?", (stamp, scan_id)
            )
            c.execute("DELETE FROM leases WHERE scan_id=?", (scan_id,))
            return dict(
                c.execute("SELECT * FROM seals WHERE scan_id=?", (scan_id,)).fetchone()
            )

    def is_sealed(self, scan_id):
        return self.get_scan(scan_id)["sealed_at"] is not None

    def upsert_finding_index(self, scan_id, findings):
        with self.transaction() as c:
            scan = self._scan(c, scan_id, True)
            row = c.execute(
                "SELECT data FROM chains WHERE scan_id=?", (scan_id,)
            ).fetchone()
            chains = json.loads(row[0]).get("chains", []) if row else []
            for f in findings:
                if not f.get("findingId"):
                    raise ValidationError("findingId required")
                fid = f["findingId"]
                taxonomy = f.get("taxonomy", {})
                path = (f.get("locations") or [{}])[0].get("path", "")
                occ = f.get("occurrenceId") or stable_id(
                    "occ", scan_id, fid, (f.get("locations") or [{}])[0]
                )
                matching = [
                    chain for chain in chains if fid in chain.get("findingIds", [])
                ]
                level = f.get("severity", "informational")
                level = (
                    level.get("level", "informational")
                    if isinstance(level, dict)
                    else level
                )
                values = (
                    scan_id,
                    occ,
                    fid,
                    level,
                    f.get("evidenceState", "candidate"),
                    f.get("title", ""),
                    scan["repo_key"],
                    scan["root"],
                    path,
                    taxonomy.get("category", ""),
                    self._json(taxonomy.get("cwe", [])),
                    self._json(taxonomy.get("owasp", [])),
                    int(bool(matching)),
                    self._json(matching),
                    None,
                    self._json(f),
                )
                c.execute(
                    """INSERT INTO findings VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(scan_id,occurrence_id) DO UPDATE SET
                    finding_id=excluded.finding_id,severity=excluded.severity,evidence_state=excluded.evidence_state,title=excluded.title,
                    path=excluded.path,category=excluded.category,cwe=excluded.cwe,owasp=excluded.owasp,
                    chained=excluded.chained,chain_text=excluded.chain_text,data=excluded.data""",
                    values,
                )

    def list_findings(
        self,
        *,
        q=None,
        severity=None,
        evidence_state=None,
        triage=None,
        repo_key=None,
        owasp=None,
        chained=None,
        scan_id=None,
        limit=50,
        offset=0,
    ):
        self._page(limit, offset)
        terms, args = [], []
        for key, val in [
            ("severity", severity),
            ("evidence_state", evidence_state),
            ("repo_key", repo_key),
            ("scan_id", scan_id),
            ("chained", None if chained is None else int(chained)),
        ]:
            if val is not None:
                terms.append("f." + key + "=?")
                args.append(val)
        if triage is not None:
            terms.append("COALESCE(t.state,'open')=?")
            args.append(triage)
        if owasp is not None:
            terms.append("EXISTS(SELECT 1 FROM json_each(f.owasp) WHERE value=?)")
            args.append(owasp)
        if q:
            terms.append(
                "("
                + " OR ".join(
                    "f." + key + " LIKE ?"
                    for key in [
                        "title",
                        "path",
                        "category",
                        "cwe",
                        "chain_text",
                        "root",
                        "repo_key",
                    ]
                )
                + ")"
            )
            args.extend(["%" + q + "%"] * 7)
        where = " WHERE " + " AND ".join(terms) if terms else ""
        base = (
            " FROM findings f LEFT JOIN triage t ON t.finding_id=f.finding_id" + where
        )
        with self._connection() as c:
            total = c.execute("SELECT count(*)" + base, args).fetchone()[0]
            # Deliberately never SELECT data: list responses cannot leak full evidence blobs.
            sql = "SELECT f.finding_id,f.occurrence_id,f.scan_id,f.severity,f.evidence_state,f.title,f.repo_key,f.root,f.path,f.category,f.cwe,f.owasp,f.chained,f.sealed_at,COALESCE(t.state,'open') AS triage_state,t.note AS triage_note,t.updated_at AS triage_updated_at"
            items = []
            for row in c.execute(
                sql + base + " ORDER BY f.rowid DESC LIMIT ? OFFSET ?",
                args + [limit, offset],
            ):
                item = dict(row)
                for key in ("cwe", "owasp"):
                    item[key] = json.loads(item[key])
                item["chained"] = bool(item["chained"])
                item["triage"] = {
                    "state": item.pop("triage_state"),
                    "note": item.pop("triage_note"),
                    "updated_at": item.pop("triage_updated_at"),
                }
                items.append(item)
            return {"items": items, "total": total}

    def get_finding(self, finding_id, *, scan_id=None):
        with self._connection() as c:
            args = [finding_id]
            where = "f.finding_id=?"
            if scan_id is not None:
                where += " AND f.scan_id=?"
                args.append(scan_id)
            row = c.execute(
                "SELECT f.data,f.scan_id FROM findings f JOIN scans s ON s.scan_id=f.scan_id WHERE "
                + where
                + " ORDER BY s.rowid DESC,f.rowid DESC LIMIT 1",
                args,
            ).fetchone()
            if row is None:
                raise NotFound("finding not found")
            item = json.loads(row["data"])
            t = c.execute(
                "SELECT * FROM triage WHERE finding_id=?", (finding_id,)
            ).fetchone()
            item["triage"] = (
                dict(t)
                if t
                else {
                    "finding_id": finding_id,
                    "state": "open",
                    "note": None,
                    "updated_at": None,
                }
            )
            item["scan"] = self._scan(c, row["scan_id"])
            return item

    def set_triage(self, finding_id, state, *, note=None):
        if state not in TRIAGE:
            raise ValidationError("invalid triage state")
        self._json(note)
        with self.transaction() as c:
            if (
                c.execute(
                    "SELECT 1 FROM findings WHERE finding_id=?", (finding_id,)
                ).fetchone()
                is None
            ):
                raise NotFound("finding not found")
            c.execute(
                "INSERT INTO triage VALUES(?,?,?,?) ON CONFLICT(finding_id) DO UPDATE SET state=excluded.state,note=excluded.note,updated_at=excluded.updated_at",
                (finding_id, state, note, utcnow()),
            )
            return dict(
                c.execute(
                    "SELECT * FROM triage WHERE finding_id=?", (finding_id,)
                ).fetchone()
            )

    def list_repositories(self, *, limit=50, offset=0):
        self._page(limit, offset)
        with self._connection() as c:
            total = c.execute("SELECT count(DISTINCT repo_key) FROM scans").fetchone()[
                0
            ]
            rows = c.execute(
                """SELECT s.repo_key,s.root,s.scan_id AS last_scan_id,s.created_at AS last_scan_time,s.status AS last_scan_status,s.snapshot_digest,
                (SELECT count(DISTINCT f.finding_id) FROM findings f LEFT JOIN triage t ON t.finding_id=f.finding_id WHERE f.repo_key=s.repo_key AND COALESCE(t.state,'open')='open') AS unresolved_count,
                CASE WHEN EXISTS(SELECT 1 FROM coverage_units u WHERE u.scan_id=s.scan_id)
                    AND NOT EXISTS(SELECT 1 FROM coverage_units u WHERE u.scan_id=s.scan_id AND u.state NOT IN ('reviewed','not_applicable'))
                    AND NOT EXISTS(SELECT 1 FROM inventory_files i WHERE i.scan_id=s.scan_id AND NOT EXISTS(SELECT 1 FROM coverage_units u WHERE u.scan_id=s.scan_id AND u.unit_id='file:'||i.path AND u.state IN ('reviewed','not_applicable')))
                    AND NOT EXISTS(SELECT 1 FROM worker_attempts w WHERE w.scan_id=s.scan_id AND w.status NOT IN ('finished','accepted'))
                    AND (SELECT count(*) FROM coverage_units u WHERE u.scan_id=s.scan_id AND u.unit_id IN
                        ('lane:A01:2025','lane:A02:2025','lane:A03:2025','lane:A04:2025','lane:A05:2025',
                         'lane:A06:2025','lane:A07:2025','lane:A08:2025','lane:A09:2025','lane:A10:2025')
                        AND u.state IN ('reviewed','not_applicable'))=10
                    THEN 'complete' ELSE 'partial' END AS coverage_completeness
                FROM scans s WHERE s.rowid=(SELECT max(s2.rowid) FROM scans s2 WHERE s2.repo_key=s.repo_key)
                ORDER BY s.rowid DESC LIMIT ? OFFSET ?""",
                (limit, offset),
            ).fetchall()
            return {"items": [dict(r) for r in rows], "total": total}

    def grant_validation(self, scan_id, grant):
        gid = grant.get("grantId")
        if not gid:
            raise ValidationError("grantId required")
        with self.transaction() as c:
            self._scan(c, scan_id, True)
            item = dict(grant, scanId=scan_id)
            old = c.execute(
                "SELECT scan_id,data FROM validation_grants WHERE grant_id=?", (gid,)
            ).fetchone()
            data = self._json(item)
            if old:
                previous = json.loads(old["data"])
                if old["scan_id"] != scan_id:
                    raise Conflict("grant already exists")
                if old["data"] != data:
                    # Scope/authority remain immutable. The validation runner
                    # charges requests before I/O through this same API; only
                    # monotonic, capped usage may change. Callers still need a
                    # lease around read/dispatch because this is not consume().
                    before = previous.get("used", 0)
                    after = item.get("used", 0)
                    cap = previous.get("maxRequests")
                    if ({k: v for k, v in previous.items() if k != "used"}
                            != {k: v for k, v in item.items() if k != "used"}
                            or previous.get("revoked") or previous.get("revokedAt")
                            or type(before) is not int or type(after) is not int
                            or type(cap) is not int or not 0 <= before < after <= cap):
                        raise Conflict("grant already exists")
                    c.execute("UPDATE validation_grants SET data=? WHERE grant_id=?", (data, gid))
            else:
                c.execute(
                    "INSERT INTO validation_grants VALUES(?,?,?)", (gid, scan_id, data)
                )
            return item

    def list_grants(self, scan_id):
        with self._connection() as c:
            self._scan(c, scan_id)
            return [json.loads(row[0]) for row in c.execute(
                "SELECT data FROM validation_grants WHERE scan_id=? ORDER BY rowid DESC", (scan_id,)
            )]

    def get_grant(self, grant_id):
        with self._connection() as c:
            row = c.execute(
                "SELECT data FROM validation_grants WHERE grant_id=?", (grant_id,)
            ).fetchone()
            if row is None:
                raise NotFound("grant not found")
            return json.loads(row[0])

    def revoke_grant(self, grant_id):
        with self.transaction() as c:
            row = c.execute(
                "SELECT * FROM validation_grants WHERE grant_id=?", (grant_id,)
            ).fetchone()
            if row is None:
                raise NotFound("grant not found")
            self._scan(c, row["scan_id"], True)
            item = json.loads(row["data"])
            item.update(revoked=True, revokedAt=utcnow())
            c.execute(
                "UPDATE validation_grants SET data=? WHERE grant_id=?",
                (self._json(item), grant_id),
            )
            return item

    def lease(self, scan_id, owner, ttl_s):
        with self.transaction() as c:
            self._scan(c, scan_id, True)
            return leases.lease(c, scan_id, owner, ttl_s)

    def release(self, scan_id, owner):
        with self.transaction() as c:
            self._scan(c, scan_id, True)
            leases.release(c, scan_id, owner)

    def lease_owner(self, scan_id):
        """Who holds *scan_id*'s lease now, for error messages; None when nobody does."""
        with self._connection() as c:
            row = c.execute("SELECT owner FROM leases WHERE scan_id=?", (scan_id,)).fetchone()
            return row[0] if row else None

    OPEN_STATUSES = ("created", "running", "awaiting_analysis", "finalizing")

    def delete_scan(self, scan_id):
        """Remove one stopped or finished scan and every row that belongs to it.

        Triage is keyed by finding identity, which later scans of the same code share;
        keep it while any remaining scan still reports that finding.
        """
        with self.transaction() as c:
            scan = self._scan(c, scan_id)
            if not scan["sealed_at"] and scan["status"] in self.OPEN_STATUSES:
                raise Conflict("stop the scan before deleting it")
            holder = leases.active(c, scan_id)
            if holder:
                raise Conflict(leases.describe(holder))
            finding_ids = [r[0] for r in c.execute("SELECT DISTINCT finding_id FROM findings WHERE scan_id=?", (scan_id,))]
            tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")
                      if any(col[1] == "scan_id" for col in c.execute("PRAGMA table_info(" + r[0] + ")"))]
            # Children first: candidates reference worker_attempts, everything references scans.
            for table in sorted(tables, key=lambda t: (t == "scans", t == "worker_attempts")):
                c.execute("DELETE FROM " + table + " WHERE scan_id=?", (scan_id,))
            for finding_id in finding_ids:
                c.execute("DELETE FROM triage WHERE finding_id=? AND NOT EXISTS(SELECT 1 FROM findings WHERE finding_id=?)",
                          (finding_id, finding_id))
            c.execute("DELETE FROM repositories WHERE repo_key=? AND NOT EXISTS(SELECT 1 FROM scans WHERE repo_key=?)",
                      (scan["repo_key"], scan["repo_key"]))
            return {"scanId": scan_id, "deleted": True, "findings": len(finding_ids)}

    def close(self):
        self._closed = True
