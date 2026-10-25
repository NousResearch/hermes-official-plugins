"""Validation of the host paths the bubblewrap backend binds by name.

Some binds name a host path that configuration or the environment chose
rather than the backend: the trust-anchor files of CA_BUNDLE_VARS
(SSL_CERT_FILE and the rest), the Hermes scratch directory under
HERMES_HOME, the profile home under ``terminal.home_mode: profile`` and
the operator's ``binds`` setting. bwrap resolves a bind
source on the host at every spawn, with symlinks followed, so a name that
leads into a hidden path mounts that path, and a name that lies in a
directory a sandbox can write to lets a command choose what the next spawn
mounts. Every such bind goes through :func:`bind_refusal` once,
at environment construction, and the argv carries the path it returned,
never the name.

Pure functions only, like ``home.py``: every input is an argument.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

from . import home as bubblewrap_home

logger = logging.getLogger(__name__)

# Kinds of host path a bind expects. The trust anchors and the scratch
# directory are a FILE or a DIRECTORY; an operator bind may name any
# existing path, a unix socket, a FIFO or a device node included.
FILE = "file"
DIRECTORY = "directory"
ANY = "any existing path"


def _is_within(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _strictly_within(path: str, root: str) -> bool:
    return path != root and _is_within(path, root)


@dataclass(frozen=True)
class BindGuard:
    """What a host path named by configuration or the environment must not resolve into.

    All paths are real paths, resolved once at environment construction.

    *secret*: credential stores (the HOME denylist and the paths the file
    safety policy refuses); never bound.
    *hermes_homes*: HERMES_HOME and the default HOME/.hermes, hidden as a
    whole; a bind resolving inside one is refused unless it lies under a
    public root the caller names (the Python environment Hermes runs from,
    the scratch directory itself).
    *masks*: every hidden path, the Hermes homes included. A path below one
    of them is out of a sandbox's reach even inside a writable root: the
    overlay covers it and the pins hold its parents in place.
    *writable*: directories a sandbox of this profile can write to: the
    working directory of every sandbox under a writable profile (this
    one's, the configured one's and those of the other sandboxes this
    process made), read-write operator bind sources, the profile home
    under a profile home_mode, the shared scratch directory (every
    sandbox's own scratch lives in it) and the sandbox directory (every
    environment's state lives in it).
    *home_root* and *home_allow*: the HOME the default-deny layout covers
    and its allowlist units; a dot entry off the allowlist is hidden, and
    every dot entry is read-only.
    """

    secret: tuple[str, ...] = ()
    hermes_homes: tuple[str, ...] = ()
    masks: tuple[str, ...] = ()
    writable: tuple[str, ...] = ()
    home_root: str | None = None
    home_allow: tuple[str, ...] = ()

    def home_hidden_unit(self, path: str) -> str | None:
        """The dot entry of HOME, off the allowlist, that *path* lies in, else None."""
        if self.home_root is None:
            return None
        unit = bubblewrap_home.allow_unit(self.home_root, path)
        return unit if unit is not None and unit not in self.home_allow else None

    def writable_root(self, path: str) -> str | None:
        """The writable root a command could change *path* under, else None.

        *path* strictly inside a writable root is out of reach when a
        hidden path between the two masks it, or when the root covers HOME
        and *path* lies in a dot entry (the layout shows those read-only or
        not at all) or is a top-level entry of HOME (the layout binds each
        one at its own path, a mount point). A root itself is a mount point
        inside the sandbox: no command can rename or replace it.
        """
        for root in self.writable:
            if not _strictly_within(path, root):
                continue
            if any(_is_within(path, mask) and _strictly_within(mask, root) for mask in self.masks):
                continue
            if self.home_root is not None and _is_within(self.home_root, root) and (
                bubblewrap_home.allow_unit(self.home_root, path) is not None
                or os.path.dirname(path) == self.home_root
            ):
                continue
            return root
        return None


def _kind_problem(real: str, kind: str) -> str | None:
    if kind == FILE and not os.path.isfile(real):
        return "it is not a regular file"
    if kind == DIRECTORY and not os.path.isdir(real):
        return "it is not a directory"
    return None


def _refusal(
    real: str,
    guard: BindGuard,
    *,
    kind: str,
    public: Sequence[str],
    dest: str | None,
    hidden_rules: bool,
) -> str | None:
    """Why *real* must not be bound, else None. The order puts the most specific reason first."""
    problem = _kind_problem(real, kind)
    if problem:
        return problem
    root = guard.writable_root(real)
    # A source bound at its own path directly under a writable root is a
    # mount point inside, and its parent is the root, also a mount point:
    # no command can rename either, so the source cannot be swapped.
    if root is not None and not (dest == real and os.path.dirname(real) == root):
        return f"{real} lies in {root}, which a sandbox can write to, so a command could swap it"
    if not hidden_rules:
        return None
    for secret in guard.secret:
        if _is_within(real, secret):
            return f"{real} lies in {secret}, a credential store the sandbox hides"
    for hermes_home in guard.hermes_homes:
        if _is_within(real, hermes_home):
            if any(_is_within(real, p) and _strictly_within(p, hermes_home) for p in public):
                return None
            return f"{real} lies in the Hermes home {hermes_home}, which the sandbox hides"
    unit = guard.home_hidden_unit(real)
    if unit is not None:
        return (
            f"{real} lies in {os.path.join(guard.home_root or '', unit)}, a dot entry of HOME the sandbox "
            f"hides (add {unit!r} to {bubblewrap_home.ALLOW_KEY} to show it)"
        )
    if os.path.isdir(real):
        for inner in (*guard.secret, *guard.hermes_homes, *((guard.home_root,) if guard.home_root else ())):
            if _is_within(inner, real):
                return f"{real} holds {inner}, which the sandbox hides"
    return None


def bind_refusal(
    path: str,
    guard: BindGuard,
    *,
    kind: str = FILE,
    public: Sequence[str] = (),
    dest: str | None = None,
    hidden_rules: bool = True,
) -> tuple[str, str | None]:
    """Resolve *path* once: ``(real path, None)`` when it may be bound, ``(real path, reason)`` otherwise.

    The bind takes the returned path as its source, never *path*: bwrap
    follows symlinks in a source at every spawn, and a link a command can
    rewrite would choose the next mount. Refused: a path missing on the
    host, one that is not of *kind*, one that lies in a directory a sandbox
    can write to (unless bound at its own path, *dest*, directly under such
    a root), and with *hidden_rules* one that lies in a credential store,
    in a hidden Hermes home outside the *public* roots, or in a dot entry
    of HOME off the allowlist, and a directory that holds any of those.
    """
    real = os.path.realpath(os.path.abspath(os.path.expanduser(path)))
    if not os.path.exists(real):
        return real, f"{real} does not exist"
    return real, _refusal(real, guard, kind=kind, public=public, dest=dest, hidden_rules=hidden_rules)


def validated_bind_source(
    label: str,
    path: str,
    guard: BindGuard,
    *,
    kind: str = FILE,
    public: Sequence[str] = (),
    dest: str | None = None,
    hidden_rules: bool = True,
) -> str | None:
    """The real path to bind for *path* (see :func:`bind_refusal`), or None with one warning.

    For a bind whose absence only takes access away (a trust anchor, the
    scratch dir, a read-write operator bind): the refusal skips the bind and
    leaves the sandbox running. *label* names the setting or variable in
    the warning.
    """
    real, reason = bind_refusal(path, guard, kind=kind, public=public, dest=dest, hidden_rules=hidden_rules)
    if reason is None:
        return real
    logger.warning("bubblewrap: not binding %s (%s): %s", label, path, reason)
    return None


def ca_bundle_binds(
    names: Iterable[str],
    guard: BindGuard,
    *,
    environ: Mapping[str, str],
    public: Sequence[str],
) -> tuple[tuple[str, str], ...]:
    """(source, destination) pairs that bring back the trust anchors the overlays hide.

    For each variable in *names* that is set and names a path on the host:
    when the path as spelled or the path it resolves to lies under a hidden
    path (guard.masks), the resolved path is validated (a directory for
    SSL_CERT_DIR, a regular file for the rest) and bound read-only at the
    resolved path, and at the spelled path too when that differs and is
    hidden, since a tool opens the spelled path and a link to it inside a
    hidden directory does not exist in the sandbox. A visible link resolves
    inside the sandbox to the resolved path, where the bind is. A path that
    nothing hides needs no bind. An unset or missing value yields nothing,
    with no warning.
    """
    binds: list[tuple[str, str]] = []
    for name in names:
        value = environ.get(name, "")
        if not value:
            continue
        spelled = os.path.abspath(os.path.expanduser(value))
        if not os.path.exists(spelled):
            continue
        resolved = os.path.realpath(spelled)
        dests = [p for p in dict.fromkeys((resolved, spelled)) if any(_is_within(p, m) for m in guard.masks)]
        if not dests:
            continue
        kind = DIRECTORY if name == "SSL_CERT_DIR" else FILE
        real = validated_bind_source(name, spelled, guard, kind=kind, public=public)
        if real is None:
            continue
        binds += [(real, dest) for dest in dests if (real, dest) not in binds]
    return tuple(binds)
