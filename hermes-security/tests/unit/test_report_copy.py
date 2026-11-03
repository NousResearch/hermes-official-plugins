import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from hermes_security.reports.copy import banned_words, check_banned_words, distinct_sections, human_label, title_quality
from hermes_security.reports import render_markdown
from test_report_bundle import inputs


def test_banned_copy():
    assert banned_words('A ROBUST review will delve into the security posture.') == ['robust', 'delve', 'security posture']
    assert check_banned_words('critical\ninsight') == ['critical insight']
    d = inputs()
    assert banned_words(render_markdown(d['manifest'], d['findings_doc'], d['coverage'], d['chains_doc'])) == []


def test_titles_for_three_readers():
    fixtures = json.loads((Path(__file__).parents[1] / 'fixtures/reports/copy-readers.json').read_text())
    assert {f['reader'] for f in fixtures} == {'developer', 'security-engineer', 'non-security'}
    for fixture in fixtures:
        assert title_quality(fixture['good']) == []
        assert title_quality(fixture['bad'])
    assert title_quality('') and title_quality('Short title')
    assert title_quality('word ' * 50)


def test_near_prefix_duplicate_and_case():
    f = inputs()['findings_doc']['findings'][0]
    assert distinct_sections(f) == []
    f['rootCause'] = f['summary'].upper() + ' More'
    assert distinct_sections(f)
    f['rootCause'] = 'A signed-in member'
    assert distinct_sections(f) == []


def test_labels():
    assert human_label('source_supported') == 'Supported by code'
    assert human_label('runtime_confirmed') == 'Confirmed by test'
    assert human_label('NOT_RUN') == 'NOT RUN'
    assert human_label('unknown_new') == 'Unknown new'
