# hermes-security — implementation contract (authoritative for all workers)

This file fixes names, shapes, and module boundaries so independent workers can
build disjoint modules in parallel. It supersedes the original product plan (not included in this repository).

## Ground rules

- Repo: https://github.com/NousResearch/hermes-official-plugins, folder `hermes-security/` (public, Apache-2.0).
- Plugin id / manifest name: `hermes-security`. Python package: `hermes_security`. Toolset: `security`.
- Runtime: Python 3.11+, **stdlib only** in `hermes_security/` (no jsonschema, no pydantic).
  `dashboard/plugin_api.py` may import `fastapi` (available in the Hermes venv).
- Test interpreter: a Python 3.11+ environment with pytest (the Hermes venv works), `python -m pytest` from the repo root.
  Tests must not need network, Hermes runtime, or external scanners; skip cleanly when a tool is absent.
- Upstream source (Apache-2.0) is pinned in `scripts/provenance.py`. To run it, check out that commit and pass
  `--source-root` or set `HERMES_SECURITY_UPSTREAM`. The completed-scan example the compat tests need is vendored under
  `tests/fixtures/upstream/` so those tests never depend on scratch.
  Adapt only from the pinned commit. Every adapted file starts with a modification notice:
  `# Modified from Apache-2.0 source <path> (see NOTICE).`
  (JSON schemas: put it in `"$comment"`; Markdown: an HTML comment — first line for references, but
  in `SKILL.md` immediately after the YAML frontmatter because Hermes requires `---` at byte 0). Several upstream
  sources are listed separated by `; `. Files with no upstream counterpart carry no notice. No upstream branding or
  tool names, and no upstream SDK/auth/cloud code.
- Repository content is untrusted data. Never execute target code in static mode. Never write into the target tree.
- Secrets: never serialize a literal secret value anywhere (DB, artifacts, logs, tool results). Store type,
  location, and `fingerprint = "sha256:" + sha256(value)[:16]`.
- All IDs are deterministic where stated: `sha256` over canonical JSON (`json.dumps(obj, sort_keys=True,
  separators=(",", ":"), ensure_ascii=False)`), hex truncated to 24 chars.
- Times: UTC ISO-8601 `YYYY-MM-DDTHH:MM:SSZ`.
- Every public function has a focused pytest test. Workers do not commit; the orchestrator commits.

## Package layout (owner wave in brackets)

```
plugin.yaml  pyproject.toml  __init__.py  README.md  LICENSE  NOTICE
THIRD_PARTY_NOTICES.md  UPSTREAM_PROVENANCE.json                     [W1-scaffold]
hermes_security/
  __init__.py            (exports __version__ = "0.1.0")             [W1-scaffold]
  errors.py              SecurityError(code, message) hierarchy      [W1-scaffold]
  canonical.py           canonical_json, sha256_hex, stable_id, utcnow, atomic_write  [W1-scaffold]
  domain/                models/identities/dispositions/taxonomy     [W1-domain]
  schemas/*.schema.json + domain/validate.py                         [W1-domain]
  compat/codex_import.py upstream v1 bundle importer                 [W1-domain]
  target/                resolve/inventory/snapshot/policy/excerpt   [W1-target]
  store/                 db/migrations/artifacts/leases              [W1-store]
  standards/             owasp-top10-2025.json asvs-5.0.0.json cwe-map.json + ledger.py  [W1-standards]
  validation/            policy/plans/runner/receipts                [W1-validation]
  chains/                graph/eligibility/compose/validate          [W1-chains]
  reports/               finalize/markdown/sarif/export/copy         [W1-reports]
  orchestration/         standard/deep/diff/reducer/coverage/worker_protocol [W2-service]
  service.py             SecurityService (the only orchestrator)     [W2-service]
  tools.py commands.py cli.py plugin.py config.py                    [W2-service]
skills/<name>/SKILL.md  references/**                                 [W1-skills]
dashboard/manifest.json dashboard/plugin_api.py                       [W3-workbench]
desktop/plugin.js                                                     [W3-workbench]
tests/{unit,integration,plugin,fixtures,upstream-compat,e2e}/        [each owner its own files]
evals/** scripts/run-evals.py EVALUATION.md                          [W3-evals]
```

`hermes_security/canonical.py` (W1-scaffold) — every other module imports from it:

