"""Lazy Hermes plugin entry point; importing this file performs no registration."""


def register(ctx):
    """Register the service only when the Hermes host requests it."""
    # Relative: Hermes loads this directory under a synthetic package name
    # (e.g. hermes_plugins.<slug>), so `hermes_security` is not on sys.path.
    from .hermes_security import plugin

    return plugin.register(ctx)
