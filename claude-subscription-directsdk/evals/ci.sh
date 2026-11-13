#!/usr/bin/env bash
# Offline evals and the session tests for CI: the real Claude Code binary against a loopback peer, with no credentials
# and no inference.
# Pinned so a new Claude Code release can't turn a PR red; bump CLAUDE_CODE_VERSION to qualify a release.
set -euo pipefail
cd "$(dirname "$0")/.."
version="${CLAUDE_CODE_VERSION:-2.1.292}"
prefix="$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/claude-code.XXXXXX")"
npm install --prefix "$prefix" --no-audit --no-fund --loglevel=error "@anthropic-ai/claude-code@$version"
claude="$prefix/node_modules/.bin/claude"
"$claude" --version
for eval in evals/directsdk_admission.py evals/directsdk_cache_wire.py; do
  echo "::group::$eval"
  python "$eval" "$claude"
  echo "::endgroup::"
done
# The session tests skip without a Claude Code on PATH, so the plugin suite alone never runs them. Run them on the pin.
echo "::group::session tests"
CLAUDE_CODE_BIN="$claude" python -m pytest -q tests/test_directsdk_session.py tests/test_directsdk_session_lifecycle.py tests/test_directsdk_session_parked.py
echo "::endgroup::"
