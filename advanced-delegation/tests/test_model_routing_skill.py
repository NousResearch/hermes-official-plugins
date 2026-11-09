"""The bundled ``model-routing`` skill: its SKILL.md meets the skill standards, its stdlib ledger script
routes over any ladder of model_map routes, and ``register()`` ships it only where the delegation modes
register. Stdlib + pytest only; ``hermes`` is never run here (its output is faked)."""

from __future__ import annotations

import importlib.util
import json
import logging
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import advanced_delegation_plugin

_ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = _ROOT / "skills" / "model-routing"
SKILL_MD = SKILL_DIR / "SKILL.md"
SCRIPT = SKILL_DIR / "scripts" / "route_ledger.py"


def _text(path: Path) -> str:
    # A Windows checkout may carry CRLF; the content is what is under test.
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


def _split_frontmatter(content: str):
    assert content.startswith("---\n"), "frontmatter must start at byte 0"
    end = content.index("\n---\n", 4)
    return content[4:end], content[end + 5:]


@pytest.fixture(scope="module")
def rl():
    spec = importlib.util.spec_from_file_location("route_ledger_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    path = tmp_path / "data" / "routing-ledger.jsonl"
    monkeypatch.setenv("ADVANCED_DELEGATION_LEDGER", str(path))
    return path


def _ladder(rl, names, models=None):
    models = models or {}
    return [rl._route(n, f"{n} route", "prov", models.get(n, f"{n}-model"), "") for n in names]


def _save_ladder(rl, names, models=None):
    path = rl.routes_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"routes": _ladder(rl, names, models)}), encoding="utf-8")


def _row(route, outcome="pass", cause="", attempt=1, model="", cls="code-feature", difficulty=3):
    return {"class": cls, "difficulty": difficulty, "route": route, "outcome": outcome, "cause": cause,
            "attempt": attempt, "model": model}


def _cli(*args, env=None):
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True,
                          encoding="utf-8", env=env, timeout=60, stdin=subprocess.DEVNULL)


# --- SKILL.md -----------------------------------------------------------------------------------

def test_frontmatter_meets_the_skill_standards():
    import hermes_yaml as yaml
    fm_text, body = _split_frontmatter(_text(SKILL_MD))
    fm = yaml.safe_load(fm_text)
    assert fm["name"] == "model-routing"
    desc = fm["description"]
    assert len(desc) <= 60 and desc.endswith(".") and desc.count(".") == 1
    assert desc == advanced_delegation_plugin.SKILL_DESCRIPTION
    for word in ("powerful", "comprehensive", "seamless", "advanced"):
        assert word not in desc.lower()
    assert fm["version"] and fm["author"]
    author = fm["author"].lower()
    assert "claude code" not in author and "hermes agent" not in author, "the author line carries no tool co-credit"
    assert fm["license"] == "MIT"
    assert fm["platforms"] == ["linux", "macos", "windows"]
    assert fm["metadata"]["hermes"]["tags"]
    assert "related_skills" not in fm["metadata"]["hermes"]
    assert body.strip()


def test_body_sections_in_order_and_size():
    _, body = _split_frontmatter(_text(SKILL_MD))
    headings = re.findall(r"^(#{1,2} .+)$", body, re.M)
    assert headings[0] == "# Model Routing Skill"
    required = ["## When to Use", "## Prerequisites", "## How to Run", "## Quick Reference", "## Procedure",
                "## Pitfalls", "## Verification"]
    positions = [headings.index(h) for h in required]
    assert positions == sorted(positions)
    assert "Don't use for" in body
    procedure = body.split("## Procedure", 1)[1].split("\n## ", 1)[0]
    steps = re.findall(r"^\d+\. ", procedure, re.M)
    assert len(steps) >= 5
    assert " ".join(procedure.split()).count("Done when") == len(steps), "every step needs a checkable completion criterion"
    for tool in ("`delegate_task`", "`terminal`", "`read_file`", "`search_files`"):
        assert tool in body
    for util in ("grep", "cat", "sed", "awk", "find"):
        assert not re.search(rf"`{util}\b", body), util


