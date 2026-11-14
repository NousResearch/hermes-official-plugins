"""Setup-time probes of the user's Claude CLI: login state and its own model picker.

Neither sends a Messages request: ``auth status`` reads the local credential store, and the
``initialize`` handshake enumerates the picker without one: it writes only ``initialize`` and no
user turn. The handshake runs first-party, because Claude Code serves the account's own picker only
then, so no relay counts its upstream calls and each row's ``upstream_requests`` is ``None``
(unobserved). With an inherited auth/backend override it stays behind the relay, which counts them.
The handshake always fetches the CLI's feature flags, which is what makes its picker list every
model the account runs. Anything unexpected returns ``None``/``False`` so callers fall back to the
pinned catalog rather than failing setup.
"""
import atexit
import base64
import ipaddress
import json
import logging
import os
import shutil
import ssl
import subprocess
import tempfile
import threading
import time
from collections import namedtuple
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import quote, urlsplit

try:
    from . import extra_usage
    from .admission import Admission
    from .model_catalog import MODEL_METADATA, PICKER_ROUTES, native_model
    from .process_tree import kill_process_tree, own_process_group
except ImportError:
    import extra_usage
    from admission import Admission
    from model_catalog import MODEL_METADATA, PICKER_ROUTES, native_model
    from process_tree import kill_process_tree, own_process_group
try:
    from agent.secret_scope import UnscopedSecretError
except ImportError:  # a core without per-profile secret scopes never raises it
    class UnscopedSecretError(RuntimeError):
        pass

INSTALL_HINT = ("Claude Code is not installed (no `claude` on PATH or in the usual install directories). Install it with "
                "`npm install -g @anthropic-ai/claude-code` or set CLAUDE_SUBSCRIPTION_DIRECTSDK_COMMAND to the binary.")
LOGIN_HINT = "Claude Code is installed but not logged in. Run `claude auth login`, then select this provider again."
# How `auth status` says native would authenticate, for logins that bill API usage rather than the subscription.
_METERED_AUTH = {"api_key": "an Anthropic API key", "api_key_helper": "an API key helper", "third_party": "a third-party cloud login"}
METERED_HINT = ("Claude Code would sign in with {0}, which bills API usage instead of your Claude subscription. Run `claude auth login` "
                "with your Claude account (and remove any Bedrock, Vertex or Foundry switch from Hermes' environment), then select "
                "this provider again.")
LOGGED_OUT_HINT = ("Claude Code is installed but has no usable login in the environment Hermes runs it in. Run `claude auth login` "
                   "as the user Hermes runs as, set CLAUDE_CODE_OAUTH_TOKEN (from `claude setup-token`) in Hermes' environment, "
                   "or point the claude_config_dir plugin setting (or CLAUDE_SUBSCRIPTION_DIRECTSDK_CONFIG_DIR) "
                   "at a logged-in config directory, then try again.")


# Install prefixes probed after PATH, as core's anthropic_adapter does: a service or GUI launch (macOS
# LaunchAgent, Desktop-spawned backend) inherits a bare PATH that carries none of them.
_HOME_PREFIXES = (".local/bin", ".claude/local", "bin", ".npm-global/bin", ".bun/bin", ".volta/bin")
_SYSTEM_PREFIXES = ("/opt/homebrew/bin", "/usr/local/bin")


