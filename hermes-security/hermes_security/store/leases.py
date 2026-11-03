"""Atomic scan leases; callers hold a write transaction and check sealing.

An owner names the process holding the lease: ``<kind>:<host>:<pid>:<start>:<nonce>``. A holder
that ended without releasing (killed, crashed, a gateway hard exit) is replaced on the next
attempt instead of blocking the scan until expiry. The expiry only covers holders this host
cannot check: another host, an older owner format, or a platform without process start times.
"""

import math
import os
import socket
import time
import uuid
from ..errors import ValidationError

HOST = socket.gethostname().replace(":", "-") or "localhost"


def process_start(pid):
    """Start time of a running process as an opaque integer; None when no such process runs,
    0 when it runs but its start time cannot be read."""
    if os.name == "nt":
        from .. import _win32
        return _win32.process_start(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        return 0
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            # The command name may contain spaces; fields after it start at field 3 (state).
            fields = f.read().rsplit(b")", 1)[1].split()
    except (OSError, IndexError):
        return 0
    if fields and fields[0] == b"Z":
        return None
    try:
        return int(fields[19])  # field 22: start time in clock ticks since boot
    except (IndexError, ValueError):
        return 0


def owner(kind):
    """A new owner token for this process."""
    pid = os.getpid()
    return f"{kind}:{HOST}:{pid}:{process_start(pid) or 0}:{uuid.uuid4().hex}"


def holder(owner):
    """``(pid, start)`` named by a same-host owner token, or None when it names no checkable process."""
    parts = owner.rsplit(":", 4)
    if len(parts) != 5 or parts[1] != HOST:
        return None
    try:
        pid, start = int(parts[2]), int(parts[3])
    except ValueError:
        return None
    return (pid, start) if pid > 0 and start >= 0 else None


def holder_alive(owner):
    """False only when the owner's process is known to be gone (or its ID now names another process)."""
    named = holder(owner)
    if named is None:
        return True
    pid, start = named
    now = process_start(pid)
    return now is not None and not (start and now and start != now)


def lease(connection, scan_id, owner, ttl_s):
    if (
        not isinstance(owner, str)
        or not owner
        or not isinstance(ttl_s, (int, float))
        or not math.isfinite(ttl_s)
        or ttl_s <= 0
    ):
        raise ValidationError("lease requires an owner and a positive finite TTL")
    now = time.time()
    result = connection.execute(
        """INSERT INTO leases(scan_id,owner,expires_at) VALUES(?,?,?)
        ON CONFLICT(scan_id) DO UPDATE SET owner=excluded.owner, expires_at=excluded.expires_at
        WHERE leases.expires_at<=? OR leases.owner=excluded.owner""",
        (scan_id, owner, now + ttl_s, now),
    )
    if result.rowcount == 1:
        return True
    # Held by another owner and not expired: take it over only when that holder's process is gone.
    row = connection.execute("SELECT owner FROM leases WHERE scan_id=?", (scan_id,)).fetchone()
    if row is None or holder_alive(row[0]):
        return False
    connection.execute(
        "UPDATE leases SET owner=?, expires_at=? WHERE scan_id=?", (owner, now + ttl_s, scan_id)
    )
    return True


def release(connection, scan_id, owner):
    connection.execute(
        "DELETE FROM leases WHERE scan_id=? AND owner=?", (scan_id, owner)
    )


def active(connection, scan_id):
    """Owner of a lease on *scan_id* that still binds, or None."""
    row = connection.execute(
        "SELECT owner FROM leases WHERE scan_id=? AND expires_at>?", (scan_id, time.time())
    ).fetchone()
    return row[0] if row is not None and holder_alive(row[0]) else None


def describe(owner):
    """Conflict text that names the holder's process when the owner token records it."""
    named = holder(owner) if owner else None
    who = f" (process {named[0]})" if named else ""
    return f"scan has an active operation{who}; retry later"
