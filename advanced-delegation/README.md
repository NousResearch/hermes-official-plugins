# Advanced Delegation for Hermes Agent

> Part of the [Hermes Official Plugins](../README.md), Nous Research's official plugins for Hermes Agent.

Official [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin that adds two
**delegation modes** to `delegate_task`, next to the built-in default:

| Mode | `delegation.backend` | What the agent gets |
|---|---|---|
| **Default** (built into Hermes) | `default` | Every subagent runs on `delegation.model` / `delegation.provider`, else the parent's model. |
| **Model routes** | `model_map` | A per-task `option` choice: a named model route from a list you define (e.g. `fast`, `deep`, `local`). |
| **Profile routes** | `profile_routes` | A per-task `profile` choice: the subagent runs as one of your Hermes profiles, with that profile's own model, credentials, skills and `SOUL.md`. |

Only the names and the one-line descriptions you write reach the model. The raw provider, model
id, endpoint and key never become tool parameters, so the agent cannot invent a route.

> [!IMPORTANT]
> Needs a Hermes core with pluggable delegation providers (`ctx.register_delegation_provider`,
> added by the hermes-agent PR *feat(delegation): pluggable delegation providers*, not in a release
> yet). On an older core the plugin installs and loads but does nothing (it logs one warning).
> A plugin catalog listing comes later, once a Hermes release carries that change.

## How it works

The plugin registers two *delegation providers*. Hermes core keeps the whole subagent lifecycle
(spawning, isolation, depth and concurrency limits, background runs, results); a provider only
answers "which route does this task take".

- **Schema.** `delegate_task`'s task items gain one property, `option` (model routes) or `profile`
  (profile routes): an `enum` of your names in config order, whose description lists
  `name: description` for each. Hermes refuses any other value before a subagent starts. With
  nothing configured there is no parameter at all.
- **A task that picks a route** runs on it. A model route resolves exactly like the
  `delegation.*` keys: `provider` uses the credentials you already configured for that provider;
  `base_url` + `model` targets an OpenAI-compatible endpoint. A profile route runs the subagent as
  that profile, exactly as if you had started `hermes -p <profile>` for that task.
- **A task that picks none** runs on the mode's `default` when you set one, otherwise on the
  parent's own model. In these two modes `delegation.model` / `delegation.provider` are not used;
  switching back to Default makes them apply again.
- **Each result reports its route.** A routed task's result carries `route`: the route name
  (`label`) plus the provider, model, reasoning effort and speed tier the subagent actually ran on.

The schema is built when a session starts, so route changes apply to new sessions (the prompt
cache of a running conversation is never disturbed).

## Install

```bash
hermes plugins install NousResearch/hermes-official-plugins#advanced-delegation --enable
```

Plugins are installed per profile; install and configure it in the profile that delegates. Target
profiles of profile routes need nothing installed: they only have to exist (`hermes profile list`)
with a working model.

## Set it up: `hermes delegation config`

```bash
hermes delegation config          # pick the mode, then edit its routes
hermes delegation config --show   # print the current mode and routes (no terminal needed)
```

1. **Pick the mode**: Default, Model routes or Profile routes (the cursor opens on the current one).
2. **Model routes** opens a route editor built from `hermes model`'s own pickers. The list shows each
   route's name, description and `provider · model · reasoning · tier`; pick a route to edit it,
   delete it or make it the default, or **+ Add a route**: a name (what the agent picks), a one-line
   description (what the agent reads), a provider from the ones you have credentials for (or a
   custom endpoint), a model from `hermes model`'s model picker, an optional reasoning effort, and a
   speed tier when the route offers one. A route is validated before it is saved: the provider must
   be known, the effort a real level, and the tier one the route offers. **Done** switches the mode.
3. **Profile routes** is just a checklist of your other profiles, an optional line about each, and
   a default. Profiles keep their own models, so there is nothing else to pick.
4. **Default** switches back; `delegation.model` / `delegation.provider` apply again.

Everything is written to the profile's `config.yaml` with Hermes's own config writer (comments and
other settings are kept). Changes apply to new sessions.

## Configure by hand

Each mode's settings live under `delegation.backends.<mode>`; `delegation.backend` picks the mode.

### Model routes (`model_map`)

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
      default: fast                      # optional: route for tasks that pick none
```

Route keys: `name` and `description` (required), `model` (required), `provider` and/or `base_url`
(at least one), and optionally `reasoning_effort`, `service_tier`, `fallback_providers`, `api_key`
and `api_mode`, each meaning the same as the matching `delegation.*` key. Prefer `provider:` over `api_key:`: provider
credentials stay in `.env` / `auth.json`, while an `api_key` here is stored in `config.yaml` in plain
text. Malformed routes and repeated names are skipped with a warning in `hermes logs`.

`service_tier` is checked like `/fast` against the route (and, for `ultrafast`, your Codex
account): a tier the route does not offer refuses the spawn with a tool error instead of silently
running at another tier. An unknown `reasoning_effort` is refused the same way.

### Profile routes (`profile_routes`)

```yaml
delegation:
  backend: profile_routes
  backends:
    profile_routes:
      profiles:                 # allowlist AND what the agent reads about each profile
        - name: coder
          description: Writes and refactors code; runs tests.
        - name: researcher
          description: Web research and long-form summaries.
      default: coder            # optional: profile for tasks that pick none
```

The list is also the allowlist: profiles you do not list can never be picked. A listed name with no
matching profile on disk is refused with a pointer to `hermes profile list`.

> [!WARNING]
> Listing a profile lets this profile's subagents use that profile's credentials, credential pool,
> `SOUL.md` and terminal policy. Only list profiles whose keys and terminal access you are happy for
> this profile's agent to use.

### From the command line

```bash
hermes config set delegation.backend model_map            # or profile_routes, or default
hermes config set delegation.backends.model_map.options '[{name: fast, description: "Cheap and quick.", provider: openrouter, model: google/gemini-3-flash-preview}]'
hermes config set delegation.backends.model_map.default fast
hermes config set delegation.backends.profile_routes.profiles '[{name: coder, description: "Writes and refactors code."}]'
```

## Development

```bash
cd advanced-delegation
HERMES_AGENT_REPO=~/src/hermes-agent PYTHONPATH=~/src/hermes-agent python -m pytest -q
```

Tests that need the core seam skip themselves on a core without `agent.delegation_provider`; the
old-core test always runs.
