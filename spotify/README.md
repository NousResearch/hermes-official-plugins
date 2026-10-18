<p align="center"><img src="../assets/spotify.jpg" alt="Spotify: official Hermes Agent plugin" width="100%"></p>

# Spotify for Hermes Agent

> Part of the [Hermes Official Plugins](../README.md), Nous Research's official plugins for Hermes Agent. Formerly [`NousResearch/hermes-spotify`](https://github.com/NousResearch/hermes-spotify), which is archived; this directory carries its full history.

Official [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin for
[Spotify](https://www.spotify.com/). It gives the agent seven tools over the Spotify Web API and adds
a `hermes spotify` command for logging in:

| Tool | What it does |
|---|---|
| `spotify_playback` | Play, pause, skip, seek, shuffle, repeat, volume, current state, recently played |
| `spotify_devices` | List Spotify Connect devices, transfer playback |
| `spotify_queue` | Read the queue, add to it |
| `spotify_search` | Search tracks, albums, artists, playlists, shows, episodes |
| `spotify_playlists` | List, read, create, edit playlists; add and remove items |
| `spotify_albums` | Album metadata and track lists |
| `spotify_library` | Saved tracks and albums: list, save, remove |

This used to be built into Hermes core. It now ships as a standalone plugin with the same toolset
(`spotify`), tool names and schemas, env vars and stored login. **If you already used Spotify with
Hermes, you don't need to do anything:** `hermes update` (or the first start after updating) installs
and enables this plugin for you and your login keeps working. The one change is the login command:
`hermes auth spotify` is now `hermes spotify login`.

## Install

From the Hermes plugin catalog:

```bash
hermes plugins install spotify
hermes tools enable spotify      # the toolset is opt-in
hermes spotify login
```

Plugins are installed per profile: use `hermes -p <profile> plugins install spotify` for another
profile. The only Python dependencies are `httpx` and `cryptography`, both of which Hermes already
ships.

## Logging in

Spotify requires every user to register their own lightweight developer app (Spotify does not allow
a shared public client). `hermes spotify login` walks you through it the first time:

1. It opens <https://developer.spotify.com/dashboard>. Click **Create app** and use
   `https://127.0.0.1:43827/spotify/callback` as the **Redirect URI**, with **Web API** checked.
   Spotify rejects `http://` redirect URIs since November 2025, so the loopback listener serves
   **https with a self-signed certificate**: your browser shows the usual "not private" warning
   once — accept it and continue, the connection never leaves your machine.
2. Paste the app's **Client ID** at the prompt. It is saved as `HERMES_SPOTIFY_CLIENT_ID` in the
   profile's `.env`, so later logins skip this step.
3. Approve access in the browser. Tokens are stored under `providers.spotify` in the profile's
   `auth.json` and refreshed automatically.

```bash
hermes spotify login [--client-id ID] [--redirect-uri URI] [--scope "..."] [--no-browser] [--timeout S]
hermes spotify status
hermes spotify logout
```

Over SSH, forward the callback port first (`ssh -N -L 43827:127.0.0.1:43827 user@host`) and run
`hermes spotify login --no-browser`. See
[OAuth over SSH](https://hermes-agent.nousresearch.com/docs/guides/oauth-over-ssh).

| Variable | Purpose |
|---|---|
| `HERMES_SPOTIFY_CLIENT_ID` | Your Spotify app's Client ID (the login wizard sets it) |
| `HERMES_SPOTIFY_REDIRECT_URI` | Redirect URI, if you registered something other than the default |

## Free vs Premium

Search, playlists, albums, library, queue reads, device lists and playback state work on a Free
account. Playback control (play, pause, skip, seek, volume, shuffle/repeat, queue add, transfer)
needs Premium and an active Spotify Connect device: open Spotify on any phone, desktop or speaker.

Full guide: <https://hermes-agent.nousresearch.com/docs/user-guide/features/spotify>

## Development

The tests import Hermes core, so run them with a hermes-agent checkout on `PYTHONPATH`:

```bash
PYTHONPATH=/path/to/hermes-agent python -m pytest
```

## License

MIT. Originally contributed to Hermes Agent core by @dlkakbs (NousResearch/hermes-agent#15096); maintained by Nous Research.
