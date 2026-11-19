"""Runtime end-of-life check: declared runtime versions against endoflife.date.

Only declarations the repository makes are checked (.nvmrc, package.json
engines, .python-version, pyproject requires-python, go.mod, composer.json,
Dockerfile base images). Lookups that fail are reported as unknown, never as
supported.
"""
import json
import re
import urllib.request

API = 'https://endoflife.date/api/v1/products/{product}'
NETWORK = ['endoflife.date']

_IMAGES = {'node': 'nodejs', 'python': 'python', 'php': 'php', 'golang': 'go', 'ruby': 'ruby'}


def _cycle(product, text):
    """Release cycle a version constraint names: major for Node, major.minor otherwise."""
    match = re.search(r'(\d+)(?:\.(\d+))?', text or '')
    if not match:
        return None
    if product == 'nodejs':
        return match.group(1)
    return match.group(1) + '.' + match.group(2) if match.group(2) else None


def _open_minimum(declared):
    """True for constraints like '>=20' or '^18 || >=22' that never cap the version."""
    return any(re.match(r'\s*>=?', part) and '<' not in part for part in (declared or '').split('||'))


def declarations(files, read):
    """Yield (product, declared, path) for runtime versions the repository pins.

    *read(path)* returns text or None; only small, well-known files are read.
    """
    found = []
    for path in sorted(files):
        name = path.rsplit('/', 1)[-1]
        if name in {'.nvmrc', '.node-version', '.python-version', 'go.mod', 'package.json', 'composer.json',
                    'pyproject.toml', 'runtime.txt'} or name == 'Dockerfile' or name.startswith('Dockerfile.'):
            if '/node_modules/' in '/' + path or '/vendor/' in '/' + path:
                continue
            text = read(path)
            if not text:
                continue
        else:
            continue
        if name in {'.nvmrc', '.node-version'}:
            found.append(('nodejs', text.strip()[:40], path))
        elif name == '.python-version':
            found.append(('python', text.strip().splitlines()[0][:40] if text.strip() else '', path))
        elif name == 'runtime.txt':
            found.append(('python', text.strip().removeprefix('python-')[:40], path))
        elif name == 'go.mod':
            match = re.search(r'^go\s+(\d+\.\d+)', text, re.M)
            if match:
                found.append(('go', match.group(1), path))
        elif name == 'pyproject.toml':
            match = re.search(r'^requires-python\s*=\s*["\']([^"\']+)', text, re.M)
            if match:
                found.append(('python', match.group(1)[:40], path))
        elif name in {'package.json', 'composer.json'}:
            try:
                data = json.loads(text)
            except ValueError:
                continue
            if name == 'package.json' and isinstance(data.get('engines'), dict) and isinstance(data['engines'].get('node'), str):
                found.append(('nodejs', data['engines']['node'][:40], path))
            if name == 'composer.json' and isinstance(data.get('require'), dict) and isinstance(data['require'].get('php'), str):
                found.append(('php', data['require']['php'][:40], path))
        else:
            for match in re.finditer(r'^\s*FROM\s+(?:--platform=\S+\s+)?(?:docker\.io/)?(?:library/)?([a-z]+):(\d[\w.\-]*)', text, re.M | re.I):
                product = _IMAGES.get(match.group(1).lower())
                if product:
                    found.append((product, match.group(2)[:40], path))
    return found


def _fetch(product, opener, timeout):
    with opener(API.format(product=product), timeout=timeout) as response:
        data = json.loads(response.read(4 * 1024 * 1024))
    return {str(r.get('name')): r for r in data.get('result', {}).get('releases', []) if isinstance(r, dict)}


def check_runtimes(files, read, *, opener=None, timeout=20):
    opener = opener or urllib.request.urlopen
    cache, results = {}, []
    for product, declared, path in declarations(files, read):
        cycle = _cycle(product, declared)
        row = {'runtime': product, 'declared': declared, 'path': path, 'cycle': cycle,
               'status': 'unknown', 'eolDate': None, 'latest': None}
        if cycle is None:
            row['reason'] = 'version constraint does not name a release cycle'
            results.append(row)
            continue
        if product not in cache:
            try:
                cache[product] = _fetch(product, opener, timeout)
            except (OSError, ValueError) as exc:
                cache[product] = exc
        releases = cache[product]
        if isinstance(releases, Exception):
            row['reason'] = 'endoflife.date lookup failed: ' + str(releases)[:200]
        elif cycle not in releases:
            row['reason'] = 'release cycle not listed on endoflife.date'
        else:
            release = releases[cycle]
            status = 'end_of_life' if release.get('isEol') else 'supported'
            # ">=20" with no upper bound only permits an end-of-life minimum; supported versions qualify too.
            if status == 'end_of_life' and _open_minimum(declared):
                status = 'allows_end_of_life'
            row.update(status=status, eolDate=release.get('eolFrom'), latest=(release.get('latest') or {}).get('name'))
        results.append(row)
    return results
