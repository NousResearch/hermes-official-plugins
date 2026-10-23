"""Compare the baseline and current subjects API using only a temporary synthetic DB."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def seed(home, sessions, messages):
    from hermes_state import SessionDB

    db = SessionDB(home / "state.db")
    db.close()
    with sqlite3.connect(home / "state.db") as conn:
        for index in range(sessions):
            session_id = f"synthetic-{index}"
            timestamp = 1700000000 + index * 3600
            conn.execute(
                "INSERT INTO sessions (id, source, started_at, title, message_count) VALUES (?, ?, ?, ?, ?)",
                (session_id, "cli", timestamp, f"Synthetic Project {index} React dashboard", messages),
            )
            for number in range(messages):
                conn.execute(
                    "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
                    (session_id, "user" if number % 2 == 0 else "assistant",
                     f'Investigate SyntheticWidget{index} in src/project{index}/view.tsx. '
                     'Use React and FastAPI with pytest. Review "Synthetic Search" and /memory. '
                     'Implemented deterministic search tests and documented the dashboard behavior. ',
                     timestamp + number),
                )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", default="9bc3913")
    parser.add_argument("--sessions", type=int, default=600)
    parser.add_argument("--messages", type=int, default=12)
    parser.add_argument("--repeats", type=int, default=5)
    args = parser.parse_args()
    if min(args.sessions, args.messages, args.repeats) < 1:
        parser.error("counts must be positive")
    baseline_ref = subprocess.run(
        ["git", "rev-parse", "--verify", "--end-of-options", args.baseline + "^{commit}"],
        cwd=ROOT, check=True, capture_output=True, text=True,
    ).stdout.strip()
    host = Path(os.environ.get("HERMES_AGENT_REPO", Path.home() / ".hermes" / "hermes-agent"))
    sys.path.insert(0, str(host))
    with tempfile.TemporaryDirectory(prefix="memory-wiki-benchmark-") as temp:
        home = Path(temp)
        os.environ["HERMES_HOME"] = str(home)
        baseline_dir = home / "baseline"
        baseline_dir.mkdir()
        for filename in ("memory_wiki_core.py", "plugin_api.py"):
            source = subprocess.run(
                ["git", "show", f"{baseline_ref}:dashboard/{filename}"],
                cwd=ROOT, check=True, capture_output=True,
            ).stdout
            (baseline_dir / filename).write_bytes(source)
        baseline = load("benchmark_baseline_api", baseline_dir / "plugin_api.py")
        baseline._CORE_MODULE = "benchmark_baseline_core"
        current = load("benchmark_current_api", ROOT / "dashboard" / "plugin_api.py")
        seed(home, args.sessions, args.messages)
        queries = ["react", "fastapi", "project599", "missing-term", "Synthetic Search"]

        async def measure(api):
            outputs, timings = [], []
            for query in queries:
                start = time.perf_counter()
                outputs.append(await api.get_subjects(q=query, limit=100))
                timings.append(round((time.perf_counter() - start) * 1000, 3))
            return outputs, timings

        expected, before = asyncio.run(measure(baseline))
        repeated_before = [asyncio.run(measure(baseline)) for _ in range(args.repeats)]
        actual, after = asyncio.run(measure(current))
        repeated_after = [asyncio.run(measure(current)) for _ in range(args.repeats)]
        assert actual == expected
        assert all(output == expected for output, _ in repeated_before + repeated_after)
        digest = hashlib.sha256(json.dumps(expected, sort_keys=True).encode()).hexdigest()
        print(json.dumps({
            "sessions": args.sessions, "messages_per_session": args.messages,
            "session_scope": 500, "queries": queries, "exact_output_parity": True,
            "output_sha256": digest,
            "baseline_first_query_ms": before[0],
            "current_cold_query_ms": after[0],
            "baseline_first_batch_ms": before, "current_first_batch_ms": after,
            "baseline_repeated_median_ms": statistics.median(t for _, times in repeated_before for t in times),
            "current_repeated_median_ms": statistics.median(t for _, times in repeated_after for t in times),
        }, indent=2))


if __name__ == "__main__":
    main()