def _install_prefixes(env):
    home = env.get("USERPROFILE" if os.name == "nt" else "HOME")
    # A relative HOME would resolve against the cwd and could run a binary planted there.
    prefixes = [os.path.join(home, prefix) for prefix in _HOME_PREFIXES] if home and os.path.isabs(home) else []
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
CONFIG_DIR_SETTING = "claude_config_dir"
# Set on every child unless the user turns claude_code_telemetry on (#331).
QUIET_TRAFFIC = {"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_TELEMETRY": "1", "DISABLE_ERROR_REPORTING": "1"}
# Either one turns off the feature-flag fetch that the account's own picker needs (2.1.319: 13 rows
# become 5, #86) and the CLI's get_usage fetch (rate_limits: null). The zero-inference picker and
# usage probes leave them unset unless the user exported one; DISABLE_ERROR_REPORTING stays on.
FLAG_FETCH_BLOCKERS = ("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "DISABLE_TELEMETRY")
# Never wanted from a child the plugin starts, setting or not: a background self-update of the
# user's CLI mid-session, and the feedback path (/feedback, /bug, /share, Claude-drafted feedback)
# that uploads a session to Anthropic. Neither gates feature flags. DISABLE_FEEDBACK_COMMAND is the
# current name of DISABLE_BUG_COMMAND, which the CLI still accepts. CLAUDE_CODE_DISABLE_TERMINAL_TITLE
# also stops native's own session-title request (#116): under an inherited Agent SDK or non-CLI
# entrypoint, or a server flag, it races the turn for the one upstream request the relay admits.
ALWAYS_QUIET = {"DISABLE_AUTOUPDATER": "1", "DISABLE_FEEDBACK_COMMAND": "1", "CLAUDE_CODE_DISABLE_TERMINAL_TITLE": "1"}
# Native names its caller on every request (`cc_entrypoint` in the billing header, the user agent).
# Under `-p` it defaults to `sdk-cli`, and subscriptions draw the 5-hour window about 2x faster per
# token for `sdk-*` callers than for the interactive CLI or a client's own name (anthropics/claude-code#97074).
# An inherited host name (`sdk-py`, `claude-desktop`) would reach the child too, so it is overwritten.
# Off the `sdk-*` names native offers its artifact tool and adds a scratchpad line to every request;
# CLAUDE_CODE_DISABLE_ARTIFACT keeps the request identical to the `sdk-cli` one apart from the name.
CALLER_IDENTITY = {"CLAUDE_CODE_ENTRYPOINT": "hermes-agent", "CLAUDE_CODE_DISABLE_ARTIFACT": "1"}
_OFF = ("false", "0", "no", "off")
# Inherited auth/backend overrides the request path refuses (directsdk.Client); discovery treats them alike.
BACKEND_OVERRIDES = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_FOUNDRY_API_KEY")
BACKEND_SWITCHES = ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY")

# Hermes loads `~/.hermes/.env` into its own process, so a key set there for its `anthropic` provider
# is inherited here too. Claude Code would sign in with it and bill API usage instead of the
# subscription, so no child of this plugin receives one.
API_CREDENTIAL_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_FOUNDRY_API_KEY")


def drop_api_credentials(env):
    for key in API_CREDENTIAL_ENV:
        env.pop(key, None)
    return env


def _setting(key, default=None):
    """Read one plugin setting, with unreadable config or missing keys treated as not set.

    Read from the active profile's config on every call (``load_config_readonly`` resolves
    ``get_hermes_home()`` per call and caches on the file signature), so a multi-profile gateway
    honors each profile's choice and a change applies to the next spawn without a restart.
    """
    try:
        from hermes_cli.config import load_config_readonly
        value = load_config_readonly()
    except Exception:
        return default
    for segment in ("plugins", "entries", PLUGIN_ID, "settings", key):
        if not isinstance(value, Mapping) or segment not in value:
            return default
        value = value[segment]
    return value


def telemetry_enabled():
    """The active profile's ``claude_code_telemetry`` plugin setting, default off."""
    value = _setting(TELEMETRY_SETTING, False)
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "on")
    return isinstance(value, (bool, int)) and bool(value)


MAX_CONCURRENT_SETTING = "max_concurrent"
MAX_CONCURRENT_DEFAULT = 4
_bad_limits = set()


def max_concurrent():
    """The active profile's ``max_concurrent``: a positive whole number, else the default (warned once per value)."""
    value = _setting(MAX_CONCURRENT_SETTING, MAX_CONCURRENT_DEFAULT)
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value)
    if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
        return value
    if repr(value) not in _bad_limits:
        _bad_limits.add(repr(value))
        _logger.warning("claude-subscription-directsdk: max_concurrent must be a whole number of at least 1 (got %r); "
                        "using %d", value, MAX_CONCURRENT_DEFAULT)
    return MAX_CONCURRENT_DEFAULT


def apply_config_dir(env):
    """Select the active profile's Claude config directory on a child env, in place."""
    override = env.pop("CLAUDE_SUBSCRIPTION_DIRECTSDK_CONFIG_DIR", None)
    setting = _setting(CONFIG_DIR_SETTING)
    config = (setting.strip() if isinstance(setting, str) else "") or override or env.get("CLAUDE_CONFIG_DIR")
    if config:
        env["CLAUDE_CONFIG_DIR"] = os.path.expanduser(config)
    return env


OAUTH_TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
# Per-profile secrets a child may carry: the login, and gateway auth headers native sends upstream.
PROFILE_SECRETS = (OAUTH_TOKEN_ENV, "ANTHROPIC_CUSTOM_HEADERS")
_CONFIG_DIR_ENV = ("CLAUDE_SUBSCRIPTION_DIRECTSDK_CONFIG_DIR", "CLAUDE_CONFIG_DIR")


