"""The single profile-local orchestrator. No agent input conveys user authority.

Mutations share a scan lease, so finalize cannot race ingestion. The store lease
schema is foreign-keyed to scans; grant:<id> is therefore the owner of a scan
lease rather than a fictitious scan key. This is stricter than a per-grant lock:
all grant consumption for one scan is serialized, including across processes.
All cooperating callers must use this service; direct store calls bypass it.
"""
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
import json
import os
import shlex
import sys
import threading

from . import PRODUCER, target, validation, reports, chains, dependencies, timing
from .canonical import canonical_json, sha256_hex, stable_id, utcnow
from .domain.models import normalize_candidate, promote_to_finding
from .domain.identities import finding_id
from .domain.validate import validate_worker_result, validate_document, public_problems
from .errors import SecurityError, ValidationError, NotFound, Conflict, SealedError, PolicyDenied
from .store.db import SecurityStore
from .store import leases
from .store.artifacts import scan_dir, write_artifact, read_artifact
from .standards import ledger
from .orchestration import METHODOLOGY_VERSION, standard, deep, diff, reducer, next_step, stall, flows, deployment
from .orchestration.coverage import build_coverage
from .dependencies import finding as dependency_finding
from .orchestration.worker_protocol import check_candidate
from .fsutil import is_link, set_private

# A lease names its holder's process, so a holder that dies is replaced at once; the TTL only
# bounds holders that cannot be checked, and outlasts several missed renewals.
LEASE_TTL_S = 300
LEASE_RENEW_S = 30


