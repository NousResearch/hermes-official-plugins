"""Check a pull request body against the details-summary shape.

A body passes when it has a short plain-language opener, then the full description inside
``<details><summary>model + effort</summary> ... </details>``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

DEFAULT_MAX_OPENER_WORDS = 60
DEFAULT_MAX_SENTENCE_WORDS = 25

# Words that make an opener read as agent-written. Matched as whole words, case-insensitive.
BANNED_OPENER_WORDS = (
    "this pr", "this pull request", "this change", "this commit", "introduces?", "introducing",
    "comprehensive", "robust", "seamless(?:ly)?", "leverag(?:e|es|ed|ing)", "enhanc(?:e|es|ed|ement|ements)",
    "streamlin(?:e|es|ed|ing)", "various", "improvements?", "utiliz(?:e|es|ed|ing)", "facilitat(?:e|es|ed)",
    "delve", "holistic", "crucial", "significant(?:ly)?", "powerful", "elegant(?:ly)?", "simply",
    "basically", "essentially", "under the hood", "out of the box",
)
_BANNED_RE = re.compile(r"\b(" + "|".join(BANNED_OPENER_WORDS) + r")\b", re.I)

GENERIC_LABELS = {
    "", "details", "detail", "more", "more details", "show more", "expand", "click to expand",
    "click here", "full description", "description", "full details", "ai", "ai generated",
    "agent", "summary", "notes",
}

_DETAILS_OPEN = re.compile(r"<details\b[^>]*>", re.I)
_FENCE = re.compile(r"^(```|~~~).*?^\1[ \t]*$", re.M | re.S)
_INLINE_CODE = re.compile(r"(`+)(?!`).+?(?<!`)\1", re.S)
_DETAILS_CLOSE = re.compile(r"</details\s*>", re.I)
_SUMMARY = re.compile(r"\A\s*<summary\b[^>]*>(?P<label>.*?)</summary\s*>", re.I | re.S)
_COMMENT = re.compile(r"<!--.*?-->", re.S)
_IMAGE_MD = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_HTML_WRAPPER = re.compile(r"</?(?:img|p|picture|source|br|a|video)\b[^>]*>", re.I)
_BARE_MEDIA = re.compile(r"^\s*https?://\S+\s*$", re.M)
_TAG = re.compile(r"<[^>]+>")
_STRUCTURE = re.compile(r"^\s*(?:#{1,6}\s|[-*+]\s|\d+[.)]\s|\||```|~~~|>\s|<table\b|<ul\b|<ol\b|<h\d\b)",
                        re.I | re.M)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


@dataclass
class Report:
    problems: list[str] = field(default_factory=list)
    opener: str = ""
    label: str = ""

    @property
    def ok(self) -> bool:
        return not self.problems


def _opener_text(raw: str) -> str:
    text = _COMMENT.sub("", raw)
    text = _IMAGE_MD.sub("", text)
    text = _HTML_WRAPPER.sub("", text)
    text = _BARE_MEDIA.sub("", text)
    return text.strip()


def _words(text: str) -> list[str]:
    return re.findall(r"[\w'’./:-]+", _TAG.sub(" ", text))


def _mask_code(text: str) -> str:
    """Blank out fenced blocks and inline code (same length, so offsets still line up), so a
    `<details>` written as an example is not taken for the real fold."""
    blank = lambda m: re.sub(r"[^\n]", " ", m.group(0))  # noqa: E731
    text = _FENCE.sub(blank, text)
    return _INLINE_CODE.sub(blank, text)


def check_body(body: str, *, max_opener_words: int = DEFAULT_MAX_OPENER_WORDS,
               max_sentence_words: int = DEFAULT_MAX_SENTENCE_WORDS) -> Report:
    report = Report()
    problems = report.problems
    if not body or not body.strip():
        problems.append("The body is empty. Write an opener, then fold the full description into <details>.")
        return report

    opened = _DETAILS_OPEN.search(_mask_code(body))
    if opened is None:
        problems.append("No <details> block. Put the full description inside "
                        "<details><summary>MODEL EFFORT</summary> ... </details> below the opener.")
        opener_raw, rest = body, ""
    else:
        opener_raw, rest = body[:opened.start()], body[opened.end():]

    opener = _opener_text(opener_raw)
    report.opener = opener
    _check_opener(opener, problems, max_opener_words, max_sentence_words)
    if opened is not None:
        _check_details(rest, report)
    return report


def _check_opener(opener: str, problems: list[str], max_words: int, max_sentence_words: int) -> None:
    if not opener:
        problems.append("No opener. Write 1-3 plain sentences above <details>: what the PR does for the "
                        "user, then why.")
        return
    structure = _STRUCTURE.search(opener)
    if structure:
        problems.append("Above <details>, keep only sentences and images. Move this into the details: "
                        f"{structure.group(0).strip()!r} (heading, list, table, quote or code block).")
    words = _words(opener)
    if len(words) > max_words:
        problems.append(f"The opener is {len(words)} words; keep it to {max_words} or fewer. Pick the one "
                        "thing a reviewer needs and move the rest into the details.")
    for sentence in _SENTENCE_SPLIT.split(" ".join(opener.split())):
        n = len(_words(sentence))
        if n > max_sentence_words:
            problems.append(f"Sentence of {n} words (limit {max_sentence_words}); split it: "
                            f"{sentence[:80]!r}")
    banned = sorted({m.group(0).lower() for m in _BANNED_RE.finditer(opener)})
    if banned:
        problems.append("Opener uses filler or hype words: " + ", ".join(banned)
                        + ". Say what changed in literal words.")
    if "—" in opener:
        problems.append("Opener uses an em-dash. Use two sentences instead.")


def _check_details(rest: str, report: Report) -> None:
    problems = report.problems
    summary = _SUMMARY.match(rest)
    if summary is None:
        problems.append("<details> must start with <summary>MODEL EFFORT</summary>, e.g. "
                        "<summary>Claude Opus 5.5 Medium</summary>.")
        return
    label = " ".join(_TAG.sub(" ", summary.group("label")).split())
    report.label = label
    if label.lower().strip(" .:") in GENERIC_LABELS:
        problems.append(f"Summary label {label!r} does not say who wrote the details. Name the model and "
                        "effort, e.g. \"Claude Opus 5.5 Medium\".")
    elif len(label) > 60:
        problems.append("Summary label is longer than 60 characters. Use the model name and effort only.")

    after = rest[summary.end():]
    closed = _DETAILS_CLOSE.search(after)
    if closed is None:
        problems.append("<details> is never closed. Add </details> after the full description.")
        inner = after
    else:
        inner = after[:closed.start()]
    if not inner.strip():
        problems.append("The <details> block is empty. Put the full description inside it.")
    elif not re.match(r"[ \t]*\r?\n[ \t]*\r?\n", inner):
        problems.append("Leave a blank line after </summary>. Without it GitHub shows the Markdown "
                        "inside <details> as raw text.")


def format_report(report: Report, source: str = "") -> str:
    where = f" ({source})" if source else ""
    lines = [f"Pull request body{where} does not follow the details-summary shape:"]
    lines += [f"- {p}" for p in report.problems]
    return "\n".join(lines)