```python
def canonical_json(obj) -> str            # sort_keys, (",",":"), ensure_ascii=False; rejects NaN/Inf (ValueError)
def sha256_hex(data: bytes | str) -> str
def stable_id(prefix: str, *parts) -> str  # f"{prefix}_{sha256_hex(canonical_json(list(parts)))[:24]}"
def utcnow() -> str
def atomic_write(path: Path, data: bytes | str, mode=0o600) -> None   # tmp+fsync+os.replace; refuses symlink target
def secret_fingerprint(value: str) -> str  # "sha256:" + sha256_hex(value)[:16]
```

`hermes_security/errors.py`: `class SecurityError(Exception): code: str` with subclasses
`ValidationError("invalid_input")`, `NotFound("not_found")`, `Conflict("conflict")`,
`SealedError("sealed")`, `PolicyDenied("policy_denied")`, `TargetError("target_error")`.

## Enumerations (domain/dispositions.py, domain/taxonomy.py)

```
EVIDENCE_STATES = ("candidate","source_supported","runtime_confirmed","rejected","inconclusive")
EVIDENCE_LABELS = {"candidate":"Needs review","source_supported":"Supported by code",
  "runtime_confirmed":"Confirmed by test","rejected":"Not an issue","inconclusive":"Couldn't confirm"}
CHAIN_STATES = ("candidate_chain","source_supported_chain","runtime_confirmed_chain","broken_chain")
SEVERITIES = ("critical","high","medium","low","informational")
CONFIDENCE = ("high","medium","low")
SCAN_MODES = ("standard","deep","diff")   # validation is record_validations on an existing scan
SAFETY_LEVELS = ("static","local-safe","active-authorized")
SCAN_STATUSES = ("created","running","awaiting_analysis","finalizing","completed","partial",
                 "canceled","interrupted","failed")
COVERAGE_STATES = ("reviewed","not_applicable","deferred","unsupported","unknown","failed")
VALIDATION_STATUS = ("NOT_RUN","passed","failed","inconclusive")
TRIAGE_STATES = ("open","closed","accepted_risk","false_positive")
OWASP_2025 = A01..A10 ids: "A01:2025".."A10:2025" (names in standards JSON)
```

Transition rule: `runtime_confirmed` only via a validation receipt with `status=="passed"`, paired
positive+negative controls, and level `local-safe` or `active-authorized`. A proposed/unrun test is `NOT_RUN`.

## Canonical artifacts (camelCase JSON; `schemaVersion: "1.0"`)

### Finding (inside `findings.json` → `{documentType:"hermes-security.findings", schemaVersion, scanId, findings:[...]}`)

```jsonc
{
  "findingId": "hsf_<24hex>",          // stable_id("hsf", repoKey, ruleId, identity.anchor)
  "occurrenceId": "occ_<24hex>",       // stable_id("occ", scanId, findingId, primary location)
  "ruleId": "authz.missing-ownership-check",   // ^[a-z0-9][a-z0-9._/-]*$
  "identity": {"anchor": "src/api/invoices.py/get_invoice", "instance": "...optional"},
  "fingerprints": {"algorithm": "hermes-security/v1", "primary": "hermes-security/v1:sha256:<64hex>"},
  "title": "A project member can download another tenant's invoices",
  "summary": "plain-language effect, who/what/conditions",
  "rootCause": "the broken or missing control",
  "attackPath": {"source": "...", "sink": "...", "steps": ["..."], "controls": ["..."], "assumptions": ["..."]},
  "severity": {"level": "high", "rationale": "...", "reachability": "...", "impact": "...",
               "likelihood": "...", "prerequisites": ["..."]},
  "confidence": {"level": "medium", "rationale": "..."},
  "evidenceState": "source_supported",
  "taxonomy": {"category": "broken-access-control", "cwe": ["CWE-639"], "owasp": ["A01:2025"], "asvs": ["V8.2.2"]},
  "locations": [{"path": "src/api/invoices.py", "startLine": 40, "endLine": 52, "role": "sink"}],   // minItems 1
  "codeEvidence": [{"id": "ev-1", "label": "...", "path": "...", "startLine": 40, "endLine": 44, "keyLine": 42,  // keyLine optional
                    "code": "<exact excerpt, <=4000 chars, secrets redacted>", "sha256": "<sha256 of exact excerpt bytes>",
                    "explanation": "..."}],
  "counterEvidence": ["strongest reason this might not be exploitable"],
  "proofGaps": ["what we could not establish"],
  "validation": {"status": "NOT_RUN", "level": "static", "receiptIds": [], "summary": "..."},
  "remediation": {"summary": "what to change", "regressionTests": ["..."]},
  "capabilities": {"preconditions": [Capability], "effects": [Capability]},   // see chains
  "secret": null | {"type": "aws-access-key", "fingerprint": "sha256:<16hex>"},
  "diffAttribution": null | "introduced" | "inherited",
  "provenance": {"methodologyVersion": "hermes-security/method-1", "workerAttemptIds": [...],
                 "sourceCandidateIds": [...], "supersedes": null}
}
```

