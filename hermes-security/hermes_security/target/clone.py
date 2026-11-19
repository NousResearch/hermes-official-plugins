"""Bring a remote repository into the profile so it can be scanned without picking a folder.

Clones land under ``<data dir>/repos/<host>/<owner>/<name>``; asking again for the same URL
fast-forwards the existing copy instead of cloning twice. Repository content is untrusted: no
hooks, submodules, LFS downloads or local-file transports run, and credentials are never
accepted inside the URL (Git's own credential helper handles private repositories).
"""
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import uuid
from urllib.parse import urlsplit

from ..fsutil import set_private

CLONE_TIMEOUT_S = 600
_SEGMENT = re.compile(r'^[A-Za-z0-9._-]{1,100}$')
_SCP = re.compile(r'^(?P<user>[A-Za-z0-9._-]+)@(?P<host>[A-Za-z0-9.-]+):(?P<path>[A-Za-z0-9._/~-]+)$')
_SHORTHAND = re.compile(r'^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$')


class CloneError(Exception):
    """A clone or update failed; ``str(exc)`` is safe to show the user."""


def parse_url(url):
    """``(clone_url, host, [path segments])`` for an https/ssh Git URL, else CloneError."""
    if not isinstance(url, str) or not url.strip() or len(url) > 2048:
        raise CloneError('Enter a repository URL, such as https://github.com/owner/repo.')
    url = url.strip()
    if url.startswith('-') or any(ch.isspace() or ord(ch) < 32 for ch in url):
        raise CloneError('That does not look like a repository URL.')
    if _SHORTHAND.match(url):
        url = 'https://github.com/' + url
    match = _SCP.match(url)
    if match:
        host, path = match['host'], match['path']
    else:
        parts = urlsplit(url)
        if parts.scheme not in {'https', 'http', 'ssh'}:
            raise CloneError('Use an https:// or ssh address. Local paths and other transports are not cloned.')
        if parts.scheme in {'https', 'http'} and (parts.username or parts.password):
            raise CloneError('Leave sign-in details out of the URL; Git will use your saved credentials.')
        if parts.query or parts.fragment or not parts.hostname:
            raise CloneError('That does not look like a repository URL.')
        host, path = parts.hostname, parts.path
    segments = [s for s in path.strip('/').split('/') if s]
    if segments and segments[-1].endswith('.git'):
        segments[-1] = segments[-1][:-4]
    if not segments or len(segments) > 6 or not all(_SEGMENT.match(s) and s not in {'.', '..'} for s in segments):
        raise CloneError('The URL needs a repository path, such as owner/repo.')
    if not _SEGMENT.match(host):
        raise CloneError('That does not look like a repository URL.')
    return url, host.lower(), segments


def _identity(url):
    """Host and path, so owner/repo, https://.../repo.git and git@...:repo are one repository."""
    try:
        _, host, segments = parse_url(url)
    except CloneError:
        return None
    return host, tuple(s.lower() for s in segments)


def _git(args, cwd=None, timeout=CLONE_TIMEOUT_S):
    # Keep the user's own Git config (credential helpers for private repos), but refuse
    # anything in the repository that could run code or reach the local filesystem.
    argv = ['git', '-c', 'protocol.file.allow=never', '-c', 'protocol.ext.allow=never',
            '-c', 'core.hooksPath=' + os.devnull, '-c', 'core.fsmonitor=false', '-c', 'core.longpaths=true',
            '-c', 'submodule.recurse=false', '-c', 'filter.lfs.required=false', '-c', 'filter.lfs.smudge=',
            '-c', 'filter.lfs.process=', *args]
    env = {**os.environ, 'GIT_TERMINAL_PROMPT': '0', 'GIT_LFS_SKIP_SMUDGE': '1'}
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
    try:
        proc = subprocess.run(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=timeout, creationflags=flags)
    except FileNotFoundError:
        raise CloneError('Git is not installed on the computer running Hermes.') from None
    except subprocess.TimeoutExpired:
        raise CloneError(f'Git did not finish within {timeout // 60} minutes.') from None
    return proc.returncode, proc.stdout.decode('utf-8', 'replace'), proc.stderr.decode('utf-8', 'replace')


def _why(stderr):
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    fatal = [line for line in lines if line.lower().startswith(('fatal:', 'error:'))]
    text = (fatal or lines or ['Git failed without a message.'])[-1]
    text = re.sub(r'://[^/@\s]+@', '://', text)  # never echo credentials
    return text[:300]


def _force_remove(path):
    def retry(func, target, *_):
        os.chmod(target, stat.S_IWRITE)
        func(target)
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=retry)
    else:
        shutil.rmtree(path, onerror=retry)


def _head(path):
    code, out, _ = _git(['-C', str(path), 'rev-parse', 'HEAD'], timeout=30)
    return out.strip() if code == 0 else None


def clone_or_update(url, data_dir):
    """Clone ``url`` (or fast-forward an earlier clone of it) and return where it lives."""
    clone_url, host, segments = parse_url(url)
    base = Path(data_dir) / 'repos'
    base.mkdir(parents=True, exist_ok=True)
    set_private(base, 0o700)
    dest = base.joinpath(host, *segments)
    if dest.exists():
        if not (dest / '.git').is_dir():
            raise CloneError('A folder for this repository already exists but is not a Git checkout.')
        code, out, _ = _git(['-C', str(dest), 'config', '--get', 'remote.origin.url'], timeout=30)
        if code != 0 or _identity(out.strip()) != _identity(clone_url):
            raise CloneError('This repository was cloned from a different address; remove the old copy first.')
        code, _, err = _git(['-C', str(dest), 'pull', '--ff-only', '--no-recurse-submodules'])
        if code != 0:
            return {'path': str(dest), 'url': clone_url, 'action': 'kept', 'revision': _head(dest),
                    'message': 'Could not update the existing copy, so it is scanned as it is: ' + _why(err)}
        return {'path': str(dest), 'url': clone_url, 'action': 'updated', 'revision': _head(dest)}
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name(dest.name + '.partial-' + uuid.uuid4().hex[:8])
    code, _, err = _git(['clone', '--no-recurse-submodules', '--origin', 'origin', '--', clone_url, str(partial)])
    if code != 0:
        if partial.exists():
            _force_remove(partial)
        raise CloneError('Could not clone the repository: ' + _why(err))
    partial.rename(dest)
    return {'path': str(dest), 'url': clone_url, 'action': 'cloned', 'revision': _head(dest)}


def list_clones(data_dir, limit=200):
    """Repositories cloned earlier, newest first, so the picker can offer them again."""
    base = Path(data_dir) / 'repos'
    found = []
    if not base.is_dir():
        return found
    stack = [(base, 0)]
    while stack and len(found) < limit:
        folder, depth = stack.pop()
        try:
            children = [p for p in folder.iterdir() if p.is_dir() and not p.is_symlink()]
        except OSError:
            continue
        for child in children:
            if '.partial-' in child.name:
                continue
            if (child / '.git').is_dir():
                found.append(child)
            elif depth < 6:
                stack.append((child, depth + 1))
    found.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return [{'path': str(p), 'name': '/'.join(p.relative_to(base).parts[1:])} for p in found[:limit]]
