"""Test bootstrap: import the plugin directory as the package ``camofox_plugin``.

The Hermes plugin loader imports a directory plugin as a package (``hermes_plugins.<slug>``) so the
relative imports in ``__init__.py`` resolve; tests mirror that under a stable alias. Hermes core
(``hermes_cli``, ``tools``, ``agent``) must be importable — run with a hermes-agent checkout on
``PYTHONPATH``.

``FakeCamofox`` is a small HTTP server speaking the camofox-browser REST routes the plugin uses, so
the tools run their real ``requests`` calls end to end.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

PACKAGE = "camofox_plugin"
_ROOT = Path(__file__).resolve().parents[1]

# ``python -m pytest`` from the repo root puts the root on sys.path, where this plugin's modules could
# shadow core packages when core is imported.
sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != _ROOT]


def _load_plugin_package():
    if PACKAGE in sys.modules:
        return sys.modules[PACKAGE]
    spec = importlib.util.spec_from_file_location(
        PACKAGE, _ROOT / "__init__.py", submodule_search_locations=[str(_ROOT)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE] = module
    spec.loader.exec_module(module)
    return module


_REAL_CORE_BUNDLES_CAMOFOX = _load_plugin_package().core_bundles_camofox

_CAMOFOX_ENV = ("CAMOFOX_URL", "CAMOFOX_API_KEY", "CAMOFOX_USER_ID", "CAMOFOX_SESSION_KEY",
                "CAMOFOX_ADOPT_EXISTING_TAB", "CAMOFOX_REWRITE_LOOPBACK_URLS", "CAMOFOX_LOOPBACK_HOST_ALIAS")


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Every test gets its own profile home and a clean Camofox environment and session map."""
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    for var in _CAMOFOX_ENV:
        monkeypatch.delenv(var, raising=False)
    from camofox_plugin import camofox_client as client
    client._sessions.clear()
    client._vnc_url_by_camofox_url.clear()
    yield home
    client._sessions.clear()
    client._vnc_url_by_camofox_url.clear()


@pytest.fixture(autouse=True)
def _post_removal_core(monkeypatch):
    """Pin "core no longer bundles Camofox" so the suite runs the same on a core that still ships
    ``tools/browser_camofox.py`` and on one that does not. Tests of the inert guard undo this."""
    import camofox_plugin
    monkeypatch.setattr(camofox_plugin, "core_bundles_camofox", lambda: False)


@pytest.fixture
def real_core_detection(monkeypatch):
    """Undo the pin: the plugin asks the running core whether it still ships Camofox."""
    import camofox_plugin
    monkeypatch.setattr(camofox_plugin, "core_bundles_camofox", _REAL_CORE_BUNDLES_CAMOFOX)


@pytest.fixture
def write_config(_isolated_home):
    """Write the test profile's ``config.yaml``."""
    return lambda text: (_isolated_home / "config.yaml").write_text(text, encoding="utf-8")


class FakeCamofox:
    """In-process camofox-browser stand-in. ``requests`` records ``(method, path, query, body, headers)``."""

    def __init__(self):
        self.requests: list[dict] = []
        self.tabs: dict[str, dict] = {}  # tabId -> {"userId", "listItemId", "url", "title"}
        self.snapshot = '- heading "Example Domain" [e1]\n- link "Learn more" [e2]'
        self.health = {"ok": True}
        self.overrides: dict[tuple[str, str], tuple[int, dict]] = {}  # (method, path) -> (status, body)
        self.evaluate_result = "Example Domain"
        self.navigate_title: str | None = None
        self._next = 0
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, status, payload, content_type="application/json"):
                body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _handle(self, method):
                url = urlsplit(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                body = json.loads(raw) if raw else None
                fake.requests.append({"method": method, "path": url.path, "query": parse_qs(url.query),
                                      "body": body, "headers": dict(self.headers)})
                override = fake.overrides.get((method, url.path))
                if override:
                    return self._send(*override)
                status, payload = fake.route(method, url.path, parse_qs(url.query), body or {})
                if isinstance(payload, bytes):
                    return self._send(status, payload, "image/png")
                return self._send(status, payload)

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

            def do_DELETE(self):
                self._handle("DELETE")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def route(self, method, path, query, body):
        parts = path.strip("/").split("/")
        if path == "/health":
            return 200, self.health
        if method == "GET" and path == "/tabs":
            user = query.get("userId", [""])[0]
            return 200, {"running": True, "tabs": [
                {"tabId": tid, "targetId": tid, "listItemId": t["listItemId"], "url": t["url"], "title": t["title"]}
                for tid, t in self.tabs.items() if t["userId"] == user]}
        if method == "POST" and path == "/tabs":
            self._next += 1
            tid = f"tab-{self._next}"
            self.tabs[tid] = {"userId": body.get("userId"), "listItemId": body.get("listItemId") or body.get("sessionKey"),
                              "url": body.get("url", "about:blank"), "title": "Example Domain"}
            return 200, {"tabId": tid, "url": body.get("url")}
        if method == "DELETE" and parts[0] == "sessions":
            for tid in [t for t, v in self.tabs.items() if v["userId"] == parts[1]]:
                del self.tabs[tid]
            return 200, {"ok": True}
        if method == "POST" and len(parts) == 3 and parts[0] == "sessions" and parts[2] == "cookies":
            return 200, {"ok": True, "userId": parts[1], "count": len(body.get("cookies", []))}
        if parts[0] == "tabs" and len(parts) == 3:
            tid, action = parts[1], parts[2]
            tab = self.tabs.get(tid)
            if tab is None:
                return 404, {"error": "Tab not found"}
            if action == "navigate":
                tab["url"] = body.get("url")
                reply = {"ok": True, "url": body.get("url")}
                if self.navigate_title is not None:
                    reply["title"] = self.navigate_title
                return 200, reply
            if action == "snapshot":
                return 200, {"url": tab["url"], "snapshot": self.snapshot, "refsCount": self.snapshot.count("[e")}
            if action == "screenshot":
                return 200, b"\x89PNG\r\n\x1a\nfake"
            if action == "evaluate":
                return 200, {"ok": True, "result": self.evaluate_result}
            return 200, {"ok": True, "url": tab["url"]}
        return 404, {"error": "not found"}

    def calls(self, method=None, path_suffix=None):
        return [r for r in self.requests
                if (method is None or r["method"] == method) and (path_suffix is None or r["path"].endswith(path_suffix))]

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


@pytest.fixture
def camofox(monkeypatch):
    server = FakeCamofox()
    monkeypatch.setenv("CAMOFOX_URL", server.url)
    yield server
    server.close()


class _RepoRootIsNotATestPackage:
    """The plugin root *is* a package (``__init__.py`` with relative imports), so pytest would collect
    it as a test ``Package`` and import that ``__init__.py`` standalone. Collect it as a plain dir.

    Registered as a global plugin (not a conftest hook) because the root directory's collector is
    created through its *parent's* hook proxy, which never sees a conftest inside the root."""

    @pytest.hookimpl(tryfirst=True)
    def pytest_collect_directory(self, path, parent):
        if Path(path).resolve() == _ROOT:
            return pytest.Dir.from_parent(parent, path=path)
        return None


def pytest_configure(config):
    if not config.pluginmanager.has_plugin("camofox-repo-root"):
        config.pluginmanager.register(_RepoRootIsNotATestPackage(), "camofox-repo-root")
