<p align="center"><img src="../assets/hermes-security.jpg" alt="Security: official Hermes Agent plugin" width="100%"></p>

# Security for Hermes Agent

> Part of the [Hermes Official Plugins](../README.md), Nous Research's official plugins for Hermes Agent.

hermes-security is a Hermes plugin that reviews a repository for security bugs.
It keeps each finding together with its evidence, any test results, and a record
of which code was actually read. Reports separate findings backed by the code
from findings confirmed by a test. Rejected and inconclusive candidates stay in
the report so you can see what was ruled out and why.

## Install

You need Hermes 0.21.5 or later and Python 3.11 or later.

```sh
hermes plugins install hermes-security --enable
```

Restart Hermes afterwards. To update, run `hermes plugins update hermes-security`.

The plugin also has a **Security** page for Hermes Desktop. Desktop finds it in
the installed package on its own. Like every Desktop plugin, it starts turned
off: switch it on under **Capabilities → Plugins**. The page needs the Python
plugin to stay installed and enabled, because that is where its data comes from.

If Desktop connects to Hermes on another machine, install through the Desktop
install dialog so it can fetch the page as well. A plugin installed only on
the remote machine shows its page as "unavailable (remote backend)". Repository
paths always refer to the machine running Hermes, not the one running Desktop.

### Installing from a local checkout

