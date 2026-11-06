"""User-only slash command and shared authorization argument parsing."""
from __future__ import annotations

import argparse
import json
import shlex
from urllib.parse import urlsplit

from .errors import ValidationError
from .tools import encode_result, error_envelope

HELP = """/security status
/security authorize-validation <scanId> <origin>   let the scan test your running app (1 hour, 200 requests)
/security revoke <grantId>                          stop app testing
/security help
To scan, just ask: "scan this repo for security issues" (add "and run tests to confirm" to allow tests)."""


class UsageError(ValidationError):
    """Malformed command syntax (CLI exit 2, distinct from domain rejection)."""


class HelpRequested(Exception):
    def __init__(self, text):
        self.text = text


class JsonArgumentParser(argparse.ArgumentParser):
    """Keep parser diagnostics machine-readable and avoid echoing input secrets."""
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("allow_abbrev", False)
        super().__init__(*args, **kwargs)

    def error(self, message):
        raise UsageError("Invalid command arguments; use help for syntax and limits")

    def print_help(self, file=None):
        raise HelpRequested(self.format_help())


def bounded_int(maximum, minimum=1):
    def parse(text):
        try:
            value = int(text)
        except (TypeError, ValueError):
            raise argparse.ArgumentTypeError("Expected an integer") from None
        if not minimum <= value <= maximum:
            raise argparse.ArgumentTypeError("Integer outside allowed range")
        return value
    return parse


def _actions(text):
    # local-command is not grantable: local execution requires a direct user call (Desktop or validate --allow-local).
    values = text.split(",")
    if not values or any(v != "http-probe" for v in values):
        raise argparse.ArgumentTypeError("Actions must be http-probe")
    return list(dict.fromkeys(values))


def _origin(text):
    try:
        url = urlsplit(text)
        valid = (url.scheme in {"http", "https"} and url.hostname and
                 not url.username and not url.password and not url.path and
                 not url.query and not url.fragment and not any(c.isspace() for c in text))
        port = url.port
        if not valid or (port is not None and not 1 <= port <= 65535):
            raise ValueError
    except ValueError:
        raise argparse.ArgumentTypeError("Origin must be scheme://host[:port], without credentials or path") from None
    return text  # Preserve the supplied token; no silent origin repair.


def add_authorization_parser(subparsers):
    parser = subparsers.add_parser("authorize-validation", help="Let a scan test your running app (default: 1 hour, 200 requests)")
    parser.add_argument("scan_id")
    parser.add_argument("origin", type=_origin)
    parser.add_argument("--actions", type=_actions, default=["http-probe"])
    parser.add_argument("--minutes", type=bounded_int(240), default=60)
    parser.add_argument("--max-requests", type=bounded_int(200), default=200)
    revoke = subparsers.add_parser("revoke", help="Revoke a validation grant")
    revoke.add_argument("grant_id")


def authorize(service, args):
    """Only user command surfaces call this function; it is not an agent tool."""
    return service.mint_grant(args.scan_id, origins=[args.origin], actions=args.actions,
                              expires_in_s=args.minutes * 60, max_requests=args.max_requests,
                              created_by="user-command")


def make_command(get_service):
    """Return the documented Hermes handler: fn(raw_args: str) -> str."""
    def handler(raw_args: str) -> str:
        try:
            parser = JsonArgumentParser(prog="/security", add_help=False)
            subs = parser.add_subparsers(dest="command", required=True)
            subs.add_parser("help", add_help=False)
            subs.add_parser("status", add_help=False)
            add_authorization_parser(subs)
            try:
                tokens = shlex.split(raw_args)
            except ValueError:
                raise UsageError("Unclosed command quoting") from None
            args = parser.parse_args(tokens or ["status"])
            if args.command == "help":
                return encode_result({"help": HELP})
            service = get_service()
            if args.command == "status":
                result = {"summary": service.summary(), "recent_scans": service.list_scans(limit=10, offset=0)}
            elif args.command == "authorize-validation":
                result = authorize(service, args)
            else:
                result = service.revoke_grant(args.grant_id)
            return encode_result(result)
        except HelpRequested:
            return encode_result({"help": HELP})
        except Exception as exc:
            return json.dumps(error_envelope(exc))
    return handler