def _secret_scope():
    """``(get_secret or None, routed)``: core's scoped reader and whether this task serves a profile other than
    the process's own. A routed task with no scope bound outside multiplexing (a HERMES_HOME override alone)
    would read the launch environment, so it gets no reader. A core without the scope API: ``(None, False)``."""
    try:
        from agent.secret_scope import current_secret_scope, get_secret, is_multiplex_active, serves_routed_profile
    except ImportError:
        return None, False
    routed = serves_routed_profile()
    if routed and current_secret_scope() is None and not is_multiplex_active():
        return None, True
    return get_secret, routed


def _scoped(get_secret, name):
    value = get_secret(name)
    return value.strip() if isinstance(value, str) and value.strip() else None


class ClaudeCodeLoggedOut(RuntimeError):
    """Claude Code refused before any upstream request because it has no usable login where Hermes runs it."""
    status_code = 401


PROFILE_LOGIN_HINT = (f"This profile has no Claude login of its own, and Claude Code would otherwise sign in with the login of "
                      f"the profile this Hermes process was started for. Give it one: set this profile's {CONFIG_DIR_SETTING} "
                      f"plugin setting to a logged-in Claude config directory, or put {{0}} (from `claude setup-token`) "
                      f"in this profile's .env.")


def apply_profile_credential(env):
    """Give a child ``env`` (in place) the active profile's Claude login, never another profile's.

    Claude Code prefers ``CLAUDE_CODE_OAUTH_TOKEN`` over the login in ``CLAUDE_CONFIG_DIR``. The profile's
    own value (core's secret scope: a multiplexed gateway never loads a served profile's ``.env`` into
    ``os.environ``) is set on the child. A profile served by a process launched for another one never
    inherits that process's token, gateway headers or config directory (#313); a nonempty
    ``claude_config_dir`` setting also drops an inherited token so the selected login is used. A served
    profile with neither its own token nor its own directory is refused (``ClaudeCodeLoggedOut``): the
    child would fall back to ``~/.claude`` under the inherited HOME, the launch profile's login. With no
    scope bound, ``UnscopedSecretError`` propagates: no child starts on a guess. ``os.environ`` is never
    changed and no value is logged.
    """
    get_secret, routed = _secret_scope()
    for name in PROFILE_SECRETS:
        value = _scoped(get_secret, name) if get_secret else None
        if value:
            env[name] = value
        elif routed:
            env.pop(name, None)
    setting = _setting(CONFIG_DIR_SETTING)
    selected = setting.strip() if isinstance(setting, str) else ""
    if selected and not (get_secret and _scoped(get_secret, OAUTH_TOKEN_ENV)):
        env.pop(OAUTH_TOKEN_ENV, None)
    if routed and not selected:
        if get_secret is None:
            raise UnscopedSecretError(OAUTH_TOKEN_ENV)
        # apply_config_dir read the launch environment; only this profile's own .env may choose.
        own = next(filter(None, (_scoped(get_secret, name) for name in _CONFIG_DIR_ENV)), None)
        if own:
            env["CLAUDE_CONFIG_DIR"] = os.path.expanduser(own)
        elif env.get(OAUTH_TOKEN_ENV):
            env.pop("CLAUDE_CONFIG_DIR", None)
        else:
            raise ClaudeCodeLoggedOut(PROFILE_LOGIN_HINT.format(OAUTH_TOKEN_ENV))
    return env


def apply_traffic_policy(env, telemetry=None):
    """Set the plugin's Claude Code traffic flags on a child ``env`` (in place) and return it.

    Never removes anything: a flag the user exported in Hermes' own environment
    (``DISABLE_TELEMETRY``, ``DO_NOT_TRACK``, ...) still reaches the child with the setting on.
    The caller name is the one value replaced, whatever the parent set.
    """
    env.update(ALWAYS_QUIET, **CALLER_IDENTITY)
    if not (telemetry_enabled() if telemetry is None else telemetry):
        env.update(QUIET_TRAFFIC)
    return env


def _child_env(env, *, flag_fetch=False):
    """The env for a setup probe. ``flag_fetch`` lifts the quiet flags that block the CLI's feature-flag
    and usage fetches (``FLAG_FETCH_BLOCKERS``), except one the user exported in ``env``."""
    child = drop_api_credentials(dict(env))
    apply_config_dir(child)
    apply_profile_credential(child)
    reap_orphaned_refresh_lock(child)
    apply_traffic_policy(child)
    if flag_fetch:
        for key in FLAG_FETCH_BLOCKERS:
            if key not in env:
                child.pop(key, None)
    return child


