"""The plugin stays installable in several profiles served by one gateway."""
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_pyproject_is_not_a_buildable_package():
    # Hermes's plugin manager keeps a buildable project's declared name in its uv workspace, so the
    # same plugin enabled in a second profile fails `uv lock` and is refused (pm/workspace.py).
    # The plugin loads from its folder with relative imports and needs no installed package.
    document = tomllib.loads((ROOT / 'pyproject.toml').read_text(encoding='utf-8'))
    assert 'build-system' not in document
    assert document.get('tool', {}).get('uv', {}).get('package') is not True
    assert document['project']['dependencies'] == []
