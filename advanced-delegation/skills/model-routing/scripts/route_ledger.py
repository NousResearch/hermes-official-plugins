#!/usr/bin/env python3
"""Model-routing ledger: record subagent outcomes and suggest the cheapest route that reliably passes.

Stdlib only; runs on any Python 3.8+ on Linux, macOS and Windows. Routes are never hardcoded: they
are your advanced-delegation ``model_map`` routes, read with ``sync-routes`` and stored in config
order, which this skill treats as cheapest -> strongest.

  route_ledger.py sync-routes [--from FILE|-] [--hermes PATH]
  route_ledger.py ladder
  route_ledger.py classes
  route_ledger.py suggest --class code-feature --difficulty 3 [--risk high]
  route_ledger.py add --class code-feature --difficulty 3 --route standard --outcome pass \\
        --attempt 1 --task "form validation" --evidence "pytest: 42 passed" [--cause CAUSE]
  route_ledger.py stats [--class code-feature]

Files: the ledger (JSON Lines) is ADVANCED_DELEGATION_LEDGER if set, else
<HERMES_HOME>/plugin-data/advanced-delegation/routing-ledger.jsonl; the synced ladder is routes.json
beside it. sync-routes runs `hermes` for at most SYNC_BUDGET seconds in total.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional
from urllib.parse import urlsplit, urlunsplit

CLASSES = {
    "file-ops": "move, rename, copy or clean up files and folders",
    "data-reshape": "convert, reformat, split or merge tabular/JSON data with given rules",
    "data-extraction": "pull fields or issues from PDFs, documents or logs into a structured form",
    "docs-edit": "edit notes, READMEs and short documents",
    "summary": "summarize given material: digests, briefs, changelogs",
    "research": "web or codebase investigation that needs judgment and citations",
    "code-feature": "new feature slice in existing code, with tests",
    "code-bugfix": "diagnose and fix a defect",
    "code-refactor": "restructure code without behaviour change",
    "code-tests": "write or repair tests only",
    "data-migration": "change a live data shape or migrate database contents",
    "ui-frontend": "page layout, styling, client-side behaviour",
    "deliverable": "polished documents, decks, reports for other people",
    "config-ops": "tool or app configuration, build and release mechanics",
    "review": "code or plan review, audits, gap checks",
    "other": "fits none of the above (say what in --task)",
}
CAUSES = ["model", "spec", "context", "env", "flaky"]
WINDOW = 10        # recent records per (class, difficulty) that count
DEMOTE_STREAK = 3  # straight first-try passes before a trial one route down
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")  # same rule as advanced-delegation's store
LEDGER_ENV = "ADVANCED_DELEGATION_LEDGER"
SYNC_BUDGET = 200  # seconds for all `hermes` calls of one sync-routes; SKILL.md's terminal timeout is above it


class LadderError(SystemExit):
    """Exits with a one-line message when the route ladder is missing or unusable."""

    def __init__(self, message: str):
        super().__init__(f"error: {message}")


def _die(message: str, code: int = 2) -> None:
    """A clean one-line error instead of a traceback."""
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(code)


# --- locations --------------------------------------------------------------------------------

def default_hermes_home(platform: str, environ: Mapping[str, str], user_home) -> Path:
    """Hermes's platform default home (mirrors hermes_constants without importing it): Windows
    ``%LOCALAPPDATA%\\hermes``, elsewhere ``~/.hermes``, plus HERMES_DATA_DIR_SUFFIX. Pure: the caller
    passes the platform, environment and a ``user_home()`` callable."""
    suffix = environ.get("HERMES_DATA_DIR_SUFFIX", "")
    if platform == "win32":
        local = environ.get("LOCALAPPDATA", "").strip()
        base = Path(local) if local else user_home() / "AppData" / "Local"
        return base / ("hermes" + suffix)
    return user_home() / (".hermes" + suffix)


def hermes_home() -> Path:
    env = os.environ.get("HERMES_HOME", "").strip()
    if env:
        return Path(os.path.expanduser(os.path.expandvars(env)))
    return default_hermes_home(sys.platform, os.environ, Path.home)


def ledger_path() -> Path:
    env = os.environ.get(LEDGER_ENV, "").strip()
    if env:
        return Path(os.path.expanduser(os.path.expandvars(env)))
    return hermes_home() / "plugin-data" / "advanced-delegation" / "routing-ledger.jsonl"


def routes_path() -> Path:
    return ledger_path().with_name("routes.json")


# --- route ladder -----------------------------------------------------------------------------

def strip_credentials(where: str) -> str:
    """Drop ``user:pass@`` from a base URL so it is never stored; provider names pass through."""
    try:
        parts = urlsplit(where)
    except ValueError:
        return where
    if not parts.scheme or "@" not in parts.netloc:
        return where
    return urlunsplit(parts._replace(netloc=parts.netloc.rsplit("@", 1)[1]))


def fingerprint(where: str, model: str, effort: str, tier: str = "", daybreak: bool = False) -> str:
    """What a route runs on; a change here means old model-caused failures stop counting.
    ``provider:model@effort`` plus ``+tier`` (a service tier other than normal) and ``+daybreak``."""
    out = f"{where}:{model}@{effort or 'inherit'}"
    if tier and tier != "normal":
        out += f"+{tier}"
    return out + ("+daybreak" if daybreak else "")


def _route(name: str, description: str, where: str, model: str, effort: str, tier: str = "",
           daybreak: bool = False) -> Dict[str, Any]:
    where = strip_credentials(where)
    route: Dict[str, Any] = {"name": name, "description": description, "where": where, "model": model,
                             "reasoning_effort": effort}
    if tier and tier != "normal":
        route["service_tier"] = tier
    if daybreak:
        route["daybreak"] = True
    route["fingerprint"] = fingerprint(where, model, effort, tier, daybreak)
    return route


def _text(value: Any) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""


def _malformed(position: int, name: str, why: str) -> str:
    return f"skipped malformed route #{position} ({name or 'no name'}): {why}"


def parse_options_json(data: Any, warnings: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """Routes from ``hermes config get delegation.backends.model_map.options --json`` (a list of dicts).
    Malformed routes are skipped (core skips them too) and reported in *warnings*."""
    if not isinstance(data, list):
        raise ValueError("expected a JSON list of model_map routes")
    warnings = [] if warnings is None else warnings
    out = []
    for i, item in enumerate(data, 1):
        if not isinstance(item, dict):
            warnings.append(_malformed(i, "", "not a mapping"))
            continue
        name = _text(item.get("name"))
        where = _text(item.get("provider")) or _text(item.get("base_url"))
        model = _text(item.get("model"))
        if not _NAME_RE.match(name):
            warnings.append(_malformed(i, name, "invalid name"))
        elif not where or not model:
            warnings.append(_malformed(i, name, "needs a provider or base_url and a model"))
        else:
            out.append(_route(name, _text(item.get("description")), where, model,
                              _text(item.get("reasoning_effort")), _text(item.get("service_tier")),
                              item.get("daybreak") is True))
    return out


_SEP = re.compile(r"\s+[\u00b7\ufffd?]\s+")  # " · " (or its mangled forms on a non-UTF-8 console)


def parse_show(text: str, warnings: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """Routes from ``hermes delegation config --show`` (only lists them in Model routes mode).

    Layout (advanced-delegation ``store.describe``): a ``  Model routes (N)...:`` header, then per route
    a 4-space-indented ``name  description`` line and a deeper-indented
    ``provider-or-url · model [· reasoning X] [· tier] [· daybreak]  [(default)]`` line. Malformed
    routes are skipped and reported in *warnings*, as in ``parse_options_json``."""
    warnings = [] if warnings is None else warnings
    lines = text.replace("\r\n", "\n").split("\n")
    start = next((i for i, l in enumerate(lines) if re.match(r"^\s*Model routes \(\d+\)", l)), None)
    if start is None:
        mode = re.search(r"delegation\.backend:\s*([\w-]+)", text)
        raise ValueError(
            f"no model routes in the output (delegation mode: {mode.group(1) if mode else 'unknown'}); "
            "`--show` lists routes only in Model routes mode")
    expected = int(re.search(r"\((\d+)\)", lines[start]).group(1))
    out: List[Dict[str, Any]] = []
    seen = 0
    pending: Optional[tuple] = None
    for line in lines[start + 1:]:
        if not line.strip() or re.match(r"^ {0,3}\S", line):
            break  # back at the section's indent: the routes block ended
        head = re.match(r"^ {4}(\S(?:\S| (?! ))*)(?:\s{2,}(.*))?$", line)  # a name may hold single spaces
        if head:
            if head.group(1).startswith("("):  # "(none yet: ...)"
                break
            pending = (head.group(1), (head.group(2) or "").strip())
            continue
        if pending:
            summary = re.sub(r"\s{2,}\(default\)\s*$", "", line.strip())
            parts = _SEP.split(summary)
            extras = parts[2:]
            effort = next((p.split(None, 1)[1] for p in extras if p.startswith("reasoning ") and " " in p), "")
            tier = next((p for p in extras if not p.startswith("reasoning ") and p != "daybreak"), "")
            seen += 1
            if not _NAME_RE.match(pending[0]):
                warnings.append(_malformed(seen, pending[0], "invalid name"))
            elif len(parts) < 2 or not parts[0] or not parts[1]:
                warnings.append(_malformed(seen, pending[0], "needs a provider or base_url and a model"))
            else:
                out.append(_route(pending[0], pending[1], parts[0], parts[1], effort, tier, "daybreak" in extras))
            pending = None
    if seen != expected:
        raise ValueError(f"parsed {seen} of {expected} routes from `--show`; the output format may have changed")
    return out


def parse_any(text: str, warnings: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    stripped = text.strip()
    if stripped.startswith("["):
        return parse_options_json(json.loads(stripped), warnings)
    return parse_show(text, warnings)


def decode_saved(data: bytes) -> str:
    """Saved command output as text: UTF-16 with a BOM (PowerShell's ``>``), else UTF-8 with or without one."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    return data.decode("utf-8-sig")


def _run(cmd: List[str], timeout: float = SYNC_BUDGET) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "NO_COLOR": "1"}
    try:
        return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              stdin=subprocess.DEVNULL, env=env, timeout=max(1, timeout))
    except subprocess.TimeoutExpired:
        _die(f"`{' '.join(cmd[1:])}` did not finish within {SYNC_BUDGET}s in total; "
             "save its output and pass --from FILE")
    except OSError as exc:  # FileNotFoundError, PermissionError: a bad --hermes path
        _die(f"could not run {cmd[0]!r}: {exc.strerror or exc}; pass --hermes <path to the hermes executable>")


