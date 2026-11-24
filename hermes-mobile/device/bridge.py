"""Durable device requests of one Hermes profile. Shared by agent and dashboard processes."""
from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

_initialized = {}
_schema_lock = threading.RLock()

NAME = "hermes-mobile-device-access"
VERSION = "1.7.2"

OPERATIONS = {
    "calendar.events": "calendar.read", "calendar.create": "calendar.write",
    "reminders.list": "reminders.read", "reminders.create": "reminders.write",
    "reminders.complete": "reminders.write", "reminders.update": "reminders.write",
    "health.summary": "health.read", "health.log": "health.write",
    "location.current": "location.read", "contacts.search": "contacts.read",
    "alarms.list": "alarms.manage", "alarms.create": "alarms.manage",
    "timers.start": "alarms.manage", "alarms.cancel": "alarms.manage",
}
CONTACT_FIELDS = {"phone", "email", "birthday", "address"}
WEEKDAYS = {"monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"}
# The categories the phone reads (DeviceHealthMetric in the app) and the ones it may also log.
HEALTH_WRITE_METRICS = {"weight", "body_fat", "dietary_energy", "protein", "carbohydrates", "fat", "sugar", "fiber",
                        "sodium", "caffeine", "water", "mindful_minutes"}
HEALTH_METRICS = HEALTH_WRITE_METRICS | {
    "steps", "walking_running_distance", "cycling_distance", "flights_climbed", "active_energy", "resting_energy",
    "exercise_minutes", "stand_minutes", "heart_rate", "resting_heart_rate", "walking_heart_rate",
    "heart_rate_variability", "vo2_max", "respiratory_rate", "blood_oxygen", "blood_pressure", "blood_glucose",
    "wrist_temperature", "lean_body_mass", "body_mass_index", "height", "sleep", "workouts"}
HEALTH_INTERVALS = {"hour", "day", "week", "month"}
WRITES = {"calendar.create", "reminders.create", "reminders.complete", "reminders.update", "health.log",
          "alarms.create", "timers.start", "alarms.cancel"}
# What an app version can do beyond its grants. A phone registers the features it
# supports; one that sends none (older Hermes Agent) has none of them, so a request that
# needs one is refused rather than run by an app that would ignore its arguments.
FEATURES = {"reminders.update", "reminders.query"}
REMINDER_STATUSES = {"incomplete", "completed", "all"}
REMINDER_PRIORITIES = {"none", "low", "medium", "high"}
REMINDER_LIST_ARGUMENTS = {"status", "due_start", "due_end", "include_undated", "completed_start", "completed_end",
                           "list_ids", "query", "limit", "offset"}
# Arguments of reminders.create that older apps ignore (they always used the default list).
REMINDER_CREATE_QUERY_ARGUMENTS = {"list_id", "list", "priority", "due_date"}
TERMINAL = {"succeeded", "unavailable", "failed", "expired", "cancelled"}
# Requests (and their results) are kept a day. A write's idempotency key outlives it as a hash for
# this long, so retrying that write refuses rather than acting twice; after it the hash is dropped.
SPENT_KEY_SECONDS = 30 * 86400
# When a request that is not finished expires: a pending one at its deadline, a claimed one
# not before its lease lapses, so a phone that claimed it just before the deadline can still
# answer, and the agent never reports expired what the phone did.
DEADLINE = "CASE WHEN status='running' THEN max(expires, coalesce(lease_until, expires)) ELSE expires END"


class BridgeError(ValueError):
    pass


class DeviceUnauthorized(BridgeError):
    """The device id and pairing token do not match a paired device."""


class AccessUnavailable(BridgeError):
    def __init__(self, message, update_required=False):
        super().__init__(message)
        self.update_required = update_required


def required_features(operation, arguments):
    """The app features a request needs beyond its grant."""
    if operation == "reminders.update":
        return {"reminders.update"}
    if operation == "reminders.list" and set(arguments) & REMINDER_LIST_ARGUMENTS:
        return {"reminders.query"}
    if operation == "reminders.create" and set(arguments) & REMINDER_CREATE_QUERY_ARGUMENTS:
        return {"reminders.query"}
    return set()