Sections Summary/rootCause/attackPath/severity.rationale/remediation.summary must be non-empty and pairwise
distinct (normalized whitespace/case comparison; reject if any two are identical or one is a >90% prefix of another).

### Candidate (worker submission unit; stored in DB, not a sealed artifact)

Same field names as Finding except: no `findingId/occurrenceId/fingerprints` (computed by the plugin),
`candidateId` is computed `stable_id("cand", scanId, ruleId, identity.anchor, locations[0].path, locations[0].startLine)`,
`evidenceState` limited to `candidate|source_supported|rejected|inconclusive`, plus `source: "worker"`.
Workers may submit partially filled candidates; finalization requires the Finding completeness rules above for
any candidate promoted to a reported finding (evidenceState in source_supported/runtime_confirmed). Rejected and
inconclusive candidates are retained in `findings.json` under `"retained"` with their reasons, not dropped.

### Worker result (input to `security_scan_submit_worker_result`)

```jsonc
{
  "scanId": "scan_<24hex>", "attemptId": "att_<anything [a-z0-9_-]{1,64}>", "workerRole": "baseline|investigator|deep-pass|diff|validator",
  "snapshotDigest": "sha256:<64hex>", "methodologyVersion": "hermes-security/method-1",
  "packetId": "pkt_..." | null,
  "candidates": [Candidate...],                  // <= 200 per submission
  "coverage": [{"unit": "file:src/a.py" | "lane:A01:2025" | "asvs:V8.2.2", "state": COVERAGE_STATE, "note": "..."}],
  "negativeResults": [{"hypothesis": "...", "control": "...", "locations": [Location]}],
  "notes": "<=4000 chars"
}
```

Payload hard limits (reject with ValidationError): total JSON <= 2 MiB; string fields <= 20000 chars;
`code` excerpt <= 4000 chars; unknown top-level keys rejected; non-finite numbers rejected.
Citation check on submit: every `locations[*].path` / `codeEvidence[*].path` must be in the scan inventory
(normalized, no `..`, no absolute), line ranges within the file, an optional `codeEvidence.keyLine` inside its
excerpt's range, and `codeEvidence.code` must match the snapshot
file lines (whitespace-trimmed line-by-line compare) — mismatches are rejected per-candidate with reasons
returned, not silently fixed.

### Optional deployment evidence (no new worker or round)

Coverage accepts `deployment:<inventory path>#<stable-key>` units alongside file,
lane and ASVS units. The unit carries optional structured `deployment` detail:
`component`, `ingress` (`public|private|local`), `actor`, `asset`, `missingEvidence`
(bounded list of exact absent artifacts), `disposition` (`pending|resolved|deferred`),
and `reviewNote`. See `references/worker-packet.md` for the complete example.
Workers may submit only `pending`/`unknown`. Parent checkpoints record
`resolved`/`reviewed` (empty missing list plus evidence note), or
`deferred`/`deferred` (nonempty missing list plus adjudication note). Pending units
block finalization; deferred units keep completeness partial. Existing payloads
without these units remain valid. There is no DB migration or new safety authority.
Same-key observations merge missing facts, reasons, review notes and evidence references
inside the store write transaction; differing component/ingress/actor/asset identities
are rejected, not overwritten. Deferring a subset cannot erase prior missing facts.
Explicit parent resolution clears the active missing list but retains internal fact
history; a later pending observation reopens the boundary with all historical facts.
Merged values exceeding schema bounds reject the write rather than truncate evidence.

