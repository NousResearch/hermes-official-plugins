"""Host-authenticated, profile-local workbench API with explicit user consent.

Hermes imports this file standalone and supplies /api/plugins/hermes-security.
Load only our sibling package by path; never depend on cwd or mutate sys.path.
"""
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import string
import sys
import threading
import logging
import traceback

from fastapi import APIRouter, Body, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute

_PACKAGE_PATH = Path(__file__).resolve().parents[1] / 'hermes_security'
_PACKAGE_NAME = '_hermes_security_dashboard_' + hashlib.sha256(str(_PACKAGE_PATH).encode()).hexdigest()[:16]
if _PACKAGE_NAME not in sys.modules:
    _spec = importlib.util.spec_from_file_location(_PACKAGE_NAME, _PACKAGE_PATH / '__init__.py', submodule_search_locations=[str(_PACKAGE_PATH)])
    if _spec is None or _spec.loader is None:
        raise ImportError('Security package unavailable')
    _package = importlib.util.module_from_spec(_spec)
    sys.modules[_PACKAGE_NAME] = _package
    try:
        _spec.loader.exec_module(_package)
    except Exception:
        sys.modules.pop(_PACKAGE_NAME, None)
        raise
SecurityService = importlib.import_module('.service', _PACKAGE_NAME).SecurityService
resolve_data_dir = importlib.import_module('.config', _PACKAGE_NAME).resolve_data_dir
SecurityError = importlib.import_module('.errors', _PACKAGE_NAME).SecurityError
ValidationError = importlib.import_module('.errors', _PACKAGE_NAME).ValidationError
redact_secrets = importlib.import_module('.target', _PACKAGE_NAME).redact_secrets

_services = {}
_services_lock = threading.Lock()
_log = logging.getLogger('hermes_security.dashboard')
MAX_JSON_BYTES = 2 * 1024 * 1024
ADVANCED_DELEGATION = 'advanced-delegation'
_ERRORS = {
    'invalid_input': (400, 'Invalid request.'),
    'not_found': (404, 'Requested record was not found.'),
    'conflict': (409, 'The current record state does not permit this action.'),
    'sealed': (409, 'This scan is sealed.'),
    'policy_denied': (403, 'This action is not authorized by the safety policy.'),
}


def get_service():
    """Resolve on every request: a cached service must not cross profile homes."""
    directory = resolve_data_dir().resolve()
    with _services_lock:
        if directory not in _services:
            home = directory.parent.parent
            profile = home.name if home.parent.name == 'profiles' else 'default'
            _services[directory] = SecurityService(directory, profile=profile)
        return _services[directory]


def _redacted_traceback():
    """The current exception's traceback with secrets masked: messages can quote target text."""
    result = redact_secrets(traceback.format_exc())
    return result[0] if isinstance(result, tuple) else result


def _error(code):
    status, message = _ERRORS.get(code, (500, 'Security service request failed.'))
    return JSONResponse({'error': {'code': code if code in _ERRORS else 'internal_error', 'message': message}}, status_code=status)


class SecurityRoute(APIRoute):
    """Bound JSON and sanitize errors without modifying the host's handlers."""
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def safe_handler(request):
            try:
                if request.method == 'POST':
                    body = bytearray()
                    async for chunk in request.stream():
                        body.extend(chunk)
                        if len(body) > MAX_JSON_BYTES:
                            return _error('invalid_input')
                    request._body = bytes(body)
                response = await handler(request)
                if response.media_type == 'application/json' and len(response.body) > MAX_JSON_BYTES:
                    _log.error('%s %s: response of %d bytes exceeds the %d byte cap', request.method,
                               request.url.path, len(response.body), MAX_JSON_BYTES)
                    return _error('internal_error')
                return response
            except RequestValidationError:
                return _error('invalid_input')
            except SecurityError as exc:
                return _error(exc.code)
            except Exception:
                # The client only sees a generic message; the gateway log keeps the cause.
                _log.error('%s %s failed:\n%s', request.method, request.url.path, _redacted_traceback())
                return _error('internal_error')
        return safe_handler


router = APIRouter(route_class=SecurityRoute)


def _broadcast(event, payload):
    try:
        from hermes_cli.plugin_events import broadcast_plugin_event
        broadcast_plugin_event('hermes-security', event, payload)
    except Exception:
        pass  # Optional delivery must never roll back an accepted action.


@router.get('/health')
def health():
    return {'ok': True, 'plugin': 'hermes-security'}


@router.get('/summary')
def summary():
    return get_service().summary()


