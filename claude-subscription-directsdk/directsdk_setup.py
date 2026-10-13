"""Setup-time probes of the user's Claude CLI: login state and its own model picker.

Neither sends a Messages request: ``auth status`` reads the local credential store, and the
``initialize`` handshake enumerates the picker without one (the admission relay proves it by
counting upstream calls). With the ``claude_code_telemetry`` setting on (the default) the CLI
fetches its feature flags for the handshake, which is what makes its picker list every model. Anything unexpected returns ``None``/``False`` so callers
fall back to the pinned catalog rather than failing setup.
"""
import json
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path

try:
    from .admission import Admission
    from .model_catalog import MODEL_METADATA, native_model
except ImportError:
    from admission import Admission
    from model_catalog import MODEL_METADATA, native_model

INSTALL_HINT = ("Claude Code is not installed (no `claude` on PATH or in the usual install directories). Install it with "
                "`npm install -g @anthropic-ai/claude-code` or set CLAUDE_SUBSCRIPTION_DIRECTSDK_COMMAND to the binary.")
LOGIN_HINT = "Claude Code is installed but not logged in. Run `claude auth login`, then select this provider again."
LOGGED_OUT_HINT = ("Claude Code is installed but has no usable login in the environment Hermes runs it in. Run `claude auth login` "
                   "as the user Hermes runs as, set CLAUDE_CODE_OAUTH_TOKEN (from `claude setup-token`) in Hermes' environment, "
                   "or point CLAUDE_SUBSCRIPTION_DIRECTSDK_CONFIG_DIR at a logged-in config directory, then try again.")


# Install prefixes probed after PATH, as core's anthropic_adapter does: a service or GUI launch (macOS
# LaunchAgent, Desktop-spawned backend) inherits a bare PATH that carries none of them.
_HOME_PREFIXES = (".local/bin", ".claude/local", "bin", ".npm-global/bin", ".bun/bin", ".volta/bin")
_SYSTEM_PREFIXES = ("/opt/homebrew/bin", "/usr/local/bin")


def _install_prefixes(env):
    home = env.get("USERPROFILE" if os.name == "nt" else "HOME")
    prefixes = [os.path.join(home, prefix) for prefix in _HOME_PREFIXES] if home else []
    return prefixes + ([] if os.name == "nt" else list(_SYSTEM_PREFIXES))


def _resolve(command, env):
    command = list(command) if command else [env.get("CLAUDE_SUBSCRIPTION_DIRECTSDK_COMMAND") or "claude"]
    head = command[0]
    exe = head if os.path.isabs(head) and os.access(head, os.X_OK) else shutil.which(head, path=env.get("PATH") or os.defpath)
    if not exe and head == "claude":
        # Only the default bare name: an explicit command or override is the user's choice, never second-guessed.
        exe = shutil.which(head, path=os.pathsep.join(_install_prefixes(env)))
    return ([exe] + command[1:]) if exe else None


# The plugin's settings live at plugins.entries.<manifest name>.settings in the active profile's
# config.yaml (plugin.yaml `config_schema`; Desktop → Capabilities → Plugins, or `hermes config set`).
PLUGIN_ID = "claude-subscription-directsdk-experimental"
TELEMETRY_SETTING = "claude_code_telemetry"
# Any one of these turns off Claude Code's feature-flag fetch, and with it every model its picker
# only offers through a flag (#86: 11 models become 5). Off only when the user turns the setting off.
QUIET_TRAFFIC = {"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_TELEMETRY": "1", "DISABLE_ERROR_REPORTING": "1"}
# Never wanted from a child the plugin starts, setting or not: a background self-update of the
# user's CLI mid-session, and the feedback path (/feedback, /bug, /share, Claude-drafted feedback)
# that uploads a session to Anthropic. Neither gates feature flags. DISABLE_FEEDBACK_COMMAND is the
# current name of DISABLE_BUG_COMMAND, which the CLI still accepts.
ALWAYS_QUIET = {"DISABLE_AUTOUPDATER": "1", "DISABLE_FEEDBACK_COMMAND": "1"}
_OFF = ("false", "0", "no", "off")


def telemetry_enabled():
    """The ``claude_code_telemetry`` plugin setting, default on.

    Read from the active profile's config on every call (``load_config_readonly`` resolves
    ``get_hermes_home()`` per call and caches on the file signature), so a multi-profile gateway
    honors each profile's choice and a change applies to the next spawn without a restart.
    """
    try:
        from hermes_cli.config import load_config_readonly
        value = load_config_readonly()
    except Exception:
        return True
    for segment in ("plugins", "entries", PLUGIN_ID, "settings", TELEMETRY_SETTING):
        if not isinstance(value, Mapping) or segment not in value:
            return True
        value = value[segment]
    if isinstance(value, str):
        return value.strip().lower() not in _OFF
    return value is not False and value != 0


