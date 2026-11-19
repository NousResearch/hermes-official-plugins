"""Cheap content signals: where untrusted input may meet authority.

They only rank review work and never become findings. A pattern that misses
lowers an area's priority; it never hides a file, because deep scans and the
baseline still see the whole inventory.
"""
import re
from itertools import islice

from .redact import PATTERNS

SCAN_BYTES = 512_000   # signals are hints; the head of a huge file is enough
HIT_CAP = 50           # matches counted per signal; enough to tell a sink-heavy file from a stray call

_SIGNALS = {
    'route': r'@\w+\.(?:get|post|put|patch|delete|route|websocket|api_route)\(|'
             r'\badd_(?:get|post|put|patch|delete|route|routes|api_route|websocket_route)\(|\bAPIRouter\(|'
             r'\b(?:uvicorn\.(?:run|Server)|start_server|create_server|createServer|HTTPServer|serve_forever|'
             r'WebSocketResponse|websockets\.serve|WebSocketServer)\b|'
             r'\b(?:app|router|server)\.(?:get|post|put|patch|delete|all|use)\(|'
             r'export\s+(?:async\s+)?function\s+(?:GET|POST|PUT|PATCH|DELETE)\b|'
             r'@(?:Get|Post|Put|Patch|Delete|RequestMapping|GetMapping|PostMapping|Controller)\b|'
             r'\bHandleFunc\(|\burlpatterns\b|\bcreateTRPCRouter\b|\b(?:publicProcedure|protectedProcedure)\b',
    'input': r'\brequest\.(?:body|json|args|form|query|params|files|headers|cookies|data|GET|POST|match_info|rel_url|'
             r'query_params|path_params|post|content|stream)\b|\b(?:ws|websocket)\.(?:recv|receive\w*)\(|'
             r'\breq\.(?:body|query|params|headers|cookies|files)\b|\bsearchParams\b|\bgetParameter\(|'
             r'\.FormValue\(|\.URL\.Query\(|\$_(?:GET|POST|REQUEST|COOKIE|FILES)\b|\bsys\.argv\b|\bprocess\.argv\b',
    'auth': r'\bauthori[sz]|\bauthenticat|\bapi[_-]?key\b|\bverify_?(?:token|signature|password)\b|\bsecrets\.token_\w+\(|'
            r'\bbearer\b|\bjwt\.(?:sign|verify|decode)|\b(?:check|has|require)_?(?:permission|role|scope)|'
            r'\bis_?admin\b|\blogin_required\b|\bgetServerSession\b|\bcsrf|\bcompare_digest\b|\btimingSafeEqual\b|'
            r'\bset_?cookie\b|\bsession\[',
    'sql': r'\$(?:queryRaw|executeRaw)|\b(?:queryRawUnsafe|executeRawUnsafe)\b|\bcursor\.execute\(|\.raw\(|'
           r'\b(?:SELECT\s+[\w*,\s.]+\s+FROM|INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM)\b',
    'exec': r'\bsubprocess\.|\bos\.(?:system|popen|exec\w*|posix_spawn\w*)\(|\bchild_process\b|\bexecSync\(|\bspawn(?:Sync)?\(|'
            r'\bPopen\(|\bcreate_subprocess_(?:exec|shell)\b|\bpty\.(?:spawn|fork|openpty)\b|\b(?:ptyprocess|pexpect|winpty)\b|'
            r'\bShellExecute\w*\(|'
            r'\beval\(|\bnew Function\(|Runtime\.getRuntime\(|\bexec\.Command\(|\bshell\s*=\s*True\b|\bProcessBuilder\(',
    'deserialize': r'\bpickle\.loads?\(|\byaml\.(?:unsafe_)?load\(|\bmarshal\.loads\(|\bunserialize\(|'
                   r'\bObjectInputStream\b|\bBinaryFormatter\b|\bjsonpickle\b',
    'file': r'\bwriteFile(?:Sync)?\(|\bcreateWriteStream\(|\bsend_?[Ff]ile\(|\bshutil\.|\bextractall\(|'
            r'\bopen\([^)\n]*[\'"][wa]b?[\'"]|\bmultipart\b|\bupload',
    'outbound': r'\brequests\.(?:get|post|put|patch|delete|request)\(|\bhttpx\.|\burllib\.request\b|\baxios\b|'
                r'\bhttp\.(?:Get|Post|NewRequest)\(|\bHttpClient\b|\bfetch\(\s*(?![\'"`]/)',
    'crypto': r'\bcreate(?:Hash|Hmac|Cipheriv|Decipheriv)\(|\bhashlib\.|\bhmac\.|\bcrypto\.subtle\b|\bbcrypt\b|'
              r'\bargon2\b|\bMath\.random\(|\brandom\.random\(|\bCipher\.getInstance\(',
    'webhook': r'\bwebhook|x-hub-signature|stripe-signature|\bconstructEvent\(|svix',
    'render': r'\bdangerouslySetInnerHTML\b|\.innerHTML\s*=|\brender_template_string\(|\bMarkup\(|\|\s*safe\b|'
              r'\bv-html\b|\bbypassSecurityTrust|\{\{\{|\bmark_safe\(',
}
_COMPILED = {name: re.compile(pattern, re.I if name in {'auth', 'sql', 'webhook'} else 0) for name, pattern in _SIGNALS.items()}
# High-confidence token formats only; JWT-shaped strings are common in test data.
_SECRETS = [pattern for kind, pattern in PATTERNS if kind != 'jwt']

# How strongly each signal suggests an attack surface (used to rank areas).
WEIGHTS = {'route': 3, 'input': 3, 'exec': 3, 'deserialize': 3, 'sql': 2, 'auth': 2, 'webhook': 2, 'render': 2,
           'file': 1, 'outbound': 1, 'crypto': 1, 'secret': 0}


def profile(data):
    """Sorted signal names present in ``data`` (bytes), and their total match count (each signal
    counted up to HIT_CAP), so a file full of sinks outranks one with a single stray call."""
    text = data[:SCAN_BYTES].decode('utf-8', 'replace')
    counts = {name: sum(1 for _ in islice(pattern.finditer(text), HIT_CAP)) for name, pattern in _COMPILED.items()}
    found = {name for name, n in counts.items() if n}
    if any(pattern.search(text) for pattern in _SECRETS):
        found.add('secret')
    return sorted(found), sum(counts.values())


def detect(data):
    """Sorted signal names present in ``data`` (bytes)."""
    return profile(data)[0]


def score(names):
    return sum(WEIGHTS.get(name, 0) for name in names or ())
