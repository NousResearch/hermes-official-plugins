"""#467: Claude Code never expands @path mentions inside a Hermes turn."""
from test_directsdk_telemetry import _turn_env


def test_file_mentions_are_disabled_even_if_the_host_enables_them(profile, tmp_path):
    # Host-level managed settings may turn mentions on; the child for a Hermes turn must not.
    env = _turn_env(profile, tmp_path, CLAUDE_CODE_DISABLE_FILE_MENTIONS='0')
    assert env['CLAUDE_CODE_DISABLE_FILE_MENTIONS'] == '1'
