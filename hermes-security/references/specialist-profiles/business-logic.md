# Business logic review

## Trace
- Write domain invariants in plain language: who may spend, approve, transfer, reserve, refund, or change ownership, and what must remain true afterward.
- Draw state transitions and preconditions; compare sibling operations, bulk actions, alternate APIs, jobs, and administrative bypasses.
- Check idempotency/replay across duplicate delivery, retries, reordered requests, and restarts. Bind idempotency keys to actor, operation, and payload.
- Trace concurrency around read-check-write sequences; check quota/accounting, balances, reservations, counters, and inventory under parallel requests.
- Check approval/version binding: an approval must cover the exact resource version and terms later executed, not merely an object identifier.

## Sinks and controls
- Transactions, uniqueness constraints, atomic compare-and-swap, version checks, ownership gates, lock ordering, bounded limits, and durable deduplication records.

## Counterevidence
- Cite database constraints and transaction boundaries, not only application pre-checks. Distinguish intentional privileged exceptions from reachable unprivileged bypasses.

## A10 exceptional conditions
- Follow partial writes across payment, fulfillment, and audit systems. Check rollback gaps, retry duplication after ambiguous success, stale approvals, cancellation between commits, and timeout defaults that release or charge resources twice.
