"""Pinned osv-scanner: fetched on first use, hash-checked on every run.

The binary is downloaded from the upstream GitHub release, never bundled, and
never executed unless its SHA-256 matches the pin below. Bump OSV_VERSION and
BINARIES together, copying hashes from the release's osv-scanner_SHA256SUMS.
"""
import hashlib
import json
import os
import platform
import subprocess
import time
import urllib.request
import uuid
from pathlib import Path

from ..errors import SecurityError
from ..fsutil import set_private

OSV_VERSION = '2.7.0'
RELEASE_URL = 'https://github.com/google/osv-scanner/releases/download/v{version}/{name}'
BINARIES = {
    ('darwin', 'amd64'): ('osv-scanner_darwin_amd64', 'e82c5df7ab0a42dff29a1cd0660711447b56b4c0483ff66ad8634df10fd81a45'),
    ('darwin', 'arm64'): ('osv-scanner_darwin_arm64', 'd4926ef8da134332543be276dff157e4c170f99ad91fc48e89448a8816bdf70b'),
    ('linux', 'amd64'): ('osv-scanner_linux_amd64', '160c6253e2922e9e0a6a9771c5c4c40a4b3c6090b34a3cdd5e80c96cc2e4218b'),
    ('linux', 'arm64'): ('osv-scanner_linux_arm64', '4061ef4c021ed3c47f203b818f337e351336cc5cd67c0274fc36bd1dfc728dd7'),
    ('windows', 'amd64'): ('osv-scanner_windows_amd64.exe', '9964ed64ec9a387391eb4cc006ad5c531154821aef29b793e02c448de4686438'),
    ('windows', 'arm64'): ('osv-scanner_windows_arm64.exe', '3e630381c1f1fd3c9c4d447e6ff2ef6c737083371722773db8337eead0f97962'),
}
MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024
MAX_REPORT_BYTES = 256 * 1024 * 1024
# Hosts osv-scanner contacts: advisories, and dependency-graph resolution for manifests
# without a lockfile (requirements.txt, pom.xml).
NETWORK = ['github.com (one-time download)', 'api.osv.dev', 'api.deps.dev']
_ENV_KEEP = ('PATH', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATHEXT', 'HTTPS_PROXY', 'HTTP_PROXY', 'NO_PROXY',
             'https_proxy', 'http_proxy', 'no_proxy', 'SSL_CERT_FILE', 'SSL_CERT_DIR')


class ScannerUnavailable(SecurityError):
    code = 'dependency_scanner_unavailable'


def platform_key(system=None, machine=None):
    system = (system or platform.system()).lower()
    machine = (machine or platform.machine()).lower()
    arch = {'x86_64': 'amd64', 'amd64': 'amd64', 'x64': 'amd64', 'arm64': 'arm64', 'aarch64': 'arm64'}.get(machine)
    key = (system, arch)
    if key not in BINARIES:
        raise ScannerUnavailable(f'no pinned osv-scanner build for {system}/{machine}')
    return key


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _download(url, dest, *, timeout=120, opener=None):
    opener = opener or urllib.request.urlopen
    total = 0
    with opener(url, timeout=timeout) as response, open(dest, 'wb') as out:
        while True:
            block = response.read(1024 * 1024)
            if not block:
                break
            total += len(block)
            if total > MAX_DOWNLOAD_BYTES:
                raise ScannerUnavailable('osv-scanner download exceeded the size limit')
            out.write(block)


