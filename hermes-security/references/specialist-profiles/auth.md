# Authentication and authorization review

## Trace
- Build an actor × resource × operation matrix: anonymous, owner, same-tenant peer, cross-tenant user, administrator, suspended user, and service account versus read/write/delete/export/bulk actions.
- Mark each cell with the enforcing control and a positive and negative case. Include role changes, token revocation, password reset, MFA recovery, and account deactivation.
- Recognize parent-router auth: trace route mounting, inherited dependencies, middleware order, exclusions, and alternate mounts before declaring a handler unprotected.
- Follow identity from issuer through token validation and session lookup into object, field, function, and tenant authorization. Compare sibling operations.

## Sinks and controls
- Permission gates before reads and mutations; trusted issuer/audience/algorithm; replay limits; session rotation and expiration; rate limits and recovery binding.
- Ensure service-to-service calls preserve the originating actor rather than accidentally adopting a stronger service identity.

## Counterevidence
- Cite inherited guards, ownership-filtered queries, database policies, explicit deny defaults, and immediately effective revocation. A UI hiding an action is not an authorization control.

## A10 exceptional conditions
- Check fail-open auth on identity-provider timeout, cache failure, token parse exceptions, or missing roles. Reject failed lookups rather than treating missing identity as privileged. Check lockout races and partial recovery writes.
