#!/usr/bin/env python3
"""Regenerate or check pinned upstream adaptation hashes without network access."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys

UPSTREAM = {
    "repository": "https://github.com/openai/codex-security",
    "commit": "89aae242136312467790947f3b122ca3f607614f",
    "license": "Apache-2.0",
}
HERMES = {
    "repository": "NousResearch/hermes-agent",
    "commit": "15cf1417e4c53ebea9d415abb5bcd6af8b1577d3",
}
DEFAULT_SOURCE = Path(os.environ.get("HERMES_SECURITY_UPSTREAM", "codex-security"))
NOTICE = re.compile(
    r'^\s*(?:#\s*|<!--\s*(?:#\s*)?|"\$comment"\s*:\s*")'
    r'Modified from Apache-2\.0 source\s+'
    r'([^\n]+?)\s+\(see NOTICE\)\.',
    re.MULTILINE,
)
SOURCE_PATH = re.compile(r'[^\s<>"]+')
SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", "node_modules", "build", "dist"}


def collect_entries(repo_root: Path, source_root: Path) -> list[dict]:
    """Hash each noticed text file and its contained, pinned source file."""
    repo_root = repo_root.resolve()
    source_root = source_root.resolve()
    entries = []
    for directory, dirs, files in os.walk(repo_root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not (Path(directory) / d).is_symlink())
        for name in sorted(files):
            destination = Path(directory) / name
            if destination.is_symlink():
                continue
            data = destination.read_bytes()
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                continue
            notices = set(NOTICE.findall(text))
            if len(notices) > 1:
                raise ValueError(f"Multiple upstream sources in {destination.relative_to(repo_root)}")
            if not notices:
                continue
            # One notice may list several sources separated by "; "; each gets its own entry.
            for source in (part.strip() for part in notices.pop().split(";")):
                relative = PurePosixPath(source)
                if (not SOURCE_PATH.fullmatch(source) or relative.is_absolute()
                        or ".." in relative.parts or "\\" in source):
                    raise ValueError(f"Unsafe upstream path: {source}")
                candidates = [source_root / "plugins/codex-security" / source, source_root / source]
                original = next((p.resolve() for p in candidates if p.is_file() and p.resolve().is_relative_to(source_root)), None)
                if original is None:
                    raise ValueError(f"{destination.relative_to(repo_root)}: upstream file missing or outside source root: {source}")
                entries.append({
                    "source": source,
                    "destination": destination.relative_to(repo_root).as_posix(),
                    "license": "Apache-2.0",
                    "modified": True,
                    "sourceSha256": hashlib.sha256(original.read_bytes()).hexdigest(),
                    "destinationSha256": hashlib.sha256(data).hexdigest(),
                })
    return sorted(entries, key=lambda entry: (entry["destination"], entry["source"]))


def main(argv=None) -> int:
    """Return nonzero on missing source, changed hashes, or missing entries."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="compare only; never update the ledger")
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--source-root", type=Path, default=DEFAULT_SOURCE)
    args = parser.parse_args(argv)
    ledger = args.repo_root / "UPSTREAM_PROVENANCE.json"
    try:
        expected = {"upstream": UPSTREAM, "hermes": HERMES, "files": collect_entries(args.repo_root, args.source_root)}
        if args.check:
            actual = json.loads(ledger.read_text(encoding="utf-8"))
            if actual != expected:
                print("Provenance drift: missing/stale entries, hashes, or revision metadata; regenerate after review.", file=sys.stderr)
                return 1
            print(f"Provenance verified: {len(expected['files'])} adapted files")
        else:
            if ledger.is_symlink():
                raise ValueError("Refusing to overwrite symlinked provenance ledger")
            ledger.write_text(json.dumps(expected, indent=2) + "\n", encoding="utf-8")
            print(f"Provenance regenerated: {len(expected['files'])} adapted files")
    except (OSError, ValueError) as exc:
        print(f"Provenance error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
