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

    def test_ephemeral_identity_is_reproducible_for_the_same_task(self, camofox):
        # A fresh process starts with an empty session map; the same task
        # must still resolve to the same browser user, or the tab its
        # previous call opened is invisible to the next one.
        first = client.get_session("t1")["user_id"]
        client.drop_session("t1")
        assert client.get_session("t1")["user_id"] == first
        assert client.get_session("t2")["user_id"] != first

    def test_a_later_call_in_a_fresh_process_finds_the_navigated_tab(self, camofox):
        # Default config (no adopt_existing_tab): issue #215's navigate -> snapshot across processes.
        assert json.loads(t.browser_navigate("https://example.com", task_id="t1"))["success"] is True
        client._sessions.clear()
        assert json.loads(t.browser_snapshot(task_id="t1"))["success"] is True
        assert len(camofox.calls("POST", "/tabs")) == 1

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


class TestDurableAgentIdentity:
    """A cron job outlives a run, so it can own one Camofox profile across every fire — opt-in,
    because that writes the job's cookies to disk."""

    ENABLED = "browser:\n  camofox:\n    durable_agent_identity: true\n"

    def test_the_same_job_keeps_one_identity_across_runs(self, camofox, write_config):
        write_config(self.ENABLED)
        first = client.get_session("cron:job123:exec-a")
        second = client.get_session("cron:job123:exec-b")
        assert first["user_id"] == second["user_id"]
        assert first["session_key"] == second["session_key"]
        assert first["user_id"] == client.get_agent_identity("cron:job123")["user_id"]
        # Neither the profile-scoped jar nor the task-scoped one: a job's cookies are its own.
        assert first["user_id"] != client.get_camofox_identity("cron:job123:exec-a")["user_id"]

    def test_off_by_default_leaves_cron_on_the_profile_identity(self, camofox, write_config):
        """Nothing about a default cron fire changes: no profile of its own, no cookies on disk."""
        write_config("browser:\n  camofox:\n    managed_persistence: true\n")
        session = client.get_session("cron:job123:exec-a")
        assert session["user_id"] == client.get_camofox_identity("cron:job123:exec-a")["user_id"]
        assert session["user_id"] != client.get_agent_identity("cron:job123")["user_id"]

    def test_two_jobs_never_share_a_profile(self, camofox, write_config):
        write_config(self.ENABLED)
        a = client.get_session("cron:joba:exec-a")
        b = client.get_session("cron:jobb:exec-a")
        assert (a["user_id"], a["session_key"]) != (b["user_id"], b["session_key"])

    def test_one_off_tasks_do_not_get_a_durable_identity(self, camofox, write_config):
        write_config(self.ENABLED)
        assert client.agent_key_from_task("subagent-0-abc123") is None
        assert client.agent_key_from_task("cron:") is None
        subagent = client.get_session("subagent-0-abc123")
        assert subagent["user_id"].startswith("hermes_")
        assert subagent["user_id"] != client.get_agent_identity("cron:abc123")["user_id"]

    def test_adoption_stays_the_agents_own_choice(self, camofox, write_config):
        """A durable identity does not silently force tab adoption: an opt-in flag is still a flag."""
        write_config(self.ENABLED)
        assert client.get_session("cron:job7:exec-a")["adopt_existing_tab"] is False
        client.drop_session("cron:job7:exec-a")
        write_config(self.ENABLED + "    adopt_existing_tab: true\n")
        assert client.get_session("cron:job7:exec-a")["adopt_existing_tab"] is True

    def test_forget_closes_the_server_session_through_the_api(self, camofox, write_config):
        write_config(self.ENABLED)
        t.browser_navigate("https://example.com", task_id="cron:job9:exec-a")
        user_id = client.get_agent_identity("cron:job9")["user_id"]

        result = client.forget_agent_identity("cron:job9")

        assert result["session_closed"] is True
        assert [c["path"] for c in camofox.calls("DELETE") if c["path"].startswith("/sessions/")] == \
            [f"/sessions/{user_id}"]
        assert client.get_session("cron:job9:exec-b")["tab_id"] is None

    def test_end_of_session_cleanup_keeps_the_jobs_profile(self, camofox, write_config):
        """A fire ends without killing its own session: the next fire lands in the same profile,
        on the same tab."""
        write_config(self.ENABLED + "    adopt_existing_tab: true\n")
        t.browser_navigate("https://example.com", task_id="cron:job5:exec-a")
        tab_id = client.get_session("cron:job5:exec-a")["tab_id"]

        t.cleanup_task("cron:job5:exec-a")

        assert camofox.calls("DELETE") == []
        again = client.get_session("cron:job5:exec-b")
        assert again["user_id"] == client.get_agent_identity("cron:job5")["user_id"]
        assert again["tab_id"] == tab_id

    def test_without_the_opt_in_cleanup_still_closes_the_session(self, camofox):
        t.browser_navigate("https://example.com", task_id="cron:job5:exec-a")
        t.cleanup_task("cron:job5:exec-a")
        assert client.get_session("cron:job5:exec-b")["tab_id"] is None

    def test_forget_without_a_server_does_not_raise(self, camofox, write_config, monkeypatch):
        """Teardown is best-effort against a server that may already be gone."""
        write_config(self.ENABLED)
        monkeypatch.delenv("CAMOFOX_URL")
        assert client.forget_agent_identity("cron:job9")["session_closed"] is False
