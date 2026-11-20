"""``hermes camofox setup|status``: point Hermes at a camofox-browser server and check it.

Replaces the Camofox row core's ``hermes tools`` picker used to carry and its reachability check.
Hermes never installs or starts the Camofox server; it only needs its URL.
"""

from __future__ import annotations

import argparse
import json

from . import camofox_client as client

_SERVER_HELP = ("Server setup: https://github.com/jo-inc/camofox-browser  "
                "(native: npm install && npm start; Docker: docker run -p 9377:9377 -e CAMOFOX_PORT=9377 "
                "jo-inc/camofox-browser)")


def setup_cli(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="camofox_command")
    setup = sub.add_parser("setup", help="Save CAMOFOX_URL (and an optional CAMOFOX_API_KEY) to this profile's .env")
    setup.add_argument("--url", help="Camofox server URL (default: prompt, http://localhost:9377)")
    sub.add_parser("status", help="Show the configured Camofox server and whether it answers")
    signed_in = sub.add_parser(
        "signed-in", help="Check whether a site is still signed in for this profile's Camofox identity")
    signed_in.add_argument("origin", help="Site to check (e.g., https://github.com or github.com)")
    signed_in.add_argument("--user-id", help="Check this Camofox userId instead of the resolved profile identity")
    signed_in.add_argument("--open-session", action="store_true",
                           help="Load the persisted profile first (opens an about:blank tab) when no session is active")
    identity = sub.add_parser(
        "identity", help="Close the browser session of a scheduled agent (its cron job)")
    identity.add_argument("job", help="Cron job id (or cron:<job_id>): that job's Camofox session is closed")


def _identity_mode() -> str:
    cfg = client.get_camofox_config()
    if client._camofox_identity_override(None, cfg):
        return "external (CAMOFOX_USER_ID / browser.camofox.user_id)"
    mode = ("managed persistence (stable per-profile Camofox profile)"
            if client._managed_persistence_enabled(cfg) else "ephemeral (new Camofox profile per session)")
    if client._durable_agent_identity_enabled(cfg):
        mode += "; scheduled agents get a profile of their own (durable_agent_identity)"
    return mode


def cmd_status() -> int:
    url = client.get_camofox_url()
    if not url:
        print("CAMOFOX_URL is not set. Run: hermes camofox setup")
        print(_SERVER_HELP)
        return 1
    print(f"Camofox server: {url}")
    print(f"Identity:       {_identity_mode()}")
    if not client.check_camofox_available():
        print("Status:         not reachable. Start your server and check CAMOFOX_URL.")
        print(_SERVER_HELP)
        return 1
    print("Status:         reachable")
    vnc = client.get_vnc_url()
    if vnc:
        print(f"VNC:            {vnc}")
    return 0


def cmd_setup(url: str | None) -> int:
    from hermes_cli.gateway_setup_wizard import save_env_values
    from hermes_cli.setup import prompt

    print("Camofox is an externally managed server; Hermes does not install or start it.")
    url = (url or prompt("Camofox server URL", client.get_camofox_url() or "http://localhost:9377")).rstrip("/")
    values = {"CAMOFOX_URL": url}
    api_key = prompt("Camofox API key (optional, Enter to skip)", "", password=True)
    if api_key:
        values["CAMOFOX_API_KEY"] = api_key
    save_env_values(**values)
    print("Saved to this profile's .env. Enable the tools with: hermes tools enable camofox")
    return cmd_status()


def cmd_identity(job: str) -> int:
    key = job.strip() if job.strip().startswith("cron:") else f"cron:{job.strip()}"
    result = client.forget_agent_identity(key)
    print(f"Agent:    {result['agent_key']}")
    print(f"Identity: {result['user_id']}")
    print(f"Server session closed: {'yes' if result['session_closed'] else 'no (server unreachable)'}")
    print("Profile:  left in place. The Camofox server owns the profile directory and has no")
    print("          endpoint that deletes one; to drop the cookies too, remove this userId's")
    print("          directory under its profile dir (CAMOFOX_PROFILE_DIR, default ~/.camofox/profiles).")
    return 0


def cmd_signed_in(origin: str, user_id: str | None, open_session: bool) -> int:
    from . import camofox_tools as tools
    result = tools.browser_signed_in_status(origin, user_id=user_id, open_session=open_session)
    try:
        data = json.loads(result)
    except ValueError:
        data = {"error": result}
    if data.get("error"):
        print(f"Signed-in check failed: {data['error']}")
        return 1
    print(f"Origin:    {data.get('origin')}")
    print(f"Identity:  {data.get('user_id')} ({data.get('identity_source')})")
    signed = data.get("signed_in")
    if signed is None:
        print("Signed in: unknown — no active Camofox session for this profile.")
        note = data.get("note") or ""
        if note:
            print(f"           {note}")
        return 0
    if signed:
        detail = f"{data.get('active_cookie_count')} cookie(s) for this origin"
        if data.get("earliest_expiry"):
            detail += f", earliest expiry {data['earliest_expiry']}"
            if data.get("earliest_expiry_cookie"):
                detail += f" (cookie {data['earliest_expiry_cookie']})"
        print(f"Signed in: yes — {detail}")
    else:
        print("Signed in: no — no live cookies for this origin (or all have expired).")
    if data.get("expired_count"):
        print(f"Expired:   {data['expired_count']} cookie(s) for this origin have expired")
    names = ", ".join(data.get("cookie_names") or []) or "(none)"
    print(f"Cookie names: {names} (values are never shown)")
    return 0


def handle_cli(args: argparse.Namespace) -> int:
    command = getattr(args, "camofox_command", None)
    if command == "setup":
        return cmd_setup(getattr(args, "url", None))
    if command == "signed-in":
        return cmd_signed_in(getattr(args, "origin", ""), getattr(args, "user_id", None),
                             bool(getattr(args, "open_session", False)))
    if command == "identity":
        return cmd_identity(getattr(args, "job", ""))
    return cmd_status()
