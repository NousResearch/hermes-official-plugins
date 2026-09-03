"""CLI commands for the google_meet plugin (``hermes meet <subcommand>``).

  setup / install — preflight and install prerequisites
  auth            — open a browser to sign into Google, save storage state
  join <url>      — join a Meet URL (locally or on a remote node)
  status / transcript / say / stop — drive the active bot
  node            — remote node host management (see node/cli.py)
"""

from __future__ import annotations

import argparse
import json
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

from hermes_constants import get_hermes_home

from plugins.google_meet import process_manager as pm
from plugins.google_meet.meet_bot import _is_safe_meet_url
from plugins.google_meet.node.cli import register_cli as _register_node_cli
from plugins.google_meet.tools import resolve_node


def _auth_state_path() -> Path:
    return Path(get_hermes_home()) / "workspace" / "meetings" / "auth.json"


def register_cli(subparser: argparse.ArgumentParser) -> None:
    """Build the ``hermes meet`` argparse tree (called at plugin load time)."""
    subs = subparser.add_subparsers(dest="meet_command")

    subs.add_parser("setup", help="Preflight: playwright, chromium, auth")

    inst_p = subs.add_parser(
        "install", help="Install prerequisites (pip deps, Chromium, platform audio tools)")
    inst_p.add_argument("--realtime", action="store_true",
                        help="Also install realtime audio tools (pulseaudio-utils on Linux, BlackHole+ffmpeg on macOS). Uses sudo/brew, prompts before invoking either.")
    inst_p.add_argument("--yes", "-y", action="store_true",
                        help="Answer yes to all prompts (use with care; will run sudo apt-get or brew without asking).")

    subs.add_parser("auth", help="Sign in to Google and save session state")

    join_p = subs.add_parser("join", help="Join a Meet URL")
    join_p.add_argument("url", help="https://meet.google.com/...")
    join_p.add_argument("--guest-name", default="Hermes Agent")
    join_p.add_argument("--duration", default=None, help="e.g. 30m, 2h, 90s")
    join_p.add_argument("--headed", action="store_true", help="show browser")
    join_p.add_argument("--mode", choices=("transcribe", "realtime"), default="transcribe",
                        help="transcribe (default, listen-only) or realtime (speak via OpenAI Realtime)")
    join_p.add_argument("--node", default=None,
                        help="remote node name, or 'auto' to use the sole registered node")

    subs.add_parser("status", help="Print current Meet bot state")

    tr_p = subs.add_parser("transcript", help="Print the scraped transcript")
    tr_p.add_argument("--last", type=int, default=None)

    say_p = subs.add_parser("say", help="Speak text in an active realtime meeting")
    say_p.add_argument("text", help="what to say")
    say_p.add_argument("--node", default=None)

    subs.add_parser("stop", help="Leave the current meeting")

    _register_node_cli(subs.add_parser(
        "node", help="Manage remote meet node hosts (run/list/approve/remove/status/ping)"))
    subparser.set_defaults(func=meet_command)


def _cmd_node(args: argparse.Namespace) -> int:
    fn = getattr(args, "func", None)
    if fn is None or fn is meet_command:
        print("usage: hermes meet node {run,list,approve,remove,status,ping}")
        return 2
    return fn(args)


_DISPATCH = {
    "setup": lambda a: _cmd_setup(),
    "install": lambda a: _cmd_install(realtime=bool(getattr(a, "realtime", False)),
                                      assume_yes=bool(getattr(a, "yes", False))),
    "auth": lambda a: _cmd_auth(),
    "join": lambda a: _cmd_join(url=a.url, guest_name=a.guest_name, duration=a.duration, headed=a.headed,
                                mode=getattr(a, "mode", "transcribe"), node=getattr(a, "node", None)),
    "status": lambda a: _print_result(pm.status()),
    "transcript": lambda a: _cmd_transcript(last=a.last),
    "say": lambda a: _cmd_say(text=a.text, node=getattr(a, "node", None)),
    "stop": lambda a: _print_result(pm.stop(reason="hermes meet stop")),
    "node": _cmd_node}


