"""Planner 4: what a source file imports and what it does, read from its text.

Regular expressions, not parsers: a missed import drops one edge from the connection map and so at
most one question; it never hides a file, because planner 3's areas still read every file they
chose. Repository text is untrusted data, and nothing here runs it. A Go import names a whole
package, so Go files also record which names they use from each import and which they define;
the map then links an import only to the files that define what is used.
"""
import re
from itertools import islice

SCAN_BYTES = 512_000   # the head of a huge file is enough for its imports and a rough profile
MAX_IMPORTS = 300
MAX_NAMES = 2000       # Go names a file uses from one import, or defines
HIT_CAP = 50           # facet matches counted per file

LANGUAGES = {'.py': 'py', '.pyi': 'py', '.js': 'js', '.jsx': 'js', '.mjs': 'js', '.cjs': 'js', '.ts': 'js',
             '.tsx': 'js', '.mts': 'js', '.cts': 'js', '.vue': 'js', '.svelte': 'js', '.rb': 'rb', '.go': 'go'}

_PY_IMPORT = re.compile(r'^[ \t]*import[ \t]+([\w.]+(?:[ \t]+as[ \t]+\w+)?(?:[ \t]*,[ \t]*[\w.]+(?:[ \t]+as[ \t]+\w+)?)*)',
                        re.M)
_PY_FROM = re.compile(r'^[ \t]*from[ \t]+(\.+[\w.]*|[\w.]+)[ \t]+import[ \t]+(\([^)]*\)|[^\n]*)', re.M)
_JS_IMPORTS = (
    re.compile(r'\bimport\s+(?:[\w$*{}\s,]+?\s+from\s+)?[\'"]([^\'"\n]+)[\'"]'),
    re.compile(r'\bexport\s+(?:type\s+)?(?:\*(?:\s+as\s+[\w$]+)?|\{[^}]*\})\s*from\s+[\'"]([^\'"\n]+)[\'"]'),
    re.compile(r'\b(?:require|import)\s*\(\s*[\'"]([^\'"\n]+)[\'"]\s*\)'),
)
_RB_RELATIVE = re.compile(r'\brequire_relative\s*\(?\s*[\'"]([^\'"\n]+)[\'"]')
_RB_REQUIRE = re.compile(r'\b(?:require|autoload\s*\(?\s*:\w+\s*,)\s*\(?\s*[\'"]([^\'"\n]+)[\'"]')
_GO_BLOCK = re.compile(r'^import[ \t]*\(([^)]*)\)', re.M)
_GO_SPEC = re.compile(r'^[ \t]*(?:([\w.]+)[ \t]+)?"([^"\n]+)"', re.M)
_GO_SINGLE = re.compile(r'^import[ \t]+(?:([\w.]+)[ \t]+)?"([^"\n]+)"', re.M)
_GO_SELECTOR = re.compile(r'\b([A-Za-z_]\w*)\.([A-Z]\w*)')
_GO_TOP = re.compile(r'^(?:func|type|var|const)[ \t]+([A-Z]\w*)', re.M)
_GO_GROUP = re.compile(r'^(?:type|var|const)[ \t]*\(([^)]*)\)', re.M)
_GO_GROUP_NAME = re.compile(r'^[ \t]+([A-Z]\w*)', re.M)
_GO_VERSION = re.compile(r'v\d+')

# Function and method definitions: (name, parameters).
_DEFS = {
    'py': re.compile(r'^[ \t]*(?:async[ \t]+)?def[ \t]+(\w+)[ \t]*\(([^)]*)\)', re.M),
    'rb': re.compile(r'^[ \t]*def[ \t]+(?:self\.)?(\w+[?!]?)[ \t]*(?:\(([^)]*)\)|([^\n;#]*))', re.M),
    'go': re.compile(r'^func[ \t]+(?:\([^)]*\)[ \t]*)?(\w+)[ \t]*(?:\[[^\]]*\])?\(([^)]*)\)', re.M),
}
_JS_DEFS = (
    re.compile(r'\bfunction\s*\*?\s*([\w$]+)\s*\(([^)]*)\)'),
    re.compile(r'\b(?:const|let|var)\s+([\w$]+)\s*=\s*(?:async\s+)?(?:function\b[^(\n]*)?\(([^)]*)\)\s*(?::[^=\n{]+)?(?:=>|\{)'),
    re.compile(r'^[ \t]+(?:(?:public|private|protected|static|async|override)\s+)*#?([\w$]+)\s*\(([^)]*)\)\s*(?::[^{;=\n]+)?\{',
               re.M),
)
_JS_EXPORTS = (
    re.compile(r'\bexport\s+(?:default\s+)?(?:async\s+)?function\s*\*?\s*([\w$]*)\s*\('),
    re.compile(r'\bexport\s+(?:const|let|var)\s+([\w$]+)\s*='),
)
_JS_NOT_NAMES = frozenset({'if', 'for', 'while', 'switch', 'catch', 'with', 'return', 'function', 'constructor'})

