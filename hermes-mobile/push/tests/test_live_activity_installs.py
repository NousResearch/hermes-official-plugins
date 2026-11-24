"""One gateway process serving profiles paired with different relay installs."""
import io
import unittest
import urllib.error

from push_plugin import load_activity
from test_live_activity import tracker

activity = load_activity()

HOME = ("https://relay.example", "0123456789abcdef0123456789abcdef")
WORK = ("https://relay.example", "fedcba9876543210fedcba9876543210")


class SeparateInstallTests(unittest.TestCase):
    def posts_to(self, relay, install):
        return [snapshot for posted, snapshot in relay.posts if posted == install]

    def test_each_install_gets_only_its_own_sessions_and_profile(self):
        t, _clock, relay = tracker()
        t.begin("home-1", profile="default", relay=HOME)
        t.begin("work-1", profile="work", relay=WORK)
        t.flush()
        home, = self.posts_to(relay, HOME)
        work, = self.posts_to(relay, WORK)
        self.assertEqual(home["profile_key"], activity.opaque_key(HOME[1], "profile", "default"))
        self.assertEqual(work["profile_key"], activity.opaque_key(WORK[1], "profile", "work"))
        self.assertEqual(home["agents"], [{
            "session_key": activity.opaque_key(HOME[1], "session", "home-1"), "started_at": home["agents"][0]["started_at"],
            "tool_calls": 0, "attention": False, "status": "thinking"}])
        self.assertEqual(work["agents"], [{
            "session_key": activity.opaque_key(WORK[1], "session", "work-1"), "started_at": work["agents"][0]["started_at"],
            "tool_calls": 0, "attention": False, "status": "thinking"}])

    def test_a_change_on_one_install_posts_only_there(self):
        t, _clock, relay = tracker()
        t.begin("home-1", profile="default", relay=HOME)
        t.begin("work-1", profile="work", relay=WORK)
        t.flush()
        t.end("work-1")
        t.flush()
        self.assertEqual(len(self.posts_to(relay, HOME)), 1)
        self.assertEqual(self.posts_to(relay, WORK)[-1]["agents"], [])
        self.assertEqual(len(self.posts_to(relay, HOME)[-1]["agents"]), 1)

    def test_an_old_relay_install_backs_off_alone(self):
        t, _clock, relay = tracker()
        def post(install, snapshot):
            if install == WORK:
                raise urllib.error.HTTPError("https://relay.example", 404, "Not Found", {}, io.BytesIO(b""))
            relay.posts.append((install, snapshot))
        t._post = post
        t.begin("work-1", profile="work", relay=WORK)
        with self.assertLogs(activity.logger.name, "INFO"):
            self.assertEqual(t.flush(), activity.BACKOFF_SECONDS)
        t.begin("home-1", profile="default", relay=HOME)
        self.assertEqual(t.due(), 0.0)
        t.flush()
        self.assertEqual(len(self.posts_to(relay, HOME)), 1)

    def test_unload_says_idle_to_every_install_that_saw_work(self):
        t, _clock, relay = tracker()
        t.begin("home-1", profile="default", relay=HOME)
        t.begin("work-1", profile="work", relay=WORK)
        t.flush()
        t.unload()
        self.assertEqual(self.posts_to(relay, HOME)[-1]["agents"], [])
        self.assertEqual(self.posts_to(relay, WORK)[-1]["agents"], [])


if __name__ == "__main__":
    unittest.main()
