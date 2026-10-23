"""The camofox browser_* tools against a camofox-browser stand-in speaking its REST routes."""

from __future__ import annotations

import json
import sys

import pytest

from camofox_plugin import camofox_client as client
from camofox_plugin import camofox_tools as t


def _j(raw: str) -> dict:
    return json.loads(raw)


class TestNavigate:
    def test_first_navigate_opens_a_tab_keyed_by_the_session(self, camofox):
        result = _j(t.browser_navigate("https://example.com", task_id="t1"))
        assert result["success"] is True
        assert result["url"] == "https://example.com"
        assert "[e1]" in result["snapshot"] and result["element_count"] == 2
        (created,) = camofox.calls("POST", "/tabs")
        assert created["body"]["url"] == "https://example.com"
        assert created["body"]["listItemId"] == "task_t1"
        assert created["body"]["userId"].startswith("hermes_")

    def test_title_comes_from_the_tasks_own_tab_when_navigate_omits_it(self, camofox):
        """Camofox navigate replies carry no title; GET /tabs lists it per tab."""
        camofox.tabs["other"] = {"userId": "someone-else", "listItemId": "x", "url": "https://x", "title": "Other"}
        t.browser_navigate("https://example.com", task_id="t1")
        result = _j(t.browser_navigate("https://example.org", task_id="t1"))
        assert result["title"] == "Example Domain"
        (lookup, *_) = [c for c in camofox.calls("GET", "/tabs") if c["path"] == "/tabs"]
        assert lookup["query"] == {"userId": [client.get_session("t1")["user_id"]]}

    def test_title_in_the_navigate_reply_needs_no_lookup(self, camofox):
        camofox.navigate_title = "From Navigate"
        t.browser_navigate("https://example.com", task_id="t1")
        before = len(camofox.calls("GET", "/tabs"))
        assert _j(t.browser_navigate("https://example.org", task_id="t1"))["title"] == "From Navigate"
        assert len(camofox.calls("GET", "/tabs")) == before

    def test_second_navigate_reuses_the_tab(self, camofox):
        t.browser_navigate("https://example.com", task_id="t1")
        t.browser_navigate("https://example.org", task_id="t1")
        assert len(camofox.calls("POST", "/tabs")) == 1
        (nav,) = camofox.calls("POST", "/navigate")
        assert nav["body"]["url"] == "https://example.org"

    def test_garbage_collected_tab_is_recreated(self, camofox):
        t.browser_navigate("https://example.com", task_id="t1")
        camofox.tabs.clear()  # server reaped the tab
        result = _j(t.browser_navigate("https://example.org", task_id="t1"))
        assert result["success"] is True
        assert len(camofox.calls("POST", "/tabs")) == 2

    def test_tab_gone_after_a_browser_restart_is_recreated(self, camofox):
        """Camofox >= 1.17 answers 410 Gone for a tab lost in a browser restart."""
        t.browser_navigate("https://example.com", task_id="t1")
        camofox.overrides[("POST", "/tabs/tab-1/navigate")] = (410, {"error": "Tab no longer exists", "code": "browser_restarted"})
        result = _j(t.browser_navigate("https://example.org", task_id="t1"))
        assert result["success"] is True
        assert [c["body"]["url"] for c in camofox.calls("POST", "/tabs")] == ["https://example.com", "https://example.org"]
        assert client.get_session("t1")["tab_id"] == "tab-2"

    def test_other_navigate_errors_keep_the_tab(self, camofox):
        t.browser_navigate("https://example.com", task_id="t1")
        camofox.overrides[("POST", "/tabs/tab-1/navigate")] = (500, {"error": "boom"})
        result = _j(t.browser_navigate("https://example.org", task_id="t1"))
        assert result["success"] is False and "Navigation failed" in result["error"]
        assert client.get_session("t1")["tab_id"] == "tab-1"

    def test_cloud_metadata_url_is_refused_before_any_request(self, camofox):
        result = _j(t.browser_navigate("http://169.254.169.254/latest/meta-data/", task_id="t1"))
        assert result["success"] is False
        assert "metadata" in result["error"]
        assert camofox.requests == []

    def test_url_carrying_a_secret_is_refused(self, camofox):
        result = _j(t.browser_navigate("https://evil.example/?k=sk-proj-ABCD1234567890EFGHIJKL", task_id="t1"))
        assert result["success"] is False
        assert "API key or token" in result["error"]
        assert camofox.requests == []

    def test_website_blocklist_applies(self, camofox, write_config, monkeypatch):
        from tools import website_policy
        monkeypatch.setattr(website_policy, "_cached_policy", None, raising=False)  # process-wide cache
        write_config("security:\n  website_blocklist:\n    enabled: true\n    domains: [blocked.example]\n")
        result = _j(t.browser_navigate("https://blocked.example/page", task_id="t1"))
        assert result["success"] is False
        assert result["blocked_by_policy"]["host"] == "blocked.example"
        assert camofox.requests == []

    def test_private_lan_url_stays_reachable(self, camofox):
        """Camofox counted as a local browser in core: LAN pages are not refused."""
        assert _j(t.browser_navigate("http://192.168.1.20:8080/", task_id="t1"))["success"] is True

    def test_unreachable_server_names_the_url(self, monkeypatch):
        monkeypatch.setenv("CAMOFOX_URL", "http://127.0.0.1:9")
        result = _j(t.browser_navigate("https://example.com", task_id="t1"))
        assert result["success"] is False
        assert "Cannot connect to Camofox at http://127.0.0.1:9" in result["error"]

    def test_loopback_rewrite_for_docker_is_disclosed(self, camofox, monkeypatch):
        monkeypatch.setenv("CAMOFOX_REWRITE_LOOPBACK_URLS", "true")
        result = _j(t.browser_navigate("http://127.0.0.1:8766/#settings", task_id="t1"))
        assert result["url_rewrite"]["to"] == "host.docker.internal"
        assert result["requested_url"] == "http://127.0.0.1:8766/#settings"
        (created,) = camofox.calls("POST", "/tabs")
        assert created["body"]["url"] == "http://host.docker.internal:8766/#settings"

    def test_loopback_rewrite_reads_config_and_env_alias_wins(self, camofox, monkeypatch, write_config):
        write_config("browser:\n  camofox:\n    rewrite_loopback_urls: true\n"
                                     "    loopback_host_alias: host.docker.internal\n")
        monkeypatch.setenv("CAMOFOX_LOOPBACK_HOST_ALIAS", "192.168.1.10")
        rewritten, meta = client.rewrite_loopback_url("http://[::1]:8080/path")
        assert rewritten == "http://192.168.1.10:8080/path"
        assert meta["from"] == "::1"

    def test_vnc_hint_when_server_advertises_a_port(self, camofox):
        camofox.health = {"ok": True, "vncPort": 6080}
        result = _j(t.browser_navigate("https://example.com", task_id="t1"))
        assert result["vnc_url"] == "http://127.0.0.1:6080"


