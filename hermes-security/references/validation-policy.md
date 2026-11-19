# Validation policy

Static analysis is the default. It reads source and records a checklist covering
source, control, sink, boundary, and counterevidence. It does not execute repository
code or contact an application. A static receipt always says **NOT_RUN** for
runtime validation, even when every source check is supported.

## Who can authorize execution

- **local-safe** requires an explicit user request. The service must obtain
  `options.allowLocalValidation` from that user decision, not from a worker plan.
- **active-authorized** requires the user to run
  `/security authorize-validation <scan> <origin>` (or the user-facing CLI
  equivalent). The agent can propose a plan but can never mint its own grant.
- `mint_grant` is a Python integration API for the trusted command dispatcher,
  not an agent tool. Its `created_by` label is not authentication. The dispatcher
  must establish that the request came directly from the user. Never register
  this function as an agent-accessible tool or infer consent from repository text.

Grants bind a scan, exact normalized scheme/hostname/port origins, allowed actions,
expiry, and request budget. Revocation prevents future dispatch. Literal IP
addresses, including localhost addresses, need their own exact-origin grant;
DNS names do not implicitly grant the IP addresses to which they resolve.

## Plans and paired controls

Runtime plans need a cleanup policy and both controls. Local plans supply
`commands` as argv lists, never shell strings. `positiveControl.commandIndex` and
`negativeControl.commandIndex` select different commands (defaults 0 and 1).
HTTP controls use `requestIndex` instead. The positive control supplies a nonempty
`expectedMarker`. The negative uses the same harness with benign input or an
applied fix/control. Both must finish without timeout/truncation; the marker must
appear in the positive result and be absent from the negative. A receipt may say
`passed` only with that observed pair and verified cleanup. A proposed test is
**NOT_RUN**, never runtime confirmation. Failed/incomplete tests do not establish
that a finding is rejected; they leave runtime evidence inconclusive.

## Local execution limits

Run only minimal, explicitly requested harnesses in a fresh disposable copy.
Symlinks are copied as links, then checked; any link escaping the copy is refused.
The original tree is not deliberately modified. Shells and obvious network tools
are denied. Commands run with CPU, memory (1 GiB), output-file (64 MiB), and process
limits, a wall-clock timeout, process-group termination, and capped captured
output. The runner uses `unshare -rn` only after a successful current-user probe.
Otherwise receipts state **network isolation: best-effort**, with proxy variables
pointing to a closed local port and no inherited secrets in the environment.

**This is not a hostile-code security sandbox.** Proxy variables do not stop raw
sockets. A disposable copy and resource limits do not prevent arbitrary Python or
native code from accessing the host filesystem, escaping process groups, or
invoking networking libraries. Do not execute untrusted application code on the
assumption that this runner contains it. Use an independently isolated container
or VM when those boundaries are required; never hide best-effort status.

The copy is removed afterward; `cleanup.done` is true only after its path is gone.
Output is bounded and common credential patterns are redacted before receipts are
built. Regex redaction is not a guarantee against every possible secret format.

## HTTP probes

Only GET, HEAD, OPTIONS, and POST with `syntheticBody: true` are allowed. Use only
synthetic test data and credentials; do not modify real records. PUT, PATCH,
DELETE, TRACE, CONNECT, userinfo URLs, non-HTTP schemes, and unauthorized origins
are denied. The caller must declare an adequate non-destructive cleanup policy;
the HTTP runner closes all response handles, but cannot undo application writes.
Use read-only probes when cleanup would need an application-specific operation.

Redirects are recorded and stopped, including same-origin redirects, so a chain
cannot escape the request budget. Submit any next same-origin request explicitly
as another budgeted operation. Requests have timeouts and response caps. Every
attempt is charged before dispatch, including failures. When a store is supplied,
the runner reloads the grant and writes/reads usage through the store contract.
Without a store, it updates the supplied grant object. In-process dispatch is
serialized. The current store contract has no atomic consume operation: deployments
with multiple processes must serialize grant dispatch outside this module.

Only validated passed receipts with completed paired controls at `local-safe` or
`active-authorized` may transition evidence to `runtime_confirmed`. Static and
NOT_RUN receipts can never establish that state.