def meet_command(args: argparse.Namespace) -> int:
    sub = getattr(args, "meet_command", None)
    if not sub:
        print("usage: hermes meet {setup,auth,join,status,transcript,say,stop,node}")
        return 2
    handler = _DISPATCH.get(sub)
    if handler is None:
        print(f"unknown subcommand: {sub}")
        return 2
    return handler(args)


def _cmd_setup() -> int:
    print("google_meet preflight")
    print("---------------------")

    system = platform.system()
    system_ok = system in {"Linux", "Darwin"}
    print(f"  platform       : {system}  [{'ok' if system_ok else 'unsupported'}]")

    try:
        import playwright  # noqa: F401
        pw_ok = True
        print("  playwright     : installed")
    except ImportError:
        pw_ok = False
        print("  playwright     : NOT installed — run: pip install playwright")

    chromium_ok = False
    chromium_msg = "unknown"
    if pw_ok:
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                exe = p.chromium.executable_path
                if exe and Path(exe).exists():
                    chromium_ok = True
                    chromium_msg = f"ok ({exe})"
                else:
                    chromium_msg = "not installed — run: python -m playwright install chromium"
        except Exception as e:
            chromium_msg = f"probe failed: {e}"
    print(f"  chromium       : {chromium_msg}")

    auth_path = _auth_state_path()
    print("  google auth    : "
          + (f"ok ({auth_path})" if auth_path.is_file() else "not saved — run: hermes meet auth"))

    print()
    all_ok = system_ok and pw_ok and chromium_ok
    print("ready. Join a meeting:  hermes meet join https://meet.google.com/abc-defg-hij" if all_ok
          else "not ready yet — fix the items above.")
    return 0 if all_ok else 1


def _cmd_install(*, realtime: bool, assume_yes: bool) -> int:
    """pip deps + Chromium; ``--realtime`` adds the platform audio bridge deps.
    Prompts before every package-manager invocation unless ``--yes``. Linux/macOS only."""
    system = platform.system()
    if system not in {"Linux", "Darwin"}:
        print(f"google_meet install: {system} is not supported (linux/macos only)")
        return 1

    def _install_pkgs(prompt: str, cmd: list[str], fail_msg: str) -> None:
        """Confirm (unless --yes) then run a package-manager command, reporting failure."""
        try:
            ok = assume_yes or input(f"{prompt} [y/N] ").strip().lower() in {"y", "yes"}
        except EOFError:
            ok = False
        if not ok:
            print("  skipped (you can run it manually later)")
            return
        print(f"  $ {' '.join(cmd)}")
        # noqa: subprocess-stdin — sudo/brew may prompt on the tty; user explicitly confirmed above
        if subprocess.run(cmd, check=False).returncode != 0:
            print(fail_msg)

    print("google_meet install")
    print("-------------------")

    pip_pkgs = ["playwright", "websockets"]
    print(f"\n[1/3] pip install: {' '.join(pip_pkgs)}")
    try:
        from hermes_cli.tools_config import _pip_install

        if _pip_install(["--upgrade", *pip_pkgs], capture_output=False).returncode != 0:
            print("  pip install failed")
            return 1
    except Exception as e:
        print(f"  pip install failed: {e}")
        return 1

    print("\n[2/3] python -m playwright install chromium")
    try:
        res = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"], check=False, stdin=subprocess.DEVNULL
        )
        if res.returncode != 0:
            print("  playwright install failed (may already be installed)")
    except Exception as e:
        print(f"  playwright install failed: {e}")
        return 1

    if not realtime:
        print("\n[3/3] skipped (pass --realtime to install audio tooling too)")
    else:
        print("\n[3/3] realtime audio deps")
        if system == "Linux":
            if shutil.which("paplay") and shutil.which("pactl"):
                print("  pulseaudio-utils already installed.")
            else:
                _install_pkgs("  install pulseaudio-utils? this runs `sudo apt-get install -y pulseaudio-utils`",
                              ["sudo", "apt-get", "install", "-y", "pulseaudio-utils"],
                              "  apt install failed — install pulseaudio-utils manually")
        elif system == "Darwin":
            have_bh = False
            try:
                out = subprocess.check_output(
                    ["system_profiler", "SPAudioDataType"], text=True, encoding='utf-8', errors='replace',
                    stdin=subprocess.DEVNULL)
                have_bh = "BlackHole" in out
            except Exception:
                pass
            needs = ([] if have_bh else ["blackhole-2ch"]) + ([] if shutil.which("ffmpeg") else ["ffmpeg"])
            if not needs:
                print("  BlackHole and ffmpeg already installed.")
            elif not shutil.which("brew"):
                print("  missing: " + ", ".join(needs) + "\n"
                      "  install Homebrew first (https://brew.sh) or install the packages manually.")
            else:
                _install_pkgs(f"  install via brew: {' '.join(needs)}?", ["brew", "install", *needs],
                              "  brew install failed — install them manually")
            print("\n  NOTE: macOS does not auto-route audio. Open\n"
                  "    System Settings → Sound → Input\n"
                  "  and select 'BlackHole 2ch' before starting a realtime meeting.\n"
                  "  hermes will not switch your default input for you.")

    print("\ndone. verify with: hermes meet setup")
    return 0


