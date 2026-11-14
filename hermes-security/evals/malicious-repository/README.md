# Hostile-repository canaries (Task 14)

Executable fixture builders live in `tests/integration/test_hostile_repository.py`.
Everything hostile is generated under pytest `tmp_path`: **no committed symlinks,
FIFOs, devices, secrets, scanner binaries or archive bombs**. Inputs use synthetic
sentinels only. Tests are offline and use the real `SecurityService` except the
focused `/dev/null` reader and subprocess-output boundary probes.

Run from the repository root:

```sh
python -m pytest -q -p no:cacheprovider tests/integration/test_hostile_repository.py
```

## Cases and expected outcomes

| Builder/canary | Expected outcome |
|---|---|
| Instruction text in AGENTS/SECURITY/README and source comments | No execution or policy changes; source snippets remain inside longer Markdown fences |
| Symlinks, FIFOs, oversized file | Skipped and listed in the report; harmless skips do not make the scan partial |
| Device descriptor | `/dev/null` rejected as non-regular before reading |
| Hardlink to external file | Link count recorded; target and external file unchanged |
| Symlink swap during excerpt verification | Rejected candidate; external sentinel never persisted |
| Hardlink swap restored before second snapshot check | Exact bytes used for evidence must match the inventoried SHA-256 |
| Traversal, doubled slash, absolute, case and Unicode-normalization aliases | No borrowed inventory identity; candidate rejected |
| Very long line, malformed UTF-8, NUL bytes | Inventoried for the reviewer; marked unsupported, the scan stays partial |
| Zip/tar high-compression content and nested archive | Never extracted; opaque/unsupported content recorded, coverage partial |
| Foreign scan packet, candidate ID, attempt ID | Rejected; second scan's candidates unchanged |
| Unknown role, malformed IDs, oversized/compacted worker summaries | Contract rejection; summaries never confer review coverage |
| Token crossing read chunk; multiline PEM; isolated excerpt line | No secret literal in any data file, including SQLite/WAL/SHM |
| Finalizer lease contention and exception on third artifact write | No sealed half-bundle; verification fails; cancel/resume/retry works |
| Late completion after seal | `SealedError`, bundle bytes unchanged |
| Provider/model changes in host-owned attempts | Recorded as metadata, not coverage authority |

The fixture guard hashes regular-file bytes, relative paths, types and modes;
it records symlink targets without following them and never reads FIFOs/devices.
Simulated attacker swaps are restored before the guard runs. Coverage canaries
explicitly close unrelated file/worker/standards units so missing baseline work
cannot mask a false-complete defect.

## Known strict xfail

`test_provider_fallback_is_present_in_sealed_runtime`: host-owned attempt metadata
persists, but sealed `runtime.fallbacks` is always empty. The worker contract has
no trusted per-attempt runtime/fallback transport. This needs host lifecycle
integration rather than accepting untrusted worker metadata or guessing provider
changes from process environment; it is deliberately **not** claimed complete.

## Test-forced production changes

- `orchestration/coverage.py`: unreadable or changed-during-read exclusions
  prevent completeness. Justified by exclusion closure tests.
- `orchestration/worker_protocol.py`: supplied candidate IDs must match computed
  scan-bound identity. Justified by cross-scan contamination test.
- `target/excerpt.py` and `orchestration/worker_protocol.py`: optional expected file
  digest is checked on the exact bytes excerpted. Justified by the transient
  hardlink swap (before/after inventory checks alone were insufficient).

These canaries do not establish arbitrary-language secret reconstruction. On
Windows the target reader uses handle-relative NT opens that refuse reparse points
(symlinks, junctions, mount points).