# Names of functions that decide whether something is allowed, and of functions that parse input.
_CHECK = re.compile(r'verif|validat|check|authori[sz]|authentic|permission|allow|deny|denied|forbid|guard|require|ensure|'
                    r'sanitiz|escape|reject|is_?admin|can_?(?:access|edit|view|read|write|manage)|has_?(?:access|perm|role|scope)',
                    re.I)
_PARSE = re.compile(r'pars(?:e|ing)|deserial|unserial|decod|unmarshal|unpack|loads$|from_?(?:json|dict|bytes|str)|unwrap|extract',
                    re.I)
_PATH = re.compile(
    r'\bopen\(|\bos\.path\.(?:join|abspath|realpath|normpath)\(|\bPath\(|\.(?:read_text|read_bytes|write_text|write_bytes)\(|'
    r'\bsend_(?:from_directory|file)\(|\bpath\.(?:join|resolve|normalize)\(|\bfs\.(?:promises\.)?\w+\(|'
    r'\b(?:readFile|createReadStream|readdir)(?:Sync)?\(|\bFile\.(?:open|read|join|expand_path|write|new)\b|\bDir\.\w|'
    r'\bFileUtils\.|\bfilepath\.(?:Join|Clean|Abs|Rel)\(|'
    r'\bos\.(?:Open|OpenFile|Create|ReadFile|WriteFile|MkdirAll|RemoveAll|Remove)\(|\bhttp\.(?:ServeFile|Dir|FileServer)\(')
_ARCHIVE = re.compile(r'\b(?:zipfile|tarfile|ZipFile|TarFile|extractall|unzip|AdmZip|yauzl|JSZip|ZipInputStream|ZipArchive|'
                      r'TarReader)\b|\barchive/(?:zip|tar)\b|\bZip::')


def suffix(path):
    name = path.rsplit('/', 1)[-1]
    return '.' + name.rsplit('.', 1)[-1].lower() if '.' in name else ''


def _python_imports(text):
    found = []
    for match in _PY_IMPORT.finditer(text):
        found += [part.split()[0] for part in match.group(1).split(',') if part.strip()]
    for match in _PY_FROM.finditer(text):
        module = match.group(1)
        found.append(module)
        names = re.sub(r'#[^\n]*', '', match.group(2))
        for name in re.sub(r'[()\\]', ' ', names).split(','):
            words = name.split()
            if words and words[0].isidentifier():
                found.append(module + ('' if module.endswith('.') else '.') + words[0])
    return found


def _js_imports(text):
    return [match.group(1) for pattern in _JS_IMPORTS for match in pattern.finditer(text)]


def _ruby_imports(text):
    found = [m.group(1) if m.group(1).startswith('.') else './' + m.group(1) for m in _RB_RELATIVE.finditer(text)]
    return found + [m.group(1) for m in _RB_REQUIRE.finditer(text)]


def _go_specs(text):
    """``(alias, import path)`` pairs, alias None when the file uses the package by its own name. A
    blank (``_``) import only runs the package's init code, so it links nothing."""
    specs = [(m.group(1), m.group(2)) for block in _GO_BLOCK.finditer(text) for m in _GO_SPEC.finditer(block.group(1))]
    specs += [(m.group(1), m.group(2)) for m in _GO_SINGLE.finditer(text)]
    return [(alias, path) for alias, path in specs if alias != '_']


def _go_imports(text):
    return [path for _, path in _go_specs(text)]


