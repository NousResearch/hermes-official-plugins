"""Camofox identity and session scoping: ephemeral, managed persistence, external identity, adoption,
and profile isolation of the tab map."""

from __future__ import annotations

import json

from camofox_plugin import camofox_client as client
from camofox_plugin import camofox_tools as t


class TestIdentity:
    def test_ephemeral_sessions_get_random_user_ids(self, camofox):
        a = client.get_session("t1")
        b = client.get_session("t2")
        assert a["user_id"].startswith("hermes_") and a["user_id"] != b["user_id"]
        assert a["managed"] is False
        assert client.get_session("t1") is a

    def test_managed_persistence_is_stable_and_matches_core(self, camofox, write_config, _isolated_home):
        """Same derivation core used, so a migrated profile keeps its Camofox browser profile."""
        import uuid
        write_config("browser:\n  camofox:\n    managed_persistence: true\n")
        first = client.get_session("t1")["user_id"]
        client.drop_session("t1")
        assert client.get_session("t1")["user_id"] == first
        root = str(_isolated_home / "browser_auth" / "camofox")
        assert first == "hermes_" + uuid.uuid5(uuid.NAMESPACE_URL, f"camofox-user:{root}").hex[:10]

    def test_external_identity_from_env_wins(self, camofox, monkeypatch, write_config):
        write_config("browser:\n  camofox:\n    managed_persistence: true\n    user_id: from-config\n")
        monkeypatch.setenv("CAMOFOX_USER_ID", "shared-user")
        monkeypatch.setenv("CAMOFOX_SESSION_KEY", "shared-key")
        session = client.get_session("t1")
        assert (session["user_id"], session["session_key"], session["managed"]) == ("shared-user", "shared-key", True)

    def test_external_identity_from_config(self, camofox, write_config):
        write_config("browser:\n  camofox:\n    user_id: cfg-user\n    session_key: cfg-key\n")
        session = client.get_session("t1")
        assert (session["user_id"], session["session_key"]) == ("cfg-user", "cfg-key")

    def test_multiplex_scope_identity_is_the_routed_profiles(self, camofox, monkeypatch):
        from agent import secret_scope
        monkeypatch.setenv("CAMOFOX_USER_ID", "launch-profile-user")
        secret_scope.set_multiplex_active(True)
        token = secret_scope.set_secret_scope({"CAMOFOX_URL": camofox.url, "CAMOFOX_USER_ID": "routed-user"})
        try:
            assert json.loads(t.browser_navigate("https://example.com", task_id="mx"))["success"] is True
        finally:
            secret_scope.reset_secret_scope(token)
            secret_scope.set_multiplex_active(False)
        assert camofox.calls("POST", "/tabs")[0]["body"]["userId"] == "routed-user"

    def test_tab_map_is_per_profile(self, camofox, monkeypatch, tmp_path):
        """One process serving two profiles never hands profile A's tab to profile B."""
        t.browser_navigate("https://example.com", task_id="same-task")
        other = tmp_path / "other-profile"
        other.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(other))
        result = json.loads(t.browser_snapshot(task_id="same-task"))
        assert result["success"] is False and "browser_navigate" in result["error"]


class TestAdoption:
    def _external(self, monkeypatch):
        monkeypatch.setenv("CAMOFOX_USER_ID", "shared-user")
        monkeypatch.setenv("CAMOFOX_SESSION_KEY", "our-session")
        monkeypatch.setenv("CAMOFOX_ADOPT_EXISTING_TAB", "true")

    def test_adopts_the_integrations_tab_after_a_restart(self, camofox, monkeypatch):
        self._external(monkeypatch)
        camofox.tabs["ext-tab"] = {"userId": "shared-user", "listItemId": "our-session", "url": "https://x", "title": "X"}
        assert client.get_session("t1")["tab_id"] == "ext-tab"
        assert json.loads(t.browser_snapshot(task_id="t1"))["success"] is True
        assert camofox.calls("POST", "/tabs") == []

    def test_never_adopts_another_sessions_tab(self, camofox, monkeypatch):
        self._external(monkeypatch)
        camofox.tabs["other"] = {"userId": "shared-user", "listItemId": "another-session", "url": "https://x", "title": "X"}
        camofox.tabs["newer"] = {"userId": "shared-user", "listItemId": "", "url": "https://y", "title": "Y"}
        assert client.get_session("t1")["tab_id"] is None
        assert json.loads(t.browser_navigate("https://example.com", task_id="t1"))["success"] is True
        (created,) = camofox.calls("POST", "/tabs")
        assert created["body"]["listItemId"] == "our-session"
        assert client.get_session("t1")["tab_id"] not in {"other", "newer"}

    def test_adopts_only_the_matching_tab_among_several(self, camofox, monkeypatch):
        self._external(monkeypatch)
        camofox.tabs["ours"] = {"userId": "shared-user", "listItemId": "our-session", "url": "https://x", "title": "X"}
        camofox.tabs["other"] = {"userId": "shared-user", "listItemId": "another-session", "url": "https://y", "title": "Y"}
        assert client.get_session("t1")["tab_id"] == "ours"

    def test_no_adoption_without_the_flag(self, camofox, monkeypatch):
        monkeypatch.setenv("CAMOFOX_USER_ID", "shared-user")
        camofox.tabs["ext-tab"] = {"userId": "shared-user", "listItemId": "x", "url": "https://x", "title": "X"}
        assert client.get_session("t1")["tab_id"] is None