def ensure_scanner(data_dir, *, opener=None, key=None):
    """Return (path, info) for a verified scanner, downloading the pinned build once.

    HERMES_SECURITY_OSV_SCANNER points at an administrator-installed binary (for
    networks that block GitHub); it is used as-is and recorded as user-provided.
    """
    override = os.environ.get('HERMES_SECURITY_OSV_SCANNER')
    if override:
        path = Path(override)
        if not path.is_file():
            raise ScannerUnavailable('HERMES_SECURITY_OSV_SCANNER does not name a file')
        return path, {'name': 'osv-scanner', 'version': None, 'sha256': _sha256(path), 'source': 'user-provided'}
    name, expected = BINARIES[key or platform_key()]
    directory = Path(data_dir) / 'tools' / 'osv-scanner' / OSV_VERSION
    directory.mkdir(parents=True, exist_ok=True)
    set_private(directory, 0o700)
    path = directory / name
    source = 'cached'
    if path.is_file() and _sha256(path) != expected:
        path.unlink()   # corrupted or replaced: never run it, fetch a fresh copy
    if not path.is_file():
        source = 'downloaded'
        partial = directory / f'.{name}.{uuid.uuid4().hex}.part'
        try:
            _download(RELEASE_URL.format(version=OSV_VERSION, name=name), partial, opener=opener)
            actual = _sha256(partial)
            if actual != expected:
                raise ScannerUnavailable(f'osv-scanner download failed its hash check (got {actual[:16]}...)')
            if os.name != 'nt':
                os.chmod(partial, 0o700)
            os.replace(partial, path)   # atomic: concurrent first scans converge on one verified file
        except OSError as exc:
            raise ScannerUnavailable(f'could not download osv-scanner: {exc}') from exc
        finally:
            if partial.exists():
                partial.unlink()
    return path, {'name': 'osv-scanner', 'version': OSV_VERSION, 'sha256': expected, 'source': source}


def run_scanner(executable, root, work_dir, *, timeout=900):
    """Scan lockfiles and manifests under *root*; return the parsed JSON report.

    An empty ``--config`` overrides any osv-scanner.toml in the repository, which
    could otherwise silently ignore advisories. Exit 1 means vulnerabilities were
    found and 128 means no package sources; both are successful runs.
    """
    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)
    home = work / 'home'
    home.mkdir(exist_ok=True)
    config = work / 'osv-scanner.toml'
    config.write_text('')
    report = work / f'osv-{uuid.uuid4().hex}.json'
    env = {k: os.environ[k] for k in _ENV_KEEP if k in os.environ}
    env.update(HOME=str(home), USERPROFILE=str(home), TEMP=str(home), TMP=str(home), TMPDIR=str(home))
    # --all-packages lists clean packages too, so the receipt can say how many were checked.
    argv = [str(executable), 'scan', 'source', '--format', 'json', '--all-packages', '--config', str(config), '-r', str(root)]
    started = time.monotonic()
    try:
        with open(report, 'wb') as out:
            proc = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.PIPE,
                                  cwd=str(work), env=env, timeout=timeout, shell=False)
    except subprocess.TimeoutExpired as exc:
        raise ScannerUnavailable(f'osv-scanner timed out after {timeout} seconds') from exc
    except OSError as exc:
        raise ScannerUnavailable(f'could not run osv-scanner: {exc}') from exc
    elapsed = int((time.monotonic() - started) * 1000)
    try:
        if proc.returncode == 128:
            return {'results': []}, {'exitCode': 128, 'durationMs': elapsed}
        if proc.returncode not in (0, 1):
            tail = proc.stderr.decode('utf-8', 'replace').strip().splitlines()[-3:]
            raise ScannerUnavailable(f'osv-scanner exited with {proc.returncode}: ' + ' | '.join(tail)[:500])
        if report.stat().st_size > MAX_REPORT_BYTES:
            raise ScannerUnavailable('osv-scanner report exceeded the size limit')
        try:
            parsed = json.loads(report.read_bytes())
        except ValueError as exc:
            raise ScannerUnavailable('osv-scanner returned invalid JSON') from exc
        if not isinstance(parsed, dict) or not isinstance(parsed.get('results', []), list):
            raise ScannerUnavailable('osv-scanner report has an unexpected shape')
        return parsed, {'exitCode': proc.returncode, 'durationMs': elapsed}
    finally:
        report.unlink(missing_ok=True)