Deployment units never alter file counts or queue file remainders. A read manifest
may be complete file coverage with unresolved deployment evidence. Public and
private ingress need independent dispositions. Resolved means evidence reviewed,
not vulnerability fixed or runtime confirmed. Do not infer deployment units from
all inconclusive candidates. Material missing evidence must be explicitly recorded;
important inconclusive claims retain their proof gaps in the human report.

### coverage.json

```jsonc
{"documentType":"hermes-security.coverage","schemaVersion":"1.0","scanId":"...",
 "completeness":"complete"|"partial",
 "files":{"total":N,"reviewed":N,"deferred":N,"unsupported":N,"excluded":[{"path":"...","reason":"..."}]},
 "units":[{"unitId":"file:src/a.py","kind":"file|lane|asvs|worker","state":COVERAGE_STATE,"reason":"...","evidence":["attemptId or receipt"]}],
 "standards":{"owaspTop10":{"version":"2025","categories":[{"id":"A01:2025","state":COVERAGE_STATE,"evidence":[...],"reason":"..."}]},
              "asvs":{"version":"5.0.0","controls":[{"id":"V8.2.2","state":...,"evidence":[...],"reason":"..."}]}},
 "workers":[{"attemptId":"...","role":"...","status":"accepted|rejected|missing|late"}],
 "gaps":["plain-language gap statements"]}
```

`completeness == "complete"` only if: every inventory file is reviewed/not_applicable (or explicitly excluded by
scope), every applicable OWASP category is reviewed/not_applicable, no worker
is `missing`. Anything else → `partial`, and `gaps` lists why. A scan with zero findings and partial coverage must
never be reported as clean.

### scan-manifest.json

```jsonc
{"documentType":"hermes-security.scan-manifest","schemaVersion":"1.0","scanId":"scan_<24hex>",
 "producer":{"name":"hermes-security","version":"0.1.0"},"methodologyVersion":"hermes-security/method-1",
 "mode":SCAN_MODE,"safetyLevel":SAFETY_LEVEL,"status":"completed|partial|canceled|failed",
 "target":{"root":"/abs/path","repoKey":"<24hex stable per root+remote>","kind":"git|directory","revision":"<sha>|null",
           "dirty":bool,"snapshotDigest":"sha256:<64hex>","scope":["glob"...],"base":null,"head":null,"fileCount":N},
 "runtime":{"provider":"...|null","model":"...|null","profile":"default","fallbacks":[]},
 "startedAt":"...","finishedAt":"...","counts":{"critical":0,"high":0,"medium":0,"low":0,"informational":0,"retained":0,"chains":0},
 "artifacts":{"findings.json":"sha256:...","coverage.json":"sha256:...","chains.json":"sha256:...","report.md":"sha256:...","exports/results.sarif":"sha256:..."},
 "supersedes":null,"seal":{"algorithm":"sha256-canonical","digest":"sha256:<64hex over canonical manifest without seal>","sealedAt":"..."}}
```

### chains.json

```jsonc
{"documentType":"hermes-security.chains","schemaVersion":"1.0","scanId":"...",
 "vocabularyVersion":"hermes-security/capabilities-1",
 "chains":[{"chainId":"chn_<24hex>","findingIds":["hsf_..",...],"edges":[Edge],"disposition":CHAIN_STATE,
            "conditional":bool,"severity":{"level":...,"rationale":"final reachable impact"},"title":"...","summary":"..."}],
 "broken":[{"findingIds":[...],"edge":Edge,"defeatedBy":"control/assumption that breaks it"}]}
```

