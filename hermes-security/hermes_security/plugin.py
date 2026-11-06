"""Hermes registration only; service imports and state creation are lazy."""
from __future__ import annotations

from pathlib import Path
from threading import Lock

_SERVICES = {}
_SERVICE_LOCK = Lock()
_SKILLS = ("security-audit", "security-diff-review", "threat-model", "validate-finding",
           "fix-finding", "verify-fix", "define-security-policy")


def get_service(ctx=None):
    """Share a service only within the same profile-scoped data directory."""
    from .config import resolve_data_dir
    directory = Path(resolve_data_dir(ctx)).resolve()
    with _SERVICE_LOCK:
        if directory not in _SERVICES:
            from .service import SecurityService
            _SERVICES[directory] = SecurityService(directory, profile=getattr(ctx, "profile_name", "default"))
        return _SERVICES[directory]


def register(ctx):
    """Register tools, commands, turn/worker hooks and read-only namespaced skills, without I/O."""
    from . import cli, commands, tools

    def service_factory():
        return get_service(ctx)

    for schema in tools.TOOL_SCHEMAS:
        ctx.register_tool(name=schema["name"], toolset="security", schema=schema,
                          handler=tools.make_handler(schema["name"], service_factory))
    ctx.register_command("security", commands.make_command(service_factory),
                         description="Security scan status and letting a scan test your running app",
                         args_hint="status | authorize-validation | revoke | help")
    if callable(getattr(ctx, "register_cli_command", None)):
        def cli_handler(args):
            # Hermes main.py propagates an integer func(args) return code.
            return cli.run_namespace(args, service_factory)
        # Hermes core already owns `hermes security` (supply-chain audit), which shadows plugin commands.
        ctx.register_cli_command("security-review", "Local repository security review",
                                 cli.setup_parser, cli_handler)
    if callable(getattr(ctx, "register_hook", None)):
        # Hooks only trigger a check; the scan database decides what happens next.
        from . import recovery
        for name, callback in recovery.make_hooks(ctx, service_factory).items():
            ctx.register_hook(name, callback)
    skill_root = Path(__file__).resolve().parent.parent / "skills"
    for name in _SKILLS:
        ctx.register_skill(name, skill_root / name / "SKILL.md")
