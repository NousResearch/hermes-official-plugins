"""Hermes Mobile plugin: push notifications and paired device capabilities."""

from .device import register as _register_device
from .push import register as _register_push


def register(ctx):
    """Register push notifications (platform and hooks) and device access tools."""
    _register_push(ctx)
    _register_device(ctx)


__all__ = ["register"]