def test_every_referenced_file_ships():
    content = _text(SKILL_MD)
    for rel in set(re.findall(r"`((?:scripts|references)/[\w./-]+)`", content)):
        assert (SKILL_DIR / rel).is_file(), rel


def test_script_calls_use_the_quoted_skill_dir_token():
    """Core substitutes ${HERMES_SKILL_DIR} in plugin SKILL.md too; quoting keeps paths with spaces whole."""
    from agent.skill_preprocessing import substitute_template_vars
    content = _text(SKILL_MD)
    calls = re.findall(r"python (\S+route_ledger\.py\S*)", content)
    assert calls and all(c == '"${HERMES_SKILL_DIR}/scripts/route_ledger.py"' for c in calls), calls
    rendered = substitute_template_vars(content, SKILL_DIR, None)
    assert f'"{SKILL_DIR}/scripts/route_ledger.py"' in rendered and "${HERMES_SKILL_DIR}" not in rendered


_SHIPPED = [SKILL_MD, SCRIPT, SKILL_DIR / "references" / "workflow.md", _ROOT / "README.md",
            _ROOT / "__init__.py", _ROOT / "plugin.yaml"]
# The skill names no real models (the plugin README's pre-existing config example does, on purpose).
_MODEL_NAMES = ["Opus", "Haiku", "Son" + "net", "claude", "gpt", "gemini"]
_SKILL_FILES = {SKILL_MD, SCRIPT, SKILL_DIR / "references" / "workflow.md"}


@pytest.mark.parametrize("path", _SHIPPED, ids=lambda p: p.name)
def test_no_real_model_names_or_machine_local_paths(path):
    for n, line in enumerate(_text(path).splitlines(), 1):
        for word in (_MODEL_NAMES if path in _SKILL_FILES else []):
            assert not re.search(rf"\b{re.escape(word)}\b", line, re.I), f"{path.name}:{n}: {word!r}"
        assert not re.search(r"\b[A-Za-z]:[\\/]\w|/home/\w|/Users/\w|AppData[\\/]", line), f"{path.name}:{n}"


def test_script_is_portable_stdlib():
    src = _text(SCRIPT)
    tmp_dir = "/" + "tmp"
    for bad in ("fcntl", "termios", "import pty", "os.fork", "os.killpg", "signal.SIG", tmp_dir, "/proc",
                "/etc/", "yaml"):
        assert bad not in src, bad
    for line in src.splitlines():
        if re.search(r"\.(?:read_text|write_text|open)\(", line):
            assert "encoding=" in line, line
    imports = {m.split(".")[0] for m in re.findall(r"^\s*(?:from|import) ([\w.]+)", src, re.M)}
    stdlib = getattr(sys, "stdlib_module_names", None)
    if stdlib:
        assert imports - {"__future__"} <= set(stdlib), imports - set(stdlib)
    assert "hermes_constants" not in imports and "hermes_cli" not in imports


# --- locations ----------------------------------------------------------------------------------

