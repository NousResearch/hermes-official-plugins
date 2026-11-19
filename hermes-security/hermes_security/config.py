"""Profile-local plugin storage. HERMES_HOME, not a profile label, is the boundary."""
import hashlib
import os
from pathlib import Path

PLUGIN_ID = 'hermes-security'


def data_namespace(plugin_id=PLUGIN_ID):
    """Mirror hermes_cli PluginState's directory name so the standalone CLI shares the tools' store."""
    slug = ''.join(ch if ch.isascii() and (ch.isalnum() or ch in '_-') else '-' for ch in plugin_id.lower())
    slug = slug.strip('-_') or 'plugin'
    return f"agent-plugin-{slug}-{hashlib.sha256(plugin_id.encode('utf-8')).hexdigest()[:8]}"


def resolve_data_dir(ctx=None):
    if ctx is not None and getattr(ctx, 'state', None) is not None:
        return Path(ctx.state.data_dir)
    try:
        from hermes_cli.plugins_state import PluginState
    except ImportError:
        default = Path(os.environ['LOCALAPPDATA']) / 'hermes' if os.name == 'nt' and os.environ.get('LOCALAPPDATA') else Path.home() / '.hermes'
        home = Path(os.environ.get('HERMES_HOME', str(default))).expanduser()
        return home / 'plugin-data' / data_namespace()
    return Path(PluginState(PLUGIN_ID).data_dir)
