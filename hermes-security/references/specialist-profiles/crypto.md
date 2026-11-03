# Cryptography review

## Trace
- Inventory secrets, keys, passwords, tokens, certificates, signatures, and encrypted stores; trace generation, distribution, use, rotation, and destruction.
- Follow nonce and IV state across restart, parallel operations, key rotation, and retries; check entropy sources and algorithm-specific uniqueness.

## Sinks and controls
- Approved primitives and parameters, authenticated encryption, costly password KDFs, constant-time comparisons, explicit signature algorithms and issuer trust, hostname/certificate validation.
- Confirm key separation, least-privilege access, secret redaction, and transport protection on internal and outbound paths.

## Counterevidence
- Record maintained library guarantees, secure default parameters, non-secret checksums, and key-management policies actually enforced by deployment. Encoding alone is not encryption.

## A10 exceptional conditions
- Reject invalid tags, unknown keys, entropy failures, and verification errors without plaintext fallback. Examine padding/error oracles, truncated ciphertext, partial rotation, and timeout-induced nonce reuse.