def read_from_hermes(hermes: str) -> tuple:
    """(routes, source, warnings). Prefers the JSON config read; falls back to parsing ``--show``.
    All calls together stay within SYNC_BUDGET seconds."""
    warnings: List[str] = []
    deadline = time.monotonic() + SYNC_BUDGET

    def run(*args: str) -> subprocess.CompletedProcess:
        return _run([hermes, *args], timeout=deadline - time.monotonic())

    got = run("config", "get", "delegation.backends.model_map.options", "--json")
    if got.returncode != 0 and "not set" in (got.stdout + got.stderr):
        return [], "hermes config get delegation.backends.model_map.options --json", warnings
    if got.returncode == 0:
        try:
            found = parse_options_json(json.loads(got.stdout.strip() or "null"), warnings)
            mode = run("config", "get", "delegation.backend").stdout.strip()
            if mode and mode != "model_map":
                warnings.append(f"delegation mode is {mode!r}, so delegate_task offers no routes yet; switch with "
                                "`hermes config set delegation.backend model_map` (or /delegation model_map)")
            return found, "hermes config get delegation.backends.model_map.options --json", warnings
        except ValueError:
            warnings.clear()
    shown = run("delegation", "config", "--show")
    if shown.returncode != 0:
        raise LadderError("could not read routes: `hermes delegation config --show` failed: "
                          + (shown.stderr.strip() or f"exit {shown.returncode}"))
    try:
        return parse_show(shown.stdout, warnings), "hermes delegation config --show", warnings
    except ValueError as exc:
        raise LadderError(f"{exc}. Switch to Model routes (`hermes config set delegation.backend model_map`) "
                          "or pass --from FILE")


