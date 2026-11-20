"""Memory Wiki — Hermes Agent dashboard plugin.

The whole feature is a dashboard extension (``dashboard/manifest.json`` +
``dashboard/dist/index.js`` + ``dashboard/plugin_api.py``); the web server
discovers and mounts it once this plugin is in ``plugins.enabled``.
``register()`` exists so the plugin has a standard loader surface and can be
listed/enabled with ``hermes plugins``; it registers no tools or hooks.

Dashboard routes can survive plugin reloads while their separately named core
module stays cached. Evict only this plugin's core on registration so existing
routes load the current file on their next request, without remounting routes.
"""

from __future__ import annotations

import sys


def register(ctx) -> None:  # noqa: ARG001 — nothing to register for a dashboard-only plugin
    sys.modules.pop("hermes_dashboard_plugin_memory_wiki_core", None)
    return None