# Claude Code's refresh lock is `stale: 60000, update: 5000`: a live holder bumps its mtime every 5 s.
_REFRESH_LOCK_STALE_S = 60


def _config_dir(env):
    """Claude Code's config dir for a child env: CLAUDE_CONFIG_DIR, else ``~/.claude`` by Node's ``os.homedir()``
    (%USERPROFILE% on Windows even when a Git Bash shell sets HOME elsewhere)."""
    if env.get("CLAUDE_CONFIG_DIR"):
        return Path(env["CLAUDE_CONFIG_DIR"]).expanduser()
    home = (env.get("USERPROFILE") or env.get("HOME")) if os.name == "nt" else env.get("HOME")
    return (Path(home) if home else Path.home()) / ".claude"


def reap_orphaned_refresh_lock(env):
    """Remove the empty ``.oauth_refresh.lock`` a Claude Code that exited mid-refresh left in the child's config dir.

    Claude Code never reclaims it (anthropics/claude-code#95236), and every later request fails with "another
    Claude Code process is refreshing it or exited mid-refresh". Only a lock past Claude Code's own 60 s stale
    window goes, and ``rmdir`` refuses one with contents."""
    lock = _config_dir(env) / ".oauth_refresh.lock"
    try:
        if lock.is_dir() and time.time() - lock.stat().st_mtime > _REFRESH_LOCK_STALE_S:
            lock.rmdir()
    except OSError:
        pass


_probes = set()
_probes_lock = threading.Lock()
_reaper_registered = False


