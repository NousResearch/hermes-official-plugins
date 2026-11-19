"""The installed profile must opt in to Hermes's streaming dispatch."""
from types import SimpleNamespace


def test_profile_routes_through_host_streaming(profile):
    from agent.turn_api_call import _should_stream

    agent = SimpleNamespace(provider=profile.name, base_url=profile.base_url,
                            _disable_streaming=False, _has_stream_consumers=lambda: True)
    assert _should_stream(agent) is True