@router.get('/scans')
def scans(q: str | None = None, status: str | None = None, mode: str | None = None, repo: str | None = None,
          limit: int = Query(50, ge=0, le=100), offset: int = Query(0, ge=0)):
    return get_service().workbench_scans(q=q, status=status, mode=mode, repo_key=repo, limit=limit, offset=offset)


def _delegation_info():
    """Subagent routes the user may pick for a scan, from the official advanced-delegation plugin.

    Only the routes of the profile's active delegation mode are offered: delegate_task's per-task
    parameter follows config.yaml, so a route of the other mode could not be passed anyway.
    """
    info = {'installed': False, 'backend': 'default', 'available': False, 'choices': [], 'default': None}
    try:
        from agent.delegation_backends import list_backends
        from hermes_cli.config import load_config_readonly
    except ImportError:
        return info
    config = load_config_readonly()
    config = config if isinstance(config, dict) else {}
    # The dashboard process may never load plugins, so its backend registry can be empty. Use it
    # when it knows the backend; otherwise trust the profile's plugins.enabled list. Core still
    # fails closed at delegate_task time if the backend cannot run.
    backends = {b.name: b for b in list_backends()}
    plugins = config.get('plugins') if isinstance(config.get('plugins'), dict) else {}
    enabled = set(plugins.get('enabled') or []) - set(plugins.get('disabled') or [])
    info['installed'] = bool({'model_map', 'profile_routes'} & backends.keys()) or ADVANCED_DELEGATION in enabled
    delegation = config.get('delegation') if isinstance(config.get('delegation'), dict) else {}
    backend = str(delegation.get('backend') or 'default').strip().lower() or 'default'
    block = (delegation.get('backends') or {}).get(backend) or {}
    available = backends[backend].available if backend in backends else (
        info['installed'] and backend in ('model_map', 'profile_routes'))
    info.update(backend=backend, available=bool(available))
    rows = block.get('options') if backend == 'model_map' else block.get('profiles') if backend == 'profile_routes' else None
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and isinstance(row.get('name'), str) and row['name'].strip():
            # Never return api_key or other route secrets: names, descriptions and the model only.
            detail = '/'.join(str(row[k]) for k in ('provider', 'model') if isinstance(row.get(k), str) and row[k])
            info['choices'].append({'name': row['name'].strip(), 'description': str(row.get('description') or ''), 'detail': detail})
    info['default'] = block.get('default') if isinstance(block.get('default'), str) else None
    return info


@router.get('/delegation')
def delegation():
    return _delegation_info()


@router.post('/scans')
def start_scan(body: dict = Body(...)):
    service = get_service()
    opts = dict(body)
    if opts.get('delegate') is not None:
        # Re-check against config.yaml: a route deleted since the page loaded must not reach the agent.
        info, chosen = _delegation_info(), opts['delegate']
        if (not isinstance(chosen, dict) or not info['installed'] or chosen.get('backend') != info['backend'] or
                chosen.get('name') not in {c['name'] for c in info['choices']}):
            raise ValidationError('unknown subagent route')
    if 'safetyLevel' in opts:
        if 'safety_level' in opts:
            raise ValidationError('ambiguous safety level')
        opts['safety_level'] = opts.pop('safetyLevel')
    if {'user_authorized', 'authorization_source'} & opts.keys():
        raise ValidationError('authority fields are not request options')
    opts.pop('confirm', None)  # legacy clients; the "run tests" checkbox is the consent
    authorize = opts.get('allow_tests') is True or opts.get('allowLocalValidation') is True
    created = service.start_scan(**opts, user_authorized=authorize,
                                 authorization_source='dashboard' if authorize else None, background=True)
    result = service.get_scan(created['scanId'])
    _broadcast('scan.updated', {'scanId': created['scanId']})
    return result


def _consent_error():
    return JSONResponse({'error': {'code': 'invalid_input', 'message': 'Please confirm this action for the selected scan.'}}, status_code=400)


def _level_error(level):
    return JSONResponse({'error': {'code': 'policy_denied', 'message': 'This action requires a scan created for ' + ('testing on this computer.' if level == 'local-safe' else 'testing a running app.')}}, status_code=403)


@router.get('/scans/{scan_id}/grants')
def grants(scan_id: str):
    return get_service().list_grants(scan_id)