def load_ladder(required: bool = True) -> List[Dict[str, str]]:
    path = routes_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        routes = [r for r in data.get("routes", []) if isinstance(r, dict) and r.get("name")]
    except FileNotFoundError:
        routes = []
    except (ValueError, AttributeError):
        raise LadderError(f"{path} is not a valid route ladder; run `route_ledger.py sync-routes` to rebuild it")
    if not routes and required:
        raise LadderError(f"no route ladder at {path}. Define model_map routes (cheapest first) with "
                          "`hermes delegation config`, then run `route_ledger.py sync-routes`")
    return routes


def cmd_sync_routes(a) -> None:
    if a.source:
        try:
            if a.source == "-":
                buf = getattr(sys.stdin, "buffer", None)
                text = decode_saved(buf.read()) if buf is not None else sys.stdin.read()
            else:
                text = decode_saved(Path(a.source).read_bytes())
        except OSError as exc:
            raise LadderError(f"could not read {a.source}: {exc.strerror or exc}")
        except UnicodeDecodeError:
            raise LadderError(f"{a.source} is neither UTF-8 nor UTF-16 with a BOM; save the output as UTF-8")
        warnings: List[str] = []
        try:
            routes = parse_any(text, warnings)
        except ValueError as exc:
            raise LadderError(str(exc))
        source = f"--from {a.source}"
    else:
        hermes = a.hermes or shutil.which("hermes")
        if not hermes:
            raise LadderError("`hermes` is not on PATH; pass --hermes <path to the hermes executable> or --from FILE")
        routes, source, warnings = read_from_hermes(hermes)
    if not routes:
        raise LadderError("no model_map routes are configured; add them (cheapest first) with "
                          "`hermes delegation config`, then sync again")
    path = routes_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"synced_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
               "source": source, "routes": routes}
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print_ladder(routes)
    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)
    print(f"synced {len(routes)} routes (cheapest -> strongest, config order) -> {path}")


