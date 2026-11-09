"""Extra-usage policy (#43): what a turn does when the Claude account has spilled past its plan limits
into paid extra usage. Per profile (`extra_usage` setting), state in memory per process, keyed by the
account a child signs in with. Nothing is written to disk.

Modes: ``allow`` (do nothing), ``notify`` (default: warn once per limit window when native reports
``isUsingOverage``), ``ask`` (refuse until the user allows this window), ``stop`` (refuse until the
plan limit resets). The request that first crosses the limit is already billed; the policy acts on
the next one. Under ``ask``/``stop`` a check that cannot tell refuses too.
"""
from datetime import datetime, timezone
import hashlib
import logging
import math
import os
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

PLUGIN_ID = "claude-subscription-directsdk-experimental"
MODE_SETTING = "extra_usage"
ALLOW_SETTING = "extra_usage_allowed_until"
MODES = ("allow", "notify", "ask", "stop")
DEFAULT_MODE = "notify"
# How long a probe or event answer stands when it names no reset time.
_RECHECK_S = 15 * 60

_lock = threading.Lock()
_state = {}  # account key -> {"using_overage": bool, "resets_at": float | None, "until": float}
_notified = set()  # (account key, resets_at) already warned about


class ClaudeExtraUsageBlocked(RuntimeError):
    """The account is drawing paid extra usage and the profile's `extra_usage` setting refuses it."""
    status_code = 429

    def __init__(self, message, resets_at=None):
        super().__init__(message)
        self.resets_at = resets_at


def account_key(env):
    """The login a child env signs in with: its config dir, and the token that overrides it."""
    home = env.get("HOME") or env.get("USERPROFILE")
    config = env.get("CLAUDE_CONFIG_DIR") or str((Path(home) if home else Path.home()) / ".claude")
    token = env.get("CLAUDE_CODE_OAUTH_TOKEN")
    digest = hashlib.sha256(token.encode()).hexdigest()[:16] if token else ""
    return os.path.normcase(os.path.abspath(os.path.expanduser(config))) + "|" + digest


def mode(setting):
    """The profile's mode; an unknown value is treated as ``stop`` (a typo must not spend money)."""
    value = setting(MODE_SETTING, DEFAULT_MODE)
    value = value.strip().lower() if isinstance(value, str) else value
    if value in MODES:
        return value
    logger.warning("claude-subscription-directsdk: unknown extra_usage setting %r, treating it as 'stop'", value)
    return "stop"


def _epoch(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    return value / 1000 if value > 1e11 else float(value)  # native sends seconds; tolerate ms


def _iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _record(key, using, resets_at, now=None):
    now = time.time() if now is None else now
    until = resets_at if using and resets_at and resets_at > now else now + _RECHECK_S
    with _lock:
        _state[key] = {"using_overage": using, "resets_at": resets_at, "until": until}


def observe(key, summary, setting=None):
    """Feed one native ``rate_limit_event`` summary; warns once per window under ``notify``."""
    using, resets_at = bool(summary.get("using_overage")), _epoch(summary.get("resets_at"))
    _record(key, using, resets_at)
    if not using or setting is None or mode(setting) != "notify":
        return
    with _lock:
        first = (key, resets_at) not in _notified
        _notified.add((key, resets_at))
    if first:
        until = f" until the plan limit resets at {_iso(resets_at)}" if resets_at else ""
        logger.warning("Claude subscription is drawing paid extra usage%s. Set the plugin's extra_usage setting "
                       "to 'ask' or 'stop' to refuse it.", until)


def usage_percent(value, fraction=False):
    """A finite number as a 0-100 percent, else None. ``fraction``: the legacy ``utilization`` windows send 0..1."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return value * 100.0 if fraction and value <= 1 else float(value)


def observe_usage(key, limits):
    """Feed a ``get_usage`` ``rate_limits`` body and return ``(using_overage, resets_at)``: spilling means extra
    usage is on and an account-wide plan window is full."""
    extra = limits.get("extra_usage") if isinstance(limits.get("extra_usage"), dict) else {}
    full = []
    for entry in limits.get("limits") if isinstance(limits.get("limits"), list) else ():
        if isinstance(entry, dict) and entry.get("kind") in ("session", "weekly_all"):
            if (usage_percent(entry.get("percent")) or 0) >= 100:
                full.append(entry.get("resets_at"))
    if not limits.get("limits"):
        for name in ("five_hour", "seven_day"):
            window = limits.get(name) if isinstance(limits.get(name), dict) else {}
            if (usage_percent(window.get("utilization"), fraction=True) or 0) >= 100:
                full.append(window.get("resets_at"))
    resets = [_parse(value) for value in full]
    resets_at = max((r for r in resets if r), default=None)
    using = bool(extra.get("is_enabled")) and bool(full)
    _record(key, using, resets_at)
    return using, resets_at


def _parse(value):
    if isinstance(value, datetime):  # an unquoted timestamp in config.yaml loads as a datetime
        return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).timestamp()
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp()


def _allowed_until(setting):
    return _parse(setting(ALLOW_SETTING, ""))


def admit(key, setting, probe):
    """Raise ``ClaudeExtraUsageBlocked`` when the profile refuses extra usage the account is drawing.

    ``probe()`` runs Claude Code's zero-inference ``get_usage`` and returns ``(rate_limits, reason)``; it is
    called only under ``ask``/``stop`` when this process holds no current answer for the account. A probe that
    cannot tell refuses: these modes exist to prevent unapproved spend. That unknown is not remembered.
    """
    current = mode(setting)
    if current in ("allow", "notify"):
        return
    now = time.time()
    allowed = _allowed_until(setting)
    if current == "ask" and allowed and allowed > now:
        return
    with _lock:
        known = _state.get(key)
    if known is None or now >= known["until"]:
        with _lock:
            _state.pop(key, None)  # a stale answer must not outlive a probe that could not tell
        limits, reason = probe()
        if limits is None:
            raise ClaudeExtraUsageBlocked(
                f"Could not check whether this Claude account is drawing paid extra usage ({reason}), and the "
                f"extra_usage setting is '{current}', so the request was not sent. Fix the cause and send the message "
                f"again, or run `hermes config set plugins.entries.{PLUGIN_ID}.settings.{MODE_SETTING} notify` to "
                f"send without this check.")
        using, resets_at = observe_usage(key, limits)
    else:
        using, resets_at = known["using_overage"], known["resets_at"]
    if not using:
        return
    when = f" The plan limit resets at {_iso(resets_at)}." if resets_at else ""
    base = f"plugins.entries.{PLUGIN_ID}.settings"
    if current == "ask":
        until = _iso(resets_at) if resets_at else _iso(now + 3600)
        how = (f" To allow extra usage until then, run `hermes config set {base}.{ALLOW_SETTING} {until}`"
               f" and send the message again.")
    else:
        how = (f" To continue on extra usage, run `hermes config set {base}.{MODE_SETTING} ask` (asks once per "
               f"limit window) or `... {MODE_SETTING} allow`.")
    raise ClaudeExtraUsageBlocked("Claude plan limit reached: this account is now drawing paid extra usage, and the "
                                  f"extra_usage setting is '{current}', so the request was not sent.{when}{how}",
                                  resets_at)


def reset():
    """Forget all state (tests)."""
    with _lock:
        _state.clear()
        _notified.clear()