def _reap_probes():
    with _probes_lock:
        live = tuple(_probes)
    for process in live:
        kill_process_tree(process)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def _probe(argv, *, input, env, timeout, prefix):
    """stdout of one zero-inference CLI probe run in its own scratch cwd; the child never outlives the call.

    A daemon thread can still be inside a probe when the interpreter exits (`hermes status` refreshes the
    model catalog on one). The scratch dir's finalizer then ran while the child held it as its cwd, which
    on Windows is a WinError 32 traceback, a leaked dir and a running claude.exe (C#146288). So the child is
    killed and waited for before the dir goes, on every path; at exit a reaper does the same first.
    """
    global _reaper_registered
    scratch = tempfile.TemporaryDirectory(prefix=prefix, ignore_cleanup_errors=True)
    with _probes_lock:
        if not _reaper_registered:
            # After the first TemporaryDirectory: atexit runs LIFO, so this runs before tempfile's finalizers.
            atexit.register(_reap_probes)
            _reaper_registered = True
    process = None
    try:
        process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   text=True, encoding="utf-8", errors="replace", cwd=scratch.name, env=env,
                                   **own_process_group())
        with _probes_lock:
            _probes.add(process)
        # Not communicate(): it reaps the leader, and once reaped its group id no longer proves the group is ours,
        # so a launcher that exited first would leave the real CLI running.
        try:
            process.stdin.write(input)
            process.stdin.close()
        except OSError:  # BrokenPipeError, or EINVAL on Windows: exited without reading; its output still counts
            pass
        # The read gets its own deadline: a descendant the kill cannot reach (a Windows launcher that already exited)
        # can hold stdout open long past the timeout.
        out = []
        reader = threading.Thread(target=lambda: out.append(process.stdout.read()), daemon=True)
        reader.start()
        reader.join(timeout)
        if reader.is_alive():
            raise subprocess.TimeoutExpired(argv, timeout)
        return "".join(out)
    finally:
        if process is not None:
            kill_process_tree(process)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            with _probes_lock:
                _probes.discard(process)
        scratch.cleanup()


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
    try:
        child = _child_env(env)
        resolve_upstream()  # an unusable gateway setting fails setup, and an opted-in one logs where the login goes
    except (UnscopedSecretError, ClaudeCodeLoggedOut, ClaudeUpstreamConfigError) as error:
        return {"available": True, "logged_in": False, "plan": "", "detail": str(error), "login_command": login_command}
    try:
        run = subprocess.run(resolved + ["auth", "status"], env=child, stdin=subprocess.DEVNULL,
                             capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
        auth = json.loads(run.stdout) if run.stdout.strip().startswith("{") else {}
    except (OSError, ValueError, subprocess.SubprocessError):
        auth = {}
    logged_in = auth.get("loggedIn") is True
    plan = str(auth.get("subscriptionType") or "")
    detail = "" if logged_in else LOGIN_HINT
    # loggedIn is also true for a Console API key or a cloud backend; requests would then bill API usage.
    provider = str(auth.get("apiProvider") or "firstParty")
    metered = _METERED_AUTH.get(auth.get("authMethod")) or (f"the {provider} backend" if provider != "firstParty" else None)
    if logged_in and metered:
        logged_in, detail = False, METERED_HINT.format(metered)
    return {"available": True, "logged_in": logged_in, "plan": _plan_label(plan), "detail": detail,
            "login_command": login_command}


def _first_party(env):
    """No inherited auth or backend override: native talks to Anthropic with the CLI's own login."""
    return (not any(env.get(key) for key in BACKEND_OVERRIDES)
            and all(str(env.get(key, "")).strip().lower() in (*_OFF, "") for key in BACKEND_SWITCHES))


def discover_models(command=None, env=None, timeout=40):
    """The account's live picker as ``[{id, label, note, upstream_requests}]`` in Hermes route ids,
    or ``None`` when the CLI is missing, logged out, or the handshake fails."""
    env = dict(env if env is not None else os.environ)
    resolved = _resolve(command, env)
    # Logged out, the handshake still answers with a generic default list; only a signed-in
    # account's picker reflects its entitlements.
    if resolved is None or not setup_status(command=resolved, env=env, timeout=timeout)["logged_in"]:
        return None
    try:
        child = _child_env(env, flag_fetch=True)
        upstream = resolve_upstream()
    except (UnscopedSecretError, ClaudeCodeLoggedOut, ClaudeUpstreamConfigError):
        return None
    # The account's own picker (entitlement-gated models such as Mythos, older models it still runs) is
    # native's served catalog, and native serves it only first-party: behind any ANTHROPIC_BASE_URL,
    # our relay included, it logs `[servedCatalog] off (not_first_party)` and answers a generic list.
    # The handshake therefore talks to Anthropic directly. It writes only `initialize` and closes
    # stdin, so native has no user turn to query; its debug log shows bootstrap, MCP-directory and
    # telemetry fetches and no Messages request. An inherited auth/backend override keeps the relay,
    # since the request path refuses those environments anyway and the bearer must not leave for them.
    # With an opted-in gateway the relay is the base URL, so the picker is the CLI's generic list; the handshake's
    # own fetches still go to api.anthropic.com, through the configured proxy and CA (egress_env).
    gate = None if _first_party(child) and not upstream.opted_in else relay(upstream, timeout)
    egress_env(child, upstream)
    try:
        if gate is not None:
            child["ANTHROPIC_BASE_URL"] = gate.url
        argv = resolved + ["-p", "--model", "sonnet", "--input-format", "stream-json", "--output-format", "stream-json",
                           "--verbose", "--tools", "", "--setting-sources", "", "--strict-mcp-config",
                           "--mcp-config", '{"mcpServers":{}}', "--disable-slash-commands", "--no-session-persistence"]
        handshake = json.dumps({"type": "control_request", "request_id": "hermes-picker",
                                "request": {"subtype": "initialize"}}) + "\n"
        stdout = _probe(argv, input=handshake, env=child, timeout=timeout, prefix="claude-directsdk-picker-")
        rows = [json.loads(line) for line in stdout.splitlines() if line.startswith("{")]
        response = next(r["response"] for r in rows if r.get("type") == "control_response")
        native = response.get("response", {}).get("models") or []
        # Counted only behind the relay; first-party nothing observes the CLI's traffic, so say so.
        upstream = None if gate is None else int(bool(gate.used))
    except (OSError, ValueError, StopIteration, KeyError, subprocess.SubprocessError):
        return None
    finally:
        if gate is not None:
            gate.close()
    if upstream or not native:
        return None
    # Fable 5, 5.1 and 5.2 are a standard part of Max and Ultra and of premium Team/Enterprise seats; Pro and
    # standard seats bill them to usage credits from the first request, and under -p Claude Code bills
    # without asking. The CLI says so only at request time, so apply the documented rule here
    # (https://support.claude.com/en/articles/15424964). The handshake names the plan, not the seat
    # tier, so a Team/Enterprise row says the rule applies to standard seats instead of guessing.
    plan = str((response.get("response", {}).get("account") or {}).get("subscriptionType") or "").lower()
    fable = {"claude-fable-5", "claude-fable-5-1", "claude-fable-5-2"}
    if "pro" in plan:
        credit_billed_on_plan, credit_note = fable, "usage credits"
    elif "team" in plan or "enterprise" in plan:
        credit_billed_on_plan, credit_note = fable, "usage credits on standard seats"
    else:
        credit_billed_on_plan, credit_note = set(), "usage credits"
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
        # A model name, never the "Default (recommended)" alias label. The served (first-party) catalog
        # names the model in displayName ("Opus 5.5"); the generic list names only the family there
        # ("Opus") and the model before the "·" in its description ("Opus 5.5 · Best for ...").
        display = str(row.get("displayName") or "").strip()
        # The version precedes any "(...)": "Opus (1M context)" names only the family.
        named = display if any(c.isdigit() for c in display.split("(")[0]) and not display.lower().startswith("default") else ""
        label = named or str(row.get("description") or "").split("·")[0].strip() or route
        entry = routes.setdefault(route, {"id": route, "label": label, "note": "" if pinned else "unpinned",
                                          "upstream_requests": upstream})
        described = "usage credit" in str(row.get("description") or "").lower()
        if described or base in credit_billed_on_plan:
            # The billing warning reads first; an unpinned row keeps its marker after it. The CLI's own
            # wording is about this account, so it beats the seat-tier caveat.
            warning = "usage credits" if described else credit_note
            entry["note"] = warning if pinned else warning + " · unpinned"
    if not routes:
        return None
    # The live picker names only each family's current model, yet the account still runs the older
    # pinned ones (Opus 5, Opus 4.8, ...). Replacing the static catalog with the picker would hide
    # them, so every pinned route the CLI did not announce is appended after the advertised rows.
    # Access-gated routes (Mythos) are never appended: only an account the CLI offers one to sees it.
    for route in PICKER_ROUTES:
        if route in routes:
            continue
        canonical = MODEL_METADATA[route]["canonical_model"]
        routes[route] = {"id": route, "label": _catalog_label(canonical),
                         "note": credit_note if canonical in credit_billed_on_plan else "",
                         "upstream_requests": upstream}
    return list(routes.values())


def fetch_usage(command=None, env=None, timeout=8):
    """``(rate_limits, None)`` from Claude Code's own ``get_usage`` control request, else ``(None, reason)``.

    The CLI fetches the plan limits with the login it already holds, so the plugin never reads or
    sends that token (catalog rule 11) and ``auth.adopt_external_logins: false`` keeps meaning what
    it says. It follows the same per-profile config dir as every native spawn, runs behind an unused
    admission gate (zero inference), and fits under core's 10 s ``/usage`` hook deadline.
    """
    env = dict(env if env is not None else os.environ)
    resolved = _resolve(command, env)
    if resolved is None:
        return None, INSTALL_HINT
    try:
        child = _child_env(env)
        upstream = resolve_upstream()
    except (UnscopedSecretError, ClaudeCodeLoggedOut, ClaudeUpstreamConfigError) as error:
        return None, str(error)
    limits, reason = _ask_usage(resolved, child, env, upstream, timeout)
    if limits is not None:
        extra_usage.observe_usage(extra_usage.account_key(child), limits)
    return limits, reason


def _ask_usage(resolved, child, exported, upstream, timeout):
    """Run the zero-inference ``get_usage`` probe on ``child``, an env whose login is already prepared."""
    if "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC" not in exported:
        # With this flag the CLI skips the very fetch /usage asks for (rate_limits: null). The probe
        # is zero-inference behind the unused admission gate; DISABLE_TELEMETRY stays on (P#258).
        child.pop("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", None)
    egress_env(child, upstream)
    gate = relay(upstream, timeout)
    try:
        child["ANTHROPIC_BASE_URL"] = gate.url
        argv = resolved + ["-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
                           "--tools", "", "--setting-sources", "", "--strict-mcp-config", "--mcp-config",
                           '{"mcpServers":{}}', "--disable-slash-commands", "--no-session-persistence"]
        request = json.dumps({"type": "control_request", "request_id": "hermes-usage",
                              "request": {"subtype": "get_usage", "skip_behaviors": True}}) + "\n"
        stdout = _probe(argv, input=request, env=child, timeout=timeout, prefix="claude-directsdk-usage-")
        rows = [json.loads(line) for line in stdout.splitlines() if line.startswith("{")]
        response = next(r["response"] for r in rows if r.get("type") == "control_response"
                        and r.get("response", {}).get("request_id") == "hermes-usage")
    except subprocess.TimeoutExpired:
        return None, "Claude Code did not answer in time"
    except (OSError, ValueError, StopIteration, KeyError, AttributeError, subprocess.SubprocessError):
        return None, "Claude Code gave no usage answer (needs a version with get_usage)"
    finally:
        gate.close()
    if response.get("subtype") != "success":
        return None, "Claude Code refused the usage request (needs a version with get_usage)"
    body = response.get("response") if isinstance(response.get("response"), dict) else {}
    limits = body.get("rate_limits")
    if isinstance(limits, dict):
        return limits, None
    if not body.get("rate_limits_available"):
        return None, "no Claude subscription login found (run `claude` and log in)"
    if child.get("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"):
        # Measured on 2.1.302: with nonessential traffic off the CLI answers rate_limits: null.
        return None, "Claude Code skips the usage fetch with nonessential traffic off (CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC is exported)"
    return None, "Claude Code returned no plan limits"


def _catalog_label(model):
    """``claude-opus-4-8`` -> ``Opus 4.8``; a trailing snapshot date is dropped."""
    parts = model.removeprefix("claude-").split("-")
    if parts and len(parts[-1]) == 8 and parts[-1].isdigit():
        parts = parts[:-1]
    family, version = parts[0], [p for p in parts[1:] if p.isdigit()]
    return " ".join(filter(None, [family.capitalize(), ".".join(version)]))


ANTHROPIC_UPSTREAM = "https://api.anthropic.com"
UPSTREAM_SETTINGS = ("upstream_url", "upstream_proxy", "upstream_ca_file")
# The CONNECT proxy's `user:password`, a secret, so it lives in the profile's .env, never in config.yaml.
PROXY_AUTH_ENV = "CLAUDE_SUBSCRIPTION_DIRECTSDK_PROXY_AUTH"
# child_env: what the CLI's own non-Messages calls need to take the same egress route (see egress_env).
Upstream = namedtuple("Upstream", "url proxy context opted_in child_env", defaults=(None,))
_logger = logging.getLogger(__name__)
_warned = set()
_warned_lock = threading.Lock()


class ClaudeUpstreamConfigError(RuntimeError):
    """A gateway setting is set but unusable: the request is refused instead of going to Anthropic directly."""


def _loopback(host):
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


def _setting_text(key):
    value = _setting(key, None)
    if value is None:
        return ""
    if not isinstance(value, str):
        # `upstream_proxy: false` is a set but unusable route, not an absent one: going direct could bypass it.
        raise ClaudeUpstreamConfigError(f"{key} must be a string (got {type(value).__name__}); refusing to send "
                                        "the Claude login without the route this profile configured")
    return value.strip()


def _upstream_url(raw):
    try:
        parts = urlsplit(raw)
        host, _ = parts.hostname, parts.port
    except ValueError:
        host, parts = None, None
    if (not host or parts.username or parts.password or parts.query or parts.fragment
            or not (parts.scheme == "https" or (parts.scheme == "http" and _loopback(host)))):
        raise ClaudeUpstreamConfigError("upstream_url must be an https:// URL (http:// only for a loopback host) with no "
                                        "user:password@, ?query or #fragment; refusing to send the Claude login anywhere else")
    return raw.rstrip("/")


def _upstream_proxy(raw):
    try:
        parts = urlsplit(raw)
        host, port = parts.hostname, parts.port or 80
    except ValueError:
        host = port = None
    if (not host or not parts or parts.scheme != "http" or parts.username or parts.password
            or parts.path not in ("", "/") or parts.query or parts.fragment):
        raise ClaudeUpstreamConfigError("upstream_proxy must be http://host:port (an HTTP CONNECT proxy; put its "
                                        f"user:password in {PROXY_AUTH_ENV} in the profile's .env)")
    headers = None
    reader, routed = _secret_scope()
    credentials = reader(PROXY_AUTH_ENV) if reader else (None if routed else os.environ.get(PROXY_AUTH_ENV))
    if credentials:
        headers = {"Proxy-Authorization": "Basic " + base64.b64encode(credentials.encode()).decode()}
    return (host, port, headers), credentials


def _upstream_context(path):
    context = ssl.create_default_context()
    try:
        context.load_verify_locations(cafile=path)
    except (OSError, ssl.SSLError, ValueError) as error:
        raise ClaudeUpstreamConfigError(f"upstream_ca_file {path!r} is not a readable PEM CA bundle ({error})") from None
    return context


def resolve_upstream():
    """Where the relay sends the Claude login: Anthropic, or the profile's opted-in gateway.

    Any of ``upstream_url``, ``upstream_proxy``, ``upstream_ca_file`` set is the opt-in, and every one set must be
    valid or ``ClaudeUpstreamConfigError`` names it: never a fallback to the direct route. The CA file adds trust to
    the system roots; certificate and hostname checks stay on. An inherited ``ANTHROPIC_BASE_URL`` is never read.
    """
    url_raw, proxy_raw, ca_raw = (_setting_text(key) for key in UPSTREAM_SETTINGS)
    if not (url_raw or proxy_raw or ca_raw):
        return Upstream(ANTHROPIC_UPSTREAM, None, None, False)
    url = _upstream_url(url_raw) if url_raw else ANTHROPIC_UPSTREAM
    proxy, credentials = _upstream_proxy(proxy_raw) if proxy_raw else (None, None)
    # Absolute once: the relay loads it from Hermes' cwd, Claude Code runs in a scratch cwd.
    ca_path = os.path.abspath(os.path.expanduser(ca_raw)) if ca_raw else None
    context = _upstream_context(ca_path) if ca_path else None
    child_env = {}
    if proxy:
        user, _, password = (credentials or "").partition(":")
        auth = f"{quote(user, safe='')}:{quote(password, safe='')}@" if credentials else ""
        proxy_host = f"[{proxy[0]}]" if ":" in proxy[0] else proxy[0]  # urlsplit's hostname drops IPv6 brackets
        for name in _proxy_names("HTTPS_PROXY"):
            child_env[name] = f"http://{auth}{proxy_host}:{proxy[1]}"
    if context:
        child_env["NODE_EXTRA_CA_CERTS"] = ca_path
    host = urlsplit(url).hostname
    try:
        from hermes_constants import get_hermes_home
        home = str(get_hermes_home())
    except ImportError:
        home = ""
    with _warned_lock:
        first = (home, url, proxy and proxy[:2], ca_raw) not in _warned
        _warned.add((home, url, proxy and proxy[:2], ca_raw))
    if first:
        route = (f" through the CONNECT proxy {proxy[0]}:{proxy[1]}" if proxy else "") + (
            ", TLS inspected with the CA in upstream_ca_file" if context else "")
        _logger.warning("claude-subscription-directsdk: the Claude login's bearer token goes to %s%s (upstream "
                        "settings for this profile)", host, route)
    return Upstream(url, proxy, context, True, child_env)


def egress_env(env, upstream):
    """Route a child's own calls (in place) the way the relay goes. Claude Code sends only Messages requests to
    ANTHROPIC_BASE_URL; get_usage, bootstrap, the MCP directory and telemetry go to api.anthropic.com itself (measured
    on 2.1.319 and 2.1.292). Under an opted-in upstream_proxy / upstream_ca_file those calls take the same CONNECT
    proxy and trust the same CA, and the loopback relay stays direct."""
    if not upstream.child_env:
        return env
    env.update(upstream.child_env)
    if "HTTPS_PROXY" in upstream.child_env:
        bypass = [entry for entry in (env.get("NO_PROXY") or env.get("no_proxy") or "").split(",") if entry.strip()]
        for name in _proxy_names("NO_PROXY"):
            env[name] = ",".join(bypass + ["127.0.0.1", "localhost"])
    return env


def _proxy_names(name):
    """Both spellings on POSIX (tools read either); one on Windows, whose env names ignore case, so a second
    spelling is a duplicate entry in the child's environment block."""
    return (name,) if os.name == "nt" else (name, name.lower())


def relay(upstream, timeout, **kwargs):
    """An admission relay to ``upstream`` (``resolve_upstream()``)."""
    return Admission(upstream.url, timeout, proxy=upstream.proxy, context=upstream.context, **kwargs)


def check_extra_usage(resolved, env, upstream, timeout=8):
    """Refuse before spawning when the account draws paid extra usage and the profile's ``extra_usage``
    setting is ``ask`` or ``stop`` (``extra_usage.ClaudeExtraUsageBlocked``). ``env`` is the turn's child env
    with its login applied and before the quiet-traffic flags; the probe runs on a copy of it, so it asks about
    the account the turn would bill, and it goes to the turn's ``upstream``."""
    def probe():
        return _ask_usage(resolved, apply_traffic_policy(dict(env)), env, upstream, timeout)
    extra_usage.admit(extra_usage.account_key(env), _setting, probe)


def observe_rate_limit(env, summary):
    """Feed one native ``rate_limit_event`` summary into the profile's extra-usage policy."""
    extra_usage.observe(extra_usage.account_key(env), summary, _setting)