class TestDeadTab:
    """The server reaps idle tabs (and drops all of them on a browser restart) while Hermes still
    holds the tab id."""

    def test_reaped_tab_says_navigate_again_and_forgets_the_id(self, camofox):
        t.browser_navigate("https://example.com", task_id="t1")
        camofox.tabs.clear()
        result = json.loads(t.browser_snapshot(task_id="t1"))
        assert result["success"] is False and "Call browser_navigate" in result["error"]
        assert client.get_session("t1")["tab_id"] is None
        assert json.loads(t.browser_navigate("https://example.com", task_id="t1"))["success"] is True

    def _reopened(self, camofox, monkeypatch):
        monkeypatch.setenv("CAMOFOX_USER_ID", "shared-user")
        monkeypatch.setenv("CAMOFOX_SESSION_KEY", "our-session")
        t.browser_navigate("https://example.com", task_id="t1")
        camofox.tabs.clear()
        camofox.tabs["reopened"] = {"userId": "shared-user", "listItemId": "our-session", "url": "https://x", "title": "X"}

    def test_a_read_continues_in_the_tab_reopened_under_the_session_key(self, camofox, monkeypatch):
        self._reopened(camofox, monkeypatch)
        assert json.loads(t.browser_snapshot(task_id="t1"))["success"] is True
        assert client.get_session("t1")["tab_id"] == "reopened"

    def test_an_input_action_is_not_replayed_on_the_reopened_tab(self, camofox, monkeypatch):
        """The ref came from the old page; on the new one it names some other element."""
        self._reopened(camofox, monkeypatch)
        result = json.loads(t.browser_type("@e7", "hunter2-password", task_id="t1"))
        assert result["success"] is False and "call browser_snapshot" in result["error"]
        assert "https://x" in result["error"]
        assert [c["path"] for c in camofox.calls("POST", "/type")] == ["/tabs/tab-1/type"]
        assert client.get_session("t1")["tab_id"] == "reopened"

    def test_scroll_stops_at_the_replaced_tab_and_says_so(self, camofox, monkeypatch):
        """browser_scroll is five REST steps; the tab-changed refusal of the first must be the result,
        not four silent scrolls of the replacement tab ending in success."""
        self._reopened(camofox, monkeypatch)
        result = json.loads(t.browser_scroll("down", task_id="t1"))
        assert result["success"] is False and "call browser_snapshot" in result["error"]
        assert [c["path"] for c in camofox.calls("POST", "/scroll")] == ["/tabs/tab-1/scroll"]

    def test_scroll_on_a_reaped_tab_says_navigate_again(self, camofox):
        t.browser_navigate("https://example.com", task_id="t1")
        camofox.tabs.clear()
        result = json.loads(t.browser_scroll("down", task_id="t1"))
        assert result["success"] is False and "Call browser_navigate to open the page again" in result["error"]

    def test_a_404_on_a_live_tab_is_reported_as_is(self, camofox):
        t.browser_navigate("https://example.com", task_id="t1")
        camofox.overrides[("POST", "/tabs/tab-1/click")] = (404, {"error": "ref e9 not found"})
        result = json.loads(t.browser_click("@e9", task_id="t1"))
        assert result["success"] is False and "404" in result["error"]
        assert client.get_session("t1")["tab_id"] == "tab-1"
