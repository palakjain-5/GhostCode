#!/usr/bin/env python3
"""Small development demos for GhostCode.

Usage::

    python3 demo.py git ~/Projects/PayGuard          # git history summary
    python3 demo.py git ~/Projects/PayGuard --verbose
    python3 demo.py code ~/Projects/PayGuard          # python code summary
    python3 demo.py code ~/Projects/PayGuard --verbose
    python3 demo.py intent ~/Projects/PayGuard        # extracted intents
    python3 demo.py drift ~/Projects/PayGuard         # intent drift report
    python3 demo.py risk ~/Projects/PayGuard          # risk scores for drift

For backward compatibility a bare path is treated as the ``git`` command::

    python3 demo.py ~/Projects/PayGuard

The script is intentionally tiny: all analysis logic lives in
``src/ghostcode/git_analyzer.py``, ``src/ghostcode/code_analyzer.py``,
``src/ghostcode/intent_extractor.py``, ``src/ghostcode/drift_detector.py``
and ``src/ghostcode/risk_scorer.py``.
"""

from __future__ import annotations

import argparse
import json
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
from ghostcode.drift_detector import IntentDriftDetector  # noqa: E402
from ghostcode.risk_scorer import calculate_risk_report  # noqa: E402

COMMANDS = ("git", "code", "intent", "drift", "risk")


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


def _run_drift(args: argparse.Namespace) -> int:
    try:
        history = GitHistoryAnalyzer(args.repository).analyze()
    except GitAnalyzerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        code = PythonCodeAnalyzer(args.repository).analyze()
    except CodeAnalyzerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    report = IntentDriftDetector().detect(history.commits, code)
    print(f"Repository: {report.repository_name}")
    print(f"Comparisons: {len(report.results)}")
    print(f"Drift detected: {len(report.drifted)}")
    print(f"Significant (medium/high): {len(report.significant)}")
    print()

    for index, result in enumerate(report.results, start=1):
        if result.is_drift:
            print("DRIFT DETECTED")
        else:
            print("NO SIGNIFICANT DRIFT")
        print(
            f"Original commit: "
            f"{result.original_commit_message.splitlines()[0]}"
        )
        print(f"Later commit: {result.later_commit_message.splitlines()[0]}")
        print()
        print("Original intent:")
        print(f"action = {result.original_intent.action}")
        print(f"category = {result.original_intent.category}")
        print(f"object = {result.original_intent.object}")
        print()
        print("Later implementation:")
        for line in result.implementation:
            print(f"- {line}")
        print()
        print("Severity:")
        print(result.severity.upper())
        print()
        print("Reason:")
        print(result.reason)
        if args.verbose:
            print()
            print("Evidence:")
            for line in result.evidence:
                print(f"- {line}")
            print()
            print(f"Confidence: {result.confidence:.2f}")
        if index < len(report.results):
            print()
    return 0


def _run_risk(args: argparse.Namespace) -> int:
    try:
        history = GitHistoryAnalyzer(args.repository).analyze()
    except GitAnalyzerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        code = PythonCodeAnalyzer(args.repository).analyze()
    except CodeAnalyzerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    drift = IntentDriftDetector().detect(history.commits, code)
    risk = calculate_risk_report(drift)
    rows_by_hash = {r.original_commit_hash: r for r in drift.results}

    print(f"Repository: {drift.repository_name}")
    print(f"Comparisons: {len(drift.results)}")
    print(f"Findings: {risk.total_findings}")
    print(f"High-risk findings: {risk.high_risk_findings}")
    print(f"Critical findings: {risk.critical_findings}")
    print(f"Overall risk: {risk.overall_risk}")
    print()

    for index, finding in enumerate(risk.findings, start=1):
        row = rows_by_hash.get(finding.source_commit)
        print("RISK SCORE:", f"{finding.score}/100")
        print("SEVERITY:", finding.severity)
        if row is not None:
            print(
                "Original commit:",
                row.original_commit_message.splitlines()[0],
            )
            print(
                "Detected commit:",
                row.later_commit_message.splitlines()[0],
            )
        print(f"Affected area: {finding.affected_area}")
        print(f"Confidence: {finding.confidence:.2f}")
        print()
        print("Factors:")
        for risk_factor in finding.factors:
            print(f"{risk_factor}")
        print()
        print(f"Raw total: {finding.raw_score}")
        print(f"Final score: {finding.score}")
        print()
        print("Explanation:")
        print(f'"{finding.explanation}"')
        if args.verbose:
            print()
            print(f"Drift type: {finding.drift_type}")
            print(f"Source commit: {finding.source_commit}")
            print(f"Detected commit: {finding.detected_commit}")
        if index < len(risk.findings):
            print()

    print()
    summary = risk.to_dict()
    print("Repository summary:")
    if args.verbose:
        print(json.dumps(summary, indent=2))
    else:
        print(
            json.dumps(
                {k: v for k, v in summary.items() if k != "findings"},
                indent=2,
            )
        )
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # Backward compatibility: `demo.py <path>` still runs the git command.
    if not argv or (argv[0] not in COMMANDS and argv[0] not in ("-h", "--help")):
        argv.insert(0, "git")

    parser = argparse.ArgumentParser(
        description=(
            "GhostCode development demos "
            "(git / code / intent / drift / risk)."
        )
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

    drift_parser = subparsers.add_parser(
        "drift",
        help="Compare intents across commits against the current code.",
    )
    drift_parser.add_argument(
        "repository",
        nargs="?",
        default=".",
        help="Path to a local Git repository (default: current directory)",
    )
    drift_parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Show full comparison evidence and confidence per result",
    )

    risk_parser = subparsers.add_parser(
        "risk",
        help="Score the engineering-impact risk of detected intent drift.",
    )
    risk_parser.add_argument(
        "repository",
        nargs="?",
        default=".",
        help="Path to a local Git repository (default: current directory)",
    )
    risk_parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Show commit hashes and the full JSON summary with findings",
    )

    args = parser.parse_args(argv)
    if args.command == "code":
        return _run_code(args)
    if args.command == "intent":
        return _run_intent(args)
    if args.command == "drift":
        return _run_drift(args)
    if args.command == "risk":
        return _run_risk(args)
    return _run_git(args)


if __name__ == "__main__":
    raise SystemExit(main())
