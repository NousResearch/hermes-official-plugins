"""Skills that Hermes compaction pruned from the history and that Claude has not reloaded since.

Compaction swaps a loaded skill's body for a stub, but Claude keeps acting as if the skill were
still loaded. The provider names these skills in its per-turn reminder. Detection reads the
frames Claude is about to see, in one ordered pass.
"""
import json
import re

# Mirrors the canonical core marker (agent/context_compressor.py `_skill_pruned_marker`). Core is
# not imported at runtime: the helpers are private, and a test pins this copy to core's output.
MARKER_PREFIX = '[SKILL_PRUNED:'
# The gap is bounded (core's is ~30 chars) so pasted runs of the prefix cannot make the scan quadratic.
MARKER = re.compile(re.escape(MARKER_PREFIX) + r"[^\]]{0,200}?reload with skill_view\(name='([^']+)'\)")
# Stubs core leaves where a tool result used to be: the lean-tail demotion (labelled with any
# tool name), the one-line skill_view summary, and the cleared placeholder.
DEMOTED = re.compile(r'\[\S+ output demoted at compaction ')
STUBS = ('[skill_view] name=', '[Old tool output cleared to save context space]')
# A pasted marker could carry arbitrary text; only plain skill names reach the reminder.
NAME = re.compile(r'[\w.:/-]{1,128}')
LIMIT = 8


def _key(name):
    """`creative/brand-voice`, `plugin:brand-voice` and `brand-voice` are the same skill to a reload."""
    return re.split(r'[/:]', name)[-1].lower()


def _text(content):
    if isinstance(content, str):
        return content
    return '\n'.join(b.get('text', '') for b in content or () if isinstance(b, dict) and b.get('type') == 'text')


def _answer(block, text):
    """The skill_view result when it settles the skill: its text is back, or the skill cannot load.

    A dedup stub or a tool error settles nothing. Hermes may append notices after the JSON.
    """
    if block.get('is_error'):
        return None
    try:
        result, _ = json.JSONDecoder().raw_decode(text)
    except ValueError:
        return None
    loaded = result.get('success') is True and not result.get('dedup') and result.get('content_returned') is not False
    return result if loaded or result.get('success') is False else None


def pruned_skills(frames, tool):
    """Pruned, not-yet-reloaded skill names (most recent `LIMIT`, oldest first) and how many more."""
    calls, pruned = {}, {}

    def prune(name):
        if isinstance(name, str) and NAME.fullmatch(name):
            key = _key(name)
            pruned.pop(key, None)  # re-insert so the most recent prune sorts last
            pruned[key] = name

    def prune_markers(text):
        names = MARKER.findall(text) if MARKER_PREFIX in text else []
        for name in names:
            prune(name)
        return bool(names)

    for frame in frames:
        for block in frame['message']['content']:
            kind = block.get('type')
            if kind == 'tool_use' and block.get('name') == tool:
                args = block.get('input') if isinstance(block.get('input'), dict) else {}
                calls[block.get('id')] = (args.get('name'), bool(args.get('file_path')))
            elif kind == 'text' and isinstance(block.get('text'), str):
                prune_markers(block['text'])
            elif kind == 'tool_result' and block.get('tool_use_id') in calls:
                requested, reference = calls[block['tool_use_id']]
                if reference:
                    continue  # a linked file was lost, not the skill's instructions
                text = _text(block.get('content')).lstrip()
                if not text.startswith('{'):
                    if not prune_markers(text) and (text.startswith(STUBS) or DEMOTED.match(text)):
                        prune(requested)
                # A real result: the skill's own text may quote a marker, so only a reload is read from it.
                elif pruned and (result := _answer(block, text)) is not None:
                    # Reloaded, or it cannot load (a stubbed failed lookup): naming it again would loop.
                    for name in (requested, result.get('name')):
                        if isinstance(name, str):
                            pruned.pop(_key(name), None)
    names = list(pruned.values())
    return names[-LIMIT:], max(len(names) - LIMIT, 0)
