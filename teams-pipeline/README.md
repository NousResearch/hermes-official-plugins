<p align="center"><img src="../assets/teams-pipeline.jpg" alt="Teams Meeting Pipeline: official Hermes Agent plugin" width="100%"></p>

# Teams Meeting Pipeline for Hermes Agent

> Part of the [Hermes Official Plugins](../README.md), Nous Research's official plugins for Hermes Agent. Formerly bundled with Hermes core as `teams_pipeline`.

Turns Microsoft Teams meetings into summaries. Microsoft Graph change notifications arrive on the
`msgraph_webhook` gateway platform; each one becomes a durable job that resolves the meeting, fetches
its transcript (or the recording, transcribed with your STT provider when no transcript exists),
summarizes it with your auxiliary model and delivers the summary to Teams. The webhook acks Graph as
soon as the job is persisted; the work runs in the background, and duplicate notifications run a job
once.

> **Notion and Linear are not delivered yet.** The pipeline has Notion and Linear writers, but the
> gateway runtime builds the pipeline with the Teams sender only (`runtime.py` `build_pipeline_runtime`),
> so `notion` / `linear` settings currently write nothing. `hermes teams-pipeline run <job_id>` re-runs
> a stored job with an empty config and no sinks (`cli.py` `_cmd_run`): it re-fetches and re-summarizes
> into the job store and delivers nowhere.

> **Tested with stand-ins only.** No real Microsoft 365 tenant, Notion workspace or Linear team has been
> exercised. See [What the tests cover](#what-the-tests-cover) before relying on a sink or on replay.

**Formerly built into Hermes core; existing users are migrated automatically.** `hermes update` (or
the first start after updating) installs and enables this plugin for every profile that had
`teams_pipeline` enabled. Config, credentials and the job store stay where they were.

## Install

```bash
hermes plugins install teams-pipeline
```

Then enable the `msgraph_webhook` platform in the gateway (it needs `extra.client_state`, and either a
loopback bind behind a tunnel/reverse proxy or `extra.allowed_source_cidrs`) and restart the gateway.

## Credentials

The pipeline reads meeting artifacts with an Entra app registration (client-credentials flow). Set
these in the profile's `.env`:

```bash
MSGRAPH_TENANT_ID=...
MSGRAPH_CLIENT_ID=...
MSGRAPH_CLIENT_SECRET=...
```

Teams delivery reuses the Teams platform's own settings (`delivery_mode`, `incoming_webhook_url`,
`team_id`/`channel_id` or `chat_id`, `access_token`, or the `TEAMS_*` env equivalents). Graph-mode
delivery without an `access_token` uses the app credentials above.

## Config

Pipeline settings live under the Teams platform in `config.yaml`:

```yaml
platforms:
  teams:
    extra:
      meeting_pipeline:
        transcript_min_chars: 80        # shorter transcripts fall back to the recording
        transcription_fallback: true
        stt_model: null                 # default STT model
        notion: {enabled: true, database_id: "..."}   # read, but not delivered yet (see above)
        linear: {enabled: true, team_id: "..."}       # read, but not delivered yet (see above)
        teams_delivery: {}              # overrides mirrored from platforms.teams.extra
```

The job store defaults to `teams_pipeline_store.json` in the profile's Hermes home
(`MSGRAPH_WEBHOOK_STORE_PATH` overrides it).

## Operator CLI

```bash
hermes teams-pipeline list                     # recent jobs
hermes teams-pipeline show <job_id>
hermes teams-pipeline run <job_id>             # re-fetch + re-summarize a stored job; no delivery, no automated test
hermes teams-pipeline fetch ...                # dry-run artifact resolution for a meeting
hermes teams-pipeline subscribe ...            # create a Graph subscription
hermes teams-pipeline subscriptions            # list stored subscriptions
hermes teams-pipeline renew-subscription ...
hermes teams-pipeline delete-subscription <subscription_id>
hermes teams-pipeline maintain-subscriptions   # renew near-expiry subscriptions (cron-friendly)
hermes teams-pipeline token-health [--force-refresh]
hermes teams-pipeline validate                 # config snapshot check
```

`hermes teams-pipeline <command> --help` lists each command's options.

## What the tests cover

All tests run without network access, against stand-ins:

| Path | Covered by | Not covered |
|---|---|---|
| Graph client (token header, 429 retry with `Retry-After`, streamed download, bad JSON) | `tests/test_graph_client.py`, `tests/test_graph_auth.py` (mocked HTTP transport) | a real tenant, real permissions, real subscription validation |
| Meeting lookup and transcript download | `tests/test_teams_pipeline_meetings.py`, resource-parsing tests in `tests/test_teams_pipeline_plugin.py` (stubbed Graph responses) | real meeting IDs and transcript formats from Graph |
| Transcript-first job, recording + STT fallback, retry when nothing is available yet, duplicate notifications | `tests/test_teams_pipeline_plugin.py`, `tests/test_teams_duplicate_job_claim.py` (stubbed Graph calls, ffmpeg, STT and summarizer) | a real recording, real STT, a real auxiliary-model summary |
| Notion delivery | the pipeline records a stand-in Notion writer's page id when one is passed in (`tests/test_teams_pipeline_plugin.py`) | the gateway runtime passing a Notion writer (it does not), the real `NotionWriter` HTTP call to api.notion.com |
| Linear delivery | nothing | the gateway runtime passing a Linear writer (it does not), the `LinearWriter` GraphQL call to api.linear.app |
| Teams delivery | a stand-in sender in the pipeline test; the sender itself lives in core (`plugins/platforms/teams`) | a real Teams post |
| Operator CLI | `show` and `validate` (`tests/test_teams_pipeline_plugin_cli.py`) | `run` (replaying a stored job), `fetch`, the subscription commands, `token-health` |
| Wiring to the `msgraph_webhook` adapter | `tests/test_teams_pipeline_runtime_wiring.py` | a live gateway receiving real Graph notifications |

## Development

```bash
cd teams-pipeline
PYTHONPATH=/path/to/hermes-agent python -m pytest -q
```

## License

MIT