@router.post('/scans/{scan_id}/authorize-validation')
def authorize_validation(scan_id: str, body: dict = Body(...)):
    if body.get('confirm') != scan_id:
        return _consent_error()
    service = get_service()
    if set(body) - {'confirm', 'origin', 'minutes', 'maxRequests'}:
        raise ValidationError('unknown authorization option')
    minutes, cap = body.get('minutes', 60), body.get('maxRequests', 200)
    if type(minutes) is not int or not 1 <= minutes <= 240 or type(cap) is not int or not 1 <= cap <= 200:
        raise ValidationError('invalid grant limits')
    # Share CLI origin syntax: do not silently discard paths or credentials.
    commands = importlib.import_module('.commands', _PACKAGE_NAME)
    try:
        origin = commands._origin(body.get('origin'))
    except (ValueError, TypeError, AttributeError, commands.argparse.ArgumentTypeError):
        raise ValidationError('invalid origin') from None
    grant = service.mint_grant(scan_id, origins=[origin], actions=['http-probe'],
                              expires_in_s=minutes * 60, max_requests=cap, created_by='dashboard')
    _broadcast('scan.updated', {'scanId': scan_id})
    return service.store.get_grant(grant['grantId'])


@router.post('/scans/{scan_id}/grants/{grant_id}/revoke')
def revoke_grant(scan_id: str, grant_id: str, body: dict = Body(...)):
    if body.get('confirm') != scan_id:
        return _consent_error()
    if set(body) != {'confirm'}:
        raise ValidationError('invalid revoke options')
    service = get_service()
    if service.store.get_grant(grant_id)['scanId'] != scan_id:
        raise ValidationError('grant does not belong to this scan')
    service.revoke_grant(grant_id)
    _broadcast('scan.updated', {'scanId': scan_id})
    return service.store.get_grant(grant_id)


@router.post('/scans/{scan_id}/run-validation')
def run_validation(scan_id: str, body: dict = Body(...)):
    if body.get('confirm') != scan_id:
        return _consent_error()
    service = get_service()
    if service.get_scan(scan_id)['safety_level'] == 'static':
        return _level_error('local-safe')
    plans = body.get('plans')
    if (set(body) != {'confirm', 'plans'} or not isinstance(plans, list) or not plans or
            any(not isinstance(p, dict) or p.get('kind') != 'local-command' or p.get('level') != 'local-safe' for p in plans)):
        raise ValidationError('requires local-command plans at local-safe level')
    result = service.record_validations(scan_id, plans=plans, user_authorized=True, authorization_source='dashboard')
    recorded = {r['receiptId']: r for r in service.store.validations(scan_id)}
    _broadcast('scan.updated', {'scanId': scan_id})
    return {'scanId': scan_id, 'receipts': [recorded[r['receiptId']] for r in result['receipts']]}


@router.get('/scans/{scan_id}')
def scan(scan_id: str, section: str | None = None,
         limit: int = Query(50, ge=0, le=100), offset: int = Query(0, ge=0)):
    return get_service().get_scan(scan_id, section=section, limit=limit, offset=offset)


@router.post('/scans/{scan_id}/cancel')
def cancel(scan_id: str):
    service = get_service()
    service.cancel(scan_id)
    result = service.get_scan(scan_id)
    _broadcast('scan.updated', {'scanId': scan_id})
    return result


@router.post('/scans/{scan_id}/resume')
def resume(scan_id: str):
    service = get_service()
    service.resume(scan_id)
    result = service.get_scan(scan_id)
    _broadcast('scan.updated', {'scanId': scan_id})
    return result


@router.post('/scans/{scan_id}/delete')
def delete_scan(scan_id: str, body: dict = Body(...)):
    if body.get('confirm') != scan_id or set(body) != {'confirm'}:
        return _consent_error()
    result = get_service().delete_scan(scan_id)
    _broadcast('scan.updated', {'scanId': scan_id})
    return result


@router.post('/clone')
def clone(body: dict = Body(...)):
    """Clone a repository URL into the profile (or update an earlier clone) for scanning."""
    if set(body) != {'url'}:
        raise ValidationError('url is required')
    CloneError = importlib.import_module('.target.clone', _PACKAGE_NAME).CloneError
    try:
        return get_service().clone_repository(body['url'])
    except CloneError as exc:
        # Our own messages, with credentials stripped: show them so the user can fix the URL or access.
        return JSONResponse({'error': {'code': 'clone_failed', 'message': str(exc)}}, status_code=400)


@router.get('/clones')
def clones():
    return get_service().list_clones()


@router.post('/scans/{scan_id}/session')
def link_session(scan_id: str, body: dict = Body(...)):
    if set(body) - {'sessionId', 'storedSessionId'} or not isinstance(body.get('sessionId'), str):
        raise ValidationError('sessionId is required')
    result = get_service().link_session(scan_id, body['sessionId'], body.get('storedSessionId'))
    _broadcast('scan.updated', {'scanId': scan_id})
    return result


