"""Record this provider's login health where Hermes already looks for it (#37).

The login itself lives in the Claude Code CLI's ``.credentials.json``; this module writes only the
verdict (status and timestamp) into Hermes' credential pool, keyed by the resolved login directory,
so profiles that share one login share one status and ``hermes auth``/monitoring see a revoked or
moved-away login without an external probe. Token material is never copied.

Best effort only: a failed write must never fail a turn, and no login directory means no entry.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROVIDER = 'claude-subscription-directsdk'
STATUS_OK = 'ok'
STATUS_DEAD = 'dead'


def login_key(env: dict[str, Any]) -> str:
    """Resolved login directory: the applied ``CLAUDE_CONFIG_DIR`` (per-profile setting already
    written into *env* by ``apply_config_dir``) or the CLI's default."""
    raw = str(env.get('CLAUDE_CONFIG_DIR') or '').strip()
    return str(Path(raw).expanduser()) if raw else str(Path.home() / '.claude')


def record(env: dict[str, Any], ok: bool) -> None:
    try:
        from hermes_cli.auth import write_credential_pool

        key = login_key(env)
        write_credential_pool(PROVIDER, [{
            'id': 'login:' + key,
            'label': key,
            'last_status': STATUS_OK if ok else STATUS_DEAD,
            'last_status_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        }])
    except Exception:
        pass