Capability: `{"kind": <vocab>, "actor": "anonymous|authenticated|same-tenant|cross-tenant|admin|local-user|internal-network|any",
"tenant": "same|cross|any", "deployment": "any|<label>", "detail": "..."}`.
Vocabulary kinds: preconditions `{network-access, authenticated-session, role, tenant-membership, user-interaction,
config-enabled, component-reachable, credential, file-write, internal-network}`; effects `{data-read, data-write,
credential-acquire, identity-change, privilege-gain, internal-network-reach, file-write, code-exec, integrity-impact,
availability-impact}`. Effect→precondition satisfaction map (only these edges are eligible):
`credential-acquire→{credential,authenticated-session}`, `identity-change→{authenticated-session,role}`,
`privilege-gain→{role,authenticated-session}`, `internal-network-reach→{internal-network,network-access,component-reachable}`,
`file-write→{file-write,config-enabled}`, `code-exec→{file-write,credential,internal-network,component-reachable}`,
`data-read→{credential}` only when the effect detail flags `yieldsCredential: true`.
Optional capability flags (schema-permitted): `yieldsActor` (actor enum; identity-change/privilege-gain/
credential-acquire effects that yield an actor), `yieldsCredential` (bool), preconditions `defeatedBy`/`brokenBy`
(string lists naming controls that break the edge). Either on the capability or inside an object `detail`.
chains.json may carry optional `ignoredExplanations` (ids/reasons only).
Edge: `{"from":"hsf_..","to":"hsf_..","effect":Capability,"precondition":Capability,"evidenceRefs":["hsf_..#ev-1"],
"counterEvidence":[...],"assumptions":[...],"state":"supported|speculative|broken"}`.

## SQLite store (store/db.py) — W1-store

`SecurityStore(data_dir: Path)` opens `data_dir/"security.db"` (WAL, foreign_keys=ON, busy_timeout=5000), creates
`data_dir/"scans"` with 0o700. Tables (migrations.py, versioned `PRAGMA user_version`):
`scans, snapshots, inventory_files, worker_attempts, candidates, evidence, validations, validation_grants,
chains, coverage_units, events, seals, triage, repositories`.

Required methods (all JSON-able dict returns; raise errors.* on misuse):

```python
create_scan(*, mode, safety_level, target: dict, inventory: list[dict], options: dict) -> dict  # scan row incl. scan_id
get_scan(scan_id) -> dict ; list_scans(*, q=None, status=None, repo_key=None, limit=50, offset=0) -> {"items","total"}
set_scan_status(scan_id, status, *, reason=None) -> dict    # refuses changes once sealed (SealedError) except no-op
inventory(scan_id, *, limit=None, offset=0) -> list[dict] ; inventory_paths(scan_id) -> set[str]
record_attempt(scan_id, attempt_id, role, *, packet_id=None, provider=None, model=None) -> dict   # idempotent
finish_attempt(scan_id, attempt_id, status, *, result_digest=None, error=None)
upsert_candidates(scan_id, attempt_id, candidates: list[dict]) -> {"inserted":n,"duplicates":n}  # idempotent on candidate_id+attempt
list_candidates(scan_id, *, states=None) -> list[dict]
set_candidate_state(scan_id, candidate_id, state, *, reason) -> dict
record_coverage(scan_id, units: list[dict], *, source: str, monotonic=False)   # unsealed only; deployment observations merge in the write transaction; other units upsert (optionally monotonic)
coverage_units(scan_id) -> list[dict]
record_validation(scan_id, receipt: dict) -> dict ; validations(scan_id, candidate_id=None) -> list[dict]
record_chains(scan_id, chains_doc: dict) -> dict ; get_chains(scan_id) -> dict | None
add_event(scan_id, kind, message, data=None) -> dict ; events(scan_id, *, after_id=0, limit=200) -> list[dict]
seal(scan_id, manifest_digest: str, artifact_dir: str) -> dict ; is_sealed(scan_id) -> bool
upsert_finding_index(scan_id, findings: list[dict])      # denormalized rows for workbench search/filter
list_findings(*, q=None, severity=None, evidence_state=None, triage=None, repo_key=None, owasp=None,
              chained=None, scan_id=None, limit=50, offset=0) -> {"items","total"}
get_finding(finding_id, *, scan_id=None) -> dict          # latest occurrence unless scan_id given
set_triage(finding_id, state, *, note=None) -> dict       # TRIAGE_STATES; does not touch sealed artifacts
list_repositories(*, limit=50, offset=0) -> {"items","total"}
grant_validation(scan_id, grant: dict) -> dict ; get_grant(grant_id) -> dict ; revoke_grant(grant_id)
lease(scan_id, owner, ttl_s) -> bool ; release(scan_id, owner)   # leases.py: finalization single-owner lock
close()
```
Any mutation (candidates, coverage, validations, chains, attempts) on a sealed scan raises `SealedError`.
`store/artifacts.py`: `scan_dir(data_dir, scan_id) -> Path` (contained, 0o700), `write_artifact(scan_dir, rel, data)`
(rejects `..`/absolute/symlink, atomic), `read_artifact(scan_dir, rel) -> bytes`.

