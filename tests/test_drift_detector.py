"""Tests for the GhostCode intent drift detector.

Synthetic scenarios are built from small temporary repositories (real
``PythonCodeAnalyzer`` runs over real files) plus hand-made ``CommitInfo``
objects, so every rule is exercised deterministically without any external
repository.

The PayGuard integration test validates the three intentional scenarios of
the controlled, read-only test repository (skipped when it is absent) using
semantic assertions only — never hard-coded hashes, line numbers or full
output strings.

The seven known PayGuard commit messages are used only here as validation
cases, never in production logic.
"""

from __future__ import annotations

import json
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

import pytest

from ghostcode.code_analyzer import PythonCodeAnalyzer, RepositoryCodeAnalysis
from ghostcode.drift_detector import (
    DRIFT_TYPES,
    SEVERITIES,
    BehaviorCheck,
    DriftReport,
    DriftResult,
    IntentDriftDetector,
)
from ghostcode.git_analyzer import CommitInfo, GitHistoryAnalyzer
from ghostcode.intent_extractor import extract_intent

#: Controlled, read-only test repository (see README.md).
PAYGUARD_PATH = Path.home() / "Projects" / "PayGuard"

# ---------------------------------------------------------------------------
# Synthetic repositories (flush-left so the sources stay readable)
# ---------------------------------------------------------------------------

VALIDATORS_PY = """\
def validate_email(email):
    return "@" in email


def register(email):
    if not validate_email(email):
        raise ValueError("invalid email")
"""

AUTH_PY = """\
def audit(event):
    pass


def _fail(username):
    audit("login_failed")


def authenticate_password(username, password):
    if password != "secret":
        _fail(username)
        return None
    return username


def authenticate_token(username, token):
    if not token:
        return None
    return username
"""

PAYMENTS_BROKEN_PY = """\
class PaymentProcessor:
    def __init__(self):
        self.processed_transactions = set()

    def process_payment(self, payment):
        self.processed_transactions.add(payment.txn_id)
        return True
"""

PAYMENTS_HEALTHY_PY = """\
class PaymentProcessor:
    def __init__(self):
        self.processed_transactions = set()

    def is_duplicate(self, txn_id):
        return txn_id in self.processed_transactions

    def process_payment(self, payment):
        if self.is_duplicate(payment.txn_id):
            return False
        self.processed_transactions.add(payment.txn_id)
        return True
"""