def apply_traffic_policy(env, telemetry=None):
    """Set the plugin's Claude Code traffic flags on a child ``env`` (in place) and return it.

    Never removes anything: a flag the user exported in Hermes' own environment
    (``DISABLE_TELEMETRY``, ``DO_NOT_TRACK``, ...) still reaches the child with the setting on.
    """
    env.update(ALWAYS_QUIET)
    if not (telemetry_enabled() if telemetry is None else telemetry):
        env.update(QUIET_TRAFFIC)
    return env


def _child_env(env):
    child = dict(env)
    config = child.pop("CLAUDE_SUBSCRIPTION_DIRECTSDK_CONFIG_DIR", None)
    if config:
        child["CLAUDE_CONFIG_DIR"] = config
    return apply_traffic_policy(child)


# Claude Code's refresh lock is `stale: 60000, update: 5000`: a live holder bumps its mtime every 5 s.
_REFRESH_LOCK_STALE_S = 60


def _config_dir(env, windows=os.name == "nt"):
    """Claude Code's config dir: CLAUDE_CONFIG_DIR, else ``~/.claude`` with Node's ``os.homedir()`` rules
    (%USERPROFILE% on Windows even when a Git Bash or MSYS shell sets HOME elsewhere; HOME everywhere else)."""
    config = env.get("CLAUDE_SUBSCRIPTION_DIRECTSDK_CONFIG_DIR") or env.get("CLAUDE_CONFIG_DIR")
    if config:
        return Path(config).expanduser()
    home = (env.get("USERPROFILE") or env.get("HOME")) if windows else env.get("HOME")
    return (Path(home) if home else Path.home()) / ".claude"


def reap_orphaned_refresh_lock(env):
    """Remove the empty ``.oauth_refresh.lock`` directory a Claude Code that exited mid-refresh left behind.

    Claude Code never reclaims it (anthropics/claude-code#95236), so every later native request fails with
    "another Claude Code process is refreshing it or exited mid-refresh". Only a lock older than Claude Code's
    own stale window goes, and ``rmdir`` refuses a directory with contents."""
    lock = _config_dir(env) / ".oauth_refresh.lock"
    try:
        if lock.is_dir() and time.time() - lock.stat().st_mtime > _REFRESH_LOCK_STALE_S:
            lock.rmdir()
    except OSError:
        pass