class SecurityService:
    def __init__(self, data_dir: Path, *, profile='default', clock=None, dependency_checker=None):
        # Resolve trusted configuration once (e.g. a symlinked HERMES_HOME); artifact
        # access below this root still refuses symlinks with O_NOFOLLOW.
        self.data_dir = Path(data_dir).resolve()
        if not isinstance(profile, str) or not profile:
            raise ValidationError('profile must be nonempty text')
        self.profile = profile
        self.clock = clock or utcnow
        self.store = SecurityStore(self.data_dir, redactor=target.redact_secrets)
        self._local = threading.local()
        self._check_dependencies = dependency_checker or dependencies.check_dependencies
        self._dependency_threads = {}
        self._dependency_lock = threading.Lock()

    def _now(self):
        value = self.clock()
        return value if isinstance(value, str) else value.strftime('%Y-%m-%dT%H:%M:%SZ')

    def _safe(self, value, *, bounded=True):
        try:
            encoded = self.store._json(value)
        except (TypeError, ValueError, RecursionError, UnicodeError) as exc:
            raise ValidationError('invalid JSON input') from exc
        if bounded and len(encoded.encode()) > 2 * 1024 * 1024:
            raise ValidationError('input exceeds 2 MiB')
        return value

    def _scan(self, scan_id, *, mutable=False, canceled=False):
        if not isinstance(scan_id, str):
            raise ValidationError('scanId must be a string')
        scan = self.store.get_scan(scan_id)
        if mutable:
            if scan['sealed_at']:
                raise SealedError('scan is sealed')
            if scan['status'] == 'canceled' and not canceled:
                raise Conflict('scan is canceled; resume before submitting work')
        return scan

    @contextmanager
    def _mutation(self, scan_id, *, canceled=False, owner=None):
        self._scan(scan_id, mutable=True, canceled=canceled)
        active = getattr(self._local, 'active', {})
        if scan_id in active:
            yield self._scan(scan_id, mutable=True, canceled=canceled)
            return
        self._scan(scan_id, mutable=True, canceled=canceled)
        owner = leases.owner(owner or 'service')
        if not self.store.lease(scan_id, owner, LEASE_TTL_S):
            raise Conflict(leases.describe(self.store.lease_owner(scan_id)))
        self._local.active = {**active, scan_id: owner}
        stop_renewal = threading.Event()
        def renew():
            # Renewal keeps long operations covered (validation plans run up to 200 steps).
            while not stop_renewal.wait(LEASE_RENEW_S):
                try:
                    if not self.store.lease(scan_id, owner, LEASE_TTL_S):
                        return
                except (SealedError, NotFound):
                    return
                except SecurityError:
                    # A busy database is transient; the TTL outlasts several missed renewals.
                    continue
        heartbeat = threading.Thread(target=renew, name='security-scan-lease', daemon=True)
        heartbeat.start()
        try:
            yield self._scan(scan_id, mutable=True, canceled=canceled)
        finally:
            stop_renewal.set()
            heartbeat.join(timeout=6)
            self._local.active = active
            if not self.store.is_sealed(scan_id):
                self.store.release(scan_id, owner)

    def _current(self, scan):
        # New files (e.g. a worker's stray notes) are out of scope, not a reason to void the
        # scan; only changed or missing inventory files invalidate the recorded evidence.
        # Re-hashing every file on each submit took minutes on large repositories; a stat
        # cache limits reading to files whose size, mtime or file ID moved.
        cache = self._load_stat_cache(scan['scan_id'])
        changed, fresh = target.changed_files(scan['target'], self.store.inventory(scan['scan_id']), cache)
        if fresh != cache:
            self._save_stat_cache(scan['scan_id'], fresh)
        if changed:
            raise Conflict('snapshot changed; start a new scan')

    def _load_stat_cache(self, scan_id):
        try:
            data = json.loads((self._work(scan_id) / target.STAT_CACHE).read_text('utf-8'))
        except (OSError, ValueError):
            return {}
        files = data.get('files') if isinstance(data, dict) and data.get('version') == 1 else None
        return files if isinstance(files, dict) else {}

    def _save_stat_cache(self, scan_id, files):
        path = self._work(scan_id) / target.STAT_CACHE
        temp = path.with_name(f'{path.name}.{threading.get_ident()}.tmp')
        temp.write_text(json.dumps({'version': 1, 'files': files}), 'utf-8')
        os.replace(temp, path)

    PHASES = (('preparing', 'Preparing scan'), ('threat_model', 'Building threat model'),
              ('reviewing', 'Reviewing code'), ('validating', 'Validating findings'),
              ('attack_paths', 'Tracing attack paths'), ('finalizing', 'Finalizing scan'))

    def progress(self, scan_id):
        """Phase checklist for the UI, derived from what the scan has actually recorded."""
        scan = self._scan(scan_id)
        kinds = {e['kind'] for e in self.store.events(scan_id, limit=500)}
        attempts = [a for a in self._attempts(scan_id) if a['status'] == 'accepted']
        reached = 0
        if 'scan.checkpoint' in kinds:
            reached = 1
        if any(a['role'] not in {'validator'} for a in attempts):
            reached = 2
        if any(a['role'] == 'validator' for a in attempts) or 'validations.recorded' in kinds:
            reached = 3
        if self.store.get_chains(scan_id):
            reached = 4
        if scan['status'] == 'finalizing':
            reached = 5
        finished = bool(scan['sealed_at']) or scan['status'] in {'completed', 'partial'}
        stopped = scan['status'] in {'canceled', 'interrupted', 'failed'}
        steps = []
        for index, (key, title) in enumerate(self.PHASES):
            if finished or index < reached:
                state = 'done'
            elif index == reached:
                state = 'stopped' if stopped else 'active'
            else:
                state = 'pending'
            steps.append({'key': key, 'label': title, 'state': state})
        current = 'done' if finished else self.PHASES[reached][0]
        return {'phase': current, 'label': 'Completed' if finished else ('Stopped' if stopped else self.PHASES[reached][1]), 'steps': steps}

    def link_session(self, scan_id, session_id, stored_session_id=None):
        """Remember which chat session is running a scan (Desktop "View activity")."""
        for value in (session_id, stored_session_id):
            if value is not None and (not isinstance(value, str) or not value or len(value) > 200):
                raise ValidationError('session id must be text')
        self._scan(scan_id)
        self.store.set_scan_option(scan_id, 'session', {'sessionId': session_id, 'storedSessionId': stored_session_id or session_id})
        return self.get_scan(scan_id)

    def _attempts(self, scan_id):
        # The v1 store has no public attempt-list method; keep the read here.
        with self.store._connection() as conn:
            return [dict(r) for r in conn.execute('SELECT * FROM worker_attempts WHERE scan_id=? ORDER BY rowid', (scan_id,))]

    def _packets(self, scan):
        inventory = self.store.inventory(scan['scan_id'])
        profiles = ledger.specialist_profiles_for(scan['options']['surfaces'])
        if scan['mode'] == 'deep':
            packets = deep.build_packets(scan['scan_id'], inventory, profiles, scan['options']['deep_passes'])
        elif scan['mode'] == 'diff':
            packets, _ = diff.build_packets(scan['scan_id'], scan['target'], profiles, manifest=scan['options']['diff'])
        else:
            # Scans keep the planner they started with, so packet identities never shift mid-scan.
            planner = scan['options'].get('planner', 1)
            packets = standard.build_packets(scan['scan_id'], inventory, profiles, planner=planner,
                                             frozen=scan['options'].get('standardAreas'),
                                             flow_map=scan['options'].get('flowMap'))
            packets += standard.remainder_packets(scan['scan_id'], packets, scan['options'].get('remainders', {}), inventory,
                                                  max_rounds=standard.remainder_rounds(planner), progress_rule=planner >= 3)
        receipt = scan['options'].get('dependencies')
        if receipt and receipt.get('leads'):
            owners = self._lead_owners(receipt, packets)
            for packet in packets:
                # Whole-scope packets read the dependencies section; area packets get their own manifests' leads.
                files = ([f['path'] for f in inventory] if packet.get('filesFrom') == 'inventory' or packet['role'] == 'deep-pass'
                         else packet.get('files', []) + owners.get(packet['packetId'], []))
                leads, total = dependencies.leads_for(receipt, files, limit=0 if packet.get('filesFrom') == 'inventory' else 100)
                if total:
                    packet['dependencyLeads'] = {'total': total, 'items': leads,
                                                 'more': 'security_scan_get section dependencies' if total > len(leads) else None}
        return packets

    @staticmethod
    def _lead_owners(receipt, packets):
        """Lockfiles no packet reads (planner 3 skips them by design): each one's leads go to the
        area packet with the most files in its folder, else in the folders below it."""
        owned = {path for packet in packets for path in packet.get('files', [])}
        areas = [p for p in packets if p.get('review') == 'area']
        owners = {}
        for lockfile in sorted({lead['path'] for lead in receipt.get('leads', []) if lead.get('path')} - owned):
            folder = lockfile.rsplit('/', 1)[0] + '/' if '/' in lockfile else ''
            def nearness(packet):
                inside = [p for p in packet['files'] if p.startswith(folder)]
                return (sum('/' not in p[len(folder):] for p in inside), len(inside))
            best = max(areas, key=nearness, default=None)
            if best is not None and nearness(best) != (0, 0):
                owners.setdefault(best['packetId'], []).append(lockfile)
        return owners

    def _plan(self, scan, packets=None):
        refs = Path(__file__).resolve().parents[1] / 'references'
        inventory = self.store.inventory(scan['scan_id'])
        profiles = ledger.specialist_profiles_for(scan['options']['surfaces'])
        excluded = scan['options']['excluded']
        return {'scanId': scan['scan_id'], 'root': scan['root'], 'snapshotDigest': scan['snapshot_digest'],
                'methodologyVersion': METHODOLOGY_VERSION, 'surfaces': scan['options']['surfaces'],
                'context': scan['options'].get('notes') or '',
                'specialistProfiles': [{'name': name, 'path': str(refs / 'specialist-profiles' / (name + '.md'))} for name in profiles],
                'lanes': standard.lanes(), 'applicableControls': ledger.applicable_controls(scan['options']['surfaces']),
                'packets': self._packets(scan) if packets is None else packets,
                'workerBriefPath': str(refs / 'worker-packet.md'),
                'workDir': str(self._work(scan['scan_id'])),
                'schedule': standard.SCHEDULE,
                'dependencyCheck': dependencies.compact(scan['options'].get('dependencies')),
                'submit': {'tool': 'security_scan_submit_worker_result', 'cli': 'PYTHONPATH=' + shlex.quote(str(Path(__file__).resolve().parents[1])) + ' ' + shlex.quote(sys.executable) + ' -m hermes_security submit --scan ' + scan['scan_id'] + ' --file <json>'},
                'inventory': {'files': len(inventory), 'bytes': sum(f['size'] for f in inventory), 'lines': sum(f['lines'] for f in inventory), 'truncated': scan['options']['truncated']},
                'excluded': self._excluded_summary(excluded), 'status': scan['status'], 'diff': scan['options'].get('diff'),
                **({'standardPlan': standard.summary(inventory, self._planner(scan), scan['options'].get('standardAreas'))}
                   if self._planner(scan) >= 2 else {})}

    @staticmethod
    def _planner(scan):
        return scan['options'].get('planner', 1) if scan['mode'] == 'standard' else 0

    @staticmethod
    def _excluded_summary(excluded, limit=500):
        """Counts by reason; out-of-scope files are only counted, since a scoped scan excludes most of the tree."""
        reasons = {}
        for item in excluded:
            reasons[item['reason']] = reasons.get(item['reason'], 0) + 1
        listed = [item for item in excluded if item['reason'] != 'out_of_scope']
        return {'total': len(excluded), 'byReason': reasons, 'items': listed[:limit], 'itemsTruncated': len(listed) > limit}

    def start_scan(self, *, user_authorized=False, authorization_source=None, background_dependencies=False,
                   background=False, **opts):
        self._safe(opts)
        allowed = {'path', 'mode', 'scope', 'base', 'head', 'safety_level', 'allowLocalValidation', 'allow_tests',
                   'deep_passes', 'budget', 'notes', 'context', 'provider', 'model', 'dependency_check', 'delegate',
                   'planner'}
        if set(opts) - allowed:
            raise ValidationError('unknown scan option')
        if opts.get('delegate') is not None:
            opts['delegate'] = self._delegate(opts['delegate'])
        if not isinstance(opts.get('path'), str):
            raise ValidationError('path is required')
        if 'context' in opts:  # friendlier name for the optional context box
            if opts.get('notes') is not None:
                raise ValidationError('use context or notes, not both')
            opts['notes'] = opts.pop('context')
        mode = opts.get('mode', 'standard')
        if not isinstance(mode, str) or mode not in {'standard', 'deep', 'diff'}:
            raise ValidationError('mode must be standard, deep or diff; use record_validations for validation')
        if 'allow_tests' in opts and type(opts['allow_tests']) is not bool:
            raise ValidationError('allow_tests must be true or false')
        if 'dependency_check' in opts and type(opts['dependency_check']) is not bool:
            raise ValidationError('dependency_check must be true or false')
        planner = opts.get('planner')
        if 'planner' in opts and (type(planner) is not int or planner not in standard.PLANNERS or mode != 'standard'):
            raise ValidationError('planner must be 3 or 4, for a standard scan')
        if mode == 'standard':
            planner = planner or standard.PLANNER_VERSION
        # One switch replaces the old safety levels: tests run only when allowed.
        safety = opts.get('safety_level', 'local-safe' if opts.get('allow_tests') or opts.get('allowLocalValidation') is True else 'static')
        if not isinstance(safety, str) or safety not in {'static', 'local-safe', 'active-authorized'}:
            raise ValidationError('invalid safety_level')
        if mode != 'diff' and (opts.get('base') is not None or opts.get('head') is not None):
            raise ValidationError('base and head require diff mode')
        for key in ('provider', 'model', 'notes'):
            if opts.get(key) is not None and not isinstance(opts[key], str):
                raise ValidationError('provider, model and notes must be text')
        passes = opts.get('deep_passes', 3)
        if type(passes) is not int or not 1 <= passes <= 8:
            raise ValidationError('deep_passes must be an integer from 1 to 8')
        resolved = target.resolve_target(opts['path'], scope=opts.get('scope'), base=opts.get('base'), head=opts.get('head'), mode=mode)
        root = Path(resolved['root'])
        if self.data_dir == root or root in self.data_dir.parents:
            raise PolicyDenied('plugin data directory must be outside the target tree')
        if mode == 'diff' and resolved['head'] and (resolved['head'] != resolved['revision'] or resolved['dirty']):
            raise Conflict('explicit diff head must be the clean checked-out revision; use a separate worktree')
        if user_authorized is True and authorization_source == 'dashboard':
            opts['authorizationSource'] = 'dashboard'
        if background:
            # Reading a large repository takes minutes, longer than the desktop waits for an answer.
            # Save the scan now; the background thread inventories it, then checks dependencies,
            # and next_step holds review until both are saved.
            options = {**opts, 'deep_passes': passes, 'surfaces': [], 'excluded': [], 'truncated': False,
                       'preparing': True, 'dependenciesPending': True,
                       # The inventory runs later, so the planner it builds for is chosen now.
                       **({'planner': planner} if mode == 'standard' else {})}
            scan = self.store.create_scan(mode=mode, safety_level=safety, target=resolved, inventory=[], options=options)
            self.store.add_event(scan['scan_id'], 'scan.preparing', 'Inventorying the target.')
            self._start_dependency_check(scan['scan_id'])
            return self._plan(scan)
        stats, links = {}, ({} if planner == 4 else None)
        inventory = target.build_inventory(resolved, stats=stats, links=links)
        resolved.update(snapshotDigest=target.snapshot_digest(inventory, resolved), fileCount=len(inventory['files']))
        options = {**opts, 'deep_passes': passes, **self._inventory_options(mode, resolved, inventory, planner, links)}
        scan = self.store.create_scan(mode=mode, safety_level=safety, target=resolved, inventory=inventory['files'], options=options)
        self._save_stat_cache(scan['scan_id'], stats)
        # Dependencies are checked before review so their advisories reach the reviewers as leads.
        if background_dependencies and opts.get('dependency_check', True) and not dependencies.disabled_by_environment():
            # osv-scanner can take minutes: answer now, and next_step holds review until the leads are saved.
            scan = self.store.set_scan_option(scan['scan_id'], 'dependenciesPending', True)
            self.store.add_event(scan['scan_id'], 'dependencies.started', 'Checking dependencies for known advisories.')
            self._start_dependency_check(scan['scan_id'])
            return self._plan(scan)
        return self._plan(self._record_dependencies(scan, self._dependency_receipt(scan)))

    @staticmethod
    def _inventory_options(mode, resolved, inventory, planner=None, links=None):
        options = {'surfaces': sorted(ledger.detect_surfaces(inventory['files'])),
                   'excluded': inventory['excluded'], 'truncated': inventory['truncated']}
        if mode == 'standard':
            options['planner'] = planner or standard.PLANNER_VERSION
            # Saved with the scan, so a planner change never moves a running scan's packets.
            options['standardAreas'] = standard.frozen_plan(inventory['files'])
            if options['planner'] >= 4:
                # Planner 4's connection map is saved too, since its questions are part of the plan.
                production = [f for f in inventory['files'] if standard.role(f, 3) == 'source']
                options['flowMap'] = flows.build_map(production, links or {}, options['standardAreas']['areas'])
        if mode == 'diff':
            options['diff'] = target.diff_manifest(resolved)
        return options

    def _prepare(self, scan):
        """Inventory a scan saved before its files were read; ``None`` when the target cannot be read."""
        resolved = dict(scan['target'])
        planner = scan['options'].get('planner')
        try:
            stats, links = {}, ({} if planner == 4 else None)
            inventory = target.build_inventory(resolved, stats=stats, links=links)
            resolved.update(snapshotDigest=target.snapshot_digest(inventory, resolved), fileCount=len(inventory['files']))
            options = self._inventory_options(scan['mode'], resolved, inventory, planner, links)
        except (SecurityError, OSError, ValueError) as exc:
            reason = f'Could not inventory the target: {exc}'[:500]
            self.store.update_scan_options(scan['scan_id'], {'preparing': False, 'dependenciesPending': False})
            self.store.set_scan_status(scan['scan_id'], 'failed', reason=reason)
            self.store.add_event(scan['scan_id'], 'scan.failed', reason)
            return None
        scan = self.store.set_inventory(scan['scan_id'], target=resolved, inventory=inventory['files'],
                                        options={**options, 'preparing': False})
        self._save_stat_cache(scan['scan_id'], stats)
        self.store.add_event(scan['scan_id'], 'scan.inventoried', f"Inventoried {len(inventory['files'])} files.",
                             {'files': len(inventory['files'])})
        return scan

    def _dependency_receipt(self, scan):
        root = scan['root']
        return self._check_dependencies(
            root=root, inventory=self.store.inventory(scan['scan_id']), data_dir=self.data_dir,
            work_dir=self._work(scan['scan_id']) / 'dependencies', enabled=scan['options'].get('dependency_check', True),
            read=lambda path, max_bytes=512_000: self._read_text(root, path, max_bytes=max_bytes))

    def _record_dependencies(self, scan, receipt):
        """Save the receipt and hand the scan to review, unless it was stopped while the check ran."""
        scan_id = scan['scan_id']
        self.store.set_scan_option(scan_id, 'dependencies', receipt)
        self.store.add_event(scan_id, 'dependencies.checked', dependencies.describe(receipt), dependencies.compact(receipt))
        scan = self.store.set_scan_option(scan_id, 'dependenciesPending', False)
        if scan['status'] == 'created':
            scan = self.store.set_scan_status(scan_id, 'awaiting_analysis')
            self.store.add_event(scan_id, 'scan.started', 'Snapshot inventoried; review packets ready.', {'packetCount': len(self._packets(scan))})
        return scan

    def _start_dependency_check(self, scan_id):
        """Run pending preparation (inventory, then the dependency check) in the background; work lost
        to a gateway restart starts again here."""
        with self._dependency_lock:
            if scan_id in self._dependency_threads:
                return
            def run():
                try:
                    scan = self._scan(scan_id)
                    if scan['options'].get('preparing'):
                        scan = self._prepare(scan)
                        if scan is None:
                            return
                    try:
                        receipt = self._dependency_receipt(scan)
                    except Exception as exc:  # a crashed check must not leave review waiting forever
                        receipt = {'status': 'not_run', 'reason': f'dependency check failed: {exc}'[:500], 'leads': [], 'runtimes': []}
                    self._record_dependencies(scan, receipt)
                except SecurityError:
                    pass  # the scan was deleted while its check ran
                finally:
                    with self._dependency_lock:
                        self._dependency_threads.pop(scan_id, None)
            thread = threading.Thread(target=run, name='security-dependency-check', daemon=True)
            self._dependency_threads[scan_id] = thread
            thread.start()

    @staticmethod
    def _read_text(root, path, max_bytes=512_000):
        from .target.inventory import safe_read
        try:
            data, _ = safe_read(root, path, max_bytes=max_bytes)
        except Exception:  # optional runtime-version files; any refusal just skips the file
            return None
        return data.decode('utf-8', 'replace')

    def _queue(self, scan):
        """Packet dispatch state for the rolling worker pool."""
        attempts = self._attempts(scan['scan_id'])
        done = {a['packet_id'] for a in attempts if a['status'] == 'accepted'}
        items = []
        for packet in sorted(self._packets(scan), key=lambda p: (p.get('priority', 0), p['packetId'])):
            items.append({'packetId': packet['packetId'], 'role': packet['role'], 'review': packet.get('review'),
                          'area': packet.get('area'), 'fileCount': packet.get('fileCount', len(packet.get('files', []))),
                          'lineCount': packet.get('lineCount'), 'state': 'done' if packet['packetId'] in done else 'pending'})
        pending = [i for i in items if i['state'] == 'pending']
        return {'schedule': standard.SCHEDULE, 'pending': len(pending), 'done': len(items) - len(pending), 'items': items}

    def get_scan(self, scan_id, section=None, limit=50, offset=0):
        scan = self._scan(scan_id)
        self.store._page(limit, offset)
        if section is not None and not isinstance(section, str):
            raise ValidationError('section must be text')
        if section is None:
            return {**scan, 'scanId': scan_id, 'plan': self._plan(scan), 'progress': self.progress(scan_id),
                    'stall': self.stall(scan), 'timing': timing.scan_timing(scan, self._now())}
        if section == 'summary':
            # Small header an agent can always read, even when the full plan exceeds the tool result cap.
            plan = self._plan(scan, packets=[])
            packets = self._packets(scan)
            keep = ('scanId', 'root', 'snapshotDigest', 'methodologyVersion', 'surfaces', 'specialistProfiles',
                    'workerBriefPath', 'workDir', 'submit', 'inventory', 'status', 'context')
            return {**{k: plan[k] for k in keep}, 'mode': scan['mode'], 'safetyLevel': scan['safety_level'],
                    'packetCount': len(packets), 'packetIds': [p['packetId'] for p in packets],
                    'laneCount': len(plan['lanes']), 'excludedTotal': plan['excluded']['total'],
                    'schedule': standard.SCHEDULE, 'dependencyCheck': plan['dependencyCheck'],
                    'delegate': scan['options'].get('delegate'),
                    'possibleDuplicates': self._possible_duplicates(scan_id),
                    'timing': timing.scan_timing(scan, self._now()),
                    'next': self.next_step(scan_id),
                    'sections': ['summary', 'next', 'queue', 'packets', 'dependencies', 'inventory', 'coverage', 'candidates',
                                 'findings', 'workers', 'validations', 'chains', 'activity', 'manifest', 'report']}
        if section == 'next':
            return self.next_step(scan_id)
        if section == 'queue':
            queue = self._queue(scan)
            return {**queue, 'items': queue['items'][offset:offset + limit], 'total': len(queue['items'])}
        if section == 'dependencies':
            receipt = scan['options'].get('dependencies') or {}
            leads = receipt.get('leads', [])
            return {'check': dependencies.compact(receipt), 'items': leads[offset:offset + limit], 'total': len(leads),
                    'runtimes': receipt.get('runtimes', [])}
        if section == 'coverage':
            return self.coverage_view(scan_id)
        if section == 'activity':
            return self.activity(scan_id, after_id=offset, limit=limit)
        if section == 'chains':
            return self.store.get_chains(scan_id) or self.propose_chains(scan_id)
        if section in {'manifest', 'report'}:
            if not scan['sealed_at']:
                raise Conflict('scan is not finalized')
            raw = read_artifact(scan_dir(self.data_dir, scan_id), 'scan-manifest.json' if section == 'manifest' else 'report.md')
            return json.loads(raw) if section == 'manifest' else {'content': raw.decode()}
        if section == 'inventory':
            rows = self.store.inventory(scan_id)
        elif section == 'packets':
            rows = self._packets(scan)
        elif section in {'findings', 'candidates'}:
            if section == 'findings' and scan['sealed_at']:
                return self.store.list_findings(scan_id=scan_id, limit=limit, offset=offset)
            rows = self._candidates(scan_id)
        elif section == 'workers':
            rows = self._attempts(scan_id)
        elif section == 'validations':
            rows = self.store.validations(scan_id)
        else:
            raise ValidationError('unknown scan section')
        return {'items': rows[offset:offset + limit], 'total': len(rows)}

    # Bookkeeping events that must not count as the scan moving forward (automatic-continue guard).
    NON_PROGRESS_EVENTS = ('autoresume.scheduled', 'autoresume.paused', 'worker.dispatched', 'worker.missing',
                           'scan.blocked')

    def stall(self, scan, now=None):
        """``{'since', 'why'}`` when an open scan has stopped moving, else ``None`` (read-time only)."""
        from datetime import datetime, timezone
        if scan['sealed_at'] or scan['status'] not in stall.OPEN:
            return None
        try:
            now = now or datetime.now(timezone.utc)
            ignore = tuple(k for k in self.NON_PROGRESS_EVENTS if k != 'scan.blocked')
            return stall.derive(scan, self.next_step(scan['scan_id'], now=now),
                                self.store.last_event(scan['scan_id'], ignore=ignore), now)
        except SecurityError:
            return None

    def _deep_stop(self, scan, attempts, packets):
        """Deep mode stops after consecutive passes with no new root cause (same rule as resume)."""
        known, counts = set(), []
        candidates = self._candidates(scan['scan_id'])
        for packet in packets:
            matched = [a for a in attempts if a['packet_id'] == packet['packetId'] and a['status'] == 'accepted']
            if not matched:
                break
            aids = {a['attempt_id'] for a in matched}
            roots = {(c['ruleId'], c['identity']['anchor']) for c in candidates if aids.intersection(c.get('provenance', {}).get('workerAttemptIds', []))}
            counts.append(len(roots - known)); known.update(roots)
        return deep.stop_reason(counts, scan['options']['deep_passes'])

    def next_step(self, scan_id, now=None):
        """The parent's next step, derived only from what the scan has stored."""
        from datetime import datetime, timezone
        scan = self._scan(scan_id)
        progress_key = self.store.progress_key(scan_id, ignore=self.NON_PROGRESS_EVENTS)
        if scan['sealed_at'] or scan['status'] == 'canceled':
            return next_step.derive(scan=scan, packets=[], attempts=[], dispatches=[], reduced=None, receipts=[],
                                    chains_recorded=False, progress_key=progress_key, now=None)
        if scan['options'].get('dependenciesPending'):
            self._start_dependency_check(scan_id)
        attempts = self._attempts(scan_id)
        packets = self._packets(scan)
        try:
            reduced = self._reduced(scan_id)
        except Conflict:
            reduced = None
        result = next_step.derive(
            scan=scan, packets=packets, attempts=attempts, dispatches=self.store.dispatches(scan_id),
            reduced=reduced, receipts=self.store.validations(scan_id),
            chains_recorded=self.store.get_chains(scan_id) is not None, progress_key=progress_key,
            now=now or datetime.now(timezone.utc), max_parallel=standard.MAX_PARALLEL_WORKERS,
            deep_stopped=scan['mode'] == 'deep' and bool(self._deep_stop(scan, attempts, packets)),
            duplicates=self._open_pairs(reduced)[0] if reduced else ())   # an undecided pair is not asked about again
        if result['step'] == 'finalize':
            unresolved = deployment.pending(self.store.coverage_units(scan_id))
            if unresolved:
                result['reason'] = 'Repository results are saved; deployment evidence needs a parent disposition.'
                result['deploymentEvidence'] = unresolved[:next_step.ITEM_LIMIT]
                result['deploymentEvidenceTotal'] = len(unresolved)
                result['how'] = ('Before finalizing, read deployment units in coverage and record each parent disposition '
                                 'with security_scan_checkpoint: resolved with source/control evidence, or deferred with '
                                 'the exact missing artifacts and review note. Do not launch repeat workers or fetch images. '
                                 'Then finalize; deferred deployment evidence remains partial.')
        delegate = scan['options'].get('delegate')
        if delegate and result['step'] in {'dispatch', 'validate'}:
            result['delegate'] = delegate
            result['how'] += ' ' + self.delegate_instruction(delegate)
        return result

    # advanced-delegation's per-task parameter for each of its backends.
    DELEGATE_PARAMS = {'model_map': ('option', 'model route'), 'profile_routes': ('profile', 'profile')}

    def _delegate(self, value):
        if not isinstance(value, dict) or set(value) != {'backend', 'name'} or value['backend'] not in self.DELEGATE_PARAMS:
            raise ValidationError('delegate must be {backend: model_map|profile_routes, name}')
        name = value['name']
        if not isinstance(name, str) or not 0 < len(name) <= 64 or not name.isprintable() or '"' in name or chr(92) in name:
            raise ValidationError('delegate name must be short text')
        return {'backend': value['backend'], 'name': name}

    @classmethod
    def delegate_instruction(cls, delegate):
        param, what = cls.DELEGATE_PARAMS[delegate['backend']]
        return (f'Set "{param}": "{delegate["name"]}" on every delegate_task task: the user chose that {what} '
                'for this scan\'s subagents.')

    def _coverage_input(self, scan, units, source, *, worker=False):
        if not isinstance(units, list) or len(units) > 50000:
            raise ValidationError('coverage must be a bounded list')
        paths = self.store.inventory_paths(scan['scan_id'])
        for key in ('changed', 'added', 'deleted'):
            paths.update(scan['options'].get('diff', {}).get(key, []))
        lanes = {r['id'] for r in standard.lanes()}
        controls = {r['id'] for r in ledger.load_asvs()['controls']}
        result = []
        for unit in units:
            if not isinstance(unit, dict):
                raise ValidationError('coverage unit must be an object')
            ident = unit.get('unit', unit.get('unitId'))
            if not isinstance(ident, str) or ':' not in ident:
                raise ValidationError('invalid coverage unit identifier')
            kind, value = ident.split(':', 1)
            detail = deployment.check_unit(ident, unit, paths, worker=worker) if kind == 'deployment' else None
            if kind != 'deployment' and 'deployment' in unit:
                raise ValidationError('deployment detail requires a deployment unit')
            valid = (kind == 'deployment') or (kind == 'file' and value in paths) or (kind == 'lane' and value in lanes) or (kind == 'asvs' and value in controls)
            if not valid or unit.get('state') not in {'reviewed', 'not_applicable', 'deferred', 'unsupported', 'unknown', 'failed'}:
                raise ValidationError('unknown review unit or coverage state')
            note = unit.get('note', unit.get('reason', 'explicit review disposition'))
            if not isinstance(note, str):
                raise ValidationError('coverage note must be text')
            result.append({'unitId': ident, 'kind': kind, 'state': unit['state'], 'reason': note, 'evidence': [source]})
            if detail is not None:
                result[-1]['deployment'] = detail
        self._safe(result)
        return result

    def checkpoint(self, scan_id, coverage=None, note=None, blocked=False):
        with self._mutation(scan_id) as scan:
            if note is not None and not isinstance(note, str):
                raise ValidationError('checkpoint note must be text')
            if not isinstance(blocked, bool):
                raise ValidationError('blocked must be a boolean')
            self._safe(note)
            source = stable_id('checkpoint', scan_id, coverage, note)
            units = self._coverage_input(scan, coverage or [], source)
            self.store.record_coverage(scan_id, units, source=source)
            if blocked:
                # The agent gave up on this turn: the workbench shows the scan as stalled with this note.
                self.store.add_event(scan_id, 'scan.blocked', note or 'The agent could not continue.')
            else:
                self.store.add_event(scan_id, 'scan.checkpoint', note or 'Coverage checkpoint recorded.')
            return {'scanId': scan_id, 'recorded': len(units), 'coverage': self.coverage_view(scan_id)}

    def submit_worker_result(self, payload):
        problems = validate_worker_result(payload)
        if problems:
            # Raw schema paths can contain attacker-supplied field names; echo only declared ones.
            raise ValidationError('worker result violates the input contract: ' + '; '.join(public_problems(problems)))
        scan_id = payload['scanId']
        with self._mutation(scan_id) as scan:
            if payload['snapshotDigest'] != scan['snapshot_digest']:
                raise Conflict('worker snapshotDigest does not match scan')
            self._current(scan)
            packets = {p['packetId']: p for p in self._packets(scan)}
            packet = payload['packetId']
            if packet is not None and (packet not in packets or packets[packet]['role'] != payload['workerRole']):
                raise ValidationError('packet does not belong to this scan or role')
            aid = payload['attemptId']
            units = self._coverage_input(scan, payload['coverage'], aid, worker=True)
            self._safe({'notes': payload['notes'], 'negativeResults': payload['negativeResults']})
            inventory = self.store.inventory(scan_id)
            accepted, rejected = [], []
            for index, raw in enumerate(payload['candidates']):
                try:
                    candidate = check_candidate(raw, scan=scan, inventory=inventory, attempt_id=aid)
                    if candidate['evidenceState'] in {'source_supported', 'runtime_confirmed'}:
                        # Finalize would demote it to inconclusive; refuse now while the worker can still fix it.
                        problems = reports.validate_finding_sections(candidate)
                        if problems:
                            raise ValidationError('reportable candidate has incomplete sections: ' + '; '.join(problems))
                    candidate['diffAttribution'] = diff.attribution(candidate, scan['options']['diff']) if scan['mode'] == 'diff' else None
                    self._safe(candidate)
                    accepted.append(candidate)
                except SecurityError as exc:
                    rejected.append({'index': index, 'reason': str(exc)})
            # Detect changes during excerpt reads before committing any evidence.
            self._current(scan)
            digest = sha256_hex(canonical_json(payload))
            attempt = self.store.record_attempt(scan_id, aid, payload['workerRole'], packet_id=packet)
            if attempt['status'] != 'running':
                if attempt['status'] == 'accepted' and attempt['result_digest'] == digest:
                    return {'scanId': scan_id, 'attemptId': aid, 'inserted': 0, 'duplicates': len(accepted), 'rejected': rejected}
                raise Conflict('attempt already finished with a different result')
            counts = self.store.upsert_candidates(scan_id, aid, accepted)
            if payload['workerRole'] == 'validator':
                for candidate in accepted:
                    state = candidate['evidenceState']
                    if state in {'source_supported', 'rejected', 'inconclusive'}:
                        self.store.set_candidate_state(scan_id, candidate['candidateId'], state, reason='Validator source review; runtime NOT RUN.')
            # The store merges deployment observations transactionally (including reopening
            # resolved boundaries), while other units retain monotonic worker closure.
            # Parent adjudication happens after all relevant workers finish; no packet is added.
            self.store.record_coverage(scan_id, units, source=aid, monotonic=True)
            if packet is not None and packets[packet].get('review') in {'area', 'remainder'}:
                self._record_remainder(scan, packets[packet], units)
            self.store.finish_attempt(scan_id, aid, 'accepted', result_digest=digest)
            self.store.add_event(scan_id, 'worker.accepted', 'Worker result recorded.', {'attemptId': aid, **counts, 'rejected': rejected, 'negativeResults': payload['negativeResults'], 'notes': payload['notes']})
            return {'scanId': scan_id, 'attemptId': aid, **counts, 'rejected': rejected}

    def _record_remainder(self, scan, packet, units):
        """Queue the files an area worker left unfinished for a fresh worker."""
        done = {'reviewed', 'not_applicable'}
        covered = {u['unitId'] for u in self.store.coverage_units(scan['scan_id']) if u['state'] in done}
        covered.update(u['unitId'] for u in units if u['state'] in done)
        left = sorted(p for p in packet.get('files', []) if 'file:' + p not in covered)
        remainders = dict(self._scan(scan['scan_id'])['options'].get('remainders', {}))
        # A later attempt on the same packet can finish what an earlier one deferred; an empty
        # list retires the queued remainder instead of leaving it pending.
        if left or packet['packetId'] in remainders:
            remainders[packet['packetId']] = left
            self.store.set_scan_option(scan['scan_id'], 'remainders', remainders)
        if left:
            self.store.add_event(scan['scan_id'], 'packet.remainder', f'{len(left)} unfinished files queued for a fresh worker.',
                                 {'parentPacketId': packet['packetId'], 'files': len(left)})

    def _work(self, scan_id):
        path = scan_dir(self.data_dir, scan_id) / 'work'
        if is_link(path):
            raise ValidationError('work directory cannot be a symlink')
        path.mkdir(mode=0o700, exist_ok=True)
        set_private(path, 0o700)
        return path

    def _candidates(self, scan_id):
        rows = self.store.list_candidates(scan_id)
        receipts = self.store.validations(scan_id)
        by_candidate = {}
        for receipt in receipts:
            by_candidate.setdefault(receipt['candidateId'], []).append(receipt)
        for row in rows:
            reason = row.pop('dispositionReason', None)
            if reason and row['evidenceState'] in {'rejected', 'inconclusive'}:
                row.setdefault('proofGaps', []).append(reason)
            rs = by_candidate.get(row['candidateId'], [])
            if rs:
                last = rs[-1]
                row['validation'] = {'status': last['status'], 'level': last['level'], 'receiptIds': [r['receiptId'] for r in rs], 'summary': '; '.join(last.get('notes', []))}
        return rows

    def record_validations(self, scan_id, receipts=None, plans=None, *, user_authorized=False, authorization_source=None):
        if receipts is not None and plans is not None:
            raise ValidationError('choose receipts or plans')
        inputs = plans if plans is not None else receipts
        if not isinstance(inputs, list) or len(inputs) > 200 or any(not isinstance(x, dict) for x in inputs):
            raise ValidationError('validation inputs must be a bounded object list')
        self._safe(inputs)
        for item in inputs:
            if not isinstance(item.get('candidateId'), str) or not isinstance(item.get('level', 'static'), str):
                raise ValidationError('validation requires candidateId and a string level')
            if plans is not None and any(not isinstance(item.get(k, []), list) for k in ('requests', 'commands')):
                raise ValidationError('plan operations must be lists')
            if item.get('grantId') is not None and not isinstance(item['grantId'], str):
                raise ValidationError('grantId must be a string')
        if plans is not None and sum(len(p.get('requests', [])) + len(p.get('commands', [])) for p in inputs) > 200:
            raise PolicyDenied('runtime operation budget exceeded')
        grant_ids = {p.get('grantId') for p in inputs if p.get('grantId')}
        owner = ('grant:' + next(iter(grant_ids))) if len(grant_ids) == 1 else None
        with self._mutation(scan_id, owner=owner) as scan:
            self._current(scan)
            candidates = {c['candidateId']: c for c in self._candidates(scan_id)}
            recorded = []
            for item in inputs:
                cid = item.get('candidateId')
                if cid not in candidates:
                    raise NotFound('candidate not found in scan')
                candidate = candidates[cid]
                inferred = {'local-command': 'local-safe', 'http-probe': 'active-authorized'}.get(item.get('kind'), 'static')
                level = item.get('level', inferred if plans is not None else 'static')
                if plans is None:
                    problems = validate_document('validation-receipt', item)
                    if problems:
                        raise ValidationError('invalid validation receipt')
                    validation.validate_receipt(item)
                    if level != 'static':
                        # Execution observations must be generated here, not claimed by an agent.
                        known = self.store.validations(scan_id, cid)
                        if item not in known:
                            raise PolicyDenied('runtime receipts must come from service-executed plans')
                    receipt = item
                else:
                    plan = {**item, 'scanId': scan_id, 'level': level}
                    grant = None
                    # The scan's "Run tests to confirm findings" switch is the consent for
                    # local tests; the agent writes and runs them itself.
                    if level == 'local-safe' and scan['safety_level'] == 'static':
                        raise PolicyDenied('this scan was started without permission to run tests; start a new scan with tests allowed')
                    if item.get('kind') == 'local-command' and level != 'local-safe':
                        raise PolicyDenied('local commands use level local-safe')
                    if level == 'active-authorized':
                        if not isinstance(item.get('grantId'), str):
                            raise PolicyDenied('testing a running app requires the user to allow its address first')
                        try:
                            grant = self.store.get_grant(item['grantId'])
                        except NotFound as exc:
                            raise PolicyDenied('testing a running app requires a valid permission from the user') from exc
                    validation.check_plan(plan, grant=grant, safety_level=level, now=self._now())
                    if level == 'static':
                        receipt = validation.static_receipt(candidate, checks=plan.get('checks', {}))
                    else:
                        if len(plan.get('requests', [])) + len(plan.get('commands', [])) > 200:
                            raise PolicyDenied('runtime operation budget exceeded')
                        if plan['kind'] == 'http-probe':
                            receipt = validation.run_http(plan, grant=grant, store=self.store)
                        else:
                            receipt = validation.run_local(plan, target=scan['target'], workdir_parent=self._work(scan_id), grant=grant)
                if plans is not None and user_authorized is True and authorization_source == 'dashboard':
                    receipt['notes'].append('User authorization source: dashboard')
                    receipt['receiptId'] = stable_id('vrc', {k: v for k, v in receipt.items() if k != 'receiptId'})
                self._safe(receipt)
                existing = next((r for r in self.store.validations(scan_id) if r['receiptId'] == receipt['receiptId']), None)
                if existing is not None and existing != receipt:
                    raise Conflict('receipt identity already records a different observation')
                state = validation.next_evidence_state(candidate['evidenceState'], receipt)
                self.store.record_validation(scan_id, receipt)
                self.store.set_candidate_state(scan_id, cid, state, reason='Validation receipt recorded: ' + receipt['receiptId'])
                candidate['evidenceState'] = state
                recorded.append(receipt)
            self.store.add_event(scan_id, 'validations.recorded', 'Validation receipts recorded.', {'count': len(recorded)})
            return {'scanId': scan_id, 'receipts': recorded}

    def _reduced(self, scan_id):
        # A validator's state is copied onto the discovery rows, so the reducer breaks the tie
        # by order: discovery rows first, then validator rows in the order they finished.
        attempts = {a['attempt_id']: a for a in self._attempts(scan_id)}
        def order(row):
            aid = (row.get('provenance', {}).get('workerAttemptIds') or [''])[0]
            a = attempts.get(aid, {})
            return (a.get('role') == 'validator', a.get('finished_at') or '', aid)
        result = reducer.reduce_candidates(sorted(self._candidates(scan_id), key=order))
        if result['problems']:
            raise Conflict('candidate conservation check failed')
        return result

    @staticmethod
    def _open_pairs(reduced):
        """Open possible duplicate pairs as (blocking, undecided, all of them in listing order).

        A pair is undecided while it is still open and either side named the other in
        provenance.maybeDuplicateOf; it no longer blocks finalize, which seals it as a gap.
        """
        parked = {tuple(pair) for pair in reduced['undecided']}
        pairs = [{**pair, 'verdict': 'undecided'} if tuple(pair['candidateIds']) in parked else pair
                 for pair in reducer.possible_duplicates(reduced['retained'], reduced['distinct'])]
        return [p for p in pairs if 'verdict' not in p], [p for p in pairs if 'verdict' in p], pairs

    def _possible_duplicates(self, scan_id):
        """Candidate pairs a validator should check for one root cause before finalize."""
        try:
            return self._open_pairs(self._reduced(scan_id))[2]
        except Conflict:  # a broken conservation check is reported by finalize, not by the summary
            return []

    def _chain_inputs(self, scan, candidates):
        result = []
        for row in candidates:
            if row['evidenceState'] not in {'candidate', 'source_supported', 'runtime_confirmed'}:
                continue
            item = deepcopy(row)
            item.update(findingId=finding_id(scan['repo_key'], item['ruleId'], item['identity']['anchor']), scanId=scan['scan_id'], snapshotDigest=scan['snapshot_digest'])
            result.append(item)
        return result

    def propose_chains(self, scan_id):
        scan = self._scan(scan_id)
        return chains.build_chains_doc(scan_id, self._chain_inputs(scan, self._reduced(scan_id)['retained']), snapshot_digest=scan['snapshot_digest'])

    def record_chains(self, scan_id, explanations=None):
        self._safe(explanations)
        if explanations is not None and not isinstance(explanations, dict):
            raise ValidationError('chain explanations must be an object')
        with self._mutation(scan_id) as scan:
            doc = chains.build_chains_doc(scan_id, self._chain_inputs(scan, self._reduced(scan_id)['retained']), snapshot_digest=scan['snapshot_digest'], explanations=explanations)
            return self.store.record_chains(scan_id, doc)

    def coverage(self, scan_id):
        scan = self._scan(scan_id)
        if scan['sealed_at']:
            return json.loads(read_artifact(scan_dir(self.data_dir, scan_id), 'coverage.json'))
        return build_coverage(scan, self.store.inventory(scan_id), self.store.coverage_units(scan_id), self._attempts(scan_id), self._packets(scan), self._candidates(scan_id))

    def coverage_view(self, scan_id):
        """Coverage for the workbench and agent reads: files a focused standard scan skipped by design
        are counted by reason, not listed one by one (tens of thousands of paths on a large
        repository). coverage.json in the sealed report keeps the full list."""
        result = self.coverage(scan_id)
        files = result.get('files', {})
        kept, by_design = [], {}
        for item in files.get('excluded', []):
            reason = str(item.get('reason', ''))
            if reason.startswith('standard_'):
                by_design[reason] = by_design.get(reason, 0) + 1
            else:
                kept.append(item)
        if by_design:
            result['files'] = {**files, 'excluded': kept, 'excludedByDesign': dict(sorted(by_design.items()))}
        return result

    def finalize(self, scan_id):
        with self._mutation(scan_id) as scan:
            if deployment.pending(self.store.coverage_units(scan_id)):
                raise ValidationError('deployment evidence requires an explicit parent checkpoint disposition before finalizing')
            reduced = self._reduced(scan_id)
            blocking, _, open_pairs = self._open_pairs(reduced)
            # A target whose reviewed files changed accepts no more results, so no verdict can
            # arrive: the scan still seals what it has, as partial, with the open pairs named.
            stale = not target.is_snapshot_current(scan['target'], scan['snapshot_digest'])
            diff = target.compare_inventory(scan['target'], self.store.inventory(scan_id)) if stale else None
            added_only = diff is not None and not diff['changed']
            if blocking and (not stale or added_only):
                raise ValidationError(f'{len(blocking)} possible duplicate pair(s) need a same-bug-or-different verdict from a validator before finalizing; read security_scan_get section next')
            findings, retained, gaps = [], [], []
            for row in reduced['retained'] + reduced['rejected']:
                if row['evidenceState'] in {'source_supported', 'runtime_confirmed'}:
                    try:
                        problems = reports.validate_finding_sections(row)
                        if problems:
                            raise ValidationError('; '.join(problems))
                        findings.append(promote_to_finding(row, scan_id=scan_id, repo_key=scan['repo_key']))
                        continue
                    except ValidationError as exc:
                        row = deepcopy(row)
                        row['evidenceState'] = 'inconclusive'
                        row.setdefault('proofGaps', []).append(str(exc))
                        gaps.append('Candidate ' + row['candidateId'] + ' has incomplete report sections.')
                row.setdefault('proofGaps', []).append('Not promoted to a reportable finding.')
                for field in ('subsumes', 'duplicateOf', 'distinctFrom', 'maybeDuplicateOf'):
                    row.get('provenance', {}).pop(field, None)
                retained.append(row)
            coverage = self.coverage(scan_id)
            receipt = scan['options'].get('dependencies')
            # Every advisory and end-of-life runtime as one finding, rated at the worst of them.
            aggregate = dependency_finding.build(receipt, scan_id=scan_id, repo_key=scan['repo_key'])
            if aggregate:
                findings.append(aggregate)
            if receipt and receipt.get('status') == 'not_run':
                # Requested but unavailable: known-vulnerable dependencies were not checked.
                gaps.append(dependencies.describe(receipt))
            if stale:
                if added_only:
                    gaps.append(f"{len(diff['added'])} files were added after this scan started and were not reviewed.")
                else:
                    gaps.append('Target changed after this recorded snapshot; evidence describes the original snapshot only.')
            if open_pairs:
                gaps.append(f'{len(open_pairs)} possible duplicate pair(s) were sealed without a same-bug-or-different verdict.')
            if gaps:
                coverage['gaps'].extend(gaps)
                coverage['completeness'] = 'partial'
            # Rebuild eligibility after final report validation; never seal dangling chain references.
            saved = self.store.get_chains(scan_id)
            explanations = {c['chainId']: {'title': c['title'], 'summary': c['summary']} for c in (saved or {}).get('chains', [])}
            chain_inputs = [{**f, 'scanId': scan_id, 'snapshotDigest': scan['snapshot_digest']} for f in findings]
            chains_doc = chains.build_chains_doc(scan_id, chain_inputs, snapshot_digest=scan['snapshot_digest'], explanations=explanations)
            manifest = {'documentType': 'hermes-security.scan-manifest', 'schemaVersion': '1.0', 'scanId': scan_id,
                        'producer': dict(PRODUCER), 'methodologyVersion': METHODOLOGY_VERSION,
                        'mode': scan['mode'], 'safetyLevel': scan['safety_level'], 'status': 'completed' if coverage['completeness'] == 'complete' else 'partial',
                        'target': scan['target'], 'runtime': {'provider': scan['options'].get('provider'), 'model': scan['options'].get('model'), 'profile': self.profile, 'fallbacks': []},
                        'startedAt': scan['created_at'], 'finishedAt': self._now(), 'counts': {}, 'artifacts': {}, 'supersedes': None}
            if receipt:
                manifest['dependencyCheck'] = {**dependencies.compact(receipt),
                                               'leads': receipt.get('leads', [])[:dependencies.REPORT_LEADS]}
            findings_doc = {'documentType': 'hermes-security.findings', 'schemaVersion': '1.0', 'scanId': scan_id, 'findings': findings, 'retained': retained}
            bundle = reports.finalize_bundle(manifest=manifest, findings_doc=findings_doc, coverage=coverage, chains_doc=chains_doc)
            directory = scan_dir(self.data_dir, scan_id)
            for rel, data in bundle.items():
                self._safe(json.loads(data) if rel.endswith(('.json', '.sarif')) else data.decode(), bounded=False)
                write_artifact(directory, rel, data)
            verified = reports.verify_bundle(directory)
            if not verified['ok']:
                raise Conflict('artifact verification failed')
            manifest = json.loads(bundle['scan-manifest.json'])
            self.store.record_chains(scan_id, chains_doc)
            # Store v1 rejects index writes after sealing; index and status must precede seal.
            self.store.upsert_finding_index(scan_id, findings)
            self.store.set_scan_status(scan_id, manifest['status'])
            self.store.seal(scan_id, manifest['seal']['digest'], str(directory))
            self.store.add_event(scan_id, 'scan.finalized', 'Verified bundle sealed.', {'status': manifest['status']})
            return {'scanId': scan_id, 'status': manifest['status'], 'manifest': manifest, 'artifactDir': str(directory), 'verified': verified,
                    'possibleDuplicates': open_pairs}   # empty unless pairs were sealed open: undecided, or a changed target

    def cancel(self, scan_id):
        with self._mutation(scan_id, canceled=True):
            for attempt in self._attempts(scan_id):
                if attempt['status'] == 'running':
                    self.store.finish_attempt(scan_id, attempt['attempt_id'], 'canceled')
            self.store.set_scan_status(scan_id, 'canceled')
            self.store.add_event(scan_id, 'scan.canceled', 'Scan canceled; outstanding work is not accepted.')
            return {'scanId': scan_id, 'status': 'canceled'}

    def resume(self, scan_id):
        with self._mutation(scan_id, canceled=True) as scan:
            self._current(scan)
            attempts = self._attempts(scan_id)
            accepted = {a['packet_id'] for a in attempts if a['status'] == 'accepted'}
            packets = [p for p in self._packets(scan) if p['packetId'] not in accepted]
            for attempt in attempts:
                if attempt['status'] == 'running':
                    self.store.finish_attempt(scan_id, attempt['attempt_id'], 'missing', error='Interrupted before an accepted result.')
            for row in self.store.dispatches(scan_id):
                if row['status'] == 'running':
                    self.store.finish_dispatch(scan_id, row['attempt_id'], detail='scan resumed')
            for packet in packets:
                prior = [a for a in attempts if a['packet_id'] == packet['packetId']]
                if prior:
                    packet['attemptId'] = stable_id('att', scan_id, packet['packetId'], 'resume', len(prior))
            stop = None
            if scan['mode'] == 'deep':
                stop = self._deep_stop(scan, attempts, self._packets(scan))
                if stop:
                    packets = []
            self.store.set_scan_status(scan_id, 'awaiting_analysis')
            # An explicit resume always lifts an automatic-continue hold. The event below is not
            # enough: while duplicate pairs wait for a verdict the progress key ignores events.
            # A continue that is still queued stays recorded, so a second one is not sent.
            self.store.update_scan_option(scan_id, 'autoResume',
                                          lambda old: {**(old or {}), 'count': 0, 'stoppedAt': None, 'offAt': None})
            self.store.add_event(scan_id, 'scan.resumed', 'Remaining review packets returned.', {'packetCount': len(packets), 'stopReason': stop})
            return {**self._plan(scan, packets), 'status': 'awaiting_analysis', 'stopReason': stop}

    def export(self, scan_id, fmt):
        scan = self._scan(scan_id)
        if not scan['sealed_at']:
            raise Conflict('scan must be finalized before export')
        if not isinstance(fmt, str) or fmt not in {'md', 'sarif', 'json', 'csv'}:
            raise ValidationError('unsupported export format')
        data, content_type, filename = reports.export(scan_dir(self.data_dir, scan_id), fmt)
        return {'scanId': scan_id, 'content': data.decode('utf-8'), 'contentType': content_type, 'filename': filename}

    def summary(self):
        with self.store._connection() as conn:
            statuses = {r[0]: r[1] for r in conn.execute('SELECT status,count(*) FROM scans GROUP BY status')}
            count = conn.execute('SELECT count(DISTINCT finding_id) FROM findings').fetchone()[0]
            repositories = conn.execute('SELECT count(*) FROM repositories').fetchone()[0]
            open_by_severity = {r[0]: r[1] for r in conn.execute(
                "SELECT f.severity,count(DISTINCT f.finding_id) FROM findings f LEFT JOIN triage t ON t.finding_id=f.finding_id "
                "WHERE COALESCE(t.state,'open')='open' GROUP BY f.severity")}
        return {'scans': sum(statuses.values()), 'statuses': statuses, 'findings': count, 'repositories': repositories,
                'open': open_by_severity, 'profile': self.profile}

    def _filters(self, filters, allowed):
        if set(filters) - set(allowed):
            raise ValidationError('unknown list filter')
        for key, value in filters.items():
            if value is None:
                continue
            if key in {'limit', 'offset'}:
                if type(value) is not int or value < 0 or (key == 'limit' and value > 1000):
                    raise ValidationError('pagination requires nonnegative integers; limit <=1000')
            elif key == 'chained':
                if type(value) is not bool:
                    raise ValidationError('chained must be a boolean')
            elif not isinstance(value, str):
                raise ValidationError('filter must be text')
        self._safe(filters)

    def list_scans(self, **filters):
        self._filters(filters, {'q', 'status', 'repo_key', 'limit', 'offset'})
        return self.store.list_scans(**filters)

    def workbench_scans(self, *, q=None, status=None, mode=None, repo_key=None, limit=50, offset=0):
        """Read-only SQL pagination including the desktop's scan-mode and repository filters."""
        self._filters(dict(q=q, status=status, mode=mode, repo_key=repo_key, limit=limit, offset=offset),
                      {'q', 'status', 'mode', 'repo_key', 'limit', 'offset'})
        terms, args = [], []
        for key, value in [('status', status), ('mode', mode), ('repo_key', repo_key)]:
            if value is not None:
                terms.append(key + '=?'); args.append(value)
        if q:
            terms.append('(root LIKE ? OR scan_id LIKE ?)'); args.extend(['%' + q + '%'] * 2)
        where = ' WHERE ' + ' AND '.join(terms) if terms else ''
        with self.store._connection() as conn:
            total = conn.execute('SELECT count(*) FROM scans' + where, args).fetchone()[0]
            ids = conn.execute('SELECT scan_id FROM scans' + where + ' ORDER BY rowid DESC LIMIT ? OFFSET ?', args + [limit, offset])
            items = [self.store._scan(conn, row[0]) for row in ids]
            for item in items:
                # Findings are indexed at seal time; unsealed scans have no counts yet.
                item['counts'] = None if not item['sealed_at'] else {
                    sev: n for sev, n in conn.execute(
                        'SELECT severity,count(DISTINCT finding_id) FROM findings WHERE scan_id=? GROUP BY severity',
                        (item['scan_id'],))}
        now = self._now()
        for item in items:
            item['stall'] = self.stall(item)
            item['timing'] = timing.scan_timing(item, now)
        return {'items': items, 'total': total}

    def workbench_findings(self, *, q=None, triage=None, severity=None, evidence_state=None,
                           validation_level=None, repo_key=None, folder=None, owasp=None,
                           chained=None, limit=50, offset=0):
        """Filter all workbench fields before LIMIT; never select evidence blobs.

        The store lacks a validation-level filter. Keep this additive query
        here rather than exposing database access to HTTP handlers.
        """
        filters = dict(q=q, triage=triage, severity=severity, evidence_state=evidence_state,
                       validation_level=validation_level, repo_key=repo_key, folder=folder, owasp=owasp,
                       chained=chained, limit=limit, offset=offset)
        self._filters(filters, filters.keys())
        terms, args = [], []
        if folder:
            # A prefix compare, not LIKE: folder names may contain % or _.
            prefix = folder.strip('/') + '/'
            terms.append('substr(f.path,1,?)=?'); args.extend([len(prefix), prefix])
        for key, value in [('severity', severity), ('evidence_state', evidence_state), ('repo_key', repo_key),
                           ('chained', None if chained is None else int(chained))]:
            if value is not None:
                terms.append('f.' + key + '=?'); args.append(value)
        if triage is not None:
            terms.append("COALESCE(t.state,'open')=?"); args.append(triage)
        if validation_level is not None:
            terms.append("json_extract(f.data,'$.validation.level')=?"); args.append(validation_level)
        if owasp is not None:
            terms.append('EXISTS(SELECT 1 FROM json_each(f.owasp) WHERE value=?)'); args.append(owasp)
        if q:
            columns = ['title', 'path', 'category', 'cwe', 'chain_text', 'root', 'repo_key']
            terms.append('(' + ' OR '.join('f.' + key + ' LIKE ?' for key in columns) + ')')
            args.extend(['%' + q + '%'] * len(columns))
        base = ' FROM findings f LEFT JOIN triage t ON t.finding_id=f.finding_id'
        if terms:
            base += ' WHERE ' + ' AND '.join(terms)
        columns = ('f.finding_id,f.occurrence_id,f.scan_id,f.severity,f.evidence_state,f.title,f.repo_key,f.root,'
                   'f.path,f.category,f.cwe,f.owasp,f.chained,f.sealed_at,'
                   "json_extract(f.data,'$.validation.level') AS validation_level,"
                   "COALESCE(t.state,'open') AS triage_state,t.note AS triage_note,t.updated_at AS triage_updated_at")
        with self.store._connection() as conn:
            total = conn.execute('SELECT count(*)' + base, args).fetchone()[0]
            items = []
            # Worst severity first (unknown/unrecognized last), newest first within a severity.
            order = (" ORDER BY CASE f.severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2"
                     " WHEN 'low' THEN 3 WHEN 'informational' THEN 4 ELSE 5 END, f.rowid DESC")
            for row in conn.execute('SELECT ' + columns + base + order + ' LIMIT ? OFFSET ?', args + [limit, offset]):
                item = dict(row)
                for key in ('cwe', 'owasp'):
                    item[key] = json.loads(item[key])
                item['chained'] = bool(item['chained'])
                item['triage'] = {'state': item.pop('triage_state'), 'note': item.pop('triage_note'),
                                  'updated_at': item.pop('triage_updated_at')}
                items.append(item)
        return {'items': items, 'total': total}

    def activity(self, scan_id, after_id=0, limit=200):
        self._scan(scan_id)
        return {'items': self.store.events(scan_id, after_id=after_id, limit=limit)}

    def list_findings(self, **filters):
        self._filters(filters, {'q', 'severity', 'evidence_state', 'triage', 'repo_key', 'owasp', 'chained', 'scan_id', 'limit', 'offset'})
        return self.store.list_findings(**filters)

    def get_finding(self, finding_id):
        if not isinstance(finding_id, str):
            raise ValidationError('findingId must be text')
        return self.store.get_finding(finding_id)

    def triage(self, finding_id, state, note=None):
        if not isinstance(finding_id, str) or not isinstance(state, str) or (note is not None and not isinstance(note, str)):
            raise ValidationError('triage identifier, state and note must be text')
        self._safe(note)
        return self.store.set_triage(finding_id, state, note=note)

    def patch_preview(self, finding_id):
        row = self.get_finding(finding_id)
        finding = row.get('data', row)
        if isinstance(finding, str):
            finding = json.loads(finding)
        remediation = finding.get('remediation', {})
        lines = ['Read-only remediation proposal (no files changed)', '', remediation.get('summary', ''), '', 'Regression tests:']
        lines += ['- ' + t for t in remediation.get('regressionTests', [])]
        for e in finding.get('codeEvidence', []):
            lines += ['', f"{e['path']}:{e['startLine']}-{e['endLine']}", e['code']]
        return {'findingId': finding_id, 'readOnly': True, 'content': '\n'.join(lines)}

    def list_repositories(self, **filters):
        self._filters(filters, {'limit', 'offset'})
        return self.store.list_repositories(**filters)

    def finding_folders(self, repo_key=None, limit=200):
        """Folders that hold findings (first two path levels), for the Findings folder filter."""
        self._filters(dict(repo_key=repo_key), {'repo_key'})
        query = 'SELECT path,count(DISTINCT finding_id) FROM findings'
        args = []
        if repo_key:
            query += ' WHERE repo_key=?'; args.append(repo_key)
        counts = {}
        with self.store._connection() as conn:
            for path, n in conn.execute(query + ' GROUP BY path LIMIT 20000', args):
                parts = str(path or '').replace('\\', '/').split('/')[:-1]
                for depth in (1, 2):
                    if len(parts) >= depth:
                        key = '/'.join(parts[:depth])
                        counts[key] = counts.get(key, 0) + n
        rows = sorted(counts.items(), key=lambda kv: kv[0].lower())
        return {'items': [{'folder': k, 'count': v} for k, v in rows[:limit]], 'total': len(rows)}

    def delete_scan(self, scan_id):
        """Remove a stopped or finished scan, its findings and its saved files."""
        self._scan(scan_id)
        result = self.store.delete_scan(scan_id)
        directory = scan_dir(self.data_dir, scan_id)
        if directory.exists() and not is_link(directory):
            from .target.clone import _force_remove
            try:
                _force_remove(directory)
            except OSError:
                result['filesLeft'] = str(directory)  # records are gone; a locked file can be removed later
        return result

    def clone_repository(self, url):
        """Clone (or update) a remote repository into the profile so it can be scanned."""
        from .target.clone import clone_or_update
        return clone_or_update(url, self.data_dir)

    def list_clones(self):
        from .target.clone import list_clones
        return {'items': list_clones(self.data_dir)}

    def list_grants(self, scan_id):
        self._scan(scan_id)
        return {'items': self.store.list_grants(scan_id)}

    def mint_grant(self, scan_id, **opts):
        """USER-ONLY: direct slash command/CLI/dashboard; never an agent tool."""
        with self._mutation(scan_id):
            return validation.mint_grant(self.store, scan_id, **opts)

    def revoke_grant(self, grant_id):
        """USER-ONLY: direct slash command/CLI/dashboard; never an agent tool."""
        if not isinstance(grant_id, str):
            raise ValidationError('grantId must be text')
        grant = self.store.get_grant(grant_id)
        with self._mutation(grant['scanId'], canceled=True, owner='grant:' + grant_id):
            return self.store.revoke_grant(grant_id)
