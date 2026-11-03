# Supply-chain review

## Trace
- Inventory resolved direct and transitive dependencies, lockfiles, container bases, plugins, build tools, and update mechanisms; distinguish declared from deployed versions.
- Start from the scan's dependency leads (OSV advisories per lockfile, runtimes past end of life). For each, trace the import and the affected API to an input that reaches it; an advisory alone proves nothing, and a check that did not run is a coverage gap.
- Trace untrusted pull-request data into CI commands, credentials, caches, artifacts, and release jobs. Check package namespace precedence and dependency confusion.

## Sinks and controls
- Trusted registries, pinned versions/digests, advisory remediation, minimal CI permissions, protected environments, isolated untrusted builds, artifact signatures and provenance checked before use.
- Separate build scripts and downloaded code from inert data; include runtime image contents and post-install hooks in scope.

## Counterevidence
- Cite resolved locks, verified signatures, isolated runners, immutable artifacts, and unreachable vulnerable functionality. Read lockfiles and manifests directly; an unreviewed dependency surface is a coverage gap, not a clean result.

## A10 exceptional conditions
- Verify failed downloads, signature checks, registry timeouts, and incomplete builds cannot fall back to unverified packages or publish partial artifacts. Check retries, stale caches, and credential cleanup after failure.