@router.get('/folders')
def folders(path: str = Query(..., min_length=1, max_length=4096)):
    """Top-level folders of a repository, for the "Scan area" picker."""
    root = Path(path).expanduser()
    if not root.is_dir():
        raise ValidationError('folder not found')
    skip = {'.git', 'node_modules', '__pycache__', '.venv', 'venv', 'dist', 'build', 'target', '.next'}
    try:
        names = sorted(p.name for p in root.iterdir()
                       if p.is_dir() and not p.is_symlink() and p.name not in skip and not p.name.startswith('.'))
    except OSError:
        raise ValidationError('folder cannot be read') from None
    return {'root': str(root.resolve()), 'items': names[:500]}


@router.get('/browse')
def browse(path: str | None = Query(None, min_length=1, max_length=4096)):
    """Subfolders of one folder on the gateway's machine, for the repository picker."""
    root = Path(path).expanduser() if path else Path.home()
    if not root.is_dir():
        raise ValidationError('folder not found')
    root = root.resolve()
    items = []
    try:
        for p in root.iterdir():
            try:
                if p.name.startswith('.') or not p.is_dir():
                    continue
                items.append({'name': p.name, 'path': str(p), 'repo': (p / '.git').exists()})
            except OSError:
                continue
    except OSError:
        raise ValidationError('folder cannot be read') from None
    items.sort(key=lambda item: item['name'].lower())
    if os.name == 'nt':
        roots = os.listdrives() if hasattr(os, 'listdrives') else [d + ':\\' for d in string.ascii_uppercase if os.path.isdir(d + ':\\')]
    else:
        roots = ['/']
    return {'path': str(root), 'parent': str(root.parent) if root.parent != root else None,
            'home': str(Path.home()), 'repo': (root / '.git').exists(), 'roots': roots,
            'items': items[:1000], 'truncated': len(items) > 1000}


@router.get('/scans/{scan_id}/activity')
def activity(scan_id: str, after_id: int = Query(0, ge=0), limit: int = Query(200, ge=0, le=500)):
    return get_service().activity(scan_id, after_id=after_id, limit=limit)


@router.get('/scans/{scan_id}/coverage')
def coverage(scan_id: str):
    return get_service().coverage_view(scan_id)


@router.get('/findings')
def findings(q: str | None = None, status: str | None = None, severity: str | None = None,
             evidenceState: str | None = None, validationLevel: str | None = None,
             repo: str | None = None, folder: str | None = None, owasp: str | None = None,
             chained: bool | None = None, limit: int = Query(50, ge=0, le=100), offset: int = Query(0, ge=0)):
    return get_service().workbench_findings(q=q, triage=status, severity=severity,
        evidence_state=evidenceState, validation_level=validationLevel, repo_key=repo, folder=folder,
        owasp=owasp, chained=chained, limit=limit, offset=offset)


@router.get('/finding-folders')
def finding_folders(repo: str | None = None):
    return get_service().finding_folders(repo_key=repo)


@router.get('/findings/{finding_id}')
def finding(finding_id: str):
    return get_service().get_finding(finding_id)


@router.post('/findings/{finding_id}/triage')
def triage(finding_id: str, body: dict = Body(...)):
    if set(body) - {'state', 'note'} or 'state' not in body:
        raise ValidationError('invalid triage body')
    service = get_service()
    service.triage(finding_id, body['state'], body.get('note'))
    result = service.get_finding(finding_id)
    _broadcast('finding.updated', {'findingId': finding_id})
    return result


@router.get('/findings/{finding_id}/patch')
def patch(finding_id: str):
    return get_service().patch_preview(finding_id)


@router.get('/repositories')
def repositories(limit: int = Query(50, ge=0, le=100), offset: int = Query(0, ge=0)):
    return get_service().list_repositories(limit=limit, offset=offset)


@router.get('/exports/{scan_id}/{format}')
def export(scan_id: str, format: str, download: bool = False):
    if format not in {'md', 'sarif', 'json', 'csv'}:
        raise ValidationError('unsupported export format')
    result = get_service().export(scan_id, format)
    if not download:
        # The desktop SDK transport JSON-parses every body, so Markdown/CSV need an envelope.
        return result
    return Response(result['content'], media_type=result['contentType'],
                    headers={'Content-Disposition': 'attachment; filename="' + result['filename'] + '"'})