def test_ledger_lives_in_the_plugin_data_folder(rl, tmp_path, monkeypatch):
    monkeypatch.delenv("ADVANCED_DELEGATION_LEDGER", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    data = tmp_path / "profile" / "plugin-data" / "advanced-delegation"
    assert rl.ledger_path() == data / "routing-ledger.jsonl"
    assert rl.routes_path() == data / "routes.json"
    monkeypatch.setenv("ADVANCED_DELEGATION_LEDGER", str(tmp_path / "x" / "l.jsonl"))
    assert rl.routes_path() == tmp_path / "x" / "routes.json"


def test_default_home_mirrors_hermes(rl, tmp_path):
    """Pure: platform, environment and home are arguments, so nothing process-wide is patched."""
    home = lambda: tmp_path  # noqa: E731
    assert rl.default_hermes_home("linux", {}, home) == tmp_path / ".hermes"
    assert rl.default_hermes_home("darwin", {"HERMES_DATA_DIR_SUFFIX": "-dev"}, home) == tmp_path / ".hermes-dev"
    assert rl.default_hermes_home("win32", {"LOCALAPPDATA": str(tmp_path / "Local")}, home) == \
        tmp_path / "Local" / "hermes"
    assert rl.default_hermes_home("win32", {}, home) == tmp_path / "AppData" / "Local" / "hermes"
    try:
        from hermes_constants import _platform_default_hermes_home
    except ImportError:
        return
    for env in ({}, {"LOCALAPPDATA": str(tmp_path / "L")}, {"HERMES_DATA_DIR_SUFFIX": "-x"}):
        assert rl.default_hermes_home(sys.platform, env, home) == _platform_default_hermes_home(env, home)


# --- difficulty mapping -------------------------------------------------------------------------

@pytest.mark.parametrize("n, low, high", [
    (1, [0, 0, 0, 0, 0], [0, 0, 0, 0, 0]),
    (2, [0, 0, 0, 1, 1], [1, 1, 1, 1, 1]),
    (3, [0, 0, 1, 1, 2], [1, 1, 2, 2, 2]),
    (4, [0, 1, 1, 2, 3], [1, 2, 2, 3, 3]),
    (5, [0, 1, 2, 3, 4], [2, 2, 3, 4, 4]),
    (7, [0, 1, 3, 4, 6], [3, 3, 4, 5, 6]),
])
def test_difficulty_spreads_over_any_ladder_and_high_risk_floors_at_the_middle(rl, n, low, high):
    assert rl.mapping_table(n) == {"low": low, "high": high}


@pytest.mark.parametrize("names, difficulty, expected", [
    (["fast", "standard", "deep"], 1, "fast"),
    (["fast", "standard", "deep"], 3, "standard"),
    (["fast", "standard", "deep"], 5, "deep"),
    (["r1", "r2", "r3", "r4", "r5"], 4, "r4"),
    (["a", "b", "c", "d", "e", "f", "g"], 3, "d"),
])
def test_suggest_without_history_uses_the_rubric(rl, names, difficulty, expected):
    out = rl.suggest([], _ladder(rl, names), "code-feature", difficulty, "low")
    assert out["route"] == expected and out["confidence"] == "rubric-only"


def test_risk_high_floors_at_the_middle_and_adds_one(rl):
    ladder = _ladder(rl, ["fast", "standard", "deep"])
    assert rl.suggest([], ladder, "code-feature", 1, "high")["route"] == "standard"
    assert rl.suggest([], ladder, "code-feature", 3, "high")["route"] == "deep"
    assert rl.suggest([], ladder, "code-feature", 5, "high")["escalate_on_model_failure_to"] is None


# --- learning rules -----------------------------------------------------------------------------

def test_model_failure_promotes_one_route(rl):
    ladder = _ladder(rl, ["r1", "r2", "r3", "r4", "r5"])
    out = rl.suggest([_row("r3", "fail", "model", model="prov:r3-model@inherit")], ladder, "code-feature", 3, "low")
    assert out["route"] == "r4" and out["escalate_on_model_failure_to"] == "r5"
    assert any("promote" in r for r in out["reasons"])


def test_spec_failure_does_not_promote(rl):
    ladder = _ladder(rl, ["r1", "r2", "r3", "r4", "r5"])
    assert rl.suggest([_row("r3", "fail", "spec")], ladder, "code-feature", 3, "low")["route"] == "r3"


def test_trial_demotion_after_three_first_try_passes(rl):
    ladder = _ladder(rl, ["fast", "standard", "deep"])
    rows = [_row("standard")] * 3
    out = rl.suggest(rows, ladder, "code-feature", 3, "low")
    assert out["route"] == "fast" and out["trial_demotion"] is True
    assert rl.suggest(rows[:2], ladder, "code-feature", 3, "low")["route"] == "standard"
    assert rl.suggest([_row("standard", attempt=2)] + rows[:2], ladder, "code-feature", 3, "low")["route"] == "standard"
    assert rl.suggest(rows, ladder, "code-feature", 3, "high")["trial_demotion"] is False


def test_failed_trial_restores_the_floor(rl):
    ladder = _ladder(rl, ["fast", "standard", "deep"])
    rows = [_row("standard")] * 3 + [_row("fast", "fail", "model", model="prov:fast-model@inherit")]
    out = rl.suggest(rows, ladder, "code-feature", 3, "low")
    assert out["route"] == "standard" and out["trial_demotion"] is False


def test_failures_stop_counting_after_the_route_changes_model(rl):
    fail = _row("fast", "fail", "model", model="prov:old-model@inherit", difficulty=1)
    same = _ladder(rl, ["fast", "standard", "deep"], {"fast": "old-model"})
    moved = _ladder(rl, ["fast", "standard", "deep"], {"fast": "new-model"})
    assert rl.suggest([fail], same, "code-feature", 1, "low")["route"] == "standard"
    assert rl.suggest([fail], moved, "code-feature", 1, "low")["route"] == "fast"


def test_records_for_removed_routes_are_ignored(rl):
    ladder = _ladder(rl, ["fast", "deep"])
    rows = [_row("gone", "fail", "model", difficulty=1)]
    assert rl.suggest(rows, ladder, "code-feature", 1, "low")["route"] == "fast"


# --- CLI ----------------------------------------------------------------------------------------

def test_missing_ladder_tells_the_user_to_sync(ledger):
    for args in (["suggest", "--class", "code-feature", "--difficulty", "2"],
                 ["add", "--class", "summary", "--difficulty", "1", "--route", "fast", "--outcome", "pass",
                  "--task", "t", "--evidence", "e"]):
        done = _cli(*args)
        assert done.returncode != 0 and "sync-routes" in done.stderr and "hermes delegation config" in done.stderr


def test_add_validates_route_and_records_the_model(rl, ledger):
    _save_ladder(rl, ["fast", "standard", "deep"])
    bad = _cli("add", "--class", "summary", "--difficulty", "1", "--route", "medium", "--outcome", "pass",
               "--task", "t", "--evidence", "e")
    assert bad.returncode != 0 and "fast, standard, deep" in bad.stderr
    no_cause = _cli("add", "--class", "summary", "--difficulty", "1", "--route", "fast", "--outcome", "fail",
                    "--task", "t", "--evidence", "e")
    assert no_cause.returncode != 0 and "--cause" in no_cause.stderr
    ok = _cli("add", "--class", "summary", "--difficulty", "1", "--route", "fast", "--outcome", "pass",
              "--task", "résumé digest", "--evidence", "pytest: 3 passed")
    assert ok.returncode == 0, ok.stderr
    (row,) = [json.loads(l) for l in ledger.read_text(encoding="utf-8").splitlines()]
    assert row["route"] == "fast" and row["model"] == "prov:fast-model@inherit" and row["task"] == "résumé digest"
    stats = _cli("stats")
    assert stats.returncode == 0 and "summary" in stats.stdout
    assert "code-feature" in _cli("classes").stdout


# --- sync-routes --------------------------------------------------------------------------------

SHOW_SAMPLE = """\
Delegation mode: Model routes (delegation.backend: model_map)
  Model routes (3), default: standard:
    fast      Cheap and quick: lookups, summaries.
              provider-a \u00b7 small-model
    standard  Default for specified feature work.
              provider-b \u00b7 mid-model \u00b7 reasoning medium  (default)
    deep      Hard debugging, design, final review.
              http://localhost:8080/v1 \u00b7 big-model \u00b7 reasoning xhigh \u00b7 fast
  Tasks that pick no route run on the mode's default, else the parent's model; delegation.model / \
delegation.provider are inactive in this mode.
"""

OPTIONS = [
    {"name": "fast", "description": "Cheap and quick.", "provider": "provider-a", "model": "small-model"},
    {"name": "standard", "description": "Default.", "provider": "provider-b", "model": "mid-model",
     "reasoning_effort": "medium", "api_key": "***"},
    {"name": "deep", "description": "Hard work.", "base_url": "http://localhost:8080/v1", "model": "big-model",
     "reasoning_effort": "xhigh", "service_tier": "fast"},
]
EXPECTED = ["provider-a:small-model@inherit", "provider-b:mid-model@medium",
            "http://localhost:8080/v1:big-model@xhigh+fast"]


def test_parse_show_sample(rl):
    routes = rl.parse_show(SHOW_SAMPLE)
    assert [r["name"] for r in routes] == ["fast", "standard", "deep"]
    assert [r["fingerprint"] for r in routes] == EXPECTED
    assert routes[0]["description"] == "Cheap and quick: lookups, summaries."
    assert rl.parse_show(SHOW_SAMPLE.replace("\n", "\r\n").replace("\u00b7", "?"))[2]["fingerprint"] == EXPECTED[2]


def test_parse_show_matches_the_plugins_own_formatter(rl):
    """Keeps the parser in step with ``store.describe``, the code that prints ``--show``."""
    from advanced_delegation_plugin import store
    section = {"backend": "model_map", "backends": {"model_map": {"options": OPTIONS, "default": "standard"}}}
    assert [r["fingerprint"] for r in rl.parse_show(store.describe(section))] == EXPECTED
    with pytest.raises(ValueError, match="Model routes mode"):
        rl.parse_show(store.describe({"backend": "default"}))


def test_parse_options_json_keeps_config_order_and_drops_secrets(rl):
    warnings = []
    routes = rl.parse_options_json(OPTIONS + [{"name": "bad name", "model": "m", "provider": "p"},
                                              {"name": "nomodel", "provider": "p"}, "junk"], warnings)
    assert [r["fingerprint"] for r in routes] == EXPECTED
    assert "api_key" not in json.dumps(routes) and "***" not in json.dumps(routes)
    assert len(warnings) == 3 and "#4 (bad name)" in warnings[0] and "#5 (nomodel)" in warnings[1]


def test_fingerprint_covers_tier_and_daybreak_and_strips_url_credentials(rl):
    (route,) = rl.parse_options_json([{"name": "x", "base_url": "https://user:s3cret@api.example.test/v1",
                                       "model": "m", "service_tier": "priority", "daybreak": True}])
    assert route["fingerprint"] == "https://api.example.test/v1:m@inherit+priority+daybreak"
    assert "s3cret" not in json.dumps(route) and "user" not in json.dumps(route)
    (normal,) = rl.parse_options_json([{"name": "y", "provider": "p", "model": "m", "service_tier": "normal"}])
    assert normal["fingerprint"] == "p:m@inherit"
    assert rl.strip_credentials("provider-a") == "provider-a"


def test_parse_show_reports_malformed_routes_like_json(rl):
    from advanced_delegation_plugin import store
    options = OPTIONS + [{"name": "bad name", "description": "x", "provider": "p", "model": "m"},
                         {"name": "db", "description": "y", "provider": "p", "model": "m", "daybreak": True}]
    section = {"backend": "model_map", "backends": {"model_map": {"options": options, "default": "standard"}}}
    warnings = []
    routes = rl.parse_show(store.describe(section), warnings)
    assert [r["name"] for r in routes] == ["fast", "standard", "deep", "db"]
    assert routes[-1]["fingerprint"] == "p:m@inherit+daybreak"
    assert warnings == ["skipped malformed route #4 (bad name): invalid name"]


def test_sync_prefers_config_get_and_warns_outside_model_routes(rl, ledger, monkeypatch, capsys):
    calls = []

    def fake_run(cmd, timeout=None):
        assert 0 < timeout <= rl.SYNC_BUDGET
        calls.append(cmd[1:])
        out = json.dumps(OPTIONS) if cmd[1:3] == ["config", "get"] and cmd[3].endswith("options") else "default"
        return SimpleNamespace(returncode=0, stdout=out + "\n", stderr="")

    monkeypatch.setattr(rl, "_run", fake_run)
    rl.cmd_sync_routes(SimpleNamespace(source=None, hermes="hermes"))
    saved = json.loads(rl.routes_path().read_text(encoding="utf-8"))
    assert [r["fingerprint"] for r in saved["routes"]] == EXPECTED
    assert ["delegation", "config", "--show"] not in calls
    assert "delegation mode is 'default'" in capsys.readouterr().err


def test_sync_falls_back_to_show_on_an_older_core(rl, ledger, monkeypatch):
    def fake_run(cmd, timeout=None):
        if cmd[1] == "config":
            return SimpleNamespace(returncode=2, stdout="", stderr="invalid choice: 'get'")
        return SimpleNamespace(returncode=0, stdout=SHOW_SAMPLE, stderr="")

    monkeypatch.setattr(rl, "_run", fake_run)
    rl.cmd_sync_routes(SimpleNamespace(source=None, hermes="hermes"))
    assert [r["name"] for r in rl.load_ladder()] == ["fast", "standard", "deep"]


def test_sync_with_no_routes_configured_says_so(rl, ledger, monkeypatch):
    monkeypatch.setattr(rl, "_run", lambda cmd, timeout=None: SimpleNamespace(
        returncode=1, stdout="Config key not set: delegation.backends.model_map.options\n", stderr=""))
    with pytest.raises(SystemExit, match="no model_map routes are configured"):
        rl.cmd_sync_routes(SimpleNamespace(source=None, hermes="hermes"))
    assert not rl.routes_path().exists()


def test_hermes_that_is_missing_or_hangs_exits_2_cleanly(rl, ledger, monkeypatch, capsys):
    with pytest.raises(SystemExit) as missing:
        rl._run([str(ledger.parent / "no-such-hermes.exe"), "config", "get", "x"], timeout=5)
    assert missing.value.code == 2 and "--hermes" in capsys.readouterr().err

    def hang(*a, **k):
        raise subprocess.TimeoutExpired(a[0], k.get("timeout"))

    monkeypatch.setattr(rl.subprocess, "run", hang)
    with pytest.raises(SystemExit) as hung:
        rl.read_from_hermes("hermes")
    err = capsys.readouterr().err
    assert hung.value.code == 2 and err.startswith("error: ") and "--from FILE" in err and "Traceback" not in err


def test_cli_reports_a_bad_hermes_path_without_a_traceback(tmp_path, ledger):
    done = _cli("sync-routes", "--hermes", str(tmp_path / "missing" / "hermes"))
    assert done.returncode == 2 and done.stderr.startswith("error: could not run") and "Traceback" not in done.stderr


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "utf-16"])
def test_sync_from_file_decodes_utf8_and_utf16_boms(tmp_path, ledger, encoding):
    sample = tmp_path / "show.txt"
    sample.write_bytes(SHOW_SAMPLE.encode(encoding))
    done = _cli("sync-routes", "--from", str(sample))
    assert done.returncode == 0, done.stderr
    assert "1. fast" in done.stdout


