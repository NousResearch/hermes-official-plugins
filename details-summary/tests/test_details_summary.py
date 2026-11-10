from __future__ import annotations

import shlex
import textwrap

import pytest

from details_summary_plugin import check_tool_call
from details_summary_plugin.fold import check_body
from details_summary_plugin.guidance import model_label

GOOD = textwrap.dedent("""\
    `hermes update` no longer deletes plugins installed from a local folder. It used to remove any
    plugin it could not find in the catalog.

    <details>
    <summary>Claude Opus 5.5 Medium</summary>

    ## The problem

    - long agent description
    - | tables | allowed |

    </details>
    """)


def test_good_body_passes():
    report = check_body(GOOD)
    assert report.ok, report.problems
    assert report.label == "Claude Opus 5.5 Medium"


def test_screenshot_in_opener_is_allowed():
    body = GOOD.replace("catalog.\n", "catalog.\n\n![after](https://example.com/a.png)\n")
    assert check_body(body).ok


def test_details_written_as_inline_code_is_not_the_fold():
    body = GOOD.replace("`hermes update` no longer", "`hermes update` (see `<details><summary>x</summary>`) no longer")
    report = check_body(body)
    assert report.ok, report.problems
    assert report.label == "Claude Opus 5.5 Medium"


@pytest.mark.parametrize("body, needle", [
    ("Just a plain body with no fold.", "No <details>"),
    ("<details>\n<summary>Claude Opus 5.5</summary>\n\nx\n</details>", "No opener"),
    ("Fixes the thing.\n\n<details>\n\nx\n</details>", "must start with <summary>"),
    ("Fixes the thing.\n\n<details>\n<summary>Details</summary>\n\nx\n</details>", "does not say who"),
    ("Fixes the thing.\n\n<details>\n<summary>GPT 6</summary>\nx\n</details>", "blank line"),
    ("Fixes the thing.\n\n<details>\n<summary>GPT 6</summary>\n\nx\n", "never closed"),
    ("## Summary\nFixes it.\n\n<details>\n<summary>GPT 6</summary>\n\nx\n</details>", "only sentences"),
    ("This PR introduces a robust fix.\n\n<details>\n<summary>GPT 6</summary>\n\nx\n</details>", "filler"),
])
def test_bad_bodies_name_the_problem(body, needle):
    report = check_body(body)
    assert not report.ok
    assert any(needle in p for p in report.problems), report.problems


def test_long_opener_and_long_sentence_are_refused():
    sentence = " ".join(["word"] * 30) + "."
    report = check_body(f"{sentence}\n\n<details>\n<summary>GPT 6</summary>\n\nx\n</details>")
    assert any("Sentence of 30 words" in p for p in report.problems)
    report = check_body(" ".join([sentence] * 3).replace("word", "w", 0) + "\n\n<details>\n"
                        "<summary>GPT 6</summary>\n\nx\n</details>", max_sentence_words=40)
    assert any("opener is 90 words" in p for p in report.problems)


@pytest.mark.parametrize("model, effort, label", [
    ("anthropic/claude-opus-5.5", "medium", "Claude Opus 5.5 Medium"),
    ("openai/gpt-6-astra", "high", "GPT 6 Astra High"),
    ("deepseek/deepseek-v4.1-flash", "", "Deepseek V4.1 Flash"),
])
def test_model_label(model, effort, label):
    assert model_label(model, effort) == label


def test_gh_pr_create_with_bad_body_file_is_blocked(tmp_path):
    (tmp_path / "body.md").write_text("Adds a thing.", encoding="utf-8")
    msg = check_tool_call("terminal", {"command": "gh pr create -t x --body-file body.md",
                                       "workdir": str(tmp_path)})
    assert msg and "Nothing was sent to GitHub" in msg and "No <details>" in msg


def test_cd_and_quoted_absolute_path_are_followed(tmp_path):
    (tmp_path / "body.md").write_text("Adds a thing.", encoding="utf-8")
    quoted = shlex.quote(str(tmp_path))
    msg = check_tool_call("terminal", {"command": f"cd {quoted} && gh pr create --body-file body.md"})
    assert msg and "No <details>" in msg
    msg = check_tool_call("terminal", {"command": f'gh pr create --body-file "{tmp_path / "body.md"}"'})
    assert msg and "No <details>" in msg


def test_gh_pr_create_with_good_body_file_passes(tmp_path):
    (tmp_path / "body.md").write_text(GOOD, encoding="utf-8")
    assert check_tool_call("terminal", {"command": "gh pr create --body-file body.md",
                                        "workdir": str(tmp_path)}) is None


@pytest.mark.parametrize("command", [
    'gh pr create --title t --body "$(cat body.md)"',
    "gh pr create --fill",
    "cat body.md | gh pr edit 12 --body-file -",
])
def test_bodies_that_cannot_be_checked_are_blocked(command, tmp_path):
    msg = check_tool_call("terminal", {"command": command, "workdir": str(tmp_path)})
    assert msg and "cannot be checked" in msg


@pytest.mark.parametrize("command", [
    "gh pr view 12 --json body",
    "gh pr edit 12 --add-label bug",
    "gh pr list",
    "gh api repos/o/r/pulls/12/comments -f body=hello",
    "gh issue create --body hi",
    "echo 'gh pr create' > notes.txt",
    "git commit -m 'gh pr create --body x'",
])
def test_unrelated_commands_pass(command):
    assert check_tool_call("terminal", {"command": command}) is None


def test_body_file_written_in_the_same_command_says_to_split(tmp_path):
    msg = check_tool_call("terminal", {"command": "echo hi > b.md && gh pr create --body-file b.md",
                                       "workdir": str(tmp_path)})
    assert msg and "Write the body file in a separate step first" in msg


def test_home_variable_in_body_file_path_is_expanded(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    (tmp_path / "b.md").write_text(GOOD, encoding="utf-8")
    for spelled in ("$HOME/b.md", "${HOME}/b.md"):
        assert check_tool_call("terminal", {"command": f"gh pr create --body-file {spelled}"}) is None
    msg = check_tool_call("terminal", {"command": "gh pr create --body-file $OTHER/b.md"})
    assert msg and "other than $HOME" in msg


def test_gh_api_pulls_body_and_mcp_tool_are_checked(tmp_path):
    assert check_tool_call("terminal", {"command": "gh api repos/o/r/pulls/9 -X PATCH -f body=short"})
    (tmp_path / "b.md").write_text(GOOD, encoding="utf-8")
    assert check_tool_call("terminal", {"command": "gh api repos/o/r/pulls -F body=@b.md",
                                        "workdir": str(tmp_path)}) is None
    assert check_tool_call("mcp__github__create_pull_request", {"body": "no fold"})
    assert check_tool_call("mcp__github__create_pull_request", {"body": GOOD}) is None
    assert check_tool_call("mcp__github__create_issue", {"body": "no fold"}) is None
