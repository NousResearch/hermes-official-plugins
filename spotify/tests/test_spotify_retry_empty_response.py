"""A 401 retry must keep the endpoint's explanatory empty-204 payload."""
import spotify_plugin.spotify_client as spotify_mod


class R:
    def __init__(self, status, payload=None, text=""):
        self.status_code, self._payload, self.text = status, payload, text
        self.headers = {"content-type": "application/json"}
        self.content = self.text.encode()

    def json(self):
        return self._payload


def test_401_then_204_keeps_explanatory_payload(monkeypatch):
    tokens = iter([{"access_token": "t1", "base_url": "https://api.spotify.com/v1"},
                   {"access_token": "t2", "base_url": "https://api.spotify.com/v1"}])
    monkeypatch.setattr(spotify_mod, "resolve_runtime_credentials", lambda **kw: next(tokens))
    calls = []

    def request(*_a, **k):
        calls.append(k["headers"]["Authorization"])
        return R(401, {"error": {"message": "expired"}}) if len(calls) == 1 else R(204, None)

    monkeypatch.setattr(spotify_mod.httpx, "request", request)
    out = spotify_mod.SpotifyClient().get_currently_playing()
    assert out == {"status_code": 204, "empty": True,
                   "message": "Spotify is not currently playing anything. Start playback in Spotify and try again."}
    assert calls == ["Bearer t1", "Bearer t2"]