def test_sync_from_stdin(tmp_path, ledger):
    done = subprocess.run([sys.executable, str(SCRIPT), "sync-routes", "--from", "-"], capture_output=True,
                          input=json.dumps(OPTIONS).encode("utf-8"), timeout=60)
    assert done.returncode == 0, done.stderr
    assert b"synced 3 routes" in done.stdout


def test_malformed_ledger_rows_are_skipped_with_a_warning(rl, ledger, capsys):
    good = _row("fast")
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text("\n".join([json.dumps(good), "{not json", json.dumps([1]),
                                  json.dumps(_row("fast", attempt="two")), json.dumps(_row("fast", attempt=0)),
                                  json.dumps(_row("fast", attempt="2"))]) + "\n", encoding="utf-8")
    rows = rl.load()
    assert [r["attempt"] for r in rows] == [1, 2]
    assert capsys.readouterr().err.count("skipping malformed ledger line") == 4
    _save_ladder(rl, ["fast", "deep"])
    rl.cmd_stats(SimpleNamespace(cls=None))
    assert "code-feature" in capsys.readouterr().out


def test_sync_from_saved_output_end_to_end(tmp_path, ledger):
    sample = tmp_path / "show.txt"
    sample.write_text(SHOW_SAMPLE, encoding="utf-8")
    done = _cli("sync-routes", "--from", str(sample))
    assert done.returncode == 0, done.stderr
    assert "1. fast" in done.stdout and "d5=deep" in done.stdout
    saved = tmp_path / "options.json"
    saved.write_text(json.dumps(OPTIONS[:2]), encoding="utf-8")
    assert _cli("sync-routes", "--from", str(saved)).returncode == 0
    out = json.loads(_cli("suggest", "--class", "code-bugfix", "--difficulty", "5").stdout)
    assert out["route"] == "standard"


