# Bubblewrap terminal backend

![Bubblewrap](../assets/bubblewrap.jpg)

Part of [Hermes Official Plugins](https://github.com/NousResearch/hermes-official-plugins): built and supported by Nous Research.

The `bubblewrap` backend runs every `terminal` and `execute_code` command inside its own
[bubblewrap](https://github.com/containers/bubblewrap) (`bwrap`) sandbox on the machine Hermes runs on.
It is not a container: the sandbox sees the host filesystem read-only at the host paths, can write to
the working directory, and cannot read your credentials. It needs no image, no daemon and no network
round trip, so it fits a personal machine or a small server where the `local` backend is too open and
Docker is too heavy.

**Linux only.** Needs bubblewrap 0.9.0 or later, `prlimit` from util-linux (installed on every
mainstream distribution) and unprivileged user namespaces (or a setuid `bwrap`).

## Install

```sh
sudo apt install bubblewrap     # Debian, Ubuntu
sudo dnf install bubblewrap     # Fedora
sudo pacman -S bubblewrap       # Arch

hermes plugins install bubblewrap
hermes config set terminal.backend bubblewrap
```

`hermes plugins install NousResearch/hermes-official-plugins#bubblewrap` installs straight from this
repo's `main`. `hermes setup` lists the backend once the plugin is installed and asks for the profile.
`hermes doctor` reports whether `bwrap` is found, its version, and whether the sandbox probe passes. If
the probe fails, unprivileged user namespaces are disabled for your user: check your distribution's
notes on enabling them for bubblewrap (on Ubuntu 24.04 and later this is the AppArmor
`kernel.apparmor_restrict_unprivileged_userns` restriction).

When `bwrap` is missing or the probe fails, the terminal tool returns a degraded result that names the
package (or an error under `terminal.degraded_mode: fail`). Commands are never run outside the sandbox.

## Settings

All settings live under `plugins.entries.bubblewrap.settings` (Desktop: Settings ▸ Plugins ▸
Bubblewrap). From the command line:

```sh
hermes config set plugins.entries.bubblewrap.settings.profile workspace    # restricted | workspace | network
hermes config set plugins.entries.bubblewrap.settings.memory_mb 2048       # 0 disables
hermes config set plugins.entries.bubblewrap.settings.cpu_seconds 600      # 0 disables
hermes config set plugins.entries.bubblewrap.settings.max_procs 256        # 0 disables
hermes config set plugins.entries.bubblewrap.settings.binds '[{"src": "/data/models"}, {"src": "/srv/scratch", "readonly": false}]'
```

Settings are read each time a sandbox environment is created, per profile. `terminal.cwd`,
`terminal.home_mode`, `terminal.timeout` and `terminal.sandbox_dir` are the usual core terminal keys.

## What bwrap isolates

Each command gets:

- **The host root, read-only**, at the same paths as on the host. `/usr/bin`, `/etc`, your project
  checkouts and your installed toolchains are all there; nothing outside the writable set can be
  changed.
- **A private pid namespace and `/proc`**: the command cannot see or signal host processes. Every other
  namespace is unshared too (IPC, UTS, cgroup, user, and network unless the profile shares it).
- **A fresh `/dev` and a fresh `/tmp` per command.** Nothing written to `/tmp` survives the command; use
  the working directory, or `$TMPDIR` (the Hermes scratch directory, which stays reachable).
- **No agent or bus sockets**: `/run/user/<uid>` is an empty tmpfs, so the gpg-agent, ssh-agent,
  keyring and D-Bus sockets there are unreachable, and `SSH_AUTH_SOCK`, `GPG_AGENT_INFO` and
  `DBUS_SESSION_BUS_ADDRESS` are removed from the environment (again after your shell rc files run).
  The docker socket, if present, is replaced by an empty file.
- **Hidden credentials** (below): directories show as empty, files as zero-length.
- **Resource limits** per command (memory, CPU time, processes), applied by `prlimit` before `bwrap`
  starts, so nothing runs in between.
- **`--die-with-parent` and `--new-session`**: a sandbox dies with Hermes and cannot inject input into
  the controlling terminal.

`cd` and exported variables carry over between commands exactly as with the `local` backend; the
environment is the one the `local` backend builds (`env_passthrough` applies, provider API keys stay
out, `HOME` follows `terminal.home_mode`).

### Hidden paths

These entries in your home directory are hidden inside every sandbox:

```
.ssh  .aws  .gnupg  .gpg  .config/gcloud  .azure  .docker  .kube  .npmrc  .pypirc  .netrc  .env
```

The Hermes home (`HERMES_HOME`, normally `.hermes` in your home directory) is hidden as well. Two
directories under it stay reachable at their own paths: the backend's own state directory
(`HERMES_HOME/sandboxes/bwrap-*`) and the Hermes scratch directory (`HERMES_HOME/cache/scratch`, which
is `TMPDIR` for every command, writable whenever the working directory is). With
`terminal.home_mode: profile`, `HERMES_HOME/home` is the subprocess `HOME` and stays readable and
writable; the rest of `HERMES_HOME` stays hidden. When `HERMES_HOME` points elsewhere (a profile under
`.hermes/profiles/<name>`), the default `.hermes` is hidden too, so a profile's sandbox cannot read the
default home's `.env` or `auth.json`.

A path that does not exist on the host is skipped. An entry that is a symlink (a dotfiles repository
linking `.ssh` to `dotfiles/ssh`) is hidden at its target, resolved when the backend starts, and the
parents of hidden paths inside a writable directory are pinned so a command cannot rename them out
from under the overlay. Writes into a hidden directory land in the sandbox's copy and never reach the
host.

One exception: a CA bundle named by `SSL_CERT_FILE`, `SSL_CERT_DIR`, `HERMES_CA_BUNDLE`,
`REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE`, `NODE_EXTRA_CA_CERTS`, `GIT_SSL_CAINFO` or `PIP_CERT` that sits
inside a hidden path (Hermes exports `SSL_CERT_FILE` pointing at the certifi bundle in its venv under
the Hermes home) is bound back read-only at its own path, so TLS keeps working under the `network`
profile.

## What it does not isolate

- **File tools run on the host.** `read_file`, `write_file`, `patch` and `search_files` are not
  confined by the sandbox; they keep Hermes' normal host-path behaviour and approval rules. The model
  is told this in its system prompt.
- **The rest of the host filesystem is readable.** Anything your user can read outside the hidden set
  (`/etc`, other dotfiles, secrets kept inside project directories) is readable by a command. Keep
  secrets in the hidden paths.
- **No seccomp filter.** System calls are not filtered; this is a filesystem and process boundary, not
  a defense against kernel exploits.
- **No cgroup limits.** The limits are per-process rlimits; a command that forks can use more memory in
  total than `memory_mb`.
- **Network is all or nothing.** The `network` profile shares the host network with no egress
  filtering; the other two have loopback only.
- **Other Unix sockets stay connectable** (a read-only mount does not block `connect()`): anything
  outside `/run/user/<uid>`, `/tmp` and the docker socket, if a command knows its path.
- **No background processes.** Each command is its own sandbox, and bwrap tears it down (pid namespace
  included) when the command's shell exits, so a `background: true` job dies right after it starts.
  The model is told background processes are unavailable on this backend.
- **No persistent `execute_code` kernel.** Hermes tries its remote session kernel first, which cannot
  outlive the sandbox that starts it; it then falls back to running each `execute_code` call as its
  own script (inside the sandbox, under the same limits). Python state does not carry over between
  calls, and Hermes logs one `remote kernel died at spawn` warning per call.

## Working directory

The working directory (`terminal.cwd`, else the directory Hermes was started in, as for the `local`
backend) is the writable set: everything under it can be changed, everything else is read-only. Point
it at a project or scratch directory. Only that setting decides what is writable: a `cd` (the model's,
or a Desktop/TUI workspace outside it) moves where commands start, and they run read-only there, also
after Hermes recreates the environment. A `terminal.cwd` that does not exist runs commands read-only in
its nearest existing parent. With your home directory as the working directory every dotfile
outside the hidden set (`.bashrc`, `.profile`, `.config/autostart`, `.local/bin`, ...) is writable,
which is a path back into your own shells; Hermes logs a warning in that case. A working directory of
`/` is refused, and so is one under the Hermes home or a hidden path, since no command could run there.
When Hermes was started from such a place (a gateway launched from its checkout under `.hermes`), the
backend starts in your home directory instead, as the gateway does for the `local` backend.

If the working directory is deleted on the host (for example by the command's own `rm -rf`), later
commands run in the nearest existing parent directory, read-only, until it exists again.

## Profiles

| Profile | Working directory | Network | Use it for |
|---------|-------------------|---------|------------|
| `restricted` | read-only | none (loopback only) | Inspection and read-only analysis |
| `workspace` | writable | none (loopback only) | Builds and edits that must not reach the network |
| `network` | writable | host network | Everything else (the default) |

## Extra binds

`binds` mounts more host directories into the sandbox, read-only unless `readonly: false`; `dest`
defaults to `src`:

```yaml
plugins:
  entries:
    bubblewrap:
      settings:
        binds:
          - {src: /data/models}
          - {src: /srv/scratch, dest: /srv/scratch, readonly: false}
```

A source under a hidden path is ignored with a warning. So is a source that contains a hidden path
(your home directory, say) when `dest` differs from `src`: the hidden paths are covered only at their
own location, so a copy of the tree elsewhere would show them. Bind such a source at its own path
instead. Because the root is read-only, a `dest` must already exist on the host or sit under a
writable mount.

A read-write bind whose source lies inside the working directory, inside another read-write bind or
inside the profile home is refused unless it has `dest` equal to `src` and sits directly under that
directory with no symlink on the way: bwrap resolves a bind source on the host for every command, so a
source a command can rename or replace with a symlink would let it choose what the next command
mounts. Under `terminal.home_mode: profile`, `HERMES_HOME/home` must be a plain directory or a link to
a directory outside the hidden set, or the backend refuses to start.

## Resource limits

| Setting | Default | Limit |
|---------|---------|-------|
| `memory_mb` | `256` | Virtual memory per process (`RLIMIT_AS`) |
| `cpu_seconds` | `30` | CPU time per process (`RLIMIT_CPU`) |
| `max_procs` | `256` | Processes and threads the command may add (`RLIMIT_NPROC`) |

A value of `0` disables that limit. The defaults are deliberately tight and some everyday tools exceed
them: `pip`, `node`, `cargo` and most compilers need more than 256 MB of address space (the command
fails with `MemoryError` or a similar message: raise `memory_mb` to 1024 or 2048), and a long compile
or test run uses more than 30 seconds of CPU (killed, exit 137: raise `cpu_seconds`). `max_procs` is
applied on top of the threads your user already runs, because the kernel counts `RLIMIT_NPROC` per
user across the machine, so a fork bomb stops at about 256 processes without touching your desktop.

## Approval

Because the sandbox writes to real host paths, Hermes' dangerous-command approval applies exactly as
for the `local` backend, including the hardline floor.

## Compared with hermes-agent#116579

This plugin is that PR's backend (and the
[#102875](https://github.com/NousResearch/hermes-agent/pull/102875) work it salvaged, by
[@TacoTakumi](https://github.com/TacoTakumi)) moved onto Hermes' terminal backend plugin interface, with
no core changes:

- Settings moved from `terminal.bubblewrap_*` to `plugins.entries.bubblewrap.settings.*`.
- The sandbox notice reaches the model as a plugin system prompt section instead of a core prompt edit.
- Background jobs are not refused up front (core has no plugin flag for that yet); they die with their
  sandbox, as described above.
- `execute_code` reaches the per-call path by falling back from the session kernel, instead of a core
  branch that skips it.
- The gateway starts in the process directory, or your home directory when that is hidden, instead of
  `MESSAGING_CWD`.

Tested with bubblewrap 0.11.1 on Linux 7.0; the original work was tested with 0.9.0 on kernel 6.8.
