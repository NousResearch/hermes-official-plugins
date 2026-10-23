<p align="center"><img src="../assets/singularity.jpg" alt="Singularity/Apptainer: official Hermes Agent plugin" width="100%"></p>

# Singularity/Apptainer for Hermes Agent

> Part of the [Hermes Official Plugins](../README.md), Nous Research's official plugins for Hermes Agent. Formerly built into Hermes core as `tools/environments/singularity.py`.

A terminal backend for [Hermes Agent](https://github.com/NousResearch/hermes-agent): the terminal,
file and code tools run inside a [Singularity/Apptainer](https://apptainer.org) container on this
machine. It is meant for HPC clusters and shared machines where Docker isn't available.

- **Hardened:** `--containall --no-home`, so the container gets its own namespaces and never sees your
  home directory. Your skills and registered credential files are bind-mounted read-only.
- **Rootless:** runs as you, through `apptainer` (preferred) or `singularity`.
- **Limits:** `terminal.container_cpu` and `terminal.container_memory` become `--cpus` / `--memory`.
- **Persistence:** with `terminal.container_persistent: true` (the default) each session writes to an
  overlay directory that outlives the container instance until the Hermes process exits; with `false`
  writes go to a tmpfs and vanish with the instance.
- **Screen and browser:** Bot Desktop, `computer_use` and the browser follow the terminal into the
  container (the image needs the desktop stack; `nousresearch/hermes-sandbox:desktop` has it).

## Install

```bash
hermes plugins install singularity
```

or straight from this repo: `hermes plugins install NousResearch/hermes-official-plugins#singularity`.

**Already used Singularity in Hermes?** You don't need to do anything. `hermes update`, or the first
start after updating, installs this plugin from the catalog for every profile whose `terminal.backend`
is `singularity`. Your config keys and env vars are unchanged.

Needs the `apptainer` or `singularity` binary on `PATH`
([install Apptainer](https://apptainer.org/docs/admin/main/installation.html)). Linux only.

## Use it

```bash
hermes config set terminal.backend singularity
```

or pick **Singularity/Apptainer** in `hermes setup terminal`. Settings (all optional):

```yaml
terminal:
  backend: singularity
  singularity_image: "docker://nousresearch/hermes-sandbox:desktop"  # default: Hermes' sandbox image
  container_cpu: 1             # CPU cores (0 = no limit)
  container_memory: 5120       # MB (0 = no limit)
  container_persistent: true   # writable overlay per session (false = tmpfs)
```

| Setting | Env var | What it does |
|---|---|---|
| `terminal.singularity_image` | `TERMINAL_SINGULARITY_IMAGE` | Image to run. `docker://...` URLs are built into a SIF once and cached; an existing `.sif` file (or sandbox directory) is used directly; `library://`, `oras://` work as Apptainer takes them. A bare reference like `python:3.12` means `docker://python:3.12`. config.yaml wins over the env var. |
| | `TERMINAL_SCRATCH_DIR` | Where overlays and the SIF cache live. Default: `/scratch/$USER/hermes-agent` when `/scratch` is writable (HPC convention), else `TERMINAL_SANDBOX_DIR/singularity` (`~/.hermes/sandboxes/singularity`). |
| | `APPTAINER_CACHEDIR` | SIF cache directory (default: `<scratch>/.apptainer`). |
| | `TERMINAL_DISK_WARNING_GB` | Warn in the log when the scratch overlays exceed this many GB (default 500). |

Batch runs (`batch_runner.py`) that give each prompt its own image register it for this backend too.

## What's different from the built-in backend

Nothing you configure. Two internals changed:

- Core's process-exit sweep of `<scratch>/hermes-*` (which removed persistent overlays) now runs in the
  plugin, and only in a process that started a Singularity instance; core no longer creates the
  scratch directory on every exit.
- The write-only `singularity_snapshots.json` file is gone: nothing ever read it.

## Development

```bash
cd singularity
HERMES_AGENT_REPO=../../hermes-agent PYTHONPATH=../../hermes-agent python -m pytest -q
```

On a Hermes core that still ships the built-in backend the plugin registers nothing (core reserves the
`singularity` name and serves it itself), so it is safe to install ahead of the core update.