def _cmd_auth() -> int:
    """Open a headed Chromium, let the user sign in, save storage_state."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright is not installed. run:\n"
              "  pip install playwright && python -m playwright install chromium")
        return 1

    path = _auth_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    print("opening Chromium — sign in to Google, then return here and press Enter.")
    print(f"saving storage state to: {path}")
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=False)
            context = browser.new_context()
            page = context.new_page()
            page.goto("https://accounts.google.com/", wait_until="domcontentloaded")
            try:
                input("press Enter after you've signed in ... ")
            except EOFError:
                pass
            context.storage_state(path=str(path))
            browser.close()
    except Exception as e:
        print(f"auth failed: {e}")
        return 1
    print("saved. you can now run: hermes meet join <url>")
    return 0


def _print_result(res: dict) -> int:
    print(json.dumps(res, indent=2))
    return 0 if res.get("ok") else 1


def _remote(node: str, op: str, call) -> int:
    """Run *call(client)* against the registered node *node* and print the result."""
    try:
        client, name = resolve_node(node)
    except ImportError as e:
        print(f"node module unavailable: {e}")
        return 1
    if client is None:
        print(f"no registered node matches {node!r}")
        return 1
    try:
        res = call(client)
    except Exception as e:
        print(f"remote {op} failed: {e}")
        return 1
    return _print_result({"node": name, **res})


def _cmd_join(url: str, *, guest_name: str, duration: Optional[str], headed: bool,
              mode: str = "transcribe", node: Optional[str] = None) -> int:
    if not _is_safe_meet_url(url):
        print(f"refusing: not a meet.google.com URL: {url}")
        return 2
    if node:
        return _remote(node, "start_bot", lambda c: c.start_bot(
            url=url, guest_name=guest_name, duration=duration, headed=headed, mode=mode))
    auth = _auth_state_path()
    return _print_result(pm.start(url=url, headed=headed, guest_name=guest_name, duration=duration,
                                  auth_state=str(auth) if auth.is_file() else None, mode=mode))


def _cmd_say(text: str, node: Optional[str] = None) -> int:
    if not (text or "").strip():
        print("refusing: empty text")
        return 2
    if node:
        return _remote(node, "say", lambda c: c.say(text))
    return _print_result(pm.enqueue_say(text))


def _cmd_transcript(last: Optional[int]) -> int:
    res = pm.transcript(last=last)
    if not res.get("ok"):
        return _print_result(res)
    for ln in res.get("lines", []):
        print(ln)
    return 0


if __name__ == "__main__":  # pragma: no cover
    parser = argparse.ArgumentParser(prog="hermes meet")
    register_cli(parser)
    sys.exit(meet_command(parser.parse_args()))
