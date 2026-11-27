"""Tests that xchat.cli._read_marker returns {} for non-dict JSON files."""
import pytest

from xchat_plugin import cli as xchat_cli


@pytest.mark.parametrize("raw", ["null", "[]", '"bad"', "123", "{corrupt"])
def test_read_marker_returns_empty_dict_for_non_dict_or_corrupt_json(tmp_path, monkeypatch, raw):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    marker = xchat_cli._marker_path()
    marker.write_text(raw, encoding="utf-8")

    assert xchat_cli._read_marker() == {}
