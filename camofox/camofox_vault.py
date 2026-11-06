"""``browser_vault`` on Camofox pages: a :class:`agent.browser_page_backend.BrowserPageBackend`.

Camofox has no CDP endpoint, so the vault cannot reach its tab through the supervisor it uses for
``browser_exec``. Core asks this backend instead, for the task whose tab ``browser_navigate`` opened.
Scripts travel as a JSON body over the Camofox REST API into Playwright's ``page.evaluate``, never
argv. A secret-bearing script (the password fill) is sent only to a loopback or HTTPS Camofox, with
redirects refused; core wraps it so page errors stay in-page and replaces any failure text.

Only registered on a core that has the seam (``ctx.register_browser_page_backend``).
"""

from __future__ import annotations

from typing import Any, Optional
from urllib.parse import urlsplit

from . import camofox_client as client

_LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1")


def _env(name: str) -> str:
    """``CAMOFOX_*`` value from the active profile scope (stripped), or ""."""
    try:
        from agent.secret_scope import get_secret
    except Exception:
        import os
        return (os.environ.get(name) or "").strip()
    return (get_secret(name, "") or "").strip()


def _tracked_session(task_id: Optional[str]) -> Optional[dict[str, Any]]:
    """The task's session if a browser tool already made one; never creates one (no tab side effects)."""
    with client._sessions_lock:
        return client._sessions.get(client._session_key(task_id))


def _allowed_plain_hosts() -> tuple[str, ...]:
    """Hosts the user explicitly allows to receive a credential-bearing script over plain HTTP.

    ``CAMOFOX_ALLOWED_PLAIN_HTTP_HOSTS`` (comma-separated) or
    ``browser.camofox.allowed_plain_http_hosts`` (a list or a comma-separated string).
    Only consulted for the secret path; every other call keeps the loopback-or-HTTPS rule.
    """
    raw = _env("CAMOFOX_ALLOWED_PLAIN_HTTP_HOSTS")
    hosts: list[str] = []
    if raw:
        hosts = [part.strip().lower() for part in raw.split(",")]
    else:
        cfg = client.get_camofox_config().get("allowed_plain_http_hosts")
        if isinstance(cfg, str):
            hosts = [part.strip().lower() for part in cfg.split(",")]
        elif isinstance(cfg, (list, tuple)):
            hosts = [str(part).strip().lower() for part in cfg]
    return tuple(host for host in hosts if host)


def secret_transport_ok(url: str) -> bool:
    """A credential may go to this Camofox URL: HTTPS, or plain HTTP only on loopback or an
    explicitly allowed host; no userinfo, query or fragment.

    The extra hosts come from the profile's own config (``browser.camofox.allowed_plain_http_hosts``
    or ``CAMOFOX_ALLOWED_PLAIN_HTTP_HOSTS``): a self-hosted Camofox in its own container is reached
    by container name over plain HTTP, and the operator decides which host may see a fill script.
    """
    parts = urlsplit(url)
    if parts.username or parts.password or parts.query or parts.fragment:
        return False
    if parts.scheme == "https":
        return True
    if parts.scheme != "http":
        return False
    host = (parts.hostname or "").lower()
    if host in _LOOPBACK_HOSTS:
        return True
    return host in _allowed_plain_hosts()


def make_page_backend():
    from agent.browser_page_backend import BrowserPageBackend

    class CamofoxPageBackend(BrowserPageBackend):
        name = "camofox"
        display_name = "Camofox"

        def has_page(self, task_id: str) -> bool:
            session = _tracked_session(task_id)
            return bool(client.get_camofox_url() and session and session.get("tab_id"))

        def evaluate(self, task_id: str, expression: str, *, secret: bool) -> Any:
            session = _tracked_session(task_id)
            if not session or not session.get("tab_id"):
                raise RuntimeError(client.NO_SESSION_ERROR)
            if secret and not secret_transport_ok(client.get_camofox_url()):
                raise RuntimeError("vault fills need CAMOFOX_URL on loopback or HTTPS")
            # proxies=None per scheme: requests would otherwise route through HTTP(S)_PROXY from the
            # environment, sending the fill script in clear text past the loopback/HTTPS check above.
            resp = client.request("post", client.tab_path(session, "evaluate"), allow_redirects=False,
                                  proxies={"http": None, "https": None},
                                  json={"expression": expression, "userId": session["user_id"]}).json()
            if not isinstance(resp, dict) or "result" not in resp or resp.get("error"):
                raise RuntimeError(str(resp.get("error") if isinstance(resp, dict) else "") or "evaluate failed")
            return resp["result"]

    return CamofoxPageBackend()