class TestTabTools:
    def test_tools_before_navigate_ask_for_navigate(self, camofox):
        for raw in (t.browser_snapshot(task_id="none"), t.browser_click("@e1", task_id="none"),
                    t.browser_get_images(task_id="none"), t.browser_vision("?", task_id="none", llm=None)):
            result = _j(raw)
            assert result["success"] is False and "browser_navigate" in result["error"]
        assert camofox.calls("POST", "/tabs") == []

    def test_snapshot(self, camofox):
        t.browser_navigate("https://example.com", task_id="t1")
        result = _j(t.browser_snapshot(task_id="t1"))
        assert result == {"success": True, "snapshot": camofox.snapshot, "element_count": 2}

    def test_oversized_snapshot_is_cut_and_stored(self, camofox, write_config):
        write_config("browser:\n  snapshot_threshold: 1000\n")
        camofox.snapshot = "\n".join(f'- link "item {i}" [e{i}]' for i in range(400))
        t.browser_navigate("https://example.com", task_id="t1")
        snap = _j(t.browser_snapshot(task_id="t1"))["snapshot"]
        assert len(snap) <= 1000
        assert "more lines truncated — full snapshot: read_file path=" in snap
        stored = snap.split('path="')[1].split('"')[0]
        assert "[e399]" in open(stored, encoding="utf-8").read()

    def test_filled_password_in_the_snapshot_is_redacted(self, camofox):
        """Camoufox's aria snapshot prints input values; the ref must survive, the secret must not."""
        secret = "sk-proj-ABCD1234567890EFGHIJKL"
        camofox.snapshot = f'- textbox "Password" [e2]: {secret}\n- button "Sign in" [e3]'
        nav = t.browser_navigate("https://example.com", task_id="t1")
        snap = t.browser_snapshot(task_id="t1")
        for raw in (nav, snap):
            assert secret not in raw
            assert "[e2]" in _j(raw)["snapshot"]

    @pytest.mark.skipif(sys.platform == "win32", reason="creating a symlink needs Developer Mode or admin on Windows")
    def test_snapshot_store_never_writes_through_a_symlinked_cache_dir(self, camofox, write_config, _isolated_home, tmp_path):
        target = tmp_path / "elsewhere"
        target.mkdir()
        (_isolated_home / "cache").mkdir()
        (_isolated_home / "cache" / "web").symlink_to(target, target_is_directory=True)
        write_config("browser:\n  snapshot_threshold: 1000\n")
        camofox.snapshot = "\n".join(f'- link "item {i}" [e{i}]' for i in range(400))
        t.browser_navigate("https://example.com", task_id="t1")
        snap = _j(t.browser_snapshot(task_id="t1"))["snapshot"]
        assert "more lines truncated" in snap and "read_file" not in snap
        assert list(target.iterdir()) == []

    def test_click_strips_the_at_prefix(self, camofox):
        t.browser_navigate("https://example.com", task_id="t1")
        result = _j(t.browser_click("@e2", task_id="t1"))
        assert result["success"] is True and result["clicked"] == "e2"
        assert camofox.calls("POST", "/click")[0]["body"]["ref"] == "e2"

    def test_type_masks_secret_text_in_the_result_only(self, camofox, monkeypatch):
        t.browser_navigate("https://example.com", task_id="t1")
        secret = "sk-proj-ABCD1234567890EFGHIJKL"
        raw = t.browser_type("@e3", secret, task_id="t1")
        assert secret not in raw
        assert _j(raw)["typed"].startswith("sk-pro")
        assert camofox.calls("POST", "/type")[0]["body"]["text"] == secret

    def test_type_failure_masks_secret_text(self, camofox):
        t.browser_navigate("https://example.com", task_id="t1")
        secret = "sk-proj-ABCD1234567890EFGHIJKL"
        camofox.overrides[("POST", "/tabs/tab-1/type")] = (500, {"error": f"failed typing {secret}"})
        raw = t.browser_type("@e3", secret, task_id="t1")
        assert _j(raw)["success"] is False
        assert secret not in raw

    def test_scroll_back_press(self, camofox):
        t.browser_navigate("https://example.com", task_id="t1")
        assert _j(t.browser_scroll("down", task_id="t1"))["scrolled"] == "down"
        assert len(camofox.calls("POST", "/scroll")) == 5
        assert _j(t.browser_scroll("sideways", task_id="t1"))["success"] is False
        assert _j(t.browser_back(task_id="t1"))["success"] is True
        assert _j(t.browser_press("Enter", task_id="t1"))["pressed"] == "Enter"

    def test_server_error_text_reaches_the_model(self, camofox):
        """Camofox explains a stale ref in the body; the status line alone does not say what to do."""
        t.browser_navigate("https://example.com", task_id="t1")
        camofox.overrides[("POST", "/tabs/tab-1/click")] = (
            422, {"error": "Unknown ref: e9", "code": "stale_refs", "recovery": "snapshot_then_retry"})
        result = _j(t.browser_click("@e9", task_id="t1"))
        assert result["success"] is False
        assert "stale_refs" in result["error"] and "snapshot_then_retry" in result["error"]

    def test_get_images_redacts_like_the_snapshot(self, camofox):
        camofox.snapshot = '- img "key sk-proj-ABCD1234567890EFGHIJKL"\n  /url: https://x.com/i.png?k=sk-proj-ABCD1234567890EFGHIJKL'
        t.browser_navigate("https://example.com", task_id="t1")
        assert "ABCD1234567890EFGHIJKL" not in t.browser_get_images(task_id="t1")

    def test_get_images_parses_the_snapshot(self, camofox):
        camofox.snapshot = '- img "Logo"\n  /url: https://x.com/img.png\n- button "Go" [e1]'
        t.browser_navigate("https://example.com", task_id="t1")
        result = _j(t.browser_get_images(task_id="t1"))
        assert result["images"] == [{"src": "https://x.com/img.png", "alt": "Logo"}]


