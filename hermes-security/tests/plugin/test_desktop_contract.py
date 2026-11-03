"""Static gates for the opt-in, SDK-only security workbench."""
from pathlib import Path
import re

SOURCE = Path(__file__).resolve().parents[2] / 'desktop' / 'plugin.js'


def test_sdk_imports_and_registration():
    text = SOURCE.read_text()
    imports = re.findall(r"from\s+['\"]([^'\"]+)", text)
    assert set(imports) <= {'@hermes/plugin-sdk', 'react', 'react/jsx-runtime'}
    assert '@hermes/plugin-sdk' in imports
    for token in ['ROUTES_AREA', 'SIDEBAR_NAV_AREA', 'PALETTE_AREA', "path: '/security'", 'defaultEnabled: false', 'useQuery', 'refetchInterval: 6000']:
        assert token in text
    assert 'plugin.hermes-security.scan.updated' in text
    assert 'plugin.hermes-security.finding.updated' in text
    assert 'pluginCtx.rest(' in text
    assert not re.search(r'\bfetch\s*\(', text)


def test_theme_safety_and_interactions():
    text = SOURCE.read_text()
    assert not re.search(r'#[0-9a-fA-F]{3,8}\b|\brgba?\(|gradient\(', text)
    assert not re.search(r'codex|openai', text, re.I)
    assert not re.search(r"type:\s*['\"]file['\"]|showOpenDialog|showDirectoryPicker", text)
    for token in ['aria-live', 'min-width:0', 'text-overflow:ellipsis', 'overflow:auto', ':focus-visible', 'Back', 'Start in chat', 'backend host', 'host.composer.insertText', 'NOT RUN']:
        assert token in text
    assert 'optimistic' not in text.lower()


def test_backend_route_builders_are_bounded():
    text = SOURCE.read_text()
    # Every REST call passes through the one route-checked request helper.
    assert text.count('pluginCtx.rest(') == 1
    for path in ['/scans', '/findings', '/repositories', '/folders', '/exports/']:
        assert path in text
    for action in ['cancel', 'resume', 'activity', 'coverage', 'triage', 'patch', 'session']:
        assert action in text
    assert 'assertBackendPath(path)' in text
    assert 'dangerouslySetInnerHTML' not in text


def _css_rule(selector):
    text = SOURCE.read_text()
    match = re.search(re.escape(selector) + r'\{([^}]+)\}', text)
    assert match, f'Missing state rule: {selector}'
    return match.group(1)


def test_shared_dropdown_chevrons_follow_real_menu_state():
    assert 'rotate(-90deg)' in _css_rule('.hs-select-trigger>[aria-hidden=true]')
    assert 'rotate(0deg)' in _css_rule('.hs-select-trigger[data-state=open]>[aria-hidden=true]')


def test_native_dropdown_chevrons_follow_popup_state_not_focus():
    assert 'translateY(-50%) rotate(-90deg)' in _css_rule('.hs-select-chevron')
    assert 'translateY(-50%) rotate(0deg)' in _css_rule('.hs-native-select:open+.hs-select-chevron')
    text = SOURCE.read_text()
    assert not re.search(r'[^{}]*:focus[^{}]*\{[^}]*rotate\(', text)
    # Every filter, triage and setup select must go through the same component.
    assert len(re.findall(r"(?:jsx|jsxs)\('select',", text)) == 1


def test_disclosure_chevrons_keep_the_same_closed_and_open_directions():
    assert 'rotate(-90deg)' in _css_rule('.hs-fold-chevron')
    assert 'rotate(0)' in _css_rule('details.hs-fold[open]>summary>.hs-fold-chevron')