# --- rubric -----------------------------------------------------------------------------------

def difficulty_index(difficulty: int, n: int) -> int:
    """Difficulty 1-5 spread evenly over n routes: position (d-1)*(n-1)/4, an exact half rounds DOWN
    (toward the cheaper route). n=5 -> 0,1,2,3,4; n=3 -> 0,0,1,1,2; n=7 -> 0,1,3,4,6."""
    if n <= 1:
        return 0
    q, r = divmod((difficulty - 1) * (n - 1), 4)
    return q + (1 if r > 2 else 0)


def middle_index(n: int) -> int:
    """The high-risk floor: the middle route (the lower of the two middles on an even ladder)."""
    return (n - 1) // 2


def baseline(difficulty: int, risk: str, n: int) -> int:
    """High risk never runs below the middle route and moves one route up from the middle upward."""
    idx = difficulty_index(difficulty, n)
    if risk == "high":
        mid = middle_index(n)
        idx = mid if idx < mid else min(idx + 1, n - 1)
    return idx


def mapping_table(n: int) -> Dict[str, List[int]]:
    return {risk: [baseline(d, risk, n) for d in range(1, 6)] for risk in ("low", "high")}


# --- ledger -----------------------------------------------------------------------------------

def _attempt(row: dict) -> int:
    """The row's attempt number; ValueError/TypeError when it is not a whole number >= 1."""
    value = row.get("attempt", 1)
    if isinstance(value, bool) or (isinstance(value, float) and not value.is_integer()):
        raise ValueError(value)
    number = int(value)
    if number < 1:
        raise ValueError(value)
    return number