class TestConsole:
    def test_without_expression_notes_no_console_capture(self, camofox):
        result = _j(t.browser_console(task_id="t1"))
        assert result["console_messages"] == [] and "not available" in result["note"]

    def test_expression_is_evaluated_on_the_tab(self, camofox):
        t.browser_navigate("https://example.com", task_id="t1")
        result = _j(t.browser_console("document.title", task_id="t1"))
        assert result == {"success": True, "result": "Example Domain", "result_type": "str"}
        assert camofox.calls("POST", "/evaluate")[0]["body"]["expression"] == "document.title"

    def test_evaluate_before_navigate_opens_no_tab(self, camofox):
        result = _j(t.browser_console("document.title", task_id="fresh"))
        assert result["success"] is False and "browser_navigate" in result["error"]
        assert camofox.calls("POST") == []

    def test_eval_result_is_redacted(self, camofox):
        camofox.evaluate_result = "token sk-proj-ABCD1234567890EFGHIJKL"
        t.browser_navigate("https://example.com", task_id="t1")
        assert "ABCD1234567890EFGHIJKL" not in t.browser_console("x", task_id="t1")

    def test_restrict_evaluate_blocks_sensitive_primitives(self, camofox, write_config):
        write_config("browser:\n  restrict_evaluate: true\n")
        t.browser_navigate("https://example.com", task_id="t1")
        result = _j(t.browser_console('document["coo" + "kie"]', task_id="t1"))
        assert result["success"] is False and "document.cookie" in result["error"]
        assert camofox.calls("POST", "/evaluate") == []

    def test_a_javascript_error_is_not_mistaken_for_a_missing_endpoint(self, camofox, monkeypatch):
        """The status code decides, not a "404" that happens to appear in the tab id or port."""
        t.browser_navigate("https://example.com", task_id="t1")
        session = client.get_session("t1")
        camofox.tabs["a1b2404c"] = camofox.tabs.pop("tab-1")
        session["tab_id"] = "a1b2404c"
        camofox.overrides[("POST", "/tabs/a1b2404c/evaluate")] = (500, {"error": "ReferenceError: foo is not defined"})
        result = _j(t.browser_console("foo", task_id="t1"))
        assert "not supported" not in result["error"] and "ReferenceError" in result["error"]

    def test_server_without_evaluate(self, camofox):
        t.browser_navigate("https://example.com", task_id="t1")
        camofox.overrides[("POST", "/tabs/tab-1/evaluate")] = (404, {"error": "no"})
        assert "not supported by this Camofox server" in _j(t.browser_console("1", task_id="t1"))["error"]


