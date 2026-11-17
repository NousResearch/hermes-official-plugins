#!/usr/bin/env bash
# Offline evals for CI: the real Claude Code binary against a loopback peer, with no credentials and no inference.
# Pinned so a new Claude Code release can't turn a PR red; bump CLAUDE_CODE_VERSION to qualify a release.
set -euo pipefail
cd "$(dirname "$0")/.."
version="${CLAUDE_CODE_VERSION:-2.1.292}"
prefix="$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/claude-code.XXXXXX")"
npm install --prefix "$prefix" --no-audit --no-fund --loglevel=error "@anthropic-ai/claude-code@$version"
claude="$prefix/node_modules/.bin/claude"
case "$(uname -s)" in
  # Git Bash on Windows: npm's shim for Windows is claude.cmd, and Python needs a Windows path.
  MINGW*|MSYS*|CYGWIN*) claude="$(cygpath -m "$prefix")/node_modules/.bin/claude.cmd" ;;
esac
"$claude" --version
for eval in evals/directsdk_admission.py evals/directsdk_cache_wire.py; do
  echo "::group::$eval"
  python "$eval" "$claude"
  echo "::endgroup::"
done