Clone [hermes-official-plugins](https://github.com/NousResearch/hermes-official-plugins), pick the
profile, then link the plugin's folder into that profile's plugins folder:

```sh
export HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
mkdir -p "$HERMES_HOME/plugins"
ln -s /absolute/path/to/hermes-official-plugins/hermes-security "$HERMES_HOME/plugins/hermes-security"
hermes plugins enable hermes-security
```

Restart Hermes after enabling it. If `$HERMES_HOME/plugins/hermes-security`
already exists, check what it points to before replacing it. With a linked
checkout, Desktop reloads the Security page whenever `desktop/plugin.js`
changes.

## Running a scan

In chat, ask something like "scan this repo for security issues". Or open the
**Security** page in Desktop, choose **New scan**, and fill in:

1. **Repository:** a folder on the machine running Hermes. Pick a recent one,
   use **Browse…**, or clone one with **From URL…**.
2. **What to scan:** **Whole codebase** or **Only my changes**. Only my changes
   means your uncommitted work, or your branch compared with the default branch.
   The plugin picks whichever applies.
3. **Scan area** (whole codebase only): the entire codebase, or one folder.
4. **Deep scan** (optional): slower, and looks harder for subtle issues.
5. **Run tests to confirm findings** (optional): the agent writes small
   reproductions and runs them with your usual toolchain, in a temporary copy
   of the repository.
6. **Model** (optional): the model for the scan's chat. It defaults to the one
   your chats use.
7. **Subagents** (optional): shown when the advanced-delegation plugin is
   enabled. It picks the model route or Hermes profile every reviewer runs on.
8. **Additional context** (optional): what the app does, what worries you, and
   where to look first.

Press **Start scan**. If the scan can't start in the background, a chat message
is drafted for you to send instead.

The agent first works out what an attacker could reach and which inputs are
trusted. Then it reviews the code, checks each candidate against the code (and
with tests, if you allowed them), and traces whether an attacker can actually
reach it. You don't need to install a scanner, because the review is done by the
model. Vendored code, symlinks, special files, and files over 8 MB are skipped,
and the report lists them.

The work is split by folder into chunks of about 4,000 lines, roughly what one
reviewer can read at once. One more reviewer looks across the whole scope for
entry points and trust boundaries. Up to 10 reviewers run at the same time.

### Standard and deep scans

A standard scan reads production code: source, config, CI, infrastructure as
code, and migrations. It skips tests, fixtures, docs, examples, translations,
generated files, assets, and lockfiles, unless one of them contains a committed
secret. Everything it skips appears in the report with the reason.

If the production code fits in 90 chunks (about 360,000 lines), all of it is
read. In a bigger repository, files are ranked by what the code does (routes, request
input, shell commands, SQL, deserialization, auth, webhooks) and how often it
does it. The riskiest files are read first until the budget runs out.
Developer tooling such as scripts and evals ranks lower. Up to two of the
riskiest chunks also get a second, independent reviewer.

If a reviewer can't finish its files, they go to a new reviewer. This repeats
for up to six rounds, as long as each round makes progress. The plan is saved
when the scan starts, so updating the plugin never reshuffles a running scan.

A deep scan skips none of those files and makes independent passes over the
whole repository, three by default.

### Dependency check

Before the review starts, the scan checks your lockfiles against published
advisories using Google's [osv-scanner](https://github.com/google/osv-scanner).
It also checks declared runtime versions (Node, Python, Go, PHP, Ruby) against
endoflife.date.

On the first scan, the plugin downloads a pinned osv-scanner release from
GitHub (about 55 MB). It checks the file's SHA-256 against the pin, refuses it
if they differ, and keeps it in the plugin's data folder. Package names and
versions are sent to api.osv.dev, and to api.deps.dev for manifests that have no
lockfile.

Advisories are collected into one dependency finding, rated at the worst
advisory it contains. Packages a `uv.lock` or `package-lock.json` installs only
for development (test runners, linters) are marked as such and count as low,
because they don't ship with your code. A separate finding is reported only
when the review shows your code reaches the vulnerable behavior. If the check
can't run, the report says so and marks the scan as partial.

To turn the check off, set `HERMES_SECURITY_DEPENDENCY_CHECK=off`. If your
network blocks GitHub, ask an admin to install osv-scanner and set
`HERMES_SECURITY_OSV_SCANNER` to its path.

### If a scan stops partway

The plugin keeps track of where every scan is. Your next message, or
**Continue scan** on the scan's page, picks up at the right step. That means
starting any reviews that have no saved result, checking candidates, tracing
attack paths, and finishing the report. If a reviewer ends without saving its
result, its work is handed out again.

The plugin can also continue a scan by itself. When a chat turn ends and the
scan still has work left, the plugin sends one "Continue security scan" message
to that chat. It never does this after you press Stop. After three automatic
continues with no progress, it pauses the scan and marks it as needing
attention until you continue it yourself.

Because this lets the plugin send messages into your chat, you have to switch
it on once:

```bash
hermes config set plugins.entries.hermes-security.allow_gateway_injection true
```

Without that setting, a stalled scan is marked as unable to continue on its own
and waits for **Continue scan** or a message from you. A turn that fails with a
model provider error doesn't trigger an automatic continue; send any message to
resume.

### Testing a running app (optional)

On an open scan, enter the app's address under **Confirming findings** and
click **Allow testing this app**. You can also run
`/security authorize-validation <scan> <origin>`. For the next hour, the agent
may then send up to 200 requests to that exact address. It follows redirects
within the same site and never to other sites. **Stop** cancels the permission
early.

## Safety

- Repository text is treated as data, never as instructions to the agent.
- Secrets are never stored. Reports keep only the secret's type, its location,
  and a one-way fingerprint.
- Git hooks and filters never run.
- Every finding quotes the exact code from the scanned snapshot.
- Finished reports are sealed, and later triage doesn't change them.

Tests run as your user, in a temporary copy. Environment variables that look
like credentials are removed, and so are the ones that point at credentials,
such as `SSH_AUTH_SOCK`, `KUBECONFIG`, `AWS_PROFILE` and `DATABASE_URL`. Your
home directory stays readable, so that is a convenience, not a sandbox: only
allow tests on code you trust.

A finding is marked "Confirmed by test" only when its test passes and a matching
negative test behaves as expected. A test that never ran is recorded as
`NOT_RUN` and counts as no proof either way.

## Reviewing findings

The **Findings** page has a **Review** layout and a full-width **Table** layout.
Both use the same filters: search, repository, folder, severity, status, and
evidence. Findings are sorted by severity. Each finding has **Overview**,
**Evidence**, and **Remediation** tabs. Gaps in the proof and counter-evidence
are shown separately from the severity. Suggested patches are shown for reading
only; the plugin never applies them.

Reports export as Markdown, JSON, CSV, and SARIF.

## Tools and commands

The agent gets these tools in the `security` toolset:

- `security_scan_start`
- `security_scan_get`
- `security_scan_checkpoint`
- `security_scan_submit_worker_result`
- `security_scan_record_validations`
- `security_scan_record_chains`
- `security_scan_finalize`
- `security_scan_cancel`
- `security_scan_export`

The `/security` chat command supports `status`,
`authorize-validation <scan> <origin>`, `revoke <grant>`, and `help`. Only you
can allow testing of a running app. The agent's tools can't grant that
permission.

From a terminal, use `hermes security-review ...` or
`python -m hermes_security ...`. Run either with `--help` to list the
subcommands. For example:

```sh
python -m hermes_security start --path <repo> [--mode diff|deep] [--tests] [--context ...]
```

## Data and limits

Scans and reports are stored in the active profile at
`$HERMES_HOME/plugin-data/agent-plugin-hermes-security-974429e7`.
`python -m hermes_security data-dir` prints the exact path. Nothing is uploaded
to a findings service or ticket system, and exports go only where you save them.

Storing data locally doesn't keep the analysis local. The model provider
configured in Hermes receives the code it is asked to review. Treat reports as
sensitive.

Keep these limits in mind:

- Code that wasn't reviewed, and reviewers that didn't return a result, show up
  as gaps in coverage. Zero findings with partial coverage doesn't mean the code
  is clean.
- The OWASP Top 10:2025 and ASVS 5.0.0 mappings show what was reviewed. They
  are not a certification. A scan is complete when every file in scope and all
  ten OWASP categories are closed; the ASVS checklist is reported as a count of
  controls checked and does not decide completeness.
- The model can miss issues or misread code, so check its conclusions.
- Deep scans have a fixed budget and can be resumed, but a scan isn't
  guaranteed to keep running through a Hermes restart.

## Development

From this folder, with a [hermes-agent](https://github.com/NousResearch/hermes-agent) checkout beside
the hermes-official-plugins checkout:

```sh
HERMES_AGENT_REPO=../../hermes-agent PYTHONPATH=../../hermes-agent python -m pytest -q
hermes plugins validate .
hermes plugins doctor .
python scripts/provenance.py --check
```

`docs/CONTRACT.md` describes the implementation contract.

Some files are adapted from an upstream project, and
`UPSTREAM_PROVENANCE.json` records where each one came from. After changing an
adapted file, run `python scripts/provenance.py` to regenerate it, then
`--check` to confirm. Both need a full checkout of the pinned upstream
revision. Pass its location with `--source-root` or `HERMES_SECURITY_UPSTREAM`.

See `LICENSE`, `NOTICE`, and `THIRD_PARTY_NOTICES.md` for attribution.
