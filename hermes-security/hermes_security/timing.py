"""Total scan time: wall clock from scan start to seal, or to now while the scan is open.

Paused or stalled time counts too; this is how long the user waited, not worker effort.
"""
from datetime import datetime, timezone


def _parse(value):
    try:
        return datetime.strptime(value, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def elapsed_seconds(start, end):
    """Whole seconds between two UTC timestamps, or ``None`` when either is unreadable or out of order."""
    a, b = _parse(start), _parse(end)
    if a is None or b is None or b < a:
        return None
    return int((b - a).total_seconds())


def duration_text(seconds):
    if seconds is None:
        return 'not recorded'
    minutes = seconds // 60
    if minutes < 1:
        return 'under a minute'
    days, hours, minutes = minutes // 1440, minutes // 60 % 24, minutes % 60
    if days:
        return f'{days} d {hours} h' if hours else f'{days} d'
    if hours:
        return f'{hours} h {minutes} min' if minutes else f'{hours} h'
    return f'{minutes} min'


def scan_timing(scan, now):
    finished = scan.get('sealed_at')
    seconds = elapsed_seconds(scan.get('created_at'), finished or now)
    return {'startedAt': scan.get('created_at'), 'finishedAt': finished, 'durationSeconds': seconds,
            'running': not finished, 'label': duration_text(seconds)}
