"""browser_vision must honour the registered auxiliary.camofox_vision task block."""
import json

from camofox_plugin import camofox_tools as t


class _FakeLlm:
    def __init__(self):
        self.calls = []

    def complete(self, messages, **kw):
        self.calls.append(kw)
        return type("R", (), {"text": "A login form"})()


def test_task_block_overrides_the_inherited_vision_settings(camofox, write_config):
    write_config(
        "auxiliary:\n"
        "  vision:\n"
        "    timeout: 45\n"
        "    temperature: 1\n"
        "  camofox_vision:\n"
        "    inherit_from: vision\n"
        "    timeout: 7\n"
        "    temperature: 0.9\n")
    t.browser_navigate("https://example.com", task_id="t1")
    llm = _FakeLlm()
    result = json.loads(t.browser_vision("what is here?", task_id="t1", llm=llm))
    assert result["success"] is True
    assert llm.calls[0]["timeout"] == 7.0 and llm.calls[0]["temperature"] == 0.9
