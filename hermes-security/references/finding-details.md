<!-- # Modified from Apache-2.0 source references/finding-detail-fields.md; references/final-report.md; references/static-finding-assessment.md (see NOTICE). -->
# Finding details and readable copy

Use the camelCase fields from `docs/CONTRACT.md`, not a parallel prose schema. Findings are structured evidence, and `report.md` is a projection. Do not reconstruct missing evidence by parsing a report.

## Distinct field purposes

| Field | Required content |
|---|---|
| `ruleId`, `identity.anchor` | Stable control family and semantic root anchor; no display numbers, scan IDs, or line-based identity. Keep independently attackable instances distinct. |
| `title`, `summary` | Who can do what to which asset, and under which conditions; short practical effect first. |
| `rootCause` | String explaining the missing/broken invariant and the implementation that breaks it. Do not use a nested `rootCause.summary` shape. |
| `attackPath` | `source`, `sink`, ordered `steps`, effective `controls`, and conditional `assumptions`. Trace material transformations, identity binding, failure paths, and the resulting capability. |
| `severity` | `level`, `rationale`, `reachability`, `impact`, `likelihood`, and `prerequisites`. Apply `severity-policy.md`; no circular severity rationale. |
| `confidence` | Evidence quality in `level` and `rationale`, not another impact score. |
| `evidenceState` | Exact canonical state, separate from runtime and triage status. |
| `taxonomy` | Concrete category, CWE identifiers, versioned OWASP Top 10:2025 IDs, applicable ASVS 5.0.0 controls. Taxonomy is not proof of coverage. |
| `locations` | Every material root control, entry point, wrapper, sink, and affected instance: repository-relative path, startLine, endLine, role. At least one. |
| `codeEvidence` | Stable id, label, path, startLine, endLine, exact `code`, SHA-256, and explanation connecting the value at this step to the next operation. Never `snippet`. |
| `counterEvidence` | Strongest concrete reason the exact claim could fail, with source-backed explanation. |
| `proofGaps` | The precise missing facts that limit the claim; no invented deployment, credentials, or assumed exposure. |
| `validation` | Actual status, level, receiptIds, and summary of checks/limits. Planned checks remain `NOT_RUN`. |
| `remediation` | Object with a minimal invariant-preserving `summary` and concrete `regressionTests`; keep legitimate behavior. |
| `capabilities` | Preconditions/effects supported by the same-snapshot evidence and versioned vocabulary; no speculative graph edges. |
| `secret` | Null, or type and fingerprint only; location is in locations. Never a literal value. |
| `diffAttribution` | Null outside diff review; `introduced` or `inherited` based on changed behavior, not nearby lines. |
| `provenance` | methodologyVersion, workerAttemptIds, sourceCandidateIds, and supersedes; preserve every source reference. |

The plugin computes candidate IDs, finding IDs, occurrence IDs, and fingerprints. Workers must not invent these. Candidate fields may be partial during discovery; reported findings require all completeness rules. Retain rejected and inconclusive candidates with reasons, not in the severity total for reportable findings.

## Evidence and interpretation

Use path + line range + exact excerpt from the pinned snapshot. Include the actual broken control or sink, not only a public wrapper. Preserve transformation/object-selection lines when they explain the bug. Give the smallest *complete* sequence of snippets from input to effect, and explain why each matters. Cite comparison controls separately, not as though they belong to the vulnerable path.

Do not add ellipses or paraphrase code inside an exact excerpt. Paths must be inventory-relative, normalized, contained, and non-symlink. Line ranges must exist. Each `code` is at most 4000 characters. SHA-256 describes the exact retained excerpt bytes; use the plugin's redacted excerpt and matching fingerprint behavior for secret-bearing lines. Never serialize literal secrets or copy raw-secret hashes into ad hoc logs. If matching redacted evidence cannot be produced, preserve a gap; do not leak a value to satisfy citation checks.

A high-confidence static finding still has runtime status NOT RUN. Runtime confirmation requires the plugin's passed receipt, permitted execution level, and paired controls; a suggested test cannot promote evidence state.

## Plain-language writing

Lead with practical effect, not taxonomy. Example title: “A project member can download another tenant's invoices.” State uncertainty directly: “The code accepts the identifier, but we could not establish whether this route is exposed in production.” Define uncommon terms on first use. Put code identifiers in backticks, but keep code snippets in evidence fields.

Summary explains the problem. Root cause explains the broken control. Attack path explains the steps. Validation states what was actually checked. Severity explains effect, reachability, and prerequisites. Fix explains what to change. These sections must not repeat one paragraph; the finalizer rejects empty, duplicate, or near-prefix copies. Preserve identifiers, commands, paths, hashes, and user values exactly; simpler prose must not alter evidence. Avoid sales language and generic assurances.

Errors state what failed, what work remains stored, and the concrete next step. Put partial coverage beside finding counts, not hidden at the bottom. Say “No reportable findings in reviewed scope; coverage is partial” when gaps remain, never “clean.” Unrun tests display **NOT RUN**, while their JSON value remains `NOT_RUN`.

## Internal state → UI label

| Internal state | UI label |
|---|---|
| `candidate` | Needs review |
| `source_supported` | Supported by code |
| `runtime_confirmed` | Confirmed by test |
| `rejected` | Not an issue |
| `inconclusive` | Couldn't confirm |

Local triage (`open`, `closed`, `accepted_risk`, `false_positive`) is a different dimension from evidence. Closing a row never changes historical source evidence or establishes that a fix works.
