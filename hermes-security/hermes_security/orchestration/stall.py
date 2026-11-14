"""Whether an open scan has stalled, derived only from stored state.

Read on every workbench poll, so no plugin timer or thread is needed. A scan is
stalled when the agent has work it should be doing but nothing has moved for a
while, when the agent reported it could not go on, or when the next step is
blocked. Workers still running ("wait") are never stalled; a worker silent past
the dispatch limit turns the step back into "dispatch" and so counts here.
"""
from datetime import datetime, timezone

STALL_SECONDS = 15 * 60
OPEN = {'created', 'running', 'awaiting_analysis', 'finalizing'}
WAITING_ON = {
    'dispatch': 'Code review has not finished and no worker is running.',
    'validate': 'Possible issues are waiting to be checked and nothing is checking them.',
    'chains': 'The review is done but the report has not been put together.',
    'finalize': 'The review is done but the report has not been written.',
}


def _parse(stamp):
    try:
        return datetime.strptime(stamp, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def derive(scan, next_step, last_event, now):
    """Return ``{'since', 'why'}`` for a stalled scan, else ``None``.

    ``last_event`` is the latest event that is progress or an agent-reported block.
    """
    if scan.get('sealed_at') or scan.get('status') not in OPEN or (scan.get('reason') or '').startswith('paused:'):
        return None
    since = (last_event or {}).get('created_at')
    if last_event and last_event.get('kind') == 'scan.blocked':
        return {'since': since, 'why': last_event.get('message') or 'The agent could not continue.'}
    step = next_step.get('step')
    if step == 'blocked':
        return {'since': since, 'why': next_step.get('reason') or 'The scan cannot continue.'}
    started = _parse(since)
    if step in WAITING_ON and started and (now - started).total_seconds() >= STALL_SECONDS:
        return {'since': since, 'why': WAITING_ON[step]}
    return None
