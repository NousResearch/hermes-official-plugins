"""Run Hermes' install-time plugin scanner over every plugin in this repo.

Usage: PYTHONPATH=<hermes-agent checkout> python .github/scripts/scan_plugins.py <label> <plugin-dir>...

Every plugin must scan `safe`. A `dangerous` verdict blocks the install outright, and `caution`
blocks a catalog re-pin on older Hermes releases, so either one fails CI. Real problems get
fixed in the plugin. False positives get fixed in the scanner (hermes-agent tools/plugin_guard.py),
or the plugin is reworded until the scanner is fixed. Medium/low findings are listed but do not fail.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from tools.plugin_guard import PLUGIN_SCANNER_VERSION, scan_plugin

LISTED = ("critical", "high", "medium")


def main() -> int:
    label, dirs = sys.argv[1], [Path(d) for d in sys.argv[2:]]
    rows, failed = [], []
    for d in dirs:
        result = scan_plugin(d)
        if result.verdict != "safe":
            failed.append(d.name)
        for f in sorted(result.findings, key=lambda f: (LISTED.index(f.severity) if f.severity in LISTED else 9, f.file, f.line)):
            if f.severity not in LISTED:
                continue
            path = (d / f.file).as_posix() if not Path(f.file).is_absolute() else Path(f.file).as_posix()
            level = "error" if result.verdict != "safe" and f.severity in ("critical", "high") else "warning"
            if level == "error" or f.severity == "high":
                print(f"::{level} file={path},line={max(f.line, 1)},title={f.pattern_id} ({f.severity}, {label})::{f.description}")
            rows.append(f"| {d.name} | {f.severity} | `{f.pattern_id}` | `{path}:{f.line}` |")
        print(f"{d.name}: {result.verdict}")
    summary = [f"### Install scanner ({label}, {PLUGIN_SCANNER_VERSION})", "",
               f"{len(dirs) - len(failed)}/{len(dirs)} plugins scan `safe`." + (f" Not safe: {', '.join(failed)}." if failed else "")]
    if rows:
        summary += ["", "<details><summary>Findings (critical/high/medium)</summary>", "",
                    "| plugin | severity | pattern | location |", "|---|---|---|---|", *rows, "", "</details>"]
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fh:
            fh.write("\n".join(summary) + "\n")
    if failed:
        print(f"::error::install scanner ({label}) verdict is not safe for: {', '.join(failed)}. "
              "Fix a real issue in the plugin; fix a false positive in hermes-agent tools/plugin_guard.py.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