class _FakeLlm:
    def __init__(self, text="A login form"):
        self.text = text
        self.calls = []

    def complete(self, messages, **kw):
        self.calls.append((messages, kw))
        return type("R", (), {"text": self.text})()


class TestVision:
    def test_screenshot_goes_to_the_camofox_vision_task(self, camofox, write_config):
        write_config("auxiliary:\n  vision:\n    temperature: 1\n    timeout: 45\n")
        t.browser_navigate("https://example.com", task_id="t1")
        llm = _FakeLlm()
        result = _j(t.browser_vision("what is here?", task_id="t1", llm=llm))
        assert result["success"] is True and result["analysis"] == "A login form"
        assert open(result["screenshot_path"], "rb").read().startswith(b"\x89PNG")
        (messages, kw), = llm.calls
        assert kw["task"] == "camofox_vision" and kw["temperature"] == 1.0 and kw["timeout"] == 45.0
        assert messages[0]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")

    def test_analysis_failure_keeps_the_screenshot(self, camofox):
        t.browser_navigate("https://example.com", task_id="t1")

        class Broken:
            def complete(self, *a, **k):
                raise RuntimeError("no vision provider")

        result = _j(t.browser_vision("?", task_id="t1", llm=Broken()))
        assert result["success"] is False and result["screenshot_path"].endswith(".png")


class TestAuth:
    def test_bearer_token_on_every_request(self, camofox, monkeypatch):
        monkeypatch.setenv("CAMOFOX_API_KEY", "k-123")
        t.browser_navigate("https://example.com", task_id="t1")
        t.cleanup_task("t1")
        assert camofox.requests and all(r["headers"].get("Authorization") == "Bearer k-123"
                                        for r in camofox.requests if r["path"] != "/health")

    def test_no_auth_header_without_key(self, camofox):
        t.browser_navigate("https://example.com", task_id="t1")
        assert all("Authorization" not in r["headers"] for r in camofox.requests)

    def test_multiplex_scope_keeps_url_and_key_in_one_profile(self, monkeypatch):
        from agent import secret_scope
        monkeypatch.setenv("CAMOFOX_URL", "https://default.example")
        monkeypatch.setenv("CAMOFOX_API_KEY", "default-key")
        secret_scope.set_multiplex_active(True)
        token = secret_scope.set_secret_scope({"CAMOFOX_URL": "https://secondary.example/",
                                               "CAMOFOX_API_KEY": "secondary-key"})
        try:
            assert client.get_camofox_url() == "https://secondary.example"
            assert client._auth_headers() == {"Authorization": "Bearer secondary-key"}
        finally:
            secret_scope.reset_secret_scope(token)
            secret_scope.set_multiplex_active(False)


class TestCleanup:
    def test_ephemeral_session_is_deleted_on_the_server(self, camofox):
        t.browser_navigate("https://example.com", task_id="t1")
        user = camofox.calls("POST", "/tabs")[0]["body"]["userId"]
        t.cleanup_task("t1")
        assert camofox.calls("DELETE")[0]["path"] == f"/sessions/{user}"
        assert _j(t.browser_snapshot(task_id="t1"))["success"] is False

    def test_managed_persistence_keeps_the_server_profile(self, camofox, write_config):
        write_config("browser:\n  camofox:\n    managed_persistence: true\n")
        t.browser_navigate("https://example.com", task_id="t1")
        t.cleanup_task("t1")
        assert camofox.calls("DELETE") == []