# --- registration -------------------------------------------------------------------------------

class _RecordingCtx:
    def __init__(self):
        self.skills = []

    def register_skill(self, **kwargs):
        self.skills.append(kwargs)

    def __getattr__(self, name):
        return lambda *a, **k: None


def test_register_ships_the_skill_with_the_providers():
    pytest.importorskip("agent.delegation_provider", reason="Hermes core without pluggable delegation providers")
    ctx = _RecordingCtx()
    advanced_delegation_plugin.register(ctx)
    (skill,) = ctx.skills
    assert skill["name"] == "model-routing"
    assert Path(skill["path"]) == SKILL_MD and Path(skill["path"]).is_file()
    assert skill["description"] == advanced_delegation_plugin.SKILL_DESCRIPTION


def test_a_failing_skill_registration_keeps_the_delegation_modes(caplog):
    pytest.importorskip("agent.delegation_provider", reason="Hermes core without pluggable delegation providers")

    class _FailingSkillCtx(_RecordingCtx):
        def __init__(self):
            super().__init__()
            self.providers = []

        def register_delegation_provider(self, provider):
            self.providers.append(provider)

        def register_skill(self, **kwargs):
            raise RuntimeError("skill registry unavailable")

    ctx = _FailingSkillCtx()
    with caplog.at_level(logging.WARNING):
        advanced_delegation_plugin.register(ctx)
    assert len(ctx.providers) == 2
    assert any("model-routing" in r.getMessage() and "skill registry unavailable" in r.getMessage()
               for r in caplog.records)


