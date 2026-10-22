# Delegation Profiles for Hermes Agent

> Part of the [Hermes Official Plugins](../README.md), Nous Research's official plugins for Hermes Agent.

Official [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin that lets the agent run
each `delegate_task` subagent **as one of your Hermes profiles**. You list the profiles it may use,
each with a one-line description; `delegate_task` gains a per-task `profile` choice, and a subagent
given one runs with that profile's own config, model and credentials, skills, `SOUL.md` persona and
terminal policy, exactly as if you had started `hermes -p <profile>` for that task.

Typical setups: a `coder` profile on a strong coding model with your engineering skills, a
`researcher` profile on a cheap long-context model with web tools, a `reviewer` profile with a
stricter persona. The main agent decides per task which one fits, from the descriptions you wrote.

> [!IMPORTANT]
> Needs a Hermes core with pluggable delegation providers (`ctx.register_delegation_provider`,
> added by the hermes-agent PR *feat(delegation): pluggable delegation providers*, not in a release
> yet). On an older core the plugin installs and loads but does nothing (it logs one warning).
> A plugin catalog listing comes later, once a Hermes release carries that change.

## How it works

The plugin is a *delegation provider*: Hermes core keeps the whole subagent lifecycle (spawning,
isolation, depth and concurrency limits, background runs, results), and the provider only answers
"which route does this task take".

- **Schema.** `delegate_task`'s task items gain a `profile` property: an `enum` of the names in
  `delegation.backends.profile_routes.profiles`, in config order, whose description lists
  `name: description` for each. Hermes refuses any other value before a subagent starts. With no
  profiles listed there is no parameter at all.
- **A task with `profile` set** runs as that profile. A listed name with no matching profile on disk
  is refused with a pointer to `hermes profile list`.
- **A task without `profile`** runs as `delegation.backends.profile_routes.default` when you set
  one, otherwise on the normal `delegation.*` route (`delegation.model` / `delegation.provider`,
  else the parent's).
- **Each result reports its route.** A routed task's result carries `route`: the profile name
  (`label`) plus the provider, model, reasoning effort and speed tier the subagent actually ran on.
- The list is also the allowlist: profiles you do not list can never be picked.

The schema is built when a session starts, so changes to the list apply to new sessions (the
prompt cache of a running conversation is never disturbed).

## Install

```bash
hermes plugins install NousResearch/hermes-official-plugins#delegation-profiles --enable
```

Plugins are installed per profile; install and configure it in the profile that delegates. The
target profiles need nothing installed: they only have to exist (`hermes profile list`) with a
working model.

## Configure

The plugin's settings live under `delegation.backends.profile_routes`; `delegation.backend` selects
it.

```yaml
# config.yaml of the profile that delegates
delegation:
  backend: profile_routes
  backends:
    profile_routes:
      profiles:                 # allowlist AND what the agent reads about each profile
        - name: coder
          description: Writes and refactors code; runs tests.
        - name: researcher
          description: Web research and long-form summaries.
      default: coder            # optional: profile for tasks that set none
```

> [!WARNING]
> Listing a profile in `delegation.backends.profile_routes` lets this profile's subagents use that
> profile's credentials, credential pool, `SOUL.md` and terminal policy. Only list profiles whose
> keys and terminal access you are happy for this profile's agent to use.

The same from the command line:

```bash
hermes config set delegation.backend profile_routes
hermes config set delegation.backends.profile_routes.profiles '[{name: coder, description: "Writes and refactors code; runs tests."}, {name: researcher, description: "Web research and long-form summaries."}]'
hermes config set delegation.backends.profile_routes.default coder   # optional
```

Entries without a `name`, and repeated names, are skipped with a warning in `hermes logs`. Switch
back to the built-in behaviour with `hermes config set delegation.backend default`.

## Development

```bash
cd delegation-profiles
HERMES_AGENT_REPO=~/src/hermes-agent PYTHONPATH=~/src/hermes-agent python -m pytest -q
```

The provider's tests skip themselves on a core without `agent.delegation_provider`; the
old-core test always runs.
