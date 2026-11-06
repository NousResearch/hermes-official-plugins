<!-- # Modified from Apache-2.0 source skills/deep-security-scan/SKILL.md; references/core-scan.md; references/scan-contract.md (see NOTICE). -->
# Bounded Deep review and source conservation

Deep is an explicit mode of security-audit, not another scan-entry skill. Require an explicit budget before starting: total wall time, N complete passes (default 3), worker/concurrency allowance (the scan's rolling pool of at most 10 delegates unless the user sets fewer), model-work cap when measurable, and time reserved for adjudication/reduction/finalization. If the user asks for Deep without enough budget information, agree a finite limit rather than launching unbounded work.

## Independent passes

Bind all passes to the same root, authorized inventory, snapshotDigest, methodologyVersion, and immutable security context. Each pass has its own attemptId and `workerRole: "deep-pass"`. Use the worker envelope in `worker-packet.md`; never recursively spawn complete scan services from children. A complete pass applies the Standard source-review method to the entire scope, closes its own units, preserves negative results, and submits candidates durably. Do not seed later passes with earlier findings or count targeted follow-up as another independent complete pass.

The parent retains its independent architecture/validation responsibility. Launch the one independent baseline as the Standard workflow requires; schedule remaining complete passes and focused work within the shared budget, not a multiplied per-pass allowance. Record unavailable independence explicitly when running sequentially yourself. Track pass coverage separately; the union must not disguise an incomplete pass as complete.

## Reducer invariants

The plugin reducer, not a chat summary, owns the combined records. Preserve exact source references: scan/attempt/source-candidate identity, source locations, code evidence, counterevidence, proof gaps, receipt references, and provenance. Record provider/model/methodology and accepted-result hashes when returned; never invent missing usage or hashes.

Merge only when **one remediation** genuinely subsumes every absorbed instance and closes the same broken control under compatible prerequisites. The same CWE, sink family, similar title, or shared file is insufficient. Separate independently attackable roots/sinks when they need distinct fixes, controls, or attack assumptions. A wrapper and shared helper can be one issue only when the common fix closes all routes and every affected location remains retained.

Each source reference must be retained, absorbed once into exactly one surviving record, or explicitly rejected with a candidate-specific evidence reason. No source may disappear, be absorbed twice, or become an untraceable generic paragraph. Preserve distinct minority-pass findings; majority vote does not prove a finding false. Keep severity disagreements and uncertainties visible until parent adjudication resolves them with evidence.

Before finalization, programmatically reconcile input references against retained/absorbed/rejected references, with exact union and no duplicate absorption. Fail reduction or mark the unresolved work partial if conservation cannot be proven. Completeness requires the parent's coverage checks too, not merely a successful reducer return.

## Stop and resume

Stop dispatching on the first of budget exhaustion, requested pass count, a no-new-root-cause threshold of **2 consecutive passes**, or cancellation. Evaluate “new root cause” after accepted submissions and conservative reduction, not titles or worker summaries. Stop or join incomplete workers and checkpoint outstanding units; accepted evidence remains stored. State the stop reason, completed versus planned passes, consumed budget when measured, and gaps.

Cancel with `security_scan_cancel`. Resume by reading `security_scan_get`, verifying the unchanged snapshot and methodology, recovering accepted attempts, and relaunching only incomplete packets/passes with fresh attempt IDs. Follow the service's resume operation if required. Do not trust stale child handles or rerun accepted passes just because the conversation was lost. A changed snapshot needs a linked new scan, and a sealed scan cannot be reopened.

Use `security_scan_finalize` only after parent adjudication, conservation, chain checks, and explicit coverage closure. Returning from a coordinator, reaching a budget, or finding no new root cause is not proof that all requested controls were reviewed. Repeated independent reviews reduce some variance; they do not prove exhaustive vulnerability discovery.
