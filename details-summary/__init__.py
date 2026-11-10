"""details-summary: every pull request body gets a short human-readable opener, with the full
agent-written description folded into <details><summary>model effort</summary>."""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

from .commands import bodies_in_tool_call
from .fold import DEFAULT_MAX_OPENER_WORDS, DEFAULT_MAX_SENTENCE_WORDS, check_body, format_report
from .guidance import prompt_section, refusal

logger = logging.getLogger(__name__)

SECTION_ID = "details-summary"
_ctx = None


def _limits() -> tuple[int, int]:
    def num(key: str, default: int) -> int:
        try:
            value = int(_ctx.get_config(key, default)) if _ctx is not None else default
        except (TypeError, ValueError):
            return default
        return value if value > 0 else default
    return (num("max_opener_words", DEFAULT_MAX_OPENER_WORDS),
            num("max_sentence_words", DEFAULT_MAX_SENTENCE_WORDS))


def _cwd() -> str:
    try:
        return _ctx.session_cwd() if _ctx is not None else ""
    except Exception:
        return ""


def check_tool_call(tool_name: str, args: Any, cwd: str = "") -> str | None:
    """The refusal message for a tool call that writes a non-conforming PR body, else None."""
    if not isinstance(args, dict):
        return None
    refs = bodies_in_tool_call(tool_name, args, cwd)
    if not refs:
        return None
    max_words, max_sentence = _limits()
    problems = []
    for ref in refs:
        if ref.text is None:
            problems.append(f"Pull request body ({ref.where}) cannot be checked:\n- {ref.unreadable}")
            continue
        report = check_body(ref.text, max_opener_words=max_words, max_sentence_words=max_sentence)
        if not report.ok:
            problems.append(format_report(report, ref.where))
    if not problems:
        return None
    return refusal("\n\n".join(problems), max_words, max_sentence)


def _pre_tool_call(tool_name: str = "", args: Any = None, **kwargs: Any):
    message = check_tool_call(tool_name, args, _cwd())
    if message is None:
        return None
    logger.info("details-summary blocked %s (session %s)", tool_name, kwargs.get("session_id", ""))
    return {"action": "block", "message": message}


def _section(session_info) -> str:
    max_words, max_sentence = _limits()
    return prompt_section(session_info, max_words, max_sentence)


def _cli_setup(parser) -> None:
    sub = parser.add_subparsers(dest="details_summary_cmd")
    check = sub.add_parser("check", help="Check a PR body file (or - for stdin)")
    check.add_argument("file")
    sub.add_parser("prompt", help="Print the prompt section the model sees")


def _cli_handler(args) -> int:
    max_words, max_sentence = _limits()
    if getattr(args, "details_summary_cmd", None) == "prompt":
        try:
            from hermes_cli.config import load_config_readonly
            model = str(((load_config_readonly() or {}).get("model") or {}).get("default") or "")
        except Exception:
            model = ""
        print(prompt_section({"model": model}, max_words, max_sentence))
        return 0
    if getattr(args, "details_summary_cmd", None) == "check":
        text = sys.stdin.read() if args.file == "-" else Path(args.file).read_text(encoding="utf-8")
        report = check_body(text, max_opener_words=max_words, max_sentence_words=max_sentence)
        if report.ok:
            print(f"details-summary: ok ({len(report.opener.split())}-word opener, summary {report.label!r})")
            return 0
        print(format_report(report, args.file))
        return 1
    print("usage: hermes details-summary {check FILE | prompt}")
    return 2


def register(ctx) -> None:
    global _ctx
    _ctx = ctx
    ctx.register_hook("pre_tool_call", _pre_tool_call)
    ctx.register_system_prompt_section(SECTION_ID, _section)
    ctx.register_cli_command("details-summary", "Check PR bodies against the details-summary shape",
                             _cli_setup, _cli_handler)
