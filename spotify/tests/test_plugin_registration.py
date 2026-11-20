"""register(ctx) wiring: the seven tools and the ``hermes spotify`` command go through the public plugin
API with the exact names, toolset and schemas a migrated install depends on."""

from __future__ import annotations

import argparse

import pytest

import spotify_plugin
from spotify_plugin import spotify_tools

TOOL_NAMES = ["spotify_playback", "spotify_devices", "spotify_queue", "spotify_search",
              "spotify_playlists", "spotify_albums", "spotify_library"]


class RecordingCtx:
    def __init__(self):
        self.tools, self.cli, self.commands = [], [], []

    def register_tool(self, **kwargs):
        self.tools.append(kwargs)

    def register_cli_command(self, **kwargs):
        self.cli.append(kwargs)

    def register_command(self, name, handler, description="", args_hint=""):
        self.commands.append({"name": name, "handler": handler, "args_hint": args_hint})


@pytest.fixture
def ctx():
    c = RecordingCtx()
    spotify_plugin.register(c)
    return c


def test_registers_the_seven_tools_in_the_spotify_toolset(ctx):
    assert [t["name"] for t in ctx.tools] == TOOL_NAMES
    for tool in ctx.tools:
        assert tool["toolset"] == "spotify"
        assert tool["schema"]["name"] == tool["name"]
        assert tool["check_fn"] is spotify_tools._check_spotify_available


def test_registers_the_spotify_cli_command(ctx):
    (cmd,) = ctx.cli
    assert cmd["name"] == "spotify"
    parser = argparse.ArgumentParser()
    cmd["setup_fn"](parser)
    args = parser.parse_args(["status", "--no-browser"])
    assert (args.action, args.no_browser) == ("status", True)
    assert parser.parse_args([]).action == "login"


def test_tools_are_unavailable_until_logged_in():
    assert spotify_tools._check_spotify_available() is False


def test_registers_one_spotify_slash_command_clear_of_core_names(ctx):
    from hermes_cli.commands import resolve_command
    from spotify_plugin.gateway_commands import spotify_command

    (cmd,) = ctx.commands
    assert cmd["name"] == "spotify" and cmd["handler"] is spotify_command
    assert resolve_command("spotify") is None