def test_no_skill_on_a_core_without_delegation_providers(monkeypatch, caplog):
    monkeypatch.setitem(sys.modules, "agent.delegation_provider", None)
    monkeypatch.delitem(sys.modules, "advanced_delegation_plugin.model_map", raising=False)
    monkeypatch.delitem(sys.modules, "advanced_delegation_plugin.profile_routes", raising=False)
    ctx = _RecordingCtx()
    with caplog.at_level(logging.WARNING):
        advanced_delegation_plugin.register(ctx)
    assert ctx.skills == []


def test_skill_loads_through_the_real_plugin_manager(tmp_path, plugin_root):
    pytest.importorskip("agent.delegation_provider", reason="Hermes core without pluggable delegation providers")
    import shutil

    import hermes_yaml as yaml
    from hermes_cli.plugins import PluginManager

    home = tmp_path / "os-home" / ".hermes"
    shutil.copytree(plugin_root, home / "plugins" / "advanced-delegation",
                    ignore=shutil.ignore_patterns("tests", "__pycache__", ".pytest_cache"))
    (home / "config.yaml").write_text(yaml.safe_dump({"plugins": {"enabled": ["advanced-delegation"]}}),
                                      encoding="utf-8")
    manager = PluginManager()
    manager.discover_and_load()
    try:
        entry = manager._plugin_skills["advanced-delegation:model-routing"]
        assert Path(entry["path"]).name == "SKILL.md"
        assert (Path(entry["path"]).parent / "scripts" / "route_ledger.py").is_file()
    finally:
        manager.unload()