def load() -> List[dict]:
    """Ledger rows; malformed lines (bad JSON, not an object, bad ``attempt``) are skipped with a warning."""
    path = ledger_path()
    if not path.exists():
        return []
    rows = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError("not an object")
            row["attempt"] = _attempt(row)
        except (ValueError, TypeError):  # json.JSONDecodeError is a ValueError
            print(f"warning: skipping malformed ledger line {n}", file=sys.stderr)
            continue
        rows.append(row)
    return rows


def suggest(rows: List[dict], ladder: List[Dict[str, str]], cls: str, difficulty: int, risk: str) -> dict:
    names = [r["name"] for r in ladder]
    current = {r["name"]: r.get("fingerprint", "") for r in ladder}
    top = len(names) - 1
    rec = base = baseline(difficulty, risk, len(names))
    recent = [r for r in rows if r.get("class") == cls and r.get("difficulty") == difficulty][-WINDOW:]
    reasons = [f"rubric baseline for difficulty {difficulty}, risk {risk} on a {len(names)}-route ladder: {names[base]}"]

    # A model-caused failure counts only while its route still runs the model it failed on.
    model_fails = [names.index(r["route"]) for r in recent
                   if r.get("outcome") == "fail" and r.get("cause") == "model" and r.get("route") in names
                   and (not r.get("model") or r.get("model") == current.get(r["route"]))]
    if model_fails:
        floor = min(max(model_fails) + 1, top)
        if floor > rec:
            rec = floor
            reasons.append(f"model-caused failure on {names[max(model_fails)]} in the last {WINDOW} similar tasks "
                           f"-> promote to {names[rec]}")

    trial = False
    if risk == "low" and rec > 0:
        streak = [r for r in recent if r.get("route") == names[rec]][-DEMOTE_STREAK:]
        clean = len(streak) == DEMOTE_STREAK and all(
            r.get("outcome") == "pass" and r.get("attempt", 1) == 1 for r in streak)
        if clean and not any(i <= rec - 1 for i in model_fails):
            rec -= 1
            trial = True
            reasons.append(f"{DEMOTE_STREAK} straight first-try passes on {names[rec + 1]} and no model failure "
                           f"below it -> trial one route down")

    sample = len(recent)
    return {
        "route": names[rec],
        "trial_demotion": trial,
        "similar_records": sample,
        "confidence": "rubric-only" if sample == 0 else ("low" if sample < 3 else "evidence"),
        "reasons": reasons,
        "escalate_on_model_failure_to": names[min(rec + 1, top)] if rec < top else None,
    }


def cmd_add(a, ladder: List[Dict[str, str]]) -> None:
    by_name = {r["name"]: r for r in ladder}
    if a.route not in by_name:
        raise LadderError(f"unknown route {a.route!r}; synced routes are: {', '.join(by_name)} "
                          "(run `route_ledger.py sync-routes` if you changed them)")
    if a.outcome == "fail" and not a.cause:
        raise SystemExit("error: --cause is required for a failed outcome: " + ", ".join(CAUSES))
    if a.attempt < 1:
        raise SystemExit("error: --attempt starts at 1")
    row = {
        "ts": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "class": a.cls, "difficulty": a.difficulty, "risk": a.risk, "route": a.route,
        "model": by_name[a.route].get("fingerprint", ""),
        "outcome": a.outcome, "attempt": a.attempt, "cause": a.cause or "",
        "task": a.task, "project": a.project, "evidence": a.evidence,
    }
    path = ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"recorded -> {path}")