def _instant(value, name):
    """An ISO 8601 date and time with a timezone."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else None  # noqa: FURB162 -- Python before 3.11 cannot parse "Z"
    except ValueError:
        parsed = None
    if parsed is None or parsed.tzinfo is None:
        raise BridgeError(f"{name} is an ISO 8601 date and time with timezone")
    return parsed


def _day(value, name):
    try:
        if isinstance(value, str) and len(value) == 10:
            return datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        pass
    raise BridgeError(f"{name} is a date as YYYY-MM-DD")


def open_store(home):
    """The store of the profile whose Hermes home is home, in Hermes' per-plugin storage
    (<home>/plugin-data/<plugin>/bridge.db)."""
    from hermes_constants import (
        profile_name_for_home,
        reset_hermes_home_override,
        set_hermes_home_override,
    )
    from plugins.plugin_storage import plugin_data_dir
    token = set_hermes_home_override(home)
    try:
        directory = plugin_data_dir(NAME)
    finally:
        reset_hermes_home_override(token)
    return Store(directory / "bridge.db", profile_name_for_home(home) or "custom")


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


class Store:
    """One profile's devices, requests and deferred replies; profile only names it in answers."""

    def __init__(self, path, profile="default", clock=time.time):
        self.path, self.profile, self.clock = Path(path), profile, clock
        with _schema_lock:
            key = str(self.path.resolve())
            try:
                info = self.path.stat()
                if _initialized.get(key) == (info.st_dev, info.st_ino):
                    return
            except FileNotFoundError:
                pass
            self._initialize()
            info = self.path.stat()
            _initialized[key] = (info.st_dev, info.st_ino)

    def _initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.parent.chmod(0o700)
        with self.db() as db:
            # WAL lets the dashboard's writes and the agent's polling reads run side by side.
            # Hermes decides whether it may, as for its own plugin databases: it keeps new files
            # out of WAL on SQLite builds with the WAL-reset bug and on network filesystems.
            try:
                from hermes_state_wal import apply_wal_with_fallback
            except ImportError:  # outside Hermes: SQLite's rollback journal
                pass
            else:
                apply_wal_with_fallback(db, db_label=f"plugin-data/{NAME}/{self.path.name}")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS devices (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, grants TEXT NOT NULL, token TEXT NOT NULL,
                    seen REAL NOT NULL, scope TEXT NOT NULL, features TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS requests (
                    id TEXT PRIMARY KEY, device TEXT NOT NULL,
                    operation TEXT NOT NULL, arguments TEXT NOT NULL, idem TEXT UNIQUE,
                    status TEXT NOT NULL, created REAL NOT NULL, expires REAL NOT NULL,
                    lease TEXT, lease_until REAL, result TEXT);
                CREATE INDEX IF NOT EXISTS device_pending ON requests(device, status);
                CREATE TABLE IF NOT EXISTS used_keys (digest TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS spent_keys (digest TEXT PRIMARY KEY, spent REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS replies (
                    request_id TEXT PRIMARY KEY REFERENCES requests(id), home TEXT NOT NULL,
                    prompt TEXT NOT NULL, job_id TEXT);
            """)
        self.path.chmod(0o600)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def _expire(self, db):
        """Expire and prune inside a write. Reads treat an overdue request as expired instead,
        so polling a result never takes the write lock."""
        now = self.clock()
        db.execute(f"UPDATE requests SET status='expired',lease=NULL WHERE status IN ('pending','running') AND {DEADLINE}<=?",
                   (now,))
        # Result retention is bounded, including health/location payloads.
        # Keep only a hash of used action keys after removing their payloads.
        # Retrying an old write must never create another native object.
        for row in db.execute("SELECT idem, created FROM requests WHERE created<? AND idem IS NOT NULL",
                              (now - 86400,)).fetchall():
            db.execute("INSERT OR IGNORE INTO spent_keys VALUES(?,?)",
                       (hashlib.sha256(row["idem"].encode()).hexdigest(), row["created"]))
        # used_keys holds the hashes of plugins before 1.7.2, without a time (and of such a process
        # still running): they count from now.
        db.execute("INSERT OR IGNORE INTO spent_keys SELECT digest, ? FROM used_keys", (now,))
        db.execute("DELETE FROM used_keys")
        db.execute("DELETE FROM spent_keys WHERE spent<?", (now - SPENT_KEY_SECONDS,))
        db.execute("DELETE FROM replies WHERE request_id IN (SELECT id FROM requests WHERE created<?)", (now - 86400,))
        db.execute("DELETE FROM requests WHERE created<?", (now - 86400,))

    def register(self, device, name, grants, token, scope=None, features=None):
        if not device or len(device) > 128 or len(name) > 128 or len(token) < 32:
            raise BridgeError("Invalid device registration")
        if features is not None and (not isinstance(features, list) or len(features) > 64
                                     or not all(isinstance(f, str) for f in features)):
            raise BridgeError("Invalid device features")
        # A newer app may name features this plugin does not know; they mean nothing here.
        features = sorted(set(features or []) & FEATURES)
        if not set(grants) <= set(OPERATIONS.values()):
            raise BridgeError("Unknown permission")
        scope = self._scope(scope or {})
        grants = set(grants)
        if not scope["calendar_ids"]:
            grants -= {"calendar.read", "calendar.write"}
        if not scope["health_metrics"]:
            grants.discard("health.read")
        if not scope["health_write_metrics"]:
            grants.discard("health.write")
        if not scope["contact_fields"]:
            grants.discard("contacts.read")
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT scope FROM devices WHERE id=?", (device,)).fetchone()
            old_scope = json.loads(previous["scope"]) if previous else {}
            calendar_changed = old_scope.get("calendar_ids", []) != scope["calendar_ids"]
            health_changed = (old_scope.get("health_metrics", []) != scope["health_metrics"]
                              or old_scope.get("health_write_metrics", []) != scope["health_write_metrics"])
            contacts_changed = old_scope.get("contact_fields", []) != scope["contact_fields"]
            db.execute("""INSERT INTO devices(id,name,grants,token,seen,scope,features) VALUES(?,?,?,?,?,?,?) ON CONFLICT(id)
                DO UPDATE SET name=excluded.name,grants=excluded.grants,token=excluded.token,seen=excluded.seen,scope=excluded.scope,
                features=excluded.features""",
                (device, name, encode(sorted(grants)), hashlib.sha256(token.encode()).hexdigest(), self.clock(), encode(scope),
                 encode(features)))
            # Permission withdrawal invalidates already queued work and cached results.
            rows = db.execute("SELECT id,operation,arguments FROM requests WHERE device=?", (device,)).fetchall()
            for row in rows:
                changed = ((calendar_changed and row["operation"].startswith("calendar."))
                           or (health_changed and row["operation"].startswith("health."))
                           or (contacts_changed and row["operation"] == "contacts.search")
                           # An app that went back to an older version would ignore what it no longer knows.
                           or not required_features(row["operation"], json.loads(row["arguments"])) <= set(features))
                if OPERATIONS.get(row["operation"]) not in grants or changed:
                    db.execute("UPDATE requests SET status='cancelled',result=NULL,lease=NULL WHERE id=?", (row["id"],))

    @staticmethod
    def _check_alarm(operation, arguments):
        label = arguments.get("label")
        if label is not None and (not isinstance(label, str) or len(label) > 100):
            raise BridgeError("An alarm label is text of at most 100 characters")
        if operation == "alarms.cancel":
            if not isinstance(arguments.get("id"), str) or not arguments["id"].strip():
                raise BridgeError("Cancelling needs the id of an alarm or timer from mobile_alarms_list")
        elif operation == "timers.start":
            seconds = arguments.get("duration_seconds")
            if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not 1 <= seconds <= 86400:
                raise BridgeError("A timer needs duration_seconds between 1 and 86400")
        else:
            at, time_of_day, weekdays = arguments.get("at"), arguments.get("time"), arguments.get("weekdays")
            if (at is None) == (time_of_day is None):
                raise BridgeError("An alarm needs either at (a date and time) or time (HH:MM), not both")
            if at is not None and (not isinstance(at, str) or not at.strip()):
                raise BridgeError("at is an ISO 8601 date and time with timezone")
            if time_of_day is not None:
                parts = time_of_day.split(":") if isinstance(time_of_day, str) else []
                if len(parts) != 2 or not all(part.isdigit() and len(part) == 2 for part in parts) \
                        or int(parts[0]) > 23 or int(parts[1]) > 59:
                    raise BridgeError("time is a 24-hour HH:MM time on the device's clock")
            if weekdays is not None:
                if time_of_day is None:
                    raise BridgeError("weekdays repeats an alarm set with time, not at")
                if not isinstance(weekdays, list) or not weekdays or not set(weekdays) <= WEEKDAYS:
                    raise BridgeError("weekdays lists days such as monday or saturday")

    @staticmethod
    def _outdated(needed):
        what = "edit reminders" if "reminders.update" in needed else (
            "choose a Reminders list or priority, or filter and page reminders")
        raise AccessUnavailable(f"The Hermes Agent app on the iPhone is too old to {what}. Ask the user to update Hermes Agent "
                                "from the App Store, then try again.", update_required=True)

    @staticmethod
    def _check_reminder(operation, arguments):
        def text(name, limit, empty=False):
            value = arguments.get(name)
            if value is not None and (not isinstance(value, str) or len(value) > limit
                                      or (not empty and not value.strip())):
                raise BridgeError(f"{name} is text of at most {limit} characters")
        if operation == "reminders.list":
            status = arguments.get("status", "incomplete")
            if status not in REMINDER_STATUSES:
                raise BridgeError("status is incomplete, completed or all")
            bounds = {}
            for name in ("due_start", "due_end", "completed_start", "completed_end"):
                if arguments.get(name) is not None:
                    bounds[name] = _instant(arguments[name], name)
            if ("completed_start" in bounds or "completed_end" in bounds) and status != "completed":
                raise BridgeError("completed_start and completed_end need status completed")
            for start, end in (("due_start", "due_end"), ("completed_start", "completed_end")):
                if start in bounds and end in bounds and bounds[end] <= bounds[start]:
                    raise BridgeError(f"{end} must be after {start}")
            if "include_undated" in arguments and not isinstance(arguments["include_undated"], bool):
                raise BridgeError("include_undated is true or false")
            ids = arguments.get("list_ids")
            if ids is not None and (not isinstance(ids, list) or not 1 <= len(ids) <= 50
                                    or not all(isinstance(i, str) and 0 < len(i) <= 256 for i in ids)):
                raise BridgeError("list_ids is a list of up to 50 list ids from a previous mobile_reminders_list")
            text("query", 200)
            for name, low, high in (("limit", 1, 100), ("offset", 0, 100000)):
                value = arguments.get(name)
                if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                          or value != int(value) or not low <= value <= high):
                    raise BridgeError(f"{name} is a whole number from {low} to {high}")
            return
        if operation == "reminders.complete":
            return
        if operation == "reminders.update":
            if not isinstance(arguments.get("id"), str) or not arguments["id"].strip():
                raise BridgeError("Updating needs the id of a reminder from mobile_reminders_list or mobile_reminder_create")
            changes = {"title", "notes", "due", "due_date", "clear_due", "list_id", "list", "priority", "completed"}
            if not set(arguments) & changes:
                raise BridgeError("Name at least one field to change")
            if "clear_due" in arguments and arguments["clear_due"] is not True:
                raise BridgeError("clear_due is true to remove the due date")
            if "completed" in arguments and not isinstance(arguments["completed"], bool):
                raise BridgeError("completed is true or false")
        if operation == "reminders.create" and (not isinstance(arguments.get("title"), str) or not arguments["title"].strip()):
            raise BridgeError("A reminder needs a title")
        if len({"due", "due_date", "clear_due"} & set(arguments)) > 1:
            raise BridgeError("Give only one of due, due_date and clear_due")
        if "list_id" in arguments and "list" in arguments:
            raise BridgeError("Give either list_id or list, not both")
        text("title", 1000)
        text("notes", 4000, empty=True)
        text("list_id", 256)
        text("list", 300)
        if "due" in arguments:
            _instant(arguments["due"], "due")
        if "due_date" in arguments:
            _day(arguments["due_date"], "due_date")
        if "priority" in arguments and arguments["priority"] not in REMINDER_PRIORITIES:
            raise BridgeError("priority is none, low, medium or high")

    def devices(self):
        with self.db() as db:
            return [{"id": r["id"], "name": r["name"], "grants": json.loads(r["grants"]), "scope": json.loads(r["scope"]),
                     "features": json.loads(r["features"]), "last_seen": r["seen"]}
                    for r in db.execute("SELECT * FROM devices ORDER BY id")]

    def _authorized(self, db, device, token):
        row = db.execute("SELECT * FROM devices WHERE id=?", (device,)).fetchone()
        if row is None or not hmac.compare_digest(row["token"], hashlib.sha256(token.encode()).hexdigest()):
            raise DeviceUnauthorized("Device is not paired")
        return json.loads(row["grants"])

    def request(self, operation, arguments, device=None, idempotency_key=None, ttl=300):
        if operation not in OPERATIONS or not isinstance(arguments, dict) or len(encode(arguments)) > 16000:
            raise BridgeError("Invalid operation or arguments")
        if operation in WRITES and not idempotency_key:
            raise BridgeError("Writes require an idempotency_key; reuse it when retrying the same action")
        if idempotency_key is not None and not 1 <= len(idempotency_key) <= 128:
            raise BridgeError("Invalid idempotency key")
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._expire(db)
            if idempotency_key:
                previous = db.execute("SELECT * FROM requests WHERE idem=?", (idempotency_key,)).fetchone()
                if previous:
                    if previous["operation"] != operation or previous["arguments"] != encode(arguments) or (device and previous["device"] != device):
                        raise BridgeError("Idempotency key already belongs to a different action")
                    return self._public(previous)
                digest = hashlib.sha256(idempotency_key.encode()).hexdigest()
                if db.execute("SELECT 1 FROM spent_keys WHERE digest=?", (digest,)).fetchone():
                    raise BridgeError("Idempotency key already used; its result has expired. This action will not execute again.")
            choices = [r for r in db.execute("SELECT * FROM devices")
                       if OPERATIONS[operation] in json.loads(r["grants"]) and (device is None or r["id"] == device)]
            if not choices:
                raise AccessUnavailable("No device has shared this data with the current agent.")
            needed = required_features(operation, arguments)
            supported = [r for r in choices if needed <= set(json.loads(r["features"]))]
            if not supported:
                self._outdated(needed)
            choices = supported
            if len(choices) != 1:
                raise BridgeError("More than one device is available. Choose a device_id from mobile_devices.")
            device = choices[0]["id"]
            scope = json.loads(choices[0]["scope"])
            if operation.startswith("calendar."):
                requested = arguments.get("calendar_ids", []) if operation == "calendar.events" else [arguments["calendar_id"]] if "calendar_id" in arguments else []
                if not isinstance(requested, list) or not all(isinstance(value, str) and value in scope.get("calendar_ids", []) for value in requested):
                    raise BridgeError("One or more calendars are not shared with this agent")
            if operation == "health.summary" and "metrics" in arguments:
                requested = arguments["metrics"]
                if not isinstance(requested, list) or not requested or not all(value in scope.get("health_metrics", []) for value in requested):
                    raise BridgeError("One or more Health categories are not shared with this agent")
            if operation == "health.summary" and "interval" in arguments and arguments["interval"] not in HEALTH_INTERVALS:
                raise BridgeError("Health interval must be hour, day, week or month")
            if operation == "health.log":
                entries = arguments.get("entries")
                if not isinstance(entries, list) or not 1 <= len(entries) <= 20 or not all(
                        isinstance(entry, dict) and isinstance(entry.get("value"), (int, float))
                        and not isinstance(entry.get("value"), bool) for entry in entries):
                    raise BridgeError("A Health log needs 1 to 20 entries, each with a metric and a numeric value")
                if not all(entry.get("metric") in scope.get("health_write_metrics", []) for entry in entries):
                    raise BridgeError("Logging one or more of these Health categories is not shared with this agent")
            if operation == "contacts.search":
                query = arguments.get("query")
                if not isinstance(query, str) or not query.strip() or len(query) > 200:
                    raise BridgeError("A contacts search needs a non-empty name, email address, or phone number")
                requested = arguments.get("fields", [])
                if not isinstance(requested, list) or not all(value in scope.get("contact_fields", []) for value in requested):
                    raise BridgeError("One or more contact details are not shared with this agent")
            if operation in {"alarms.create", "timers.start", "alarms.cancel"}:
                self._check_alarm(operation, arguments)
            if operation.startswith("reminders."):
                self._check_reminder(operation, arguments)
            if db.execute("SELECT count(*) FROM requests WHERE status IN ('pending','running')").fetchone()[0] >= 32:
                raise BridgeError("Too many pending device requests")
            request_id = uuid.uuid4().hex
            now = self.clock()
            db.execute("INSERT INTO requests(id,device,operation,arguments,idem,status,created,expires) VALUES(?,?,?,?,?,'pending',?,?)",
                       (request_id, device, operation, encode(arguments), idempotency_key, now, now + max(15, min(ttl, 900))))
            return self._public(db.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone())

    def claim(self, device, token):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            grants = self._authorized(db, device, token)
            self._expire(db)
            now = self.clock()
            db.execute("UPDATE devices SET seen=? WHERE id=?", (now, device))
            rows = db.execute("SELECT * FROM requests WHERE device=? AND (status='pending' OR (status='running' AND lease_until<?)) ORDER BY created LIMIT 4", (device, now)).fetchall()
            claimed = []
            for row in rows:
                if OPERATIONS[row["operation"]] not in grants:
                    continue
                lease = uuid.uuid4().hex
                db.execute("UPDATE requests SET status='running',lease=?,lease_until=? WHERE id=?", (lease, now + 35, row["id"]))
                claimed.append({**self._public(row), "lease": lease, "status": "running"})
            return claimed

    def complete(self, device, token, request_id, lease, result):
        if result.get("status") not in {"succeeded", "unavailable", "failed"} or len(encode(result)) > 128000:
            raise BridgeError("Invalid device result")
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            grants = self._authorized(db, device, token)
            self._expire(db)
            row = db.execute("SELECT * FROM requests WHERE id=? AND device=?", (request_id, device)).fetchone()
            if row is None or OPERATIONS[row["operation"]] not in grants:
                raise BridgeError("Request is unavailable")
            if row["lease"] == lease and row["status"] in TERMINAL and row["result"] == encode(result):
                return self._public(row)
            if row["status"] != "running" or row["lease"] != lease:
                raise BridgeError("Request expired, was revoked, or belongs to another execution")
            db.execute("UPDATE requests SET status=?,result=? WHERE id=?", (result["status"], encode(result), request_id))
            return self._public(db.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone())

    def result(self, request_id):
        with self.db() as db:
            row = db.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
        if row is None:
            raise BridgeError("Request not found in this profile")
        result = self._public(row)
        if result["status"] in {"pending", "running"} and self._deadline(row) <= self.clock():
            result["status"] = "expired"
        return result

    def prune(self):
        """Expire overdue requests and drop results past their retention, for a process that
        writes nothing else (the agent's expiry timer)."""
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._expire(db)

    def defer_reply(self, request_id, prompt, home):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._expire(db)
            if db.execute("SELECT id FROM requests WHERE id=?", (request_id,)).fetchone() is None:
                raise BridgeError("Request not found")
            db.execute("INSERT OR IGNORE INTO replies(request_id,home,prompt) VALUES(?,?,?)",
                       (request_id, home, str(prompt)[:4000]))

    def ready_replies(self):
        """Replies not yet scheduled whose request finished or is overdue."""
        with self.db() as db:
            return [dict(r) for r in db.execute(f"""SELECT replies.* FROM replies JOIN requests ON requests.id=replies.request_id
                WHERE replies.job_id IS NULL AND (requests.status NOT IN ('pending','running') OR {DEADLINE}<=?) LIMIT 20""",
                (self.clock(),))]

    def reply_due(self, request_id):
        """When the request's unscheduled reply becomes ready (now or earlier if it is), or None once
        the reply is scheduled or there is none."""
        with self.db() as db:
            row = db.execute("""SELECT requests.status, requests.expires, requests.lease_until FROM replies
                JOIN requests ON requests.id=replies.request_id WHERE replies.request_id=? AND replies.job_id IS NULL""",
                (request_id,)).fetchone()
        if row is None:
            return None
        return self._deadline(row) if row["status"] in {"pending", "running"} else self.clock()

    def mark_reply(self, request_id, job_id):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._expire(db)
            db.execute("UPDATE replies SET job_id=? WHERE request_id=?", (job_id, request_id))

    @staticmethod
    def _deadline(row):
        """DEADLINE, for a row read without writing."""
        if row["status"] == "running" and row["lease_until"] is not None:
            return max(row["expires"], row["lease_until"])
        return row["expires"]

    def _public(self, row):
        return {"id": row["id"], "profile": self.profile, "device_id": row["device"], "operation": row["operation"],
                "arguments": json.loads(row["arguments"]), "status": row["status"], "created_at": row["created"],
                "expires_at": row["expires"], "result": json.loads(row["result"]) if row["result"] else None}

    @staticmethod
    def _scope(value):
        if not isinstance(value, dict):
            raise BridgeError("Invalid device scope")
        ids, metrics = value.get("calendar_ids", []), value.get("health_metrics", [])
        if not isinstance(ids, list) or len(ids) > 128 or not all(isinstance(i, str) and 0 < len(i) <= 256 for i in ids):
            raise BridgeError("Invalid calendar selection")
        if not isinstance(metrics, list) or not all(isinstance(m, str) and m in HEALTH_METRICS for m in metrics):
            raise BridgeError("Invalid Health selection")
        write_metrics = value.get("health_write_metrics", [])
        if not isinstance(write_metrics, list) or not all(isinstance(m, str) and m in HEALTH_WRITE_METRICS for m in write_metrics):
            raise BridgeError("Invalid Health logging selection")
        fields = value.get("contact_fields", [])
        if not isinstance(fields, list) or not all(isinstance(f, str) and f in CONTACT_FIELDS for f in fields):
            raise BridgeError("Invalid contact detail selection")
        calendars = value.get("calendars", [])
        if not isinstance(calendars, list) or len(calendars) > 128:
            raise BridgeError("Invalid calendar details")
        details = []
        for calendar in calendars:
            if not isinstance(calendar, dict) or calendar.get("id") not in ids or not isinstance(calendar.get("title"), str):
                raise BridgeError("Calendar details must belong to a selected calendar")
            details.append({"id": calendar["id"], "title": calendar["title"][:300], "writable": calendar.get("writable") is True})
        return {"calendar_ids": sorted(set(ids)), "health_metrics": sorted(set(metrics)),
                "health_write_metrics": sorted(set(write_metrics)),
                "contact_fields": sorted(set(fields)), "calendars": sorted(details, key=lambda c: c["id"])}
