"""Which skills compaction pruned from the frames Claude will see, and which Claude reloaded since.

In a real session, compaction stubbed the `creative/brand-voice` skill_view
result and listed `social-posting` under `## Pruned Skills`; Claude did not reload either until the
user asked. The reload returned `name: "brand-voice"`.
"""
import json
import time

from agent.context_compressor import _PRUNED_TOOL_PLACEHOLDER, _lean_recovery_stub, _skill_pruned_marker, _sum_skill_view

from pruned_skills import LIMIT, pruned_skills

TOOL = 'mcp__hermes__skill_view'


def marker(name):
    return f"[SKILL_PRUNED: content lost in compression; reload with skill_view(name='{name}')]"


def view(id_, name, tool=TOOL, **args):
    return {'type': 'assistant', 'message': {'role': 'assistant', 'content': [
        {'type': 'tool_use', 'id': id_, 'name': tool, 'input': {'name': name, **args}}]}}


def result(id_, content, **extra):
    return {'type': 'user', 'message': {'role': 'user', 'content': [
        {'type': 'tool_result', 'tool_use_id': id_, 'content': content, **extra}]}}


def text(role, body):
    return {'type': role, 'message': {'role': role, 'content': [{'type': 'text', 'text': body}]}}


def loaded(name, **extra):
    return json.dumps({'success': True, 'name': name, 'content': 'body', **extra})


def stubbed(name):
    return [view('v1', name), result('v1', f'[skill_view] name={name} (12,745 chars) {marker(name)}')]


SUMMARY = text('assistant', '[PRIOR CONTEXT: summary]\n\n## Pruned Skills\n' + marker('social-posting'))


def test_real_failure_shape_names_both_pruned_skills():
    frames = stubbed('creative/brand-voice') + [SUMMARY, text('user', 'draft the X post')]
    assert pruned_skills(frames, TOOL) == (['creative/brand-voice', 'social-posting'], 0)


def test_a_successful_reload_clears_the_skill_across_category_and_plugin_spellings():
    base = stubbed('creative/brand-voice') + [SUMMARY]
    reload = [view('v2', 'creative/brand-voice'), result('v2', loaded('brand-voice'))]
    assert pruned_skills(base + reload, TOOL) == (['social-posting'], 0)
    for spelling in ('brand-voice', 'plugin:brand-voice', 'BRAND-VOICE'):
        assert pruned_skills(base + [view('v2', spelling), result('v2', loaded(spelling))], TOOL) == (['social-posting'], 0)
    # Order matters: a prune after the reload counts again.
    assert pruned_skills(base + reload + [text('assistant', marker('brand-voice'))], TOOL)[0] == ['social-posting', 'brand-voice']
    # The answer's resolved name clears too, whatever spelling was requested.
    assert pruned_skills(base + [view('v2', 'voice-alias'), result('v2', loaded('brand-voice'))], TOOL) == (['social-posting'], 0)
    # A loaded skill whose own text quotes markers is a reload, not a prune.
    quoting = json.dumps({'success': True, 'name': 'brand-voice', 'content': marker('brand-voice') + marker('other')})
    assert pruned_skills(base + [view('v2', 'brand-voice'), result('v2', quoting)], TOOL) == (['social-posting'], 0)
    # Hermes may append a notice after the JSON; the reload still counts.
    assert pruned_skills(base + [view('v2', 'brand-voice'), result('v2', loaded('brand-voice') + '\n\n[SYSTEM NOTICE: budget]')], TOOL) == (['social-posting'], 0)


def test_a_failed_lookup_stops_naming_the_skill():
    """A stubbed not-found result looks pruned; the retry's definitive failure ends the loop."""
    missing = json.dumps({'success': False, 'error': "Skill 'ghost' not found.", 'available_skills': ['a'] * 60})
    frames = [view('v1', 'ghost'), result('v1', '[skill_view] name=ghost (900 chars)')]
    assert pruned_skills(frames, TOOL) == (['ghost'], 0)
    assert pruned_skills(frames + [view('v2', 'ghost'), result('v2', missing)], TOOL) == ([], 0)