## Target (target/*) — W1-target

```python
resolve_target(path: str, *, scope: list[str] | None = None, base: str | None = None, head: str | None = None,
               mode: str = "standard") -> dict
  # -> {"root","repoKey","kind","revision","dirty","base","head","scope"}; root realpath; rejects non-dir, "/" and $HOME itself
build_inventory(target: dict, *, max_file_bytes=2_000_000, max_files=50_000) -> {"files":[FileEntry],"excluded":[{path,reason}],"truncated":bool}
  # FileEntry {"path":posix rel,"size":int,"sha256":hex,"language":str|None,"kind":"source|config|doc|binary|lock|iac|other","lines":int}
  # git: tracked + untracked-not-ignored; never follow symlinks (record excluded reason "symlink"); skip submodules
  # (reason "submodule"); oversize -> "too_large"; undecodable names -> "invalid_encoding"; .git/ always excluded
snapshot_digest(inventory: dict, target: dict) -> "sha256:<64hex>"   # deterministic over sorted (path,sha256) + revision/base/head
diff_manifest(target: dict) -> {"base","head","changed":[path],"deleted":[path],"added":[path]}   # git diff base..head or worktree vs HEAD
read_excerpt(root, path, start, end, *, inventory_paths, max_chars=4000) -> str   # containment+symlink checks; redacts secrets
verify_excerpt(root, path, start, end, code, *, inventory_paths) -> bool
is_snapshot_current(target: dict, digest: str) -> bool   # recompute; False when dirty tree changed
redact_secrets(text) -> (str, list[{"type","fingerprint","line"}])   # shared by excerpts/reports
```

## Standards (standards/*) — W1-standards

JSON data files with `version`, `source` URL, `attribution`, entries. Top 10:2025 = 10 categories with id, name,
short plain-language description, required review lanes (from the plan table), related CWE list.
ASVS 5.0.0: chapters + a curated set of applicable controls (≥80 controls across V1–V17 as identified in ASVS 5.0.0)
with id, text summary (paraphrased, short), level (1/2/3), surfaces (e.g. `web-route`, `auth`, `crypto`, `file-upload`,
`config`, `logging`, `api`, `session`), owasp mapping. `ledger.py`:
```python
load_top10() / load_asvs() / load_cwe_map()
detect_surfaces(inventory_files: list[dict]) -> set[str]   # from paths/extensions/manifests (no code execution)
applicable_controls(surfaces) -> list[dict]
build_ledger(surfaces, coverage_units: list[dict], findings: list[dict]) -> dict   # -> coverage.json "standards" object
map_cwe(cwe: str) -> {"owasp":[...], "asvs":[...]}
specialist_profiles_for(surfaces) -> list[str]   # names of references/specialist-profiles/*.md to load
```

## Validation (validation/*) — W1-validation

```python
POLICY: static default; local-safe requires options["allowLocalValidation"] set by the user; active-authorized requires a grant.
mint_grant(store, scan_id, *, origins: list[str], actions: list[str], expires_in_s: int, max_requests: int, created_by="user-command") -> dict
  # ONLY called from direct user slash/CLI or confirmed authenticated dashboard routes; never an agent tool
check_plan(plan: dict, *, grant: dict | None, safety_level, now) -> None  # raises PolicyDenied
  # plan {"candidateId","level","kind":"static|local-command|http-probe","commands":[argv...],"requests":[{"method","url","headers","body"}],
  #       "positiveControl":{...},"negativeControl":{...},"cleanup":{...},"timeoutS","maxOutputBytes"}
  # deny: missing grant, expired, origin not in grant (scheme+host+port exact), redirects off-origin, methods other than
  # GET/HEAD/OPTIONS/POST-with-synthetic-body, >max_requests, missing cleanup, missing paired controls for runtime confirm
run_local(plan, *, target, workdir_parent) -> ValidationReceipt     # disposable copy (shutil.copytree no symlinks), no network
  # (unshare -n when available else env proxies to blackhole + record "network isolation: best-effort"), resource limits via
  # resource.setrlimit (CPU, AS, FSIZE, NPROC), timeout, output caps; cleanup verified
run_http(plan, *, grant) -> ValidationReceipt     # urllib with redirect handler that refuses cross-origin
static_receipt(candidate, *, checks: dict) -> ValidationReceipt   # source/control/sink/boundary/counterevidence checklist
ValidationReceipt = {"receiptId","candidateId","level","kind","status":VALIDATION_STATUS,"positive":{...},"negative":{...},
  "commandsDigest","startedAt","finishedAt","cleanup":{"done":bool,"detail"},"outputExcerpt":"<=4000, redacted","notes"}
next_evidence_state(current, receipt) -> str   # implements the transition rule
```

