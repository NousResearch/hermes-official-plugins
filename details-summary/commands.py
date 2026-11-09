"""Find pull request bodies inside tool calls.

Covers ``gh pr create``, ``gh pr edit``, ``gh api`` writes to ``.../pulls`` (terminal tool), and any
tool whose name says it creates or updates a pull request and takes a ``body`` argument (GitHub MCP
servers and connectors).
"""
from __future__ import annotations

import json
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

_SEPARATORS = {"&&", "||", ";", "|", "&", "\n", "(", ")"}
_PULLS_PATH = re.compile(r"(?:^|/)repos/[^/\s]+/[^/\s]+/pulls(?:/\d+)?/?$")
_PR_TOOL_NAME = re.compile(r"(create|update|edit|open)_?(pull_?request|pr)\b|pull_?request_(create|update)", re.I)
_SHELL_EXPANSION = re.compile(r"\$\(|`|\$\{?\w")


@dataclass
class BodyRef:
    """A pull request body found in a tool call. ``text`` is None when it cannot be read."""

    where: str
    text: Optional[str] = None
    unreadable: str = ""


def _tokens(command: str) -> list[str]:
    lexer = shlex.shlex(command, posix=True, punctuation_chars="();<>|&\n")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    lexer.commenters = ""
    return list(lexer)


def _segments(command: str) -> list[list[str]]:
    try:
        toks = _tokens(command)
    except ValueError:
        return []
    out, cur = [], []
    for tok in toks:
        if tok in _SEPARATORS or set(tok) <= set("&|;()<>\n"):
            if cur:
                out.append(cur)
            cur = []
            continue
        cur.append(tok)
    if cur:
        out.append(cur)
    return out


def _flag_value(args: list[str], i: int, names: tuple[str, ...]) -> tuple[Optional[str], int]:
    """Value of ``args[i]`` when it is one of ``names`` (``--x v``, ``--x=v``, ``-xv`` forms)."""
    tok = args[i]
    for name in names:
        if tok == name:
            return (args[i + 1] if i + 1 < len(args) else ""), 2
        if name.startswith("--") and tok.startswith(name + "="):
            return tok[len(name) + 1:], 1
        if not name.startswith("--") and tok.startswith(name) and len(tok) > len(name):
            return tok[len(name):], 1
    return None, 0


def _read_file(path: str, cwd: str) -> BodyRef:
    where = f"body file {path}"
    if path == "-":
        return BodyRef(where, unreadable="The body comes from stdin, which cannot be checked. Write it "
                                         "to a file and pass --body-file <file>.")
    if _SHELL_EXPANSION.search(path):
        return BodyRef(where, unreadable="The body file path uses shell expansion. Pass a literal path.")
    p = Path(os.path.expanduser(path))
    if not p.is_absolute():
        p = Path(cwd or os.getcwd()) / p
    try:
        return BodyRef(f"body file {p}", text=p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return BodyRef(where, unreadable=f"The body file {p} does not exist yet. The check runs before the "
                                         "command, so a file written in the same command cannot be read. "
                                         "Write the body file in a separate step first, then run gh.")
    except OSError as exc:
        return BodyRef(where, unreadable=f"Cannot read the body file {p}: {exc.strerror or exc}.")


def _inline(value: str, where: str) -> BodyRef:
    if _SHELL_EXPANSION.search(value):
        return BodyRef(where, unreadable="The body is built with shell expansion ($(...), `...` or "
                                         "$VAR), which cannot be checked before it runs. Write it to a "
                                         "file and pass --body-file <file>.")
    return BodyRef(where, text=value)


def _gh_pr(args: list[str], cwd: str) -> list[BodyRef]:
    sub = args[0] if args else ""
    if sub not in ("create", "new", "edit"):
        return []
    rest = args[1:]
    found: list[BodyRef] = []
    filled = False
    i = 0
    while i < len(rest):
        val, step = _flag_value(rest, i, ("--body-file", "-F"))
        if step:
            found.append(_read_file(val or "", cwd))
            i += step
            continue
        val, step = _flag_value(rest, i, ("--body", "-b"))
        if step:
            found.append(_inline(val or "", "--body"))
            i += step
            continue
        if rest[i] in ("--fill", "-f", "--fill-first", "--fill-verbose"):
            filled = True
        if rest[i] in ("--web", "-w"):
            return []
        i += 1
    if not found and sub in ("create", "new"):
        how = "--fill copies commit messages into the body" if filled else "no body was given"
        found.append(BodyRef("gh pr create", unreadable=f"The PR body cannot be checked: {how}. Write "
                                                        "the body to a file and pass --body-file <file>."))
    return found


def _gh_api(args: list[str], cwd: str) -> list[BodyRef]:
    endpoint = next((a for a in args if not a.startswith("-") and "pulls" in a), "")
    if not _PULLS_PATH.search(endpoint.split("?")[0]):
        return []
    found: list[BodyRef] = []
    i = 0
    while i < len(args):
        val, step = _flag_value(args, i, ("--field", "-F", "--raw-field", "-f"))
        if step:
            key, _, value = (val or "").partition("=")
            if key == "body":
                from_file = args[i] in ("--field", "-F") or args[i].startswith(("--field=", "-F"))
                if from_file and value.startswith("@"):
                    found.append(_read_file(value[1:], cwd))
                else:
                    found.append(_inline(value, "gh api body field"))
            i += step
            continue
        val, step = _flag_value(args, i, ("--input",))
        if step:
            ref = _read_file(val or "", cwd)
            if ref.text is not None:
                try:
                    payload = json.loads(ref.text)
                except ValueError:
                    payload = None
                if isinstance(payload, dict) and isinstance(payload.get("body"), str):
                    found.append(BodyRef(ref.where, text=payload["body"]))
            elif ref.unreadable:
                found.append(ref)
            i += step
            continue
        i += 1
    return found


def bodies_in_command(command: str, cwd: str = "") -> list[BodyRef]:
    found: list[BodyRef] = []
    for seg in _segments(command):
        while seg and re.match(r"^\w+=", seg[0]):
            seg = seg[1:]
        if len(seg) >= 2 and seg[0] == "cd":
            target = os.path.expanduser(seg[1])
            cwd = target if os.path.isabs(target) else os.path.join(cwd or os.getcwd(), target)
            continue
        if not seg or os.path.basename(seg[0]) != "gh":
            continue
        args = seg[1:]
        while args and args[0] in ("-R", "--repo"):
            args = args[2:]
        if len(args) >= 2 and args[0] == "pr":
            found += _gh_pr(args[1:], cwd)
        elif args and args[0] == "api":
            found += _gh_api(args[1:], cwd)
    return found


def bodies_in_tool_call(tool_name: str, args: dict[str, Any], cwd: str = "") -> list[BodyRef]:
    if tool_name == "terminal":
        command = args.get("command")
        if not isinstance(command, str) or "gh" not in command:
            return []
        workdir = args.get("workdir")
        return bodies_in_command(command, workdir if isinstance(workdir, str) and workdir else cwd)
    if _PR_TOOL_NAME.search(tool_name) and isinstance(args.get("body"), str):
        return [BodyRef(f"{tool_name} body", text=args["body"])]
    return []
