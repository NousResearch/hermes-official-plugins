"""#457: the client reports the output cap it will send, so cache warming can run."""
import directsdk


def _client():
    return directsdk.Client(command="claude")


def test_reports_the_cap_it_will_send():
    client = _client()
    assert client.effective_output_cap(max_tokens=1) == 1
    assert client.effective_output_cap(max_completion_tokens=16) == 16
    assert client.effective_output_cap(extra_body={"max_tokens": 8}) == 8


def test_unbounded_or_rejected_calls_are_above_any_replay_cap():
    client = _client()
    assert client.effective_output_cap() > 16
    assert client.effective_output_cap(max_tokens=0) > 16
    assert client.effective_output_cap(max_tokens=1, max_completion_tokens=1) > 16