SCAFFOLD_PY = """\
def scaffold():
    return None
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_commit(
    hash_: str, message: str, changed: List[str], day: int
) -> CommitInfo:
    """A CommitInfo with a distinct chronological timestamp per ``day``."""
    when = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=day)
    return CommitInfo(
        hash=hash_,
        short_hash=hash_[:7],
        message=message,
        summary=message.splitlines()[0],
        author="tester",
        author_email="tester@example.com",
        authored_at=when,
        committed_at=when,
        changed_files=list(changed),
        insertions=1,
        deletions=0,
    )


def write_repo(root: Path, files: Dict[str, str]) -> Path:
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return root


def analyze(root: Path) -> RepositoryCodeAnalysis:
    return PythonCodeAnalyzer(root).analyze()


# ---------------------------------------------------------------------------
# 1-2. No drift / preserved intent
# ---------------------------------------------------------------------------


def test_preserved_intent_reports_no_drift(tmp_path: Path) -> None:
    """A related later commit that keeps the behavior -> severity ``none``."""
    write_repo(tmp_path, {"validators.py": VALIDATORS_PY})
    code = analyze(tmp_path)
    commits = [
        make_commit(
            "a" * 40,
            "Add email validation for user registration",
            ["validators.py"],
            day=1,
        ),
        make_commit(
            "b" * 40,
            "Clean up application structure",
            ["validators.py"],
            day=2,
        ),
    ]

    report = IntentDriftDetector().detect(commits, code)

    assert len(report.results) == 1
    result = report.results[0]
    assert result.original_commit_hash == commits[0].hash
    assert result.later_commit_hash == commits[1].hash
    assert result.drift_type == "none"
    assert result.severity == "none"
    assert not report.drifted
    assert result.reason
    assert result.evidence
    assert 0.0 <= result.confidence <= 1.0


def test_preserved_behavior_check_is_present(tmp_path: Path) -> None:
    """The structural check behind 'no drift' really sees the behavior."""
    write_repo(tmp_path, {"validators.py": VALIDATORS_PY})
    code = analyze(tmp_path)
    commit = make_commit(
        "a" * 40,
        "Add email validation for user registration",
        ["validators.py"],
        day=1,
    )
    intent = extract_intent(commit.message, commit_hash=commit.hash)

    check = IntentDriftDetector().check_behavior(intent, commit, code)

    assert isinstance(check, BehaviorCheck)
    assert check.status == "present"
    assert check.rule == "validation-presence"
    assert any("validate_email" in line for line in check.evidence)
    # and the full comparison keeps it out of the drift list
    later = make_commit(
        "b" * 40, "Clean up application structure", ["validators.py"], day=2
    )
    report = IntentDriftDetector().detect([commit, later], code)
    assert report.drifted == []


# ---------------------------------------------------------------------------
# 3. Partial drift (medium)
# ---------------------------------------------------------------------------


def test_partial_drift_when_logging_loses_a_sibling_path(
    tmp_path: Path,
) -> None:
    """Password path logs, token path does not -> MEDIUM partial drift."""
    write_repo(tmp_path, {"auth.py": AUTH_PY})
    code = analyze(tmp_path)
    commits = [
        make_commit(
            "c" * 40, "Log every failed login attempt", ["auth.py"], day=1
        ),
        make_commit(
            "d" * 40, "Improve authentication flow", ["auth.py"], day=2
        ),
    ]

    report = IntentDriftDetector().detect(commits, code)

    assert len(report.results) == 1
    result = report.results[0]
    assert result.drift_type == "behavior_partial"
    assert result.severity == "medium"
    assert result.is_drift and result.is_significant
    # The later commit is the authentication refactor.
    assert result.later_commit_message.startswith("Improve authentication")
    # Per-function coverage explains the partial loss.
    joined = " ".join(result.implementation)
    assert "authenticate_password" in joined and "present" in joined
    assert "authenticate_token" in joined and "absent" in joined
    # Explainable: reason + evidence mention the refactoring context.
    assert "authentication" in result.reason
    assert "refactoring" in result.reason
    assert any(line.startswith("pairing: ") for line in result.evidence)
    assert any(
        "category continuity" in line for line in result.evidence
    )


# ---------------------------------------------------------------------------
# 4. Significant drift (high)
# ---------------------------------------------------------------------------


def test_significant_drift_when_duplicate_guard_disappears(
    tmp_path: Path,
) -> None:
    """Duplicate protection existed; refactor left write-only state
    -> HIGH / behavior_lost."""
    write_repo(tmp_path, {"payments.py": PAYMENTS_BROKEN_PY})
    code = analyze(tmp_path)
    commits = [
        make_commit(
            "e" * 40,
            "Prevent duplicate payment processing",
            ["payments.py"],
            day=1,
        ),
        make_commit(
            "f" * 40,
            "Refactor payment processing",
            ["payments.py"],
            day=2,
        ),
    ]

    report = IntentDriftDetector().detect(commits, code)

    assert len(report.results) == 1
    result = report.results[0]
    assert result.drift_type == "behavior_lost"
    assert result.severity == "high"
    assert result.is_significant
    assert result.original_intent.action == "prevent"
    assert result.later_intent.action == "refactor"
    joined = " ".join(result.implementation)
    assert "processed_transactions" in joined  # write-only state found
    assert "duplicate" in joined  # distinctive marker searched
    assert "no longer represented" in result.reason
    assert result.confidence > 0.5


def test_prevention_guard_present_counts_as_no_drift(
    tmp_path: Path,
) -> None:
    """The same intent with an intact guard stays drift-free."""
    write_repo(tmp_path, {"payments.py": PAYMENTS_HEALTHY_PY})
    code = analyze(tmp_path)
    commits = [
        make_commit(
            "e" * 40,
            "Prevent duplicate payment processing",
            ["payments.py"],
            day=1,
        ),
        make_commit(
            "f" * 40,
            "Refactor payment processing",
            ["payments.py"],
            day=2,
        ),
    ]

    report = IntentDriftDetector().detect(commits, code)

    assert len(report.results) == 1
    assert report.results[0].severity == "none"
    assert any(
        "is_duplicate" in line for line in report.results[0].implementation
    )


# ---------------------------------------------------------------------------
# 5. Unrelated commits
# ---------------------------------------------------------------------------


def test_unrelated_commits_produce_no_drift(tmp_path: Path) -> None:
    """A later commit from an unrelated area is never paired with the
    original, and the preserved behavior produces no rows."""
    write_repo(tmp_path, {"payments.py": PAYMENTS_HEALTHY_PY})
    code = analyze(tmp_path)
    commits = [
        make_commit(
            "g" * 40,
            "Prevent duplicate payment processing",
            ["payments.py"],
            day=1,
        ),
        make_commit(
            "h" * 40,
            "Update project documentation",
            ["docs.md"],
            day=2,
        ),
    ]

    report = IntentDriftDetector().detect(commits, code)

    assert report.results == []
    assert report.drifted == []


def test_unrelated_commits_report_lost_behavior(tmp_path: Path) -> None:
    """Same unrelated pairing, but the behavior IS gone: the safety net
    reports the loss against the current state."""
    write_repo(tmp_path, {"payments.py": PAYMENTS_BROKEN_PY})
    code = analyze(tmp_path)
    commits = [
        make_commit(
            "g" * 40,
            "Prevent duplicate payment processing",
            ["payments.py"],
            day=1,
        ),
        make_commit(
            "h" * 40,
            "Update project documentation",
            ["docs.md"],
            day=2,
        ),
    ]

    report = IntentDriftDetector().detect(commits, code)

    # The original is checked against the current state even though no
    # later commit touches payments.py.
    assert len(report.results) == 1
    result = report.results[0]
    assert result.severity == "high"
    assert any("current state" in line for line in result.evidence)
    assert result.later_commit_hash == commits[1].hash


# ---------------------------------------------------------------------------
# 6. Multiple intents
# ---------------------------------------------------------------------------


def test_multiple_intents_are_each_compared(tmp_path: Path) -> None:
    """Two independent originals each produce their own comparison."""
    write_repo(
        tmp_path, {"validators.py": VALIDATORS_PY, "auth.py": AUTH_PY}
    )
    code = analyze(tmp_path)
    commits = [
        make_commit(
            "1" * 40,
            "Add email validation for user registration",
            ["validators.py"],
            day=1,
        ),
        make_commit(
            "2" * 40, "Log every failed login attempt", ["auth.py"], day=2
        ),
        make_commit(
            "3" * 40, "Improve authentication flow", ["auth.py"], day=3
        ),
        make_commit(
            "4" * 40,
            "Clean up application structure",
            ["validators.py", "auth.py"],
            day=4,
        ),
    ]

    report = IntentDriftDetector().detect(commits, code)

    originals = {r.original_commit_hash for r in report.results}
    assert len(report.results) >= 2
    assert {commits[0].hash, commits[1].hash} <= originals
    categories = {r.original_intent.category for r in report.results}
    assert {"validation", "authentication"} <= categories
    # The logging partial drift is still found among the others.
    assert any(r.severity == "medium" for r in report.results)
    # Results stay in chronological order of their original commit.
    days = [r.original_commit_hash for r in report.results]
    assert days == sorted(days, key=lambda h: [c.hash for c in commits].index(h))


# ---------------------------------------------------------------------------
# 7. Low-confidence / unknown intents
# ---------------------------------------------------------------------------


def test_low_confidence_intents_are_skipped(tmp_path: Path) -> None:
    """An original below the confidence threshold never anchors drift."""
    write_repo(tmp_path, {"app.py": SCAFFOLD_PY})
    code = analyze(tmp_path)
    weak = make_commit(
        "9" * 40, "Initial application scaffold", ["app.py"], day=1
    )
    strong = make_commit(
        "8" * 40, "Refactor application structure", ["app.py"], day=2
    )

    # The weak message really does extract below the threshold.
    weak_intent = extract_intent(weak.message, commit_hash=weak.hash)
    assert weak_intent.confidence < IntentDriftDetector().min_intent_confidence

    report = IntentDriftDetector().detect([weak, strong], code)

    # Weak original skipped; the strong one is last, so nothing is reported.
    assert report.results == []


def test_unknown_category_intent_is_skipped(tmp_path: Path) -> None:
    """A category-less (unknown) intent with low confidence is skipped."""
    write_repo(tmp_path, {"app.py": SCAFFOLD_PY})
    code = analyze(tmp_path)
    commits = [
        make_commit("5" * 40, "Tweak purple widgets", ["app.py"], day=1),
        make_commit("6" * 40, "Refactor application structure", ["app.py"], day=2),
    ]
    intent = extract_intent(commits[0].message, commit_hash=commits[0].hash)
    assert intent.category == "unknown"
    assert intent.confidence == 0.0

    report = IntentDriftDetector().detect(commits, code)
    assert report.results == []


# ---------------------------------------------------------------------------
# 8. Chronological comparison
# ---------------------------------------------------------------------------


def test_comparison_is_chronological_not_input_ordered(
    tmp_path: Path,
) -> None:
    """Commits fed in reverse order still pair original -> later correctly."""
    write_repo(tmp_path, {"payments.py": PAYMENTS_BROKEN_PY})
    code = analyze(tmp_path)
    original = make_commit(
        "c1" + "0" * 38,
        "Prevent duplicate payment processing",
        ["payments.py"],
        day=1,
    )
    later = make_commit(
        "c2" + "0" * 38,
        "Refactor payment processing",
        ["payments.py"],
        day=2,
    )

    report = IntentDriftDetector().detect([later, original], code)

    assert len(report.results) == 1
    result = report.results[0]
    assert result.original_commit_hash == original.hash
    assert result.later_commit_hash == later.hash
    assert result.severity == "high"


# ---------------------------------------------------------------------------
# Data model / API extras
# ---------------------------------------------------------------------------


def test_serialization_to_dict(tmp_path: Path) -> None:
    write_repo(tmp_path, {"auth.py": AUTH_PY})
    code = analyze(tmp_path)
    commits = [
        make_commit(
            "c" * 40, "Log every failed login attempt", ["auth.py"], day=1
        ),
        make_commit(
            "d" * 40, "Improve authentication flow", ["auth.py"], day=2
        ),
    ]
    report = IntentDriftDetector().detect(commits, code)

    data = report.to_dict()
    assert set(data) == {
        "repository_name",
        "results",
        "total_comparisons",
        "drifted",
        "significant",
    }
    assert json.loads(json.dumps(data)) == data  # JSON-ready plain types

    row = data["results"][0]
    assert set(row) == {
        "original_commit_hash",
        "original_commit_message",
        "later_commit_hash",
        "later_commit_message",
        "original_intent",
        "later_intent",
        "drift_type",
        "severity",
        "reason",
        "evidence",
        "implementation",
        "confidence",
    }
    assert row["original_intent"]["action"] == "log"
    assert row["drift_type"] in DRIFT_TYPES
    assert row["severity"] in SEVERITIES
    assert isinstance(report.results[0], DriftResult)
    assert isinstance(report, DriftReport)


def test_detect_accepts_preextracted_intents(tmp_path: Path) -> None:
    write_repo(tmp_path, {"auth.py": AUTH_PY})
    code = analyze(tmp_path)
    commits = [
        make_commit(
            "c" * 40, "Log every failed login attempt", ["auth.py"], day=1
        ),
        make_commit(
            "d" * 40, "Improve authentication flow", ["auth.py"], day=2
        ),
    ]
    intents = [
        extract_intent(c.message, commit_hash=c.hash) for c in commits
    ]

    detector = IntentDriftDetector()
    with_intents = detector.detect(commits, code, intents=intents)
    without = detector.detect(commits, code)

    assert (
        [(r.drift_type, r.severity) for r in with_intents.results]
        == [(r.drift_type, r.severity) for r in without.results]
    )


def test_detector_validates_configuration() -> None:
    with pytest.raises(ValueError):
        IntentDriftDetector(min_intent_confidence=1.5)
    with pytest.raises(ValueError):
        IntentDriftDetector(max_call_depth=0)


# ---------------------------------------------------------------------------
# PayGuard integration: the three intentional scenarios
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not PAYGUARD_PATH.exists(),
    reason="PayGuard test repository not found at ~/Projects/PayGuard",
)
def test_payguard_drift_scenarios() -> None:
    history = GitHistoryAnalyzer(PAYGUARD_PATH).analyze()
    code = PythonCodeAnalyzer(PAYGUARD_PATH).analyze()
    detector = IntentDriftDetector()
    report = detector.detect(history.commits, code)

    assert 7 >= history.total_commits
    assert all(r.reason for r in report.results)
    assert all(r.evidence for r in report.results)
    assert all(0.0 <= r.confidence <= 1.0 for r in report.results)
    assert all(r.drift_type in DRIFT_TYPES for r in report.results)
    assert all(r.severity in SEVERITIES for r in report.results)

    by_original = {
        r.original_commit_message.splitlines()[0]: r
        for r in report.results
    }

    # --- Scenario A: preserved intent — NO significant drift -------------
    validation_commit = next(
        c
        for c in history.commits
        if c.summary.startswith("Add email validation")
    )
    validation_intent = detector._extractor.extract(validation_commit)
    check = detector.check_behavior(validation_intent, validation_commit, code)
    assert check.status == "present"
    assert not [
        r
        for r in report.results
        if r.original_intent.category == "validation" and r.is_drift
    ]

    # --- Scenario B: MEDIUM drift — token path lost its logging ----------
    scenario_b = by_original["Log every failed login attempt"]
    assert (
        scenario_b.later_commit_message.splitlines()[0]
        == "Improve authentication flow"
    )
    assert scenario_b.drift_type == "behavior_partial"
    assert scenario_b.severity == "medium"
    joined_b = " ".join(scenario_b.implementation)
    assert "authenticate_password" in joined_b and "present" in joined_b
    assert "authenticate_token" in joined_b and "absent" in joined_b
    assert "partially lost" in scenario_b.reason

    # --- Scenario C: HIGH drift — duplicate guard no longer checked ------
    scenario_c = by_original["Prevent duplicate payment processing"]
    assert (
        scenario_c.later_commit_message.splitlines()[0]
        == "Refactor payment processing"
    )
    assert scenario_c.drift_type == "behavior_lost"
    assert scenario_c.severity == "high"
    joined_c = " ".join(scenario_c.implementation)
    assert "processed_transaction_ids" in joined_c
    assert "duplicate" in joined_c

    # Exactly the two intentional scenarios are significant.
    assert {r.original_commit_message.splitlines()[0] for r in report.significant} == {
        "Log every failed login attempt",
        "Prevent duplicate payment processing",
    }
