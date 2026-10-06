#!/usr/bin/env python3
"""Small development demo for the GhostCode Git history analyzer.

Usage::

    python3 demo.py ~/Projects/PayGuard
    python3 demo.py ~/Projects/PayGuard --verbose

The script is intentionally tiny: all analysis logic lives in
``src/ghostcode/git_analyzer.py``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional

# Allow running the demo directly from the repository root without installing.
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from ghostcode.git_analyzer import GitAnalyzerError, GitHistoryAnalyzer  # noqa: E402


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Print a readable summary of a local Git repository's history."
    )
    parser.add_argument(
        "repository",
        nargs="?",
        default=".",
        help="Path to a local Git repository (default: current directory)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Show per-commit hash, author and change statistics",
    )
    args = parser.parse_args(argv)

    try:
        history = GitHistoryAnalyzer(args.repository).analyze()
    except GitAnalyzerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"Repository: {history.repository_name}")
    print(f"Branch: {history.branch or '(detached HEAD)'}")
    print(f"Commits: {history.total_commits}")
    print()

    for index, commit in enumerate(history.commits, start=1):
        print(f"{index}. {commit.summary}")
        if args.verbose:
            print(f"   {commit.short_hash} | {commit.author} | {commit.authored_at:%Y-%m-%d %H:%M}")
            print(
                f"   +{commit.insertions} -{commit.deletions} "
                f"| {commit.files_changed} file(s)"
            )
            for path in commit.changed_files:
                print(f"     - {path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
