"""Tests that malformed JSON in assistant tool_calls arguments degrades cleanly instead of raising JSONDecodeError (#536)."""
import logging

import directsdk


def test_prepare_history_degrades_malformed_tool_call_arguments(caplog):
    bad_args = '{"path": "foo.py", "content": "unterminated'
    messages = [
        {"role": "user", "content": "write foo.py"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "write_file", "arguments": bad_args},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
    ]

    with caplog.at_level(logging.WARNING):
        _system, frames = directsdk.prepare_history(messages)

    assistant_blocks = frames[1]["message"]["content"]
    assert assistant_blocks == [
        {
            "type": "tool_use",
            "id": "call_1",
            "name": f"{directsdk.PREFIX}write_file",
            "input": {"_unparsed_arguments": bad_args},
        }
    ]
    assert "Malformed JSON in assistant tool_calls arguments" in caplog.text


def test_prepare_history_treats_corrupted_carrier_arguments_as_mismatch_and_caps_raw_text():
    long_bad_args = '{"content": "' + ("x" * 5000)
    messages = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_2",
                    "type": "function",
                    "function": {"name": "write_file", "arguments": long_bad_args},
                }
            ],
            "reasoning_details": [
                {
                    "type": directsdk.CARRIER,
                    "version": 1,
                    "projection": {
                        "content": "",
                        "tool_calls": [{"id": "call_2", "name": "write_file", "input": {"content": "valid"}}],
                    },
                    "messages": [{"role": "assistant", "content": [{"type": "thinking", "thinking": "stale"}]}],
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_2", "content": "ok"},
    ]

    _system, frames = directsdk.prepare_history(messages)
    tool_use = frames[1]["message"]["content"][0]
    assert tool_use["type"] == "tool_use"
    assert len(tool_use["input"]["_unparsed_arguments"]) == directsdk._UNPARSED_ARGUMENTS_CAP
