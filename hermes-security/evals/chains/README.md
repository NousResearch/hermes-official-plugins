# Exploit-chain canaries

Run from the repository root:

```sh
python scripts/eval_chains.py
python -m pytest tests/unit/test_chain_*.py -q
```

The JSON files are synthetic, hand-labeled policy fixtures, not results from a real
scan. Each includes `findings`, `scanId`, `snapshotDigest`, and an `expected` object:

- `chains`: exact ordered finding-id paths expected in the live graph.
- `broken`: exact ordered finding-id pairs expected in the defeated-transition list.
- `allowedEdges`: independently labeled `[from, to, effect.kind, precondition.kind]`
  tuples. The evaluator does not derive its oracle from the implementation.
- Optional `disposition`: required disposition for each emitted live chain.

The runner computes precision/recall over typed live/broken paths, checks emitted
edges against `allowedEdges`, and fails on any mismatch. A zero-negative prediction
set is not sufficient: valid cases must also be recovered. These are canned release
canaries, not a claim of measured generalization to unseen repositories.

## API integration notes

The package exports `eligible_edges`, `compose`, `validate_chain`, `chain_severity`,
and `build_chains_doc`. All are pure; findings/edges are not mutated. Exceptions for
invalid graph input are `hermes_security.errors.ValidationError`.

- Bind every input finding to the trusted scan context with **both** `scanId` and
  `snapshotDigest`, either top-level or under `provenance`. Canonical findings omit
  these fields in the contract example, so the service must enrich an internal copy
  from trusted stored context before calling the chain API. The supplied snapshot
  argument alone never proves a finding's membership. Mixed, missing, conflicting,
  or duplicate identity contexts are rejected, including excluded findings.
- Capabilities require explicit kind, actor, tenant and deployment. Missing scopes
  fail closed. Actor `any` on the *required precondition* accepts any known actor;
  `any` on an effect does not establish a specific identity. Tenant scope must match
  unless the precondition says `any`. Deployment can match or either side be `any`.
- Actor-changing effects need explicit `yieldsActor` for a different actor. The
  allowed changes are in `vocabulary.ACTOR_YIELDS`. No kind alone manufactures an
  authenticated session, administrator identity, credential, or auth scope.
- `data-read` satisfies `credential` only with literal boolean `yieldsCredential:
  true`. Structured flags can be placed on the capability or in a `detail` object.
  Optional `authScope`, `role`, `credentialType`, and `component` requirements must
  match an explicit effect field. The surrounding domain schema must allow these
  structured flags if producers emit them.
- `defeatedBy` (text) / `brokenBy` (list) declare defeating control labels. A control
  is present only when found in the target's `counterEvidence` or
  `attackPath.controls`, compared with normalized whitespace/case, **not** fuzzy
  natural-language inference. Defeat of any required target precondition breaks
  every route into that finding. Broken entries preserve the defeating control.
- Each supported edge has existing `codeEvidence.id` citations on **both** endpoints.
  Missing citations, candidate findings, unresolved assumptions, or unsatisfied
  additional target preconditions make the chain conditional. Bad citations are
  rejected by independent validation, not silently discarded.
- Composition emits ordered simple paths, including prefixes, deduplicates multiple
  proofs of the same path, and enforces hard caps of four findings/fifty chains even
  if a caller asks for more. Broken edges are never traversed. The document records
  broken transitions separately and does not assign them an inflated severity.
- Severity is never summed. Conditional paths retain maximum primitive severity;
  supported final code execution/privilege gain or cross-tenant credential
  acquisition may justify at most one band above that maximum. Invalid final
  effect scopes cannot justify uplift.
- Explanations may set only nonempty `title`/`summary` strings for existing chain IDs.
  Unknown IDs/fields are reported in optional `ignoredExplanations`; their payloads
  cannot introduce edges, findings, state changes, or severity changes.

Vocabulary duplication in `chains/vocabulary.py` is intentional during parallel
implementation; reconcile it with `domain.taxonomy` without relaxing these gates.
