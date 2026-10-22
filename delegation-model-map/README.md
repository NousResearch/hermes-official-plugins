# Delegation Model Map for Hermes Agent

> Part of the [Hermes Official Plugins](../README.md), Nous Research's official plugins for Hermes Agent.

Official [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin that lets the agent
**pick a model for each `delegate_task` subagent** from a short list you define. Each entry has a
name and a one-line description the agent reads, plus the route it maps to: a provider and/or an
endpoint, a model, and optionally a reasoning effort and speed tier. `delegate_task` gains a
per-task `option` choice; the agent sends quick lookups to the cheap model and hard design work to
the careful one, without ever seeing (or being able to invent) a provider, model id, endpoint or key.

> [!IMPORTANT]
> Needs a Hermes core with pluggable delegation providers (`ctx.register_delegation_provider`,
> added by the hermes-agent PR *feat(delegation): pluggable delegation providers*, not in a release
> yet). On an older core the plugin installs and loads but does nothing (it logs one warning).
> A plugin catalog listing comes later, once a Hermes release carries that change.

## How it works

The plugin is a *delegation provider*: Hermes core keeps the whole subagent lifecycle (spawning,
isolation, depth and concurrency limits, background runs, results), and the provider only answers
"which route does this task take".

- **Schema.** `delegate_task`'s task items gain an `option` property: an `enum` of the names in
  `delegation.backends.model_map.options`, in config order, whose description lists
  `name: description` for each. Hermes refuses any other value before a subagent starts. With no
  valid options there is no parameter at all. Only names and descriptions reach the model.
- **A task with `option` set** runs on that option's route. The route is resolved exactly like the
  `delegation.*` keys: `provider` uses the credentials you already configured for that provider, as
  `delegation.provider` does; `base_url` + `model` targets an OpenAI-compatible endpoint.
- **A task without `option`** runs on `delegation.backends.model_map.default` when you set one,
  otherwise on the normal `delegation.*` route (`delegation.model` / `delegation.provider`, else the
  parent's).
- **Each result reports its route.** A routed task's result carries `route`: the option name
  (`label`) plus the provider, model, reasoning effort and speed tier (`service_tier`) the subagent
  actually ran on.

The schema is built when a session starts, so changes to the options apply to new sessions (the
prompt cache of a running conversation is never disturbed).

## Install

```bash
hermes plugins install NousResearch/hermes-official-plugins#delegation-model-map --enable
```

Plugins are installed per profile; install and configure it in the profile that delegates.

## Configure

The plugin's settings live under `delegation.backends.model_map`; `delegation.backend` selects it.

```yaml
delegation:
  backend: model_map
  backends:
    model_map:
      options:
        - name: fast                     # what the agent picks
          description: "Cheap and quick: lookups, summaries, simple edits."
          provider: openrouter           # provider and/or base_url, plus model
          model: google/gemini-3-flash-preview
        - name: deep
          description: "Slow and careful: hard debugging and design work."
          provider: anthropic
          model: claude-opus-5
          reasoning_effort: high
          service_tier: fast             # optional speed tier: normal, fast or ultrafast
        - name: local
          description: "Private: runs on this machine."
          base_url: http://localhost:8080/v1
          model: qwen3-coder
      default: fast                      # optional: option for tasks that set none
```

Option keys: `name` and `description` (required), `model` (required), `provider` and/or `base_url`
(at least one), and optionally `api_key`, `api_mode`, `reasoning_effort` and `service_tier`, each
meaning the same as the matching `delegation.*` key. Prefer `provider:` over `api_key:`: provider
credentials stay in `.env` / `auth.json`, while an `api_key` here is stored in `config.yaml` in plain
text. Malformed options and repeated names are skipped with a warning in `hermes logs`.

`service_tier` is checked like `/fast` against the option's resolved route (and, for `ultrafast`,
your Codex account): a tier the route does not offer refuses the spawn with a tool error instead of
silently running at another tier. An unknown `reasoning_effort` is refused the same way.

The same from the command line:

```bash
hermes config set delegation.backend model_map
hermes config set delegation.backends.model_map.options '[{name: fast, description: "Cheap and quick: lookups, summaries, simple edits.", provider: openrouter, model: google/gemini-3-flash-preview}, {name: deep, description: "Slow and careful: hard debugging and design work.", provider: anthropic, model: claude-opus-5, reasoning_effort: high}]'
hermes config set delegation.backends.model_map.default fast   # optional
```

Switch back to the built-in behaviour with `hermes config set delegation.backend default`.

## Development

```bash
cd delegation-model-map
HERMES_AGENT_REPO=~/src/hermes-agent PYTHONPATH=~/src/hermes-agent python -m pytest -q
```

The provider's tests skip themselves on a core without `agent.delegation_provider`; the
old-core test always runs.
