# Web and API review

## Trace
- Inventory routes, RPC methods, GraphQL resolvers, WebSocket messages, and middleware mounted above them; include versioned and bulk endpoints.
- Follow each untrusted parameter through validation, authorization, transformations, and the final database, shell, template, file, or outbound HTTP operation.
- Check object and field permissions, upload execution paths, archive extraction, URL redirects, DNS changes, and response data selection.

## Sinks and controls
- Parameterized queries, context-aware encoding, safe parsers, confined file paths, outbound destination allowlists, and bounded GraphQL complexity.
- Compare HTTP framing across proxies and backends; verify WebSocket Origin checks, session binding, and per-message authorization.

## Counterevidence
- Record inherited router guards, safe framework defaults, ORM binding, unreachable paths, and network egress controls with exact locations. Do not report a sink alone as exploitation.

## A10 exceptional conditions
- Trace validation exceptions, malformed multipart bodies, disconnects during uploads, canceled requests, and downstream timeouts. Look for leaked handles, partially committed writes, retry duplication, and successful responses after failure.
