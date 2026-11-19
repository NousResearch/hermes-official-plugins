# Messaging platforms (Telegram, Discord, Slack, WhatsApp, Signal, Matrix ...)

## Find HERMES_HOME

Run `hermes config path`. HERMES_HOME is the folder holding the `config.yaml` it prints; the CLI
already resolves the active profile, so never assume `~/.hermes`. Use that absolute path below
(on Windows a `C:\...` path, which `MEDIA:` accepts).

## Send the thumbnail as a photo

```bash
curl -fsSL --create-dirs -o "<HERMES_HOME>/cache/images/meshy-<task_id>.png" "<thumbnail_url>" &&
  realpath "<HERMES_HOME>/cache/images/meshy-<task_id>.png"
```

Then reply with the path `realpath` printed, copied verbatim, on its own line (never shorten it
to `~/.hermes/...`):

```
Here's the preview of your treasure chest.
MEDIA:<HERMES_HOME>/cache/images/meshy-<task_id>.png
```

A `.png`/`.jpg` attachment goes out as a photo. `cache/images` is one of the gateway's media
delivery folders, so this works in strict media mode too; it is a cache, so download right before
you reply. Never `![preview](<url>)`: on a streaming gateway the image is
dropped and the raw markdown stays in the message. When the download fails, the link has expired:
`meshy_get_task_status` returns a fresh `thumbnail_url`. If it still fails, send no image rather
than the URL.

## Long jobs: don't hold the chat

Generation, refine, rig and animation take about 1–5 minutes, and a blocking wait keeps the chat
silent for all of it. On a messaging platform:

1. As soon as the create returns its `task_id`, reply in one line: "Started <what> (task <id>),
   about N min. I'll send it here when it's done." Do not call `meshy_get_task_status` in this
   turn (its default `wait: true` blocks until the job ends).
2. In the same turn, arm a wake sized to the job:
   `terminal(command="sleep 180 && echo meshy task <task_id>", background=true, notify_on_complete=true)`
   (text or image to 3D 180, refine 150, rig or animation 120, remesh or convert 60).
   `background=true` and `notify_on_complete=true` are both required: a sleep without them runs in
   the foreground and holds the chat exactly like `wait: true`. When the
   sleep exits, Hermes posts its completion into this chat as a new turn for you; the command line
   in that message carries the task id.
3. On that turn, check once: `meshy_get_task_status` with `wait: false`. Finished: deliver it
   (download, thumbnail, file, next steps). Still running: arm one more sleep sized from its
   progress, but stop after about 15 minutes of waiting in total and tell the user the task id and
   that it is still running on Meshy. Failed: say so with Meshy's message; never re-create it on
   your own.
4. If the user writes in the meantime, answer them. For "is it done?" check with `wait: false`
   (never `wait: true`, which holds the chat again) and deliver now if it finished; the wake that
   arrives later then gets a one-line "already sent above", not a second delivery. One wake per
   task: don't arm another while one is pending.
5. If the terminal tool is not available here, or its result says async notifications are
   unsupported (`notify_unsupported`: cron jobs, `hermes chat -q`), no wake will come: wait inline
   with `wait: true` instead.

`meshy_creative_lab` waits inside the call (prototype, then build: about 10 minutes at its default
`timeout_seconds`, up to 20) and cannot be backgrounded. Say how long it may take before you call it.