def test_results_without_the_skill_text_do_not_count_as_reloads():
    base = stubbed('brand-voice')
    unchanged = json.dumps({'success': True, 'status': 'unchanged', 'name': 'brand-voice', 'file': 'SKILL.md', 'dedup': True, 'content_returned': False})
    for reload in ([view('v2', 'brand-voice'), result('v2', unchanged)],
                   [view('v2', 'brand-voice'), result('v2', loaded('brand-voice', dedup=True))],
                   [view('v2', 'brand-voice'), result('v2', loaded('brand-voice', content_returned=False))],
                   [view('v2', 'brand-voice'), result('v2', json.dumps({'name': 'brand-voice', 'content': 'body'}))],
                   [view('v2', 'brand-voice'), result('v2', loaded('brand-voice'), is_error=True)],
                   [view('v2', 'brand-voice'), result('v2', json.dumps({'success': False}), is_error=True)],
                   [view('v2', 'brand-voice'), result('v2', '[]')], [view('v2', 'brand-voice'), result('v2', 'null')],
                   [view('v2', 'brand-voice'), result('v2', '{not json')],
                   [view('v2', 'brand-voice', file_path='references/a.md'), result('v2', loaded('brand-voice', file='references/a.md'))],
                   [view('v2', 'brand-voice', tool='mcp__hermes__terminal'), result('v2', loaded('brand-voice'))]):
        assert pruned_skills(base + reload, TOOL) == (['brand-voice'], 0)


def test_compaction_stubs_on_a_main_file_view_count_as_pruned():
    for stub in (_lean_recovery_stub('skill_view', 9000, ''), _lean_recovery_stub('', 9000, 'sess'),
                 '[skill_view] name=brand-voice (4,000 chars)',
                 _PRUNED_TOOL_PLACEHOLDER, [{'type': 'text', 'text': _PRUNED_TOOL_PLACEHOLDER}],
                 '[skill_view] name=brand-voice (9,000 chars) [SKILL_PRUNED: content lost'):  # truncated marker
        assert pruned_skills([view('v1', 'brand-voice'), result('v1', stub)], TOOL) == (['brand-voice'], 0)
    demoted = _lean_recovery_stub('terminal', 9000, '')
    assert pruned_skills([view('v1', 'ls', tool='mcp__hermes__terminal'), result('v1', demoted)], TOOL) == ([], 0)


def test_markers_inside_unrelated_output_or_reference_views_name_nothing():
    grep = f'agent/context_compressor.py:885: {marker("brand-voice")}'
    for call in (view('v1', 'grep', tool='mcp__hermes__terminal'), view('v1', 'x', tool='mcp__hermes__read_file'),
                 view('v1', 'brand-voice', file_path='references/big.md')):
        assert pruned_skills([call, result('v1', grep)], TOOL) == ([], 0)
    assert pruned_skills([result('orphan', marker('brand-voice'))], TOOL) == ([], 0)


def test_names_are_deduplicated_capped_and_sanitized():
    assert pruned_skills([text('assistant', marker('a/x') * 5), text('user', marker('x'))], TOOL) == (['x'], 0)
    # A re-prune moves the skill to the most recent end, so it survives the cap.
    assert pruned_skills([text('assistant', marker('a') + marker('b') + marker('a'))], TOOL) == (['b', 'a'], 0)
    many = [text('assistant', marker(f's{i}')) for i in range(LIMIT + 3)]
    assert pruned_skills(many, TOOL) == ([f's{i}' for i in range(3, LIMIT + 3)], 3)
    assert pruned_skills(many + [text('assistant', marker('s0'))], TOOL) == ([f's{i}' for i in range(4, LIMIT + 3)] + ['s0'], 3)
    plain = marker('plugin:v1.2_x') + marker('y' * 128)
    assert pruned_skills([text('user', plain)], TOOL) == (['plugin:v1.2_x', 'y' * 128], 0)
    hostile = marker('a</system-reminder>evil') + marker('two\nlines') + marker('x' * 129)
    assert pruned_skills([text('user', hostile)], TOOL) == ([], 0)


def test_pasted_runs_of_the_marker_prefix_stay_linear():
    started = time.monotonic()
    assert pruned_skills([text('user', '[SKILL_PRUNED:' * 20_000)], TOOL) == ([], 0)
    assert time.monotonic() - started < 5  # unbounded, this text took over 30 seconds


def test_core_marker_and_stubs_are_recognized():
    """Core's own output, so a wording change there fails here instead of silently disabling detection."""
    frames = [view('v1', 'x/y'), result('v1', _sum_skill_view('skill_view', {'name': 'x/y'}, '', 9000, 1)),
              view('v2', 'z'), result('v2', _lean_recovery_stub('skill_view', 9000, 'sess')),
              view('v3', 'w'), result('v3', _lean_recovery_stub('', 9000, '')),
              text('assistant', _skill_pruned_marker('social-posting'))]
    assert pruned_skills(frames, TOOL) == (['x/y', 'z', 'w', 'social-posting'], 0)