## Chains (chains/*) — W1-chains

```python
eligible_edges(findings: list[Finding], *, snapshot_digest) -> list[Edge]   # deterministic; same scan only; only map above;
   # actor/tenant/deployment compatible; source finding evidenceState in {source_supported,runtime_confirmed} else edge "speculative"
compose(findings, edges, *, max_depth=4, max_chains=50) -> list[Chain]   # simple paths, no cycles, dedupe by finding set+order
validate_chain(chain, findings_by_id) -> Chain   # every step retained finding, every edge evidenceRefs resolve,
   # any speculative edge -> conditional=True and disposition candidate_chain; broken control -> moved to "broken"
chain_severity(chain, findings_by_id) -> {"level","rationale"}   # final reachable impact; never sum; never above max
   # justified by final effect (code-exec/privilege-gain/credential-acquire cross-tenant → may raise by at most one band
   # over the max primitive, only if no edge is speculative)
build_chains_doc(scan_id, findings, *, snapshot_digest, explanations: dict | None = None) -> dict  # chains.json
   # explanations keyed by chainId may only set title/summary for existing chains; unknown keys ignored
```

## Reports (reports/*) — W1-reports

```python
validate_finding_sections(finding) -> list[str]     # problems (empty == OK): missing/duplicated/placeholder sections
finalize_bundle(*, manifest: dict, findings_doc: dict, coverage: dict, chains_doc: dict) -> dict[str, bytes]
   # validates via domain/validate.py, reconciles counts, produces {"findings.json","coverage.json","chains.json",
   # "report.md","exports/results.sarif","scan-manifest.json"} with manifest.artifacts hashes + seal digest
verify_bundle(scan_dir: Path) -> {"ok":bool,"problems":[...]}   # seal tamper detection, artifact hash mismatch
render_markdown(manifest, findings_doc, coverage, chains_doc) -> str   # layered: plain summary first, partial-coverage banner,
   # per-finding Summary/Root cause/Attack path/Validation/Severity/Fix; NOT RUN shown literally; never says "clean" when partial
render_sarif(manifest, findings_doc) -> dict   # SARIF 2.1.0, tool.driver.name "hermes-security"
export(scan_dir, fmt: "md|sarif|json|csv") -> (bytes, content_type, filename)
human_label(state) -> str   # EVIDENCE_LABELS etc.
```
Banned copy words in generated text: comprehensive, robust, seamless, delve, critical insight, security posture.

## Service + tools (W2) — summary for W3 consumers

`SecurityService(data_dir: Path, *, profile: str = "default")` wraps the store and all libraries. Methods mirror the
tools and HTTP API: `start_scan(**opts)`, `get_scan(scan_id, section=None, limit, offset)` (sections: `summary` = small
header with scanId/root/snapshotDigest/methodologyVersion/submit/packetIds and `next`, always under the tool cap;
`next` = `next_step(scan_id)`: `{step: dispatch|wait|validate|chains|finalize|done|blocked, items (<=50, to-do
first then running), total, actionable, reason, progressKey, how}` derived only from stored state; `packets`
paginated; plus inventory, coverage, candidates, findings, workers, validations, chains, activity, manifest,
report), `checkpoint(...)`,
`submit_worker_result(payload)`, `record_validations(scan_id, receipts|plans)`, `propose_chains(scan_id)`,
`record_chains(scan_id, explanations)`, `finalize(scan_id)`, `cancel(scan_id)`, `resume(scan_id)`, `export(scan_id, fmt)`,
`summary()`, `list_scans(...)`, `activity(scan_id, after_id, limit)`, `coverage(scan_id)`, `list_findings(...)`,
`get_finding(finding_id)`, `triage(finding_id, state, note)`, `patch_preview(finding_id)`, `list_repositories(...)`.
Data dir resolution (config.py): `ctx.state.data_dir` inside Hermes; outside (dashboard API, CLI) use
`$HERMES_HOME/plugin-data/agent-plugin-hermes-security-974429e7`, computed exactly the way
`hermes_cli.plugins_state.PluginState.data_dir` does (import it when available; fall back to the same formula).
The directory name was confirmed by the host, not assumed from the plugin ID.

