"""#385: native's private cwd and git-status lines never contradict Hermes' runtime section."""
import json

from admission import stable_request_context

MARK = {'type': 'ephemeral', 'ttl': '1h'}


def _payload(env):
    return json.dumps({"messages": [
        {"role": "user", "content": "go"},
        {"role": "system", "content": env},
        {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
    ]}).encode("utf-8")


def test_strips_cwd_and_git_lines_from_a_block_environment():
    env = [{"type": "text", "text": "Primary working directory: /tmp/claude-directsdk-cwd-501\n"
                                    "Is a git repository: false\nPlatform: linux",
            "cache_control": MARK}]
    out = json.loads(stable_request_context(_payload(env), None))
    text = out["messages"][1]["content"][0]["text"]
    assert "Primary working directory" not in text
    assert "Is a git repository" not in text
    assert "Platform: linux" in text
    assert out["messages"][1]["content"][0]["cache_control"] == MARK


def test_strips_from_a_string_environment():
    env = "Primary working directory: /tmp/x\nIs a git repository: false\nPlatform: linux"
    out = json.loads(stable_request_context(_payload(env), None))
    assert out["messages"][1]["content"] == "Platform: linux"


def test_unrelated_text_and_shape_are_untouched():
    env = [{"type": "text", "text": "Platform: linux\nOther guidance"}]
    raw = _payload(env)
    assert stable_request_context(raw, None) == raw