def settle_login(resolved, env, timeout):
    """Let a due OAuth refresh land before a short-lived probe runs.

    Every Claude Code command starts the refresh at init, and ``auth status`` exits before it lands
    (anthropics/claude-code#95822): the lock is left behind and the server may already have rotated the
    refresh token the CLI never saved, which forces a re-login. ``-p /usage`` is a local command that needs a
    valid token, so it waits for that refresh to be saved before exiting; it sends no Messages request. The
    plugin cannot tell whether a refresh is due without opening the credential store, so it always runs it."""
    reap_orphaned_refresh_lock(env)
    try:
        subprocess.run(resolved + ["-p", "/usage"], env=_child_env(env), stdin=subprocess.DEVNULL,
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    except (OSError, ValueError, subprocess.SubprocessError):
        pass


def _plan_label(raw):
    """``"pro"`` (auth status) and ``"Claude Pro"`` (handshake) name the same plan."""
    raw = str(raw or "").strip()
    if not raw:
        return ""
    return raw if raw.lower().startswith("claude") else "Claude " + raw.replace("_", " ").title()


def setup_status(command=None, env=None, timeout=20):
    """``{available, logged_in, plan, detail, login_command}`` from the CLI's own ``auth status``."""
    env = dict(env if env is not None else os.environ)
    resolved = _resolve(command, env)
    if resolved is None:
        return {"available": False, "logged_in": False, "plan": "", "detail": INSTALL_HINT, "login_command": None}
    login_command = resolved + ["auth", "login"]
    settle_login(resolved, env, timeout)
    try:
        run = subprocess.run(resolved + ["auth", "status"], env=_child_env(env), stdin=subprocess.DEVNULL,
                             capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
        auth = json.loads(run.stdout) if run.stdout.strip().startswith("{") else {}
    except (OSError, ValueError, subprocess.SubprocessError):
        auth = {}
    logged_in = auth.get("loggedIn") is True
    plan = str(auth.get("subscriptionType") or "")
    detail = "" if logged_in else LOGIN_HINT
    return {"available": True, "logged_in": logged_in, "plan": _plan_label(plan), "detail": detail,
            "login_command": login_command}


def discover_models(command=None, env=None, timeout=40):
    """The account's live picker as ``[{id, label, note, upstream_requests}]`` in Hermes route ids,
    or ``None`` when the CLI is missing, logged out, or the handshake fails."""
    env = dict(env if env is not None else os.environ)
    resolved = _resolve(command, env)
    # Logged out, the handshake still answers with a generic default list; only a signed-in
    # account's picker reflects its entitlements.
    if resolved is None or not setup_status(command=resolved, env=env, timeout=timeout)["logged_in"]:
        return None
    child = _child_env(env)
    gate = Admission("https://api.anthropic.com", timeout)
    try:
        child["ANTHROPIC_BASE_URL"] = gate.url
        argv = resolved + ["-p", "--model", "sonnet", "--input-format", "stream-json", "--output-format", "stream-json",
                           "--verbose", "--tools", "", "--setting-sources", "", "--strict-mcp-config",
                           "--mcp-config", '{"mcpServers":{}}', "--disable-slash-commands", "--no-session-persistence"]
        handshake = json.dumps({"type": "control_request", "request_id": "hermes-picker",
                                "request": {"subtype": "initialize"}}) + "\n"
        with tempfile.TemporaryDirectory(prefix="claude-directsdk-picker-") as cwd:
            run = subprocess.run(argv, input=handshake, env=child, cwd=cwd, capture_output=True,
                                 text=True, encoding="utf-8", errors="replace", timeout=timeout)
        rows = [json.loads(line) for line in run.stdout.splitlines() if line.startswith("{")]
        response = next(r["response"] for r in rows if r.get("type") == "control_response")
        native = response.get("response", {}).get("models") or []
        upstream = int(bool(gate.used))
    except (OSError, ValueError, StopIteration, KeyError, subprocess.SubprocessError):
        return None
    finally:
        gate.close()
    if upstream or not native:
        return None
    # Pro / Team-standard seats bill Fable to usage credits from the first request; the CLI only
    # says so at request time, so apply the documented plan rule here.
    plan = str((response.get("response", {}).get("account") or {}).get("subscriptionType") or "").lower()
    credit_billed_on_plan = {"claude-fable-5-1"} if plan and "max" not in plan else set()
    # The pinned table adds metadata (1M route, window, aliases) to the models it knows; it never
    # decides visibility, so a model the CLI ships before the table does is listed the same day.
    announced = [str(row.get("resolvedModel") or row.get("value") or "") for row in native]
    # An unpinned model gets [1m] only from the CLI itself; offered both ways it collapses onto [1m]
    # like the pinned 1M models (behind the relay the suffix is the client-side window selection, #8).
    long_context = {model.removesuffix("[1m]") for model in announced if model.endswith("[1m]")}
    routes = {}
    for row, model in zip(native, announced):
        base = model.removesuffix("[1m]")
        if not base:
            continue
        route = native_model(base)
        pinned = route in MODEL_METADATA
        if not pinned:
            # A `value` the CLI did not resolve (`default`, `best`) is an alias row, not a model.
            if not row.get("resolvedModel"):
                continue
            if base in long_context:
                route = base + "[1m]"
        # Model names, not the native "Default (recommended)" alias row.
        label = str(row.get("description") or "").split("·")[0].strip() or route
        entry = routes.setdefault(route, {"id": route, "label": label, "note": "" if pinned else "unpinned",
                                          "upstream_requests": upstream})
        if "usage credit" in str(row.get("description") or "").lower() or base in credit_billed_on_plan:
            # The billing warning reads first; an unpinned row keeps its marker after it.
            entry["note"] = "usage credits" if pinned else "usage credits · unpinned"
    if not routes:
        return None
    # The live picker names only each family's current model, yet the account still runs the older
    # pinned ones (Opus 5, Opus 4.8, ...). Replacing the static catalog with the picker would hide
    # them, so every pinned route the CLI did not announce is appended after the advertised rows.
    for route, meta in MODEL_METADATA.items():
        if route in routes:
            continue
        canonical = meta["canonical_model"]
        routes[route] = {"id": route, "label": _catalog_label(canonical),
                         "note": "usage credits" if canonical in credit_billed_on_plan else "",
                         "upstream_requests": upstream}
    return list(routes.values())


def _catalog_label(model):
    """``claude-opus-4-8`` -> ``Opus 4.8``; a trailing snapshot date is dropped."""
    parts = model.removeprefix("claude-").split("-")
    if parts and len(parts[-1]) == 8 and parts[-1].isdigit():
        parts = parts[:-1]
    family, version = parts[0], [p for p in parts[1:] if p.isdigit()]
    return " ".join(filter(None, [family.capitalize(), ".".join(version)]))
