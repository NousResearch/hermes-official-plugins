# Model routing: workflow details

Load this before splitting a multi-module or multi-wave build. The core loop is in `SKILL.md`.

## Difficulty and risk rubric

- 1: pure mechanics with a deterministic check.
- 2: small change following an existing pattern.
- 3: normal feature slice or bug fix in known code; several files in one area.
- 4: novel design, cross-module change, migration, non-obvious debugging, polished deliverable.
- 5: architecture, security, audits, ambiguous high-stakes reasoning.
- Risk high: live data or database writes, money logic, auth/security, durable writes outside the
  repo, releases.
- Classes: `route_ledger.py classes` prints the fixed list; names are exact (`code-feature`, not
  `feature`). Use `other` and say what in `--task` when nothing fits.

## How difficulty maps onto your ladder

With `n` routes ordered cheapest (index 0) -> strongest (index `n-1`), difficulty `d` starts at
position `(d-1)*(n-1)/4`; an exact half rounds down, toward the cheaper route.

| Routes | d1 | d2 | d3 | d4 | d5 |
|---|---|---|---|---|---|
| 1 | 1 | 1 | 1 | 1 | 1 |
| 2 | 1 | 1 | 1 | 2 | 2 |
| 3 | 1 | 1 | 2 | 2 | 3 |
| 4 | 1 | 2 | 2 | 3 | 4 |
| 5 | 1 | 2 | 3 | 4 | 5 |
| 7 | 1 | 2 | 4 | 5 | 7 |

(Route numbers, 1 = cheapest.) High risk floors the task at the middle route (index
`(n-1)//2`, the lower middle on an even ladder) and moves anything already at or above the middle
one route up, capped at the strongest. `route_ledger.py ladder` prints the table for your ladder.

## Learning rules (implemented in the script)

- No history for the class and difficulty: rubric baseline, `confidence: rubric-only`.
- A model-caused failure on route R among the last 10 records of the same class and difficulty
  floors future tasks at R+1.
- Trial demotion one route down only after 3 straight first-try passes on the current route, low
  risk, and no counted model failure below it. A failed trial restores the floor.
- Each record stores what its route ran on (`provider:model@effort`, plus `+tier` and
  `+daybreak` when set). After `sync-routes` sees a route re-pointed, that route's earlier
  model-caused failures stop counting; passes always count.
- `escalate_on_model_failure_to` is `null` on the strongest route: fix the brief or split the
  task instead.

## Briefs

Every brief is self-contained (subagents know nothing): goal, owned files, files to read first,
acceptance check, and these rules:

- Do not commit; the parent commits per wave.
- Edit only your owned files; report, but do not fix, failures in other tasks' unfinished files.
- Do not edit agent skills; put proposed skill text in your final reply.
- End with "decisions the parent must record" (renamed exports, chosen defaults, deviations).

## Multi-module builds

- **Contract first.** Write and commit a contract doc yourself: IDs, request/reply shapes, error
  codes, and a module table naming the owning task per file. Every brief says "read the contract
  first". A follow-on slice gets an additive contract file that names what it overrides, instead
  of editing the base contract in place.
- **Gap-check the plan** with one read-only task on the strongest route before dispatch: give it
  the file paths, the agreed rules it must not re-litigate, a separate list of your own design
  choices to challenge, and numbered focus areas. Verify its top claims in code, then fold them in.
  Skip for trivial changes.
- **Wave by dependency.** Wave 1: pieces nothing else needs (pure rules modules, data layer,
  tooling). Wave 2: their consumers. Wave 3: anything sharing a file with wave 2.
- **One shared file, one owner per wave.** Give a shared single-file resource (translations,
  config JSON) to exactly one task, limited to a key prefix; a second task writes its keys to a
  side file the parent merges. Generated manifests belong to the parent, rebuilt after the merge;
  name their staleness tests in briefs as "expected red, do not fix".
- **Record decisions between waves.** Append each task's "decisions the parent must record" to the
  contract before the next wave starts.
- **Integration task.** After producer and consumer waves, one task drives the real modules of
  both sides together and fixes the mismatches: per-side unit tests can all pass while the sides
  disagree on shapes and IDs. List every pair of child decisions that read the same contract word
  differently as known candidates for it to check first.
- **Final review fixes** split by side run in parallel; a shared end-to-end test file belongs to
  exactly one side, and the other reports the assertions its change breaks.
- **Re-run the suite after your own commit, merge or branch switch**: a child's green run in a
  dirty tree can turn red once git rewrites the working tree.

## Pause and resume

1. Stop every live child with `delegate_task(action='stop', subagent_id=...)`, one call per child;
   the ids are in each spawn response, or list them with `delegate_task(action='list')`.
2. Commit the unfinished work to a separate work-in-progress branch (never onto the verified
   branch) with a state note: verified vs unverified parts, which task finished or was stopped,
   and numbered resume steps.
3. To resume: check out that branch, run the full suite, re-dispatch only the stopped task with
   "continue from the committed draft, do not start over", then merge into the verified branch
   and delete the work-in-progress branch.
4. Ledger: a child stopped by the pause is `--outcome fail --cause env`; its resume is attempt 2
   on the same route.
