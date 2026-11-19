#!/usr/bin/env python3
"""Offline canaries by default; model scoring only with externally supplied bundles."""
import argparse
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from evals.harness import deterministic, score_bundle_directory, print_result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--deterministic', action='store_true')
    mode.add_argument('--score-bundles', type=Path)
    args = parser.parse_args(argv)
    try:
        result = score_bundle_directory(args.score_bundles) if args.score_bundles else deterministic()
    except Exception as exc:
        return print_result({'gates': [{'gate': 'evaluation-input', 'threshold': 'valid input', 'measured': str(exc), 'status': 'FAIL'}]})
    return print_result(result)


if __name__ == '__main__':
    raise SystemExit(main())
