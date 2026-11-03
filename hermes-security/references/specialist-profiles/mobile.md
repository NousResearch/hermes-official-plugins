# Mobile review

## Trace
- Inventory exported activities, services, receivers, providers, deep links, app links, URL schemes, and WebView bridges. Trace caller-controlled intents and URLs into privileged operations.
- Follow tokens and sensitive data through local databases, preferences, logs, backups, screenshots, notifications, and remote sync.

## Sinks and controls
- Component permissions, caller validation, URI grants, safe WebView configuration, keychain/keystore use, TLS trust checks, server authorization, and per-account data isolation.

## Counterevidence
- Cite manifest export defaults for the supported platform version, OS sandbox boundaries, verified app links, server-side permission checks, and actual backup exclusions.

## A10 exceptional conditions
- Check process death mid-write, offline replay, account-switch races, canceled biometric prompts, keystore unavailability, and failed certificate validation. Recovery must not restore another account's data or weaken transport trust.
