"""The configured default Spotify device.

Playback needs a target device. Without one, Spotify's own idea of the active
device decides, and that device is often a phone that is no longer playing
anything. ``spotify.default_device_name`` names a device instead, so a call
without an explicit ``device_id`` lands on the speaker the user meant.
"""

from __future__ import annotations

from typing import Any

from .spotify_client import SpotifyClient, SpotifyError

# The config key, read from the active profile's config.yaml.
_CONFIG_SECTION = "spotify"
_CONFIG_KEY = "default_device_name"


def configured_default_device_name() -> str:
    """The ``spotify.default_device_name`` setting, or "" when it is not set.

    Read from the active profile's config on every call, so a multi-profile
    gateway honors each profile's choice and a change applies without a
    restart.
    """
    try:
        from hermes_cli.config import load_config_readonly
        config: Any = load_config_readonly()
    except Exception:
        return ""
    section = config.get(_CONFIG_SECTION) if isinstance(config, dict) else None
    if not isinstance(section, dict):
        return ""
    return str(section.get(_CONFIG_KEY) or "").strip()


def _devices(client: SpotifyClient) -> list[dict]:
    payload = client.request("GET", "/me/player/devices") or {}
    devices = payload.get("devices") if isinstance(payload, dict) else None
    return [device for device in (devices or []) if isinstance(device, dict)]


def resolve_default_device(client: SpotifyClient, args: dict) -> dict:
    """Fill in ``device_id`` from the configured default device.

    An explicit ``device_id`` always wins. With no default configured the
    arguments are returned unchanged, and the caller falls back to Spotify's
    own choice.
    """
    if args.get("device_id"):
        return args
    wanted = configured_default_device_name()
    if not wanted:
        return args
    devices = _devices(client)
    for device in devices:
        if str(device.get("name") or "").strip().lower() == wanted.lower():
            device_id = device.get("id")
            if device_id:
                return {**args, "device_id": device_id}
    available = ", ".join(
        f"{device.get('name')} (id {device.get('id')})"
        for device in devices if device.get("name")
    ) or "none"
    raise SpotifyError(
        f'Configured default device "{wanted}" is offline. Available devices: {available}. '
        f"Pass device_id explicitly to use a different one.")
