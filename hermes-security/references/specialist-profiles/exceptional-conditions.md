# Exceptional-condition review (A10:2025)

## Trace
- Enumerate dependency failures, invalid input, missing values, cancellation, interrupts, disk-full, permission errors, and resource exhaustion at each trust boundary.
- Follow each error from its originating operation through catch/finally blocks to returned status, persisted state, logs, and later retries.

## Required cases
- **Fail-open auth:** unavailable identity/permission services, empty claims, malformed tokens, and caught authorization errors must deny access rather than default to success.
- **Partial writes:** interrupt every multi-step mutation; check which state survives and whether a success response can precede durable completion.
- **Timeout defaults:** inspect missing timeout settings, fallback values, ambiguous remote success, and cancellation propagation; defaulting must not expand authority.
- **Rollback gaps:** trace transaction scope, external side effects, cleanup, compensating actions, and crashes between commits. A database rollback cannot undo an already-sent message.
- **Retry duplication:** require bounded retries with backoff and durable idempotency for non-repeatable actions; reconcile ambiguous success before retrying.
- **Resource exhaustion:** bound input sizes, archive expansion, memory, handles, pools, threads, queued work, recursion, logs, and expensive queries; confirm resources release on every exit.

## Sinks and controls
- Atomic transactions, checked return values, typed errors, secure defaults, scoped cleanup, bounded pools, circuit breakers, cancellation-safe state machines, and a final exception handler.
- Log actionable failure metadata without exposing secrets or attacker-controlled log syntax; keep error responses generic.

## Counterevidence
- Cite guaranteed rollback boundaries, idempotency constraints, finally/context-manager cleanup, explicit deny defaults, and tested negative cases. Distinguish caught-and-recovered errors from swallowed errors.
- Record failure paths not exercised and the exact residual state. Static reasoning is not a passed runtime test.
