# Browser client review

## Trace
- Follow URL, message, storage, API, and user input into DOM rendering, navigation, event handlers, templates, and browser storage.
- Trace postMessage origin/source checks, service-worker scope, token handling, third-party scripts, and client-side route guards back to server enforcement.

## Sinks and controls
- Safe text DOM APIs, context encoding, HTML sanitization, restrictive CSP, secure cookie attributes, allowed navigation schemes, and minimal sensitive local storage.

## Counterevidence
- Cite framework auto-escaping, sanitizer configuration, server-side authorization, and actual CSP deployment. A client-side guard does not protect a server action.

## A10 exceptional conditions
- Check stale cached identities, failed logout, refresh races, offline writes replayed twice, malformed messages, and fallback rendering that bypasses sanitization. Expired authorization must not become trusted local state.
