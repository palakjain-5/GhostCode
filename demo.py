#!/usr/bin/env python3
"""Small development demos for GhostCode.

Usage::

    python3 demo.py git ~/Projects/PayGuard          # git history summary
    python3 demo.py git ~/Projects/PayGuard --verbose
    python3 demo.py code ~/Projects/PayGuard          # python code summary
    python3 demo.py code ~/Projects/PayGuard --verbose
    python3 demo.py intent ~/Projects/PayGuard        # extracted intents

For backward compatibility a bare path is treated as the ``git`` command::

    python3 demo.py ~/Projects/PayGuard

The script is intentionally tiny: all analysis logic lives in
``src/ghostcode/git_analyzer.py``, ``src/ghostcode/code_analyzer.py`` and
``src/ghostcode/intent_extractor.py``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional

# Allow running the demo directly from the repository root without installing.
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from ghostcode.code_analyzer import (  # noqa: E402
    CodeAnalyzerError,
    PythonCodeAnalyzer,
    RepositoryCodeAnalysis,
)
from ghostcode.git_analyzer import GitAnalyzerError, GitHistoryAnalyzer  # noqa: E402
from ghostcode.intent_extractor import IntentExtractor  # noqa: E402

COMMANDS = ("git", "code", "intent")


def _display_path(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _run_git(args: argparse.Namespace) -> int:
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
            print(
                f"   {commit.short_hash} | {commit.author} | "
                f"{commit.authored_at:%Y-%m-%d %H:%M}"
            )
            print(
                f"   +{commit.insertions} -{commit.deletions} "
                f"| {commit.files_changed} file(s)"
            )
            for path in commit.changed_files:
                print(f"     - {path}")
    return 0


def _important_calls(analysis: RepositoryCodeAnalysis) -> List[str]:
    """Call names that resolve to functions/methods defined in the
    repository itself — deterministic, purely structural selection."""
    defined = {fn.name for f in analysis.files for fn in f.functions}
    defined |= {
        method.name
        for f in analysis.files
        for cls in f.classes
        for method in cls.methods
    }
    used = {c.function_name for f in analysis.files for c in f.function_calls}
    return sorted(defined & used)


def _run_code(args: argparse.Namespace) -> int:
    try:
        analysis = PythonCodeAnalyzer(args.repository).analyze()
    except CodeAnalyzerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    root = analysis.repository_path
    print(f"Repository: {root.name}")
    print()
    print(f"Python files: {analysis.total_python_files}")
    print(f"Functions: {analysis.total_functions}")
    print(f"Classes: {analysis.total_classes}")
    print(f"Imports: {analysis.total_imports}")
    print(f"Function calls: {analysis.total_function_calls}")

    if analysis.errors:
        print(f"Files with errors: {analysis.total_errors}")
        for error in analysis.errors:
            print(
                f"  - {_display_path(error.file_path, root)}: "
                f"[{error.error_type}] {error.message}"
            )

    print()
    print("Important discovered calls (defined in this repository):")
    important = _important_calls(analysis)
    if important:
        for name in important:
            print(f"- {name}")
    else:
        print("- (none)")

    unique = {
        c.function_name for f in analysis.files for c in f.function_calls
    }
    print(f"Unique call names: {len(unique)}")

    if args.verbose:
        print()
        print("Files:")
        for f in analysis.files:
            print(
                f"- {_display_path(f.file_path, root)} "
                f"({f.lines_of_code} LOC, {len(f.function_calls)} calls)"
            )
            for fn in f.functions:
                conditions = "if" if fn.has_conditions else "-"
                loops = "loop" if fn.has_loops else "-"
                print(
                    f"    def {fn.name}({', '.join(fn.arguments)}) "
                    f"[{conditions} {loops}] returns={len(fn.returns)}"
                )
            for cls in f.classes:
                bases = f"({', '.join(cls.base_classes)})" if cls.base_classes else ""
                methods = ", ".join(m.name for m in cls.methods)
                print(f"    class {cls.name}{bases}: {methods}")
    return 0


def _run_intent(args: argparse.Namespace) -> int:
    try:
        history = GitHistoryAnalyzer(args.repository).analyze()
    except GitAnalyzerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    intents = IntentExtractor().extract_all(history.commits)
    print(f"Repository: {history.repository_name}")
    print(f"Commits: {len(intents)}")
    print()

    for index, intent in enumerate(intents, start=1):
        print(f"Commit:")
        print(intent.commit_message.splitlines()[0])
        print()
        print("Intent:")
        print(f"Action: {intent.action or '-'}")
        print(f"Object: {intent.object}")
        print(f"Category: {intent.category}")
        print(f"Scope: {intent.scope or '-'}")
        print(f"Confidence: {intent.confidence:.2f}")
        print()
        print("Evidence:")
        for line in intent.evidence:
            print(f"- {line}")
        if index < len(intents):
            print()
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # Backward compatibility: `demo.py <path>` still runs the git command.
    if not argv or (argv[0] not in COMMANDS and argv[0] not in ("-h", "--help")):
        argv.insert(0, "git")

    parser = argparse.ArgumentParser(
        description="GhostCode development demos (git history / python code)."
    )
    subparsers = parser.add_subparsers(dest="command")

    git_parser = subparsers.add_parser(
        "git", help="Print a summary of a repository's Git history."
    )
    git_parser.add_argument(
        "repository",
        nargs="?",
        default=".",
        help="Path to a local Git repository (default: current directory)",
    )
    git_parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Show per-commit hash, author and change statistics",
    )

    code_parser = subparsers.add_parser(
        "code", help="Print a structural summary of a repository's Python code."
    )
    code_parser.add_argument(
        "repository",
        nargs="?",
        default=".",
        help="Path to a local repository (default: current directory)",
    )
    code_parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Show per-file functions, classes and call counts",
    )

    intent_parser = subparsers.add_parser(
        "intent",
        help="Extract structured developer intents from the commit history.",
    )
    intent_parser.add_argument(
        "repository",
        nargs="?",
        default=".",
        help="Path to a local Git repository (default: current directory)",
    )

    args = parser.parse_args(argv)
    if args.command == "code":
        return _run_code(args)
    if args.command == "intent":
        return _run_intent(args)
    return _run_git(args)


if __name__ == "__main__":
    raise SystemExit(main())
