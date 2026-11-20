"""Read-only state tools for the Hermes MCP tool face.

Three tools let an MCP client read a small, non-secret slice of Hermes' state without a shell and
without access to the profile home:

* ``hermes_status`` — the version, the profile home, the config file's path/size/sha256 and the
  plugin counts;
* ``hermes_config_get`` — one value from :data:`ALLOWED_CONFIG_KEYS` by dotted key; a secret-shaped
  key is refused **by name** before any lookup, and the allowlist bounds the tool, so it can never
  become a general config reader;
* ``hermes_plugins_list`` — the plugins installed under the profile, with their manifest name,
  version, kind and state. Skills are not duplicated here: ``skills_list`` (already on this face)
  is their surface.

Nothing here writes, shells out or returns a credential. The status carries the config's digest,
not its contents; the lists carry names and descriptions, never a value from a secret block. The
config is read through Hermes' own loader (which returns a deepcopy), so this module can neither
mutate nor corrupt the process-wide cache.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

#: The only config keys ``hermes_config_get`` will answer. Values are non-secret by construction
#: (scalars or lists of names/ids); nothing here can reach a credential block.
ALLOWED_CONFIG_KEYS: dict[str, str] = {
    "model.default": "the default model id",
    "model.provider": "the default inference provider",
    "model.fallback": "the fallback model, if any",
    "plugins.enabled": "the enabled plugin names",
    "plugins.disabled": "the disabled plugin names",
    "delegation.backend": "the delegation backend",
    "terminal.backend": "the terminal tool's sandbox backend",
}

#: Broad on purpose: a key that *smells* like a credential is refused by name even when it is not on
#: the allowlist, so the refusal itself never becomes an oracle for which secret keys exist.
_SECRET_SHAPED = re.compile(
    r"(?i)(api[_-]?key|access[_-]?key|secret|token|passw(?:or)?d|bearer|credential|authorization|"
    r"auth[_-]?key|private[_-]?key|cookie|jwt|webhook|dsn|conn(?:ection)?[_-]?string)"
)

_MISSING = object()


def _json(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _error(tool: str, code: str, **fields: Any) -> str:
    return _json({"tool": tool, "error": code, **fields})


def _home() -> Path:
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home())
    except Exception:
        return Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))


def _config() -> dict:
    """The merged config, through Hermes' loader; a deepcopy, never mutated here.

    Deliberately not wrapped in a silent fallback: a failed read must surface as an error in the
    tool's answer, not as a confident "no plugins".
    """
    from hermes_cli.config import load_config

    loaded = load_config()
    return loaded if isinstance(loaded, dict) else {}


def _walk(config: dict, dotted: str) -> Any:
    current: Any = config
    for part in dotted.split("."):
        if not isinstance(current, dict) or part not in current:
            return _MISSING
        current = current[part]
    return current


def _scalars(text: str) -> dict:
    """Top-level ``key: value`` pairs from a small YAML file.

    Deliberately dependency-free: the isolated Hermes runtime does not ship PyYAML, and a plugin
    manifest needs only its top-level scalars. Nested keys are indented and skipped.
    """
    found: dict[str, str] = {}
    for line in text.splitlines():
        if not line or line[0] in "#- \t" or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if not key or not value:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        found[key] = value
    return found


def _version() -> tuple[str, str]:
    try:
        from hermes_cli.version_info import get_version_info

        info = get_version_info()
        return str(info.display_version), str(info.source)
    except Exception:
        try:
            from hermes_cli import __version__

            return str(__version__), "module"
        except Exception:
            return "unknown", "unavailable"


def _config_digest(home: Path) -> dict:
    path = home / "config.yaml"
    try:
        data = path.read_bytes()
    except OSError:
        return {"path": str(path), "present": False}
    return {
        "path": str(path),
        "present": True,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def _plugin_state(name: str, config: dict) -> str:
    """The plugin's state under the profile's ``plugins`` config.

    ``plugins.enabled`` is an allowlist: absent means the profile's default, where every installed
    plugin is active; present means only the listed names are. ``plugins.disabled`` wins over both.
    """
    section = config.get("plugins") if isinstance(config.get("plugins"), dict) else {}
    enabled = section.get("enabled") if isinstance(section.get("enabled"), list) else None
    disabled = section.get("disabled") if isinstance(section.get("disabled"), list) else None
    if disabled and name in disabled:
        return "disabled"
    if enabled is None:
        return "default"
    return "enabled" if name in enabled else "unlisted"


def _plugins(home: Path, config: dict) -> list[dict]:
    """The plugins installed under the profile's ``plugins/`` directory, by manifest."""
    found: list[dict] = []
    root = home / "plugins"
    if not root.is_dir():
        return found
    for directory in sorted(root.iterdir()):
        manifest = directory / "plugin.yaml"
        if not directory.is_dir() or directory.name.startswith(".") or not manifest.is_file():
            continue
        try:
            meta = _scalars(manifest.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            meta = {}
        name = str(meta.get("name") or directory.name)
        found.append(
            {
                "name": name,
                "version": str(meta.get("version") or "unknown"),
                "kind": str(meta.get("kind") or "unknown"),
                "state": _plugin_state(name, config),
            }
        )
    return found


def hermes_status() -> str:
    """Read-only Hermes status: the version, the profile home, the config digest and the counts.

    Never returns config contents or credentials — only the config file's path, size and sha256.
    """
    try:
        home = _home()
        version, source = _version()
        config = _config()
        plugins = _plugins(home, config)
        return _json(
            {
                "tool": "hermes_status",
                "version": version,
                "version_source": source,
                "home": str(home),
                "config": _config_digest(home),
                "plugins": {
                    "installed": len(plugins),
                    "enabled": sum(1 for plugin in plugins if plugin["state"] in ("enabled", "default")),
                },
                "python": "%d.%d.%d" % tuple(sys.version_info[:3]),
            }
        )
    except Exception as exc:  # an MCP tool call reports the failure, it never kills the server
        return _error("hermes_status", "failed", detail=str(exc))


def hermes_config_get(key: str = "") -> str:
    """Read one allowlisted, non-secret config value by dotted key.

    A secret-shaped key is refused **by name** before any lookup; a key outside
    ``ALLOWED_CONFIG_KEYS`` is refused with the allowlist, so the tool can never become a
    general config reader.

    Args:
        key: A dotted config key from the allowlist (e.g. ``model.default``).
    """
    try:
        key = str(key or "").strip()
        if not key:
            return _error("hermes_config_get", "missing_key", allowed=sorted(ALLOWED_CONFIG_KEYS))
        if _SECRET_SHAPED.search(key):
            return _error("hermes_config_get", "secret_key", key=key,
                          reason="secret-shaped keys are refused by name and never read")
        if key not in ALLOWED_CONFIG_KEYS:
            return _error("hermes_config_get", "not_allowlisted", key=key,
                          allowed=sorted(ALLOWED_CONFIG_KEYS))
        value = _walk(_config(), key)
        if value is _MISSING:
            return _error("hermes_config_get", "not_found", key=key)
        return _json({"tool": "hermes_config_get", "key": key, "value": value})
    except Exception as exc:
        return _error("hermes_config_get", "failed", detail=str(exc))


def hermes_plugins_list() -> str:
    """List the plugins installed under the profile (name, manifest version, kind, enabled state).

    Read-only, no credentials. Skills are not repeated here — the ``skills_list`` tool is their
    surface on this same face.
    """
    try:
        home = _home()
        config = _config()
        plugins = _plugins(home, config)
        return _json(
            {
                "tool": "hermes_plugins_list",
                "home": str(home),
                "plugins": plugins,
                "plugin_count": len(plugins),
            }
        )
    except Exception as exc:
        return _error("hermes_plugins_list", "failed", detail=str(exc))


_SPECS = (
    (
        hermes_status,
        "hermes_status",
        "Hermes status, read-only: version, profile home, the config file's digest (path/bytes/"
        "sha256) and the plugin counts. No config contents and no credentials.",
    ),
    (
        hermes_config_get,
        "hermes_config_get",
        "Read one allowlisted, non-secret Hermes config value by dotted key (e.g. model.default). A "
        "secret-shaped key is refused by name; keys outside the allowlist are refused with the list.",
    ),
    (
        hermes_plugins_list,
        "hermes_plugins_list",
        "List the plugins installed under the profile (name, manifest version, kind, enabled state). "
        "Read-only; no credentials. Skills are served by skills_list.",
    ),
)


def register_state_tools(mcp: Any) -> int:
    """Register the three read-only state tools on *mcp*; returns how many were registered.

    The MCP SDK derives each tool's input schema from the handler's signature, which is exactly the
    shape these tools need (the only argument is ``key``); no private SDK state is touched.
    """
    for handler, name, description in _SPECS:
        mcp.add_tool(handler, name=name, description=description)
    return len(_SPECS)
