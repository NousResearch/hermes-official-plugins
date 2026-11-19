"""JSON CLI for Hermes users and worker submissions; stdlib-only and lazy."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .commands import (HelpRequested, JsonArgumentParser, UsageError,
                       add_authorization_parser, authorize, bounded_int)
from .errors import SecurityError, ValidationError
from .tools import MAX_INPUT_BYTES, encode_result, error_envelope, export_document


def get_service():
    from .plugin import get_service as resolve_service
    return resolve_service()


def setup_parser(parser):
    """Configure a standalone or ctx.register_cli_command argparse parser."""
    subs = parser.add_subparsers(dest="security_command", required=True,
                                parser_class=(JsonArgumentParser if isinstance(parser, JsonArgumentParser)
                                              else argparse.ArgumentParser))
    start = subs.add_parser("start", help="Start a security scan of a local repository")
    start.add_argument("--path", required=True)
    start.add_argument("--mode", choices=["standard", "deep", "diff"], default="standard",
                       help="standard = full codebase, diff = recent changes, deep = more thorough")
    start.add_argument("--tests", dest="allow_tests", action="store_true", help="Run tests to confirm findings")
    start.add_argument("--context", help="Optional extra context for the reviewer")
    start.add_argument("--scope", action="append", help="Only scan this folder/glob (repeatable)")
    # Advanced/legacy options.
    start.add_argument("--safety-level", choices=["static", "local-safe", "active-authorized"], default=None, help=argparse.SUPPRESS)
    start.add_argument("--base", help=argparse.SUPPRESS)
    start.add_argument("--head", help=argparse.SUPPRESS)
    start.add_argument("--allow-local", action="store_true", help=argparse.SUPPRESS)
    for command in ("get", "submit", "checkpoint", "validate", "chains", "finalize", "cancel", "resume", "export"):
        p = subs.add_parser(command)
        p.add_argument("--scan", dest="scan_id", required=True)
        if command in {"submit", "checkpoint", "validate"}:
            p.add_argument("--file", required=True)
        if command == "get":
            p.add_argument("--section")
            _pagination(p)
        elif command == "validate":
            p.add_argument("--allow-local", action="store_true")
        elif command == "chains":
            p.add_argument("--explanations")
        elif command == "export":
            p.add_argument("--format", dest="fmt", choices=["md", "sarif", "json", "csv"], required=True)
            p.add_argument("--out")
    listing = subs.add_parser("list")
    for name in ("q", "status", "repo-key"):
        listing.add_argument("--" + name)
    _pagination(listing)
    add_authorization_parser(subs)
    subs.add_parser("data-dir")


def _pagination(parser):
    parser.add_argument("--limit", type=bounded_int(500), default=50)
    parser.add_argument("--offset", type=bounded_int(2**63 - 1, 0), default=0)


def read_json_file(filename):
    """Read bounded UTF-8 JSON; reject duplicate keys and non-finite numbers."""
    def pairs(items):
        obj = {}
        for key, value in items:
            if key in obj:
                raise ValueError("Duplicate key")
            obj[key] = value
        return obj

    def constant(value):
        raise ValueError("Non-finite value")

    try:
        path = Path(filename)
        if not path.is_file():
            raise ValidationError("Input must be a readable regular JSON file")
        with path.open("rb") as stream:
            raw = stream.read(MAX_INPUT_BYTES + 1)
        if len(raw) > MAX_INPUT_BYTES:
            raise ValidationError("Input JSON exceeds the 2 MiB limit")
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant)
        # Overflowing exponent numbers parse to infinity even without parse_constant.
        json.dumps(data, allow_nan=False)
        return data
    except (OSError, UnicodeError, ValueError, RecursionError):
        raise ValidationError("Input file must contain valid finite UTF-8 JSON without duplicate keys") from None


def _object(value, allowed=None):
    if not isinstance(value, dict) or (allowed is not None and set(value) - set(allowed)):
        raise ValidationError("Input JSON must be an object with only the documented fields")
    return value


def _execute(args, service_factory):
    command = args.security_command
    if command == "data-dir":
        from .config import resolve_data_dir
        return {"data_dir": str(resolve_data_dir())}
    # Read and check files before constructing a service or making any mutation.
    payload = read_json_file(args.file) if command in {"submit", "checkpoint", "validate"} else None
    if command == "submit":
        _object(payload)
        if payload.get("scanId", args.scan_id) != args.scan_id:
            raise ValidationError("Worker result scanId does not match --scan")
        payload = {**payload, "scanId": args.scan_id}
    elif command == "checkpoint":
        payload = {"coverage": payload} if isinstance(payload, list) else payload
        _object(payload, {"coverage", "note", "blocked"})
        if not isinstance(payload.get("coverage"), list):
            raise ValidationError("Checkpoint requires a coverage array")
    elif command == "validate":
        payload = {"plans": payload} if isinstance(payload, list) else payload
        _object(payload, {"receipts", "plans"})
        if not payload or any(not isinstance(v, list) for v in payload.values()):
            raise ValidationError("Validation file requires receipts or plans arrays")
        if args.allow_local and ("plans" not in payload or "receipts" in payload):
            raise ValidationError("--allow-local requires a plans file, not imported receipts")
    explanations = None
    if command == "chains" and args.explanations:
        explanations = _object(read_json_file(args.explanations))
    service = service_factory()
    if command == "start":
        opts = {key: getattr(args, key) for key in ("path", "mode", "safety_level", "scope", "base", "head", "context")
                if getattr(args, key) is not None}
        if args.allow_tests:
            opts["allow_tests"] = True
        if args.allow_local:
            opts["allowLocalValidation"] = True
        return service.start_scan(**opts)
    if command == "get":
        return service.get_scan(args.scan_id, section=args.section, limit=args.limit, offset=args.offset)
    if command == "submit":
        return service.submit_worker_result(payload)
    if command == "checkpoint":
        return service.checkpoint(args.scan_id, **payload)
    if command == "validate":
        return service.record_validations(args.scan_id, **payload, user_authorized=args.allow_local)
    if command == "chains":
        return (service.record_chains(args.scan_id, explanations=explanations) if explanations is not None
                else service.propose_chains(args.scan_id))
    if command in {"finalize", "cancel", "resume"}:
        return getattr(service, command)(args.scan_id)
    if command == "export":
        result = service.export(args.scan_id, args.fmt)
        if not args.out:
            return result
        document = export_document(result)
        content = document.get("content") if isinstance(document, dict) else document
        if content is None:
            raise ValidationError("Export result has no content to write")
        if not isinstance(content, (bytes, str)):
            content = json.dumps(content, ensure_ascii=False, allow_nan=False)
        from .canonical import atomic_write
        atomic_write(Path(args.out), content)
        return {"out": str(Path(args.out)), "format": args.fmt}
    if command == "list":
        return service.list_scans(q=args.q, status=args.status, repo_key=args.repo_key, limit=args.limit, offset=args.offset)
    if command == "authorize-validation":
        return authorize(service, args)
    if command == "revoke":
        return service.revoke_grant(args.grant_id)
    raise UsageError("Unknown security command")


def run_namespace(args, service_factory=None):
    """Execute a parsed CLI invocation and print exactly one JSON envelope."""
    try:
        result = _execute(args, service_factory or get_service)
        print(encode_result(result, cap=False))
        return 0
    except Exception as exc:
        print(json.dumps(error_envelope(exc)))
        return 2 if isinstance(exc, UsageError) else 3 if isinstance(exc, SecurityError) else 1


def main(argv=None):
    """Standalone entry point; exit 0 success, 2 usage, 3 domain, 1 internal."""
    parser = JsonArgumentParser(prog="python -m hermes_security")
    setup_parser(parser)
    try:
        args = parser.parse_args(argv)
    except HelpRequested as exc:
        print(encode_result({"help": exc.text}, cap=False))
        return 0
    except UsageError as exc:
        print(json.dumps(error_envelope(exc)))
        return 2
    return run_namespace(args)