def _go_package_names(path):
    """Names the package at import *path* is likely called: its folder, less a /vN suffix or go- prefix."""
    parts = path.split('/')
    last = parts[-2] if len(parts) > 1 and _GO_VERSION.fullmatch(parts[-1]) else parts[-1]
    last = re.sub(r'\.v\d+$', '', last)
    names = {last, last.replace('-', '_'), last.replace('-', '')}
    if last[:3] in ('go-', 'go_'):
        names.add(last[3:])
    if last[-3:] in ('-go', '_go', '.go'):
        names.add(last[:-3])
    return names


def _go_names(text):
    """What a Go file uses from each import (``pkg.Name`` selectors), and the exported names it defines."""
    selectors = {}
    for m in islice(_GO_SELECTOR.finditer(text), 20_000):
        selectors.setdefault(m.group(1), set()).add(m.group(2))
    uses = {}
    for alias, path in _go_specs(text):
        if alias == '.':
            continue   # a dot import uses bare names, so the whole package stays linked
        found = set()
        for name in ({alias} if alias else _go_package_names(path)):
            found |= selectors.get(name, set())
        if not found and not alias:
            # A package named unlike its folder: anything the file selects may come from it.
            found = set().union(*selectors.values())
        uses[path] = sorted(found)[:MAX_NAMES]
    defines = set(_GO_TOP.findall(text))
    for group in _GO_GROUP.finditer(text):
        defines.update(_GO_GROUP_NAME.findall(group.group(1)))
    return uses, sorted(defines)[:MAX_NAMES]


_IMPORTS = {'py': _python_imports, 'js': _js_imports, 'rb': _ruby_imports, 'go': _go_imports}


def _definitions(lang, text):
    """``(name, parameters, public)`` for every function or method *text* defines."""
    if lang == 'js':
        exported = {m.group(1) for pattern in _JS_EXPORTS for m in pattern.finditer(text)}
        return [(m.group(1), m.group(2), m.group(1) in exported)
                for pattern in _JS_DEFS for m in pattern.finditer(text) if m.group(1) not in _JS_NOT_NAMES]
    found = []
    for m in _DEFS[lang].finditer(text):
        name = m.group(1)
        params = (m.group(2) or m.group(3) or '') if lang == 'rb' else m.group(2)
        found.append((name, params, name[:1].isupper() if lang == 'go' else not name.startswith('_')))
    return found


def _takes_input(params):
    for part in params.split(','):
        name = part.strip().lstrip('*&').split(':')[0].split('=')[0].strip()
        if name and name not in {'self', 'cls', '/'}:
            return True
    return False


def _count(pattern, text):
    return sum(1 for _ in islice(pattern.finditer(text), HIT_CAP))


def extract(path, data):
    """``{'imports': [...], 'facets': {...}}`` for one code file (bytes); both empty for other files.

    Imports stay as written (``..core.util``, ``./lib/auth.js``, ``zip/entry``); resolving them to
    inventory paths needs the whole file list (``orchestration.flows.edges``). Facets count what the
    file does: ``api`` public functions that take arguments, ``checks`` and ``parse`` functions named
    for verifying or parsing something, ``path`` file-path operations, ``archive`` archive handling.
    """
    lang = LANGUAGES.get(suffix(path))
    if lang is None:
        return {'imports': [], 'facets': {}}
    text = data[:SCAN_BYTES].decode('utf-8', 'replace')
    defs = _definitions(lang, text)
    facets = {'api': sum(1 for _, params, public in defs if public and _takes_input(params)),
              'checks': sum(1 for name, _, _ in defs if _CHECK.search(name)),
              'parse': sum(1 for name, _, _ in defs if _PARSE.search(name)),
              'path': _count(_PATH, text), 'archive': _count(_ARCHIVE, text)}
    imports = list(dict.fromkeys(spec for spec in _IMPORTS[lang](text) if spec))[:MAX_IMPORTS]
    result = {'imports': imports, 'facets': {name: min(n, HIT_CAP) for name, n in facets.items() if n}}
    if lang == 'go':
        uses, defines = _go_names(text)
        used = {spec: uses[spec] for spec in imports if uses.get(spec)}
        if used:
            result['uses'] = used
        if defines:
            result['defines'] = defines
    return result