Agent tools (toolset `security`, every handler returns a JSON string `{"ok":true,...}` or `{"ok":false,"error":{code,message}}`):
`security_scan_start, security_scan_get, security_scan_checkpoint, security_scan_submit_worker_result,
security_scan_record_validations, security_scan_record_chains,
security_scan_finalize, security_scan_cancel, security_scan_export`.
Slash command `/security` (status | authorize-validation <scan> <origin> [--actions ..] [--minutes N] | revoke <grant>).
CLI `hermes security-review ...` and `python -m hermes_security ...` expose the same service for subagents/terminal use.

## Dashboard user consent and execution

The host mounts `dashboard/plugin_api.py` beneath `/api/plugins/hermes-security`
behind its session-token authentication. Do not mount this router publicly without
that authentication. Body confirmation guards accidental requests; knowing a scan
ID is not authentication. No new agent tools are registered. `tools.py` continues
to pass `user_authorized=False` for validation regardless of scan options.

- `POST /scans`: accepts service options plus `safetyLevel` (alias of
  `safety_level`, reject both together). To opt into `local-safe`, require literal
  `allowLocalValidation: true` and `confirm: "local-safe"`; missing/mismatched
  confirmation returns 400. Only then supply `user_authorized=True` and
  `authorization_source="dashboard"` to `start_scan`. Store
  `options.authorizationSource="dashboard"`. This creates a snapshot, not a test.
  Static/default starts and active-authorized starts have no execution authority.
  Client-supplied `user_authorized`/`authorization_source` are rejected.
- `GET /scans/{scan_id}/grants`: `{items: [grant, ...]}` for this profile and scan,
  including expiry, used/maxRequests, and revoked state.
- `POST /scans/{scan_id}/authorize-validation`: `{confirm: scan_id, origin,
  minutes?: 30, maxRequests?: 20}`. Requires an active-authorized scan (403 otherwise).
  Reuses `mint_grant` with `created_by="dashboard"`, actions fixed to `["http-probe"]`,
  CLI origin syntax (scheme://host[:port], no credentials/path/query/fragment),
  1–240 minutes and 1–200 requests. Returns the stored grant.
- `POST /scans/{scan_id}/grants/{grant_id}/revoke`: `{confirm: scan_id}`. Reject
  a grant from another scan (400); reuse existing revoke/lease/seal checks and
  return the stored revoked grant.
- `POST /scans/{scan_id}/run-validation`: `{confirm: scan_id, plans: [...]}`.
  Requires local-safe (403 otherwise), a nonempty bounded list of local-command,
  local-safe plans with candidate IDs, paired controls and cleanup. Execute through
  `record_validations(..., user_authorized=True, authorization_source="dashboard")`.
  Receipts retain `User authorization source: dashboard` in notes (included in
  receipt identity). Return `{scanId, receipts}` read back from the store.

All three scan-scoped POST actions reject missing, non-string, or mismatched
confirmation with 400 before any mutation. All errors remain public-safe, never
reflecting supplied paths/secrets. Existing snapshot checks, leases, scan status,
seal protection, operation budgets, runner policy and redaction remain in force.
Grants never let agent calls run local commands. Successful mutations broadcast
`scan.updated`; the UI refreshes stored permissions and test results.

Desktop uses everyday-language choices and buttons. Non-static choices reveal an
unchecked warning checkbox; changes of choice reset it. Creating the scan opens
its detail page; **Open in chat** continues the existing scan, not a replacement.
**Test the bugs on this computer**, **Allow testing my running app**, and
**Stop allowing app testing** each require a user confirmation dialog. Read-only
scans retain the chat-draft path. Do not mistake creating a scan or allowing app
tests for executed findings: only returned test results prove execution.
