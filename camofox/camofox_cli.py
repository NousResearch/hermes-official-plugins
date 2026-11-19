"""``hermes camofox setup|status``: point Hermes at a camofox-browser server and check it.

Replaces the Camofox row core's ``hermes tools`` picker used to carry and its reachability check.
Hermes never installs or starts the Camofox server; it only needs its URL.
"""

from __future__ import annotations

import argparse

from . import camofox_client as client

_SERVER_HELP = ("Server setup: https://github.com/jo-inc/camofox-browser  "
                "(native: npm install && npm start; Docker: docker run -p 9377:9377 -e CAMOFOX_PORT=9377 "
                "jo-inc/camofox-browser)")


def setup_cli(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="camofox_command")
    setup = sub.add_parser("setup", help="Save CAMOFOX_URL (and an optional CAMOFOX_API_KEY) to this profile's .env")
    setup.add_argument("--url", help="Camofox server URL (default: prompt, http://localhost:9377)")
    sub.add_parser("status", help="Show the configured Camofox server and whether it answers")


def _identity_mode() -> str:
    cfg = client.get_camofox_config()
    if client._camofox_identity_override(None, cfg):
        return "external (CAMOFOX_USER_ID / browser.camofox.user_id)"
    if client._managed_persistence_enabled(cfg):
        return "managed persistence (stable per-profile Camofox profile)"
    return "ephemeral (new Camofox profile per session)"


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


def handle_cli(args: argparse.Namespace) -> int:
    command = getattr(args, "camofox_command", None)
    if command == "setup":
        return cmd_setup(getattr(args, "url", None))
    return cmd_status()
