"""``hermes xchat ...`` CLI subcommands — registered by the plugin via
``ctx.register_cli_command()``.

Subcommands:

    setup       full first-time setup (token + user id + key generation/registration)
    register    (re)register the E2EE public keys only
    status      show token / key / registration state

Key registration is a rare, rate-limited write (only a few per 24h per
account). ``setup`` is safe to re-run: the private-key blob and the
registration payload are persisted to ``~/.hermes/xchat/`` BEFORE any
network call, so an interrupted run resumes the same identity instead of
minting a new one and burning the daily budget.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path



class Colors:
    RED = "\033[31m"
    GREEN = "\033[32m"
    BOLD = "\033[1m"


def color(text: str, code: str) -> str:
    """ANSI-wrap *text* on a colour-capable terminal (honours NO_COLOR)."""
    import sys

    if os.environ.get("NO_COLOR") or not sys.stdout.isatty():
        return text
    return f"{code}{text}\033[0m"


def _state_dir() -> Path:
    from hermes_constants import get_hermes_home

    d = get_hermes_home() / "xchat"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _blob_path() -> Path:
    return _state_dir() / "private_keys.b64"


def _marker_path() -> Path:
    return _state_dir() / "registration.json"


def _read_marker() -> dict:
    try:
        data = json.loads(_marker_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_marker(marker: dict) -> None:
    _marker_path().write_text(json.dumps(marker, indent=2) + "\n", encoding="utf-8")


def _write_private_blob(path: Path, blob_b64: str) -> None:
    """Write the private-key blob with 0o600 from the very first byte.

    ``write_text`` + ``chmod`` leaves a window where the file is world
    readable (and swallows the chmod failure); ``os.open`` with an explicit
    mode closes it. On platforms without POSIX modes (Windows) the mode
    argument is a no-op, which matches the previous best-effort chmod.
    """
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, (blob_b64 + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    try:
        # In case the file pre-existed with looser permissions.
        os.chmod(path, 0o600)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# argparse wiring


def register_cli(parser: argparse.ArgumentParser) -> None:
    """Wire up `hermes xchat ...` subcommands."""
    subs = parser.add_subparsers(dest="xchat_command", required=False)

    p_setup = subs.add_parser(
        "setup",
        help="First-time setup (OAuth token + E2EE key generation/registration)",
    )
    p_setup.add_argument(
        "--force",
        action="store_true",
        help="Generate a NEW identity even if one is already registered (dangerous)",
    )

    p_reg = subs.add_parser("register", help="(Re)register the E2EE public keys with the X API")
    p_reg.add_argument(
        "--force",
        action="store_true",
        help="Generate a NEW identity even if one is already registered (dangerous)",
    )
    subs.add_parser("status", help="Show token / key / registration state")

    parser.set_defaults(func=dispatch)


def dispatch(args: argparse.Namespace) -> int:
    sub = getattr(args, "xchat_command", None)
    if sub in (None, "status"):
        return cmd_status()
    if sub == "setup":
        return cmd_setup(force=getattr(args, "force", False))
    if sub == "register":
        return cmd_register(force=getattr(args, "force", False))
    print(color(f"Unknown xchat subcommand: {sub}", Colors.RED))
    return 1


def gateway_setup() -> None:
    """Zero-arg hook for the unified `hermes gateway setup` wizard."""
    cmd_setup(force=False)


# ---------------------------------------------------------------------------
# Helpers


def _get_env(key: str) -> str:
    val = os.getenv(key, "").strip()
    if val:
        return val
    # Fall back to the persisted .env (the CLI may run before env load).
    try:
        from hermes_cli.config import get_env_value

        return (get_env_value(key) or "").strip()
    except Exception:
        return ""


def _save_env(key: str, value: str) -> None:
    from hermes_cli.config import save_env_value

    save_env_value(key, value)
    os.environ[key] = value


def _run(coro):
    return asyncio.run(coro)


async def _fetch_user_id(api) -> str:
    me = await api.get_my_user()
    return str(me.get("id") or "")


# ---------------------------------------------------------------------------
# Commands


def cmd_status() -> int:
    token = _get_env("XCHAT_ACCESS_TOKEN")
    user_id = _get_env("XCHAT_USER_ID")
    marker = _read_marker()
    blob = _blob_path()

    print(color("X Chat integration status", Colors.BOLD))
    print(f"  access token:  {'✓ set' if token else '✗ missing'}")
    print(f"  refresh token: {'✓ set' if _get_env('XCHAT_REFRESH_TOKEN') else '– not set (no auto-renew)'}")
    print(f"  account id:    {user_id or '– not set'} (Hermes reads and replies as this X account)")
    print(f"  key blob:      {'✓ ' + str(blob) if blob.exists() else '✗ missing'}")
    if marker.get("registered"):
        print(f"  registration:  ✓ version {marker.get('version')} ({marker.get('registered_at', '?')})")
    else:
        print("  registration:  ✗ not registered — run `hermes xchat setup`")
    return 0


ACCOUNT_EXPLAINER = (
    "Hermes runs AS the X account whose token you paste here: it reads that\n"
    "account's encrypted DMs and replies from it. Two ways to use it:\n"
    "  - Your own account: open your chat with yourself on X (\"message\n"
    "    yourself\") and talk to Hermes there. Anyone else who DMs you can be\n"
    "    answered too, so keep XCHAT_ALLOWED_USERS to people you trust.\n"
    "  - A separate account made for the agent (e.g. @yourname_hermes):\n"
    "    message it from your personal account like any other chat.\n"
)


def cmd_setup(*, force: bool) -> int:
    print(color("X Chat setup — end-to-end encrypted X DMs", Colors.BOLD))
    print(ACCOUNT_EXPLAINER)
    print(
        "You need an X developer app with OAuth 2.0 user-context enabled and a\n"
        "user access token, for the account Hermes should run as, carrying:\n"
        "dm.read dm.write users.read tweet.read (offline.access too if you want\n"
        "refresh tokens; media.write to send encrypted attachments).\n"
        "Docs: https://docs.x.com/xchat/getting-started\n"
    )

    # 1. Token
    token = _get_env("XCHAT_ACCESS_TOKEN")
    if token:
        print("Found existing XCHAT_ACCESS_TOKEN.")
    else:
        import getpass

        token = getpass.getpass("Paste the OAuth2 user access token: ").strip()
        if not token:
            print(color("No token provided — aborting.", Colors.RED))
            return 1
        _save_env("XCHAT_ACCESS_TOKEN", token)
    refresh = _get_env("XCHAT_REFRESH_TOKEN")
    if not refresh:
        import getpass

        refresh = getpass.getpass(
            "Paste the OAuth2 refresh token (optional, Enter to skip): "
        ).strip()
        if refresh:
            _save_env("XCHAT_REFRESH_TOKEN", refresh)
            client_id = input("X app OAuth2 client id (needed for refresh): ").strip()
            if client_id:
                _save_env("XCHAT_CLIENT_ID", client_id)

    # 2. Bot user id
    from .api import XChatApi, XChatApiError

    api = XChatApi(token)
    user_id = _get_env("XCHAT_USER_ID")
    if not user_id:
        try:
            user_id = _run(_fetch_user_id(api))
        except XChatApiError as e:
            print(color(f"Could not resolve the account's user id: {e}", Colors.RED))
            print("Check the token's scopes (users.read) and validity.")
            return 1
        finally:
            _run(api.aclose())
            api = None
        if not user_id:
            print(color("Could not resolve the account's user id.", Colors.RED))
            return 1
        _save_env("XCHAT_USER_ID", user_id)
    else:
        _run(api.aclose())
        api = None
    print(f"Hermes will run as X account {_account_label(user_id)}.")
    _allow_self(user_id)

    # 3. Keys + registration
    rc = cmd_register(force=force)
    if rc != 0:
        return rc

    print()
    print(color("Setup complete.", Colors.GREEN))
    print("Start the gateway, then message yourself on X (or DM this account from another one):")
    print("  hermes gateway start")
    return 0


def _account_label(user_id: str) -> str:
    """``@handle (id)`` for the configured account, or the bare id when the lookup fails."""
    from .api import XChatApi, XChatApiError

    async def _lookup() -> str:
        api = XChatApi(_get_env("XCHAT_ACCESS_TOKEN"))
        try:
            me = await api.get_my_user()
        finally:
            await api.aclose()
        return str(me.get("username") or "")

    try:
        handle = _run(_lookup())
    except (XChatApiError, OSError):
        handle = ""
    return f"@{handle} ({user_id})" if handle else user_id


def _allow_self(user_id: str) -> None:
    """Add the account itself to XCHAT_ALLOWED_USERS so its self-chat reaches Hermes."""
    current = [u.strip() for u in _get_env("XCHAT_ALLOWED_USERS").split(",") if u.strip()]
    if user_id in current:
        return
    _save_env("XCHAT_ALLOWED_USERS", ",".join([*current, user_id]))
    print(f"Added {user_id} (this account) to XCHAT_ALLOWED_USERS so your self-chat works.")


def cmd_register(*, force: bool) -> int:
    """Generate + register the E2EE keys. Re-runnable / resume-safe."""
    token = _get_env("XCHAT_ACCESS_TOKEN")
    if not token:
        print(color("XCHAT_ACCESS_TOKEN not set — run `hermes xchat setup` first.", Colors.RED))
        return 1
    user_id = _get_env("XCHAT_USER_ID")

    from .api import XChatApi, XChatApiError, XChatRateLimited
    from .crypto import XChatCrypto

    marker = _read_marker()
    if marker.get("registered") and not force:
        print(
            f"Already registered (key version {marker.get('version')}). "
            "Use `hermes xchat setup --force` to mint a NEW identity."
        )
        return 0

    async def _register() -> int:
        nonlocal user_id
        api = XChatApi(
            token,
            refresh_token=_get_env("XCHAT_REFRESH_TOKEN"),
            client_id=_get_env("XCHAT_CLIENT_ID"),
            client_secret=_get_env("XCHAT_CLIENT_SECRET"),
        )
        try:
            if not user_id:
                user_id = await _fetch_user_id(api)
                if not user_id:
                    print(color("Could not resolve the account's user id.", Colors.RED))
                    return 1
                _save_env("XCHAT_USER_ID", user_id)

            blob_path = _blob_path()
            resuming = blob_path.exists() and marker.get("body") and not force
            if not resuming and blob_path.exists() and not force:
                # A key blob with no (or corrupt) registration marker.
                # There is no forward secrecy in this protocol — silently
                # regenerating over the blob would permanently kill every
                # existing conversation. Refuse without --force.
                print(
                    color(
                        f"A private-key blob already exists at {blob_path} but the "
                        "registration marker is missing or unreadable. Overwriting it "
                        "would PERMANENTLY lose access to every existing conversation.",
                        Colors.RED,
                    )
                )
                print(
                    "If the blob belongs to a registered identity, restore or re-create "
                    f"{_marker_path()}. To really mint a NEW identity, re-run with --force."
                )
                return 1

            crypto = XChatCrypto()
            if resuming:
                crypto.load_keys(blob_path.read_text(encoding="utf-8").strip())
                body = marker["body"]
                version = str(marker.get("version") or "1")
                print(f"Resuming the saved identity ({blob_path}).")
            else:
                if blob_path.exists():
                    # force=True (guard above returns otherwise): back the old
                    # identity up rather than destroying it outright.
                    backup = blob_path.with_name(
                        blob_path.name + "." + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S") + ".bak"
                    )
                    blob_path.replace(backup)
                    try:
                        backup.chmod(0o600)
                    except OSError:
                        pass
                    print(f"Backed up the previous key blob to {backup}.")
                payload = crypto.generate_and_register_payload()
                body = payload["registration"]
                version = payload["version"]
                _write_private_blob(blob_path, payload["private_keys_b64"])
                _write_marker(
                    {"registered": False, "user_id": user_id, "version": version, "body": body}
                )
                print(f"Generated a new identity; private keys saved to {blob_path} (mode 600).")

            our_public_key = body["public_key"]["public_key"]

            # Reconcile: adopt an already-registered key instead of re-POSTing
            # (a prior POST may have applied server-side after erroring).
            try:
                existing = await api.get_public_keys(user_id)
            except XChatApiError:
                existing = []
            already = next(
                (k for k in existing if k.get("public_key") == our_public_key), None
            )
            if already:
                version = str(already.get("public_key_version") or version)
                print(f"Public key already registered (version {version}); skipping POST.")
            else:
                print(f"Registering public key version {version} …")
                try:
                    resp = await api.add_public_key(user_id, body)
                except XChatRateLimited as limited:
                    when = (
                        datetime.fromtimestamp(limited.reset_epoch, tz=timezone.utc).isoformat()
                        if limited.reset_epoch
                        else "the next window"
                    )
                    print(
                        color(
                            "Registration is rate limited (429). The daily budget is "
                            f"exhausted; wait until {when} and re-run — the saved "
                            "identity resumes, so no budget is wasted.",
                            Colors.RED,
                        )
                    )
                    return 1
                data = resp.get("data") or {}
                if isinstance(data, list):
                    data = data[0] if data else {}
                version = str(data.get("public_key_version") or version)

            _save_env("XCHAT_SIGNING_KEY_VERSION", version)
            _write_marker(
                {
                    "registered": True,
                    "user_id": user_id,
                    "version": version,
                    "body": body,
                    "registered_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            print(color(f"Key registration complete (version {version}).", Colors.GREEN))
            return 0
        finally:
            await api.aclose()

    try:
        return _run(_register())
    except XChatApiError as e:
        print(color(f"Registration failed: {e}", Colors.RED))
        return 1
