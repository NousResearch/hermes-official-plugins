<!-- # Modified from Apache-2.0 source references/scan-contract.md; references/final-report.md (see NOTICE). -->
# Canonical artifact contract

`docs/CONTRACT.md` is authoritative. All canonical JSON uses camelCase and `schemaVersion: "1.0"`. The plugin owns serialization, identities, storage, finalization, and seals. Workers submit records, never author or rewrite shared artifacts.

## Lifecycle and storage

`security_scan_start` returns the authoritative scan/root/snapshot and methodology context. Resume and continuation use `security_scan_get`, not a guessed ID or new target. Work is profile-scoped in the plugin data directory: `security.db` plus private `scans/<scanId>/` directories, never the target repository. Use unique worker JSON input files only for CLI fallback; those are not canonical bundles.

`security_scan_submit_worker_result` records bounded worker results. `security_scan_checkpoint` records explicit coverage/progress and supported scan context. `security_scan_record_validations` records checked plans/receipts; only authorized passed runtime receipts can raise evidence state. `security_scan_record_chains` proposes eligible same-snapshot chains and accepts optional explanations. Read each mutation back through `security_scan_get` before treating it as accepted evidence.

Stop via `security_scan_cancel` and preserve accepted work. Resume relaunches incomplete packets using persisted state and fresh attempt IDs; memory-only child handles are not durable. Do not accept cross-scan/stale-snapshot evidence, use wrong-root fallbacks, or resurrect a sealed scan. The service may require its installed CLI resume operation; consult help rather than invent an agent tool.

## Sealed bundle

| Artifact | Meaning |
|---|---|
| `scan-manifest.json` | Target root/revision/base/head/dirty snapshot, mode, safety level, methodology/runtime provenance, terminal status, counts, artifact hashes, supersedes link, seal. |
| `findings.json` | Reportable findings plus retained rejected/inconclusive candidates and their reasons; original evidence is not silently lost. |
| `coverage.json` | Inventory/standards units, reasons, evidence, exclusions, worker outcomes, partial/complete state, and gaps. |
| `chains.json` | Eligible chains and broken paths with evidence and assumptions; present even when the result is empty. |
| `report.md` | Deterministic plain-language projection of the canonical records. |
| `exports/results.sarif` | Deterministic SARIF 2.1.0 projection for local consumers. |

Bounded supporting evidence and validation receipts live under scan-owned artifact storage. Paths must be contained regular files, not traversal or symlink escapes. Secrets are type/location/fingerprint only. Neither raw source trees nor literal credentials belong in reports.

The parent calls `security_scan_finalize` after worker join, adjudication, and coverage closure. The finalizer validates completeness, citations, distinct finding sections, and reconciled counts, generates all artifacts, and seals the bundle. Do not write `report.md` by hand or declare completion because a tool returned a path. Read back status, counts, coverage, and exact report path, and inspect the generated summary.

The manifest hashes `findings.json`, `coverage.json`, `chains.json`, `report.md`, and `exports/results.sarif`; its seal is SHA-256 over the canonical manifest without `seal`. This plugin seals these projections too. Do not assume a different producer's projection/sealing rules. No late worker or agent edit may alter a sealed artifact.

Corrections must **supersede** the earlier scan/result with an explicit provenance link; never rewrite sealed history. Mutable local triage remains in SQLite and does not change evidence. `security_scan_export` produces requested local exports; it does not authorize upload, tracking, or publication. Keep the producer/version exact and never claim another producer's format identity.

## Coverage truth

Every inventory file and mandatory `lane:A01:2025` through `lane:A10:2025` unit gets a state and reason. Applicable ASVS control coverage is separate from file counts and informational: unchecked ASVS controls are counted in the report but never make a scan incomplete. States are `reviewed`, `not_applicable`, `deferred`, `unsupported`, `unknown`, and `failed`; exclusion is a scoped reason, not silent omission.

Complete coverage requires all inventory files reviewed/not_applicable or explicitly excluded, all applicable OWASP lanes reviewed/not_applicable, and no missing worker. Preserve all other gaps as partial. A canceled, failed, interrupted, or zero-finding partial result cannot support a clean conclusion.

## Evidence truth

States are `candidate`, `source_supported`, `runtime_confirmed`, `rejected`, and `inconclusive`. Workers cannot directly set runtime confirmation. Validation status is `NOT_RUN`, `passed`, `failed`, or `inconclusive`; display unrun as **NOT RUN**. Runtime confirmation requires a passed receipt at `local-safe` or `active-authorized` with paired positive/negative controls. Findings, chains, coverage, and triage states must not be substituted for one another.
