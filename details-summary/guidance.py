"""What the model is told: the system prompt section and the text of a refusal."""
from __future__ import annotations

import re
from typing import Any, Mapping

OPENER_RULES = """\
The opener is the only part most reviewers read. Write it last, after the details, and pick the one \
fact a reviewer needs.
- 1 to 3 sentences, {max_words} words or fewer in total, {max_sentence} words or fewer per sentence.
- Sentence 1: what is different after the merge, for the person who uses it. Name the exact command, \
screen, setting or file.
- Sentence 2 (optional): why. One concrete fact about the problem, with a number when there is one.
- Sentence 3 (optional): the one thing the reviewer must check or decide.
- Use words a new intern knows. Use a technical term only when it is the exact name the user types or \
sees, and put it in backticks. No internal class or function names unless the PR is about them.
- Present tense, active voice, literal words. One fact per sentence. No metaphors, no hype, no em-dashes.
- Start with the thing that changed, never with "This PR" or "This change".
- Only sentences above <details>: no headings, lists, tables or code blocks. Add one screenshot when \
something visible changed."""

EXAMPLES = """\
Good openers:
- "The row page showed its single row as a one-row table. For wide tables, that scrolled far off to the \
right and left most of the page blank."
- "`edit-tools.js` (220 KB) loaded on every page, even for users who cannot edit. It now loads only when \
an editor first uses an edit feature."
- "Adds `llm openai endpoint add/list/remove` so OpenAI-compatible endpoints can be saved from the \
command line instead of by editing YAML."
Bad: "This PR introduces a comprehensive refactor of the endpoint management system to improve \
maintainability.\""""

SHAPE = """\
Every pull request body you write or edit has this shape:

```
<opener: 1-3 plain sentences>

<details>
<summary>{label}</summary>

<the full description: problem, changes, diagrams, verification>

</details>
```

- The summary label names the model and reasoning effort that wrote the details{label_note}.
- Leave a blank line after </summary> and before </details>, or GitHub shows the Markdown as raw text.
- Write the body file in its own step (the file tool, or a separate command), then pass it with \
`gh pr create --body-file <file>` or `gh pr edit <n> --body-file <file>`. The check runs before a \
command starts, so a file written in the same command, a body built with $(...) or a body piped \
from stdin cannot be checked and is refused."""

_UPPER = {"gpt", "glm", "llm", "ai", "oss", "vl"}


def model_label(model: str, effort: str = "") -> str:
    """``anthropic/claude-opus-5.5`` + ``medium`` -> ``Claude Opus 5.5 Medium``."""
    name = (model or "").rsplit("/", 1)[-1]
    name = re.sub(r":.*$", "", name)
    words = []
    for part in re.split(r"[-_\s]+", name):
        if not part:
            continue
        words.append(part.upper() if part.lower() in _UPPER else part[:1].upper() + part[1:])
    label = " ".join(words) or "MODEL"
    effort = (effort or "").strip()
    if effort and effort.lower() not in ("none", "default", "off"):
        label += " " + effort[:1].upper() + effort[1:].lower()
    return label


def configured_effort(model: str) -> str:
    """The reasoning effort config gives ``model``: ``agent.reasoning_overrides`` then
    ``agent.reasoning_effort``. Empty when unreadable."""
    try:
        from hermes_cli.config import load_config_readonly
        agent = (load_config_readonly() or {}).get("agent") or {}
    except Exception:
        return ""
    overrides = agent.get("reasoning_overrides") or {}
    short = (model or "").rsplit("/", 1)[-1]
    for key, value in overrides.items() if isinstance(overrides, Mapping) else ():
        if str(key).lower() in ((model or "").lower(), short.lower()):
            return str(value or "")
    return str(agent.get("reasoning_effort") or "")


def opener_rules(max_words: int, max_sentence: int) -> str:
    return OPENER_RULES.format(max_words=max_words, max_sentence=max_sentence)


def prompt_section(session_info: Mapping[str, Any], max_words: int, max_sentence: int) -> str:
    model = str(session_info.get("model") or "")
    if model:
        label = model_label(model, configured_effort(model))
        note = f". For this session: `{label}`"
    else:
        label, note = "MODEL EFFORT", ', for example "Claude Opus 5.5 Medium"'
    return "\n\n".join((
        "# Pull request bodies: details-summary",
        SHAPE.format(label=label, label_note=note),
        "## The opener",
        opener_rules(max_words, max_sentence),
        EXAMPLES,
    ))


def refusal(problems_text: str, max_words: int, max_sentence: int) -> str:
    return "\n\n".join((
        "Blocked by the details-summary plugin. Nothing was sent to GitHub.",
        problems_text,
        "Fix the body file and run the same command again. Required shape: a short opener, then\n"
        "<details>\n<summary>MODEL EFFORT</summary>\n\nfull description\n\n</details>",
        "Opener rules:\n" + opener_rules(max_words, max_sentence),
    ))