def cmd_stats(a) -> None:
    rows = load()
    if a.cls:
        rows = [r for r in rows if r.get("class") == a.cls]
    order = {r["name"]: i for i, r in enumerate(load_ladder(required=False))}
    table = defaultdict(lambda: {"pass1": 0, "pass_later": 0, "fail_model": 0, "fail_other": 0})
    for r in rows:
        cell = table[(str(r.get("class")), r.get("difficulty") or 0, str(r.get("route")))]
        if r.get("outcome") == "pass":
            cell["pass1" if r.get("attempt", 1) == 1 else "pass_later"] += 1
        else:
            cell["fail_model" if r.get("cause") == "model" else "fail_other"] += 1
    if not table:
        print(f"no records ({ledger_path()})")
        return
    print(f"{'class':16} {'diff':>4} {'route':12} {'pass1':>5} {'passN':>5} {'failM':>5} {'failO':>5}")
    for (cls, diff, route), c in sorted(table.items(), key=lambda kv: (kv[0][0], kv[0][1], order.get(kv[0][2], 99))):
        print(f"{cls:16} {diff!s:>4} {route:12} {c['pass1']:>5} {c['pass_later']:>5} "
              f"{c['fail_model']:>5} {c['fail_other']:>5}")


def print_ladder(routes: List[Dict[str, str]]) -> None:
    width = max(len(r["name"]) for r in routes)
    for i, r in enumerate(routes):
        print(f"{i + 1}. {r['name']:{width}}  {r.get('fingerprint', '')}  {r.get('description', '')}".rstrip())
    table = mapping_table(len(routes))
    for risk in ("low", "high"):
        cells = ", ".join(f"d{d}={routes[i]['name']}" for d, i in enumerate(table[risk], 1))
        print(f"baseline, {risk} risk: {cells}")


def main(argv: Optional[List[str]] = None) -> None:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def task_args(sp, *, route: bool) -> None:
        sp.add_argument("--class", dest="cls", required=True, choices=sorted(CLASSES))
        sp.add_argument("--difficulty", type=int, required=True, choices=range(1, 6))
        sp.add_argument("--risk", choices=["low", "high"], default="low")
        if route:
            sp.add_argument("--route", required=True, help="a synced route name (see `ladder`)")

    task_args(sub.add_parser("suggest", help="cheapest route likely to pass this task"), route=False)
    ad = sub.add_parser("add", help="record one subagent outcome (passes too)")
    task_args(ad, route=True)
    ad.add_argument("--outcome", required=True, choices=["pass", "fail"])
    ad.add_argument("--attempt", type=int, default=1)
    ad.add_argument("--cause", choices=CAUSES)
    ad.add_argument("--task", required=True)
    ad.add_argument("--project", default="")
    ad.add_argument("--evidence", required=True, help="real verification, e.g. test command + result")
    st = sub.add_parser("stats", help="pass/fail counts per class, difficulty and route")
    st.add_argument("--class", dest="cls", choices=sorted(CLASSES))
    sub.add_parser("classes", help="list task classes")
    sub.add_parser("ladder", help="print the synced routes and the difficulty mapping")
    sy = sub.add_parser("sync-routes", help="store your model_map routes (config order) as the ladder")
    sy.add_argument("--from", dest="source", help="read saved `config get --json` or `--show` output (- = stdin); UTF-8, or UTF-16 with a BOM")
    sy.add_argument("--hermes", help="path to the hermes executable (default: from PATH)")

    a = p.parse_args(argv)
    if a.cmd == "suggest":
        print(json.dumps(suggest(load(), load_ladder(), a.cls, a.difficulty, a.risk), indent=2, ensure_ascii=False))
    elif a.cmd == "add":
        cmd_add(a, load_ladder())
    elif a.cmd == "stats":
        cmd_stats(a)
    elif a.cmd == "sync-routes":
        cmd_sync_routes(a)
    elif a.cmd == "ladder":
        print_ladder(load_ladder())
    else:
        for k in sorted(CLASSES):
            print(f"{k:16} {CLASSES[k]}")


if __name__ == "__main__":
    main()
