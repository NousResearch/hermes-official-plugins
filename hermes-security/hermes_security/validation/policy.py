"""Fail-closed validation policy. Grant minting is a user-command-only API."""
from datetime import datetime, timedelta, timezone
from pathlib import PurePath
from urllib.parse import urlsplit
import uuid
from ..canonical import stable_id, utcnow
from ..errors import PolicyDenied, ValidationError

LEVELS = {'static', 'local-safe', 'active-authorized'}
ACTIONS = {'http-probe', 'local-command'}

def normalize_origin(url):
    """Normalize only scheme, hostname and effective port; never resolve DNS."""
    try:
        if not isinstance(url, str) or any(c.isspace() for c in url) or '\\' in url:
            raise PolicyDenied('invalid URL')
        p = urlsplit(url)
        if p.scheme.lower() not in {'http', 'https'}:
            raise PolicyDenied('non-http(s) scheme denied')
        if p.username is not None or p.password is not None:
            raise PolicyDenied('userinfo in URL denied')
        host = p.hostname
        if not host: raise PolicyDenied('missing origin hostname')
        host = host.encode('idna').decode().lower()
        if ':' in host: host = '[' + host + ']'
        port=p.port if p.port is not None else (443 if p.scheme.lower()=='https' else 80)
        if port==0: raise PolicyDenied('invalid origin port')
        return f'{p.scheme.lower()}://{host}:{port}'
    except (ValueError, UnicodeError) as exc:
        raise PolicyDenied('invalid origin URL') from exc

def _time(value):
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00')) if isinstance(value,str) else value
        if dt.tzinfo is None: raise ValueError()
        return dt
    except (ValueError, AttributeError, TypeError) as exc:
        raise PolicyDenied('invalid expiry/time') from exc

def mint_grant(store, scan_id, *, origins, actions, expires_in_s, max_requests, created_by='user-command'):
    """Called only by trusted slash/CLI/dashboard dispatch, never an agent tool.

    The caller must establish user provenance; a Python argument is not authentication.
    """
    if created_by not in {'user-command', 'user-cli', 'dashboard'}:
        raise PolicyDenied('only a direct user command may mint grants')
    if (not isinstance(actions,list) or not actions or not set(actions)<=ACTIONS or
        type(expires_in_s) is not int or expires_in_s<=0 or type(max_requests) is not int or max_requests<=0 or
        not isinstance(origins,list) or not scan_id):
        raise ValidationError('invalid grant actions, origins, expiry or request cap')
    normalized = sorted(set(normalize_origin(o) for o in origins))
    if 'http-probe' in actions and not normalized: raise ValidationError('HTTP grant requires origins')
    created = utcnow()
    g = dict(grantId=stable_id('grt',scan_id,uuid.uuid4().hex),scanId=scan_id,origins=normalized,
             actions=sorted(set(actions)),createdBy=created_by,createdAt=created,
             expiresAt=(_time(created)+timedelta(seconds=expires_in_s)).strftime('%Y-%m-%dT%H:%M:%SZ'),
             maxRequests=max_requests,used=0)
    store.grant_validation(scan_id,g)
    return store.get_grant(g['grantId'])

def _argv(argv):
    if not isinstance(argv,list) or not argv or any(not isinstance(a,str) or not a or '\x00' in a for a in argv):
        raise PolicyDenied('commands must be nonempty argv lists')
    # Normal dev tooling (shells, package runners, curl to localhost) is fine for
    # testing your own code; only privilege escalation is refused.
    forbidden={'sudo','su','doas','runas','pkexec'}
    if any(PurePath(a).name.lower().removesuffix('.exe') in forbidden for a in argv[:1]):
        raise PolicyDenied('privilege escalation commands are not allowed')

def check_plan(plan, *, grant, safety_level, now):
    """Validate a proposed plan without running target code or performing I/O."""
    if not isinstance(plan,dict): raise PolicyDenied('plan must be an object')
    kind=plan.get('kind'); level=plan.get('level',safety_level)
    if safety_level not in LEVELS or level not in LEVELS: raise PolicyDenied('unknown safety level')
    if kind not in ACTIONS|{'static'}: raise PolicyDenied('unknown validation action')
    if kind=='static':
        if level!='static': raise PolicyDenied('static kind requires static level')
        if plan.get('commands') or plan.get('requests'): raise PolicyDenied('static cannot execute commands or requests')
        return
    if safety_level=='static' or level=='static': raise PolicyDenied('static level forbids runtime execution')
    if level!=safety_level: raise PolicyDenied('plan safety level mismatch')
    plan.setdefault('cleanup',{'strategy':'remove-copy' if kind=='local-command' else 'none-needed'})
    if not isinstance(plan.get('cleanup'),dict): raise PolicyDenied('cleanup must be an object')
    if not all(isinstance(plan.get(k),dict) and plan[k] for k in ('positiveControl','negativeControl')):
        raise PolicyDenied('runtime confirmation requires paired controls')
    if kind=='http-probe' and level!='active-authorized': raise PolicyDenied('HTTP action requires active-authorized level')
    if level=='active-authorized':
        if not grant: raise PolicyDenied('missing grant')
        if grant.get('revoked') or grant.get('revokedAt'): raise PolicyDenied('revoked grant')
        if _time(grant.get('expiresAt'))<=_time(now): raise PolicyDenied('expired grant')
        if not plan.get('scanId') or plan['scanId']!=grant.get('scanId'): raise PolicyDenied('wrong scan for grant')
        if kind not in grant.get('actions',[]): raise PolicyDenied('action not granted')
    if kind=='local-command':
        commands=plan.get('commands')
        if not isinstance(commands,list) or not commands: raise PolicyDenied('missing commands')
        for command in commands: _argv(command)
    else:
        requests=plan.get('requests')
        if not isinstance(requests,list) or not requests: raise PolicyDenied('missing requests')
        used=grant.get('used',0); cap=grant.get('maxRequests')
        if type(used) is not int or type(cap) is not int or used<0 or len(requests)>cap-used:
            raise PolicyDenied('request count exceeds remaining grant maxRequests')
        for req in requests:
            if not isinstance(req,dict): raise PolicyDenied('request must be an object')
            origin=normalize_origin(req.get('url'))
            if origin not in grant.get('origins',[]): raise PolicyDenied('origin not granted (IP literals require exact grant)')
            method=req.get('method','GET')
            if method not in {'GET','HEAD','OPTIONS','POST','PUT','PATCH'}: raise PolicyDenied('DELETE and unsupported methods are not allowed')
            headers=req.get('headers',{})
            if not isinstance(headers,dict) or any(k.lower() in {'host','proxy-authorization','connection','transfer-encoding'} for k in headers):
                raise PolicyDenied('unsafe request headers')
