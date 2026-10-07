"""Tests for the GhostCode intent extractor.

Unit tests operate on raw commit messages (fully deterministic, no
repository needed). Integration tests read the controlled PayGuard test
repository through the Git History Analyzer (skipped when it is absent).

The PayGuard expectations validate *semantic* output (action/category),
never exact formatting or line numbers, and the seven known messages are
used only here as validation cases — never in production logic.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ghostcode.git_analyzer import CommitInfo, GitHistoryAnalyzer
from ghostcode.intent_extractor import (
    CATEGORIES,
    Intent,
    IntentExtractor,
    extract_intent,
)

#: Controlled, read-only test repository (see README.md).
PAYGUARD_PATH = Path.home() / "Projects" / "PayGuard"

#: Validation cases (test data only — not part of production logic).
PAYGUARD_CASES = [
    ("Initial PayGuard application", None, {"feature"}),
    ("Add email validation for user registration", "add", {"validation"}),
    ("Log every failed login attempt", "log", {"logging", "authentication"}),
    ("Prevent duplicate payment processing", "prevent", {"payment"}),
    ("Improve authentication flow", "improve", {"authentication"}),
    ("Refactor payment processing", "refactor", {"payment", "refactoring"}),
    ("Clean up application structure", "clean", {"cleanup", "refactoring"}),
]


# ---------------------------------------------------------------------------
# 1. Action normalization
# ---------------------------------------------------------------------------


def test_add_action_normalization() -> None:
    assert extract_intent("Add email validation").action == "add"
    assert extract_intent("Added email validation").action == "add"
    assert extract_intent("Adding email validation").action == "add"

    # Related normalizations from the spec.
    assert extract_intent("Improved authentication flow").action == "improve"
    assert extract_intent("Preventing crashes").action == "prevent"
    assert extract_intent("Refactored dead code").action == "refactor"
    assert extract_intent("Simplified config").action == "simplify"


# ---------------------------------------------------------------------------
# 2-8. Category extraction
# ---------------------------------------------------------------------------


def test_prevent_intent_extraction() -> None:
    intent = extract_intent("Prevent duplicate payment processing")
    assert intent.action == "prevent"
    assert intent.category == "payment"
    assert "payment" in intent.keywords


def test_logging_intent_extraction() -> None:
    intent = extract_intent("Log every failed login attempt")
    assert intent.action == "log"
    # The spec accepts either logging or authentication for this message;
    # the deterministic tie-break picks authentication, so accept both.
    assert intent.category in {"logging", "authentication"}


def test_validation_intent_extraction() -> None:
    intent = extract_intent("Add email validation for user registration")
    assert intent.action == "add"
    assert intent.category == "validation"
    assert set(intent.keywords) >= {"email", "validation"}


def test_authentication_intent_extraction() -> None:
    intent = extract_intent("Improve authentication flow")
    assert intent.action == "improve"
    assert intent.category == "authentication"
    assert intent.keywords == ["authentication"]


def test_payment_intent_extraction() -> None:
    intent = extract_intent("Add settlement report")
    assert intent.action == "add"
    assert intent.category == "payment"
    assert intent.keywords == ["settlement"]


def test_refactoring_intent_extraction() -> None:
    intent = extract_intent("Refactor user service layer")
    assert intent.action == "refactor"
    assert intent.category == "refactoring"

    # Domain categories may win over the change-style one when they score
    # more keyword hits (accepted by the spec as payment/refactoring).
    mixed = extract_intent("Refactor payment processing")
    assert mixed.action == "refactor"
    assert mixed.category in {"payment", "refactoring"}


def test_cleanup_intent_extraction() -> None:
    intent = extract_intent("Clean up application structure")
    assert intent.action == "clean"
    assert intent.category in {"cleanup", "refactoring"}


def test_unknown_ambiguous_commit() -> None:
    intent = extract_intent("Tweak purple widgets")
    assert intent.category == "unknown"
    assert intent.action is None
    assert intent.keywords == []
    assert intent.confidence == 0.0
    # Conservative fallback: no invented object.
    assert intent.object == "Tweak purple widgets"
    assert intent.scope is None
    assert "no known action verb found" in intent.evidence
    assert (
        "no category keyword matched; default category: unknown"
        in intent.evidence
    )

    # The controlled vocabulary is never exceeded.
    assert intent.category in CATEGORIES


# ---------------------------------------------------------------------------
# 10-11. Object and scope extraction
# ---------------------------------------------------------------------------


def test_object_extraction() -> None:
    cases = {
        "Add email validation for user registration": "email validation",
        "Log every failed login attempt": "failed login attempt",
        "Prevent duplicate payment processing": "duplicate payment processing",
        "Refactor payment processing": "payment processing",
        "Clean up application structure": "application structure",
        "Improve authentication flow": "authentication flow",
    }
    for message, expected_object in cases.items():
        intent = extract_intent(message)
        assert intent.object == expected_object, message


def test_scope_extraction() -> None:
    # Explicit "for <phrase>" scope.
    explicit = extract_intent("Add email validation for user registration")
    assert explicit.scope == "user registration"

    # Implicit scope: the object phrase itself.
    implicit = extract_intent("Refactor payment processing")
    assert implicit.scope == "payment processing"
    assert implicit.scope == implicit.object

    # No identifiable scope.
    none = extract_intent("Tweak purple widgets")
    assert none.scope is None


# ---------------------------------------------------------------------------
# 12-13. Confidence behaviour and evidence
# ---------------------------------------------------------------------------


def test_confidence_behaviour() -> None:
    full = extract_intent("Add email validation for user registration")
    assert full.confidence == 1.0  # action + category + object + scope

    empty = extract_intent("Tweak purple widgets")
    assert empty.confidence == 0.0  # nothing matched

    partial = extract_intent("Add widgets")  # action + object + scope
    assert 0.0 < partial.confidence < 1.0
    assert partial.confidence > empty.confidence

    # Always a bounded, deterministic heuristic score (never a probability
    # claim): same message, same score, within [0, 1].
    for message, _, _ in PAYGUARD_CASES:
        first = extract_intent(message).confidence
        second = extract_intent(message).confidence
        assert first == second
        assert 0.0 <= first <= 1.0


def test_evidence_generation() -> None:
    intent = extract_intent("Prevent duplicate payment processing")
    assert "action keyword: prevent" in intent.evidence
    assert "category keyword: payment" in intent.evidence
    assert any(line.startswith("scope phrase: ") for line in intent.evidence)
    assert any(line.startswith("object phrase: ") for line in intent.evidence)

    # Ties are explained too.
    tied = extract_intent("Log every failed login attempt")
    assert any(
        line.startswith("category scores: ") for line in tied.evidence
    )
    assert "category scores: authentication=1, logging=1" in tied.evidence

    # Evidence is ordered, human-readable and non-empty for every case.
    for message, _, _ in PAYGUARD_CASES:
        assert extract_intent(message).evidence


# ---------------------------------------------------------------------------
# 14. Serialization
# ---------------------------------------------------------------------------


def test_serialization_to_dict() -> None:
    intent = extract_intent("Prevent duplicate payment processing",
                            commit_hash="abc1234")
    data = intent.to_dict()

    assert isinstance(data, dict)
    assert set(data) == {
        "commit_hash",
        "commit_message",
        "action",
        "object",
        "scope",
        "category",
        "keywords",
        "confidence",
        "evidence",
    }
    assert data["commit_hash"] == "abc1234"
    assert data["action"] == "prevent"
    assert isinstance(data["keywords"], list)
    assert all(isinstance(k, str) for k in data["keywords"])
    assert isinstance(data["confidence"], float)
    # JSON-ready plain types.
    assert json.loads(json.dumps(data)) == data

    # Models round-trip consistently.
    assert isinstance(intent, Intent)
    assert intent.to_dict() == data


def test_extract_from_commit_info() -> None:
    """The primary input path: a CommitInfo from the Git analyzer."""
    commit = CommitInfo(
        hash="e253148052fbf7e2e6a31b834888dba499afc5a8",
        short_hash="e253148",
        message="Prevent duplicate payment processing",
        summary="Prevent duplicate payment processing",
        author="tester",
        author_email="tester@example.com",
        authored_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        committed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        changed_files=["payments.py"],
        insertions=10,
        deletions=2,
    )
    intent = IntentExtractor().extract(commit)
    assert intent.commit_hash == commit.hash
    assert intent.commit_message == commit.message
    assert intent.action == "prevent"
    assert intent.category == "payment"

    # extract_all preserves order and count.
    intents = IntentExtractor().extract_all([commit, "Add widgets"])
    assert len(intents) == 2
    assert intents[0].commit_hash == commit.hash
    assert intents[1].commit_hash is None


# ---------------------------------------------------------------------------
# 15. The seven PayGuard commit messages (semantic checks)
# ---------------------------------------------------------------------------


def test_payguard_commit_messages_produce_meaningful_intents() -> None:
    extractor = IntentExtractor()
    for message, expected_action, expected_categories in PAYGUARD_CASES:
        intent = extractor.extract_from_message(message)

        assert intent.commit_message == message
        if expected_action is None:
            assert intent.action is None
        else:
            assert intent.action == expected_action, message
        assert intent.category in expected_categories, message
        assert intent.category in CATEGORIES
        assert 0.0 <= intent.confidence <= 1.0
        assert intent.evidence
        # Object is never fabricated: it is a substring of the message.
        assert intent.object in message


# ---------------------------------------------------------------------------
# Integration: PayGuard via the Git History Analyzer
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not PAYGUARD_PATH.exists(),
    reason="PayGuard test repository not found at ~/Projects/PayGuard",
)
def test_payguard_repository_intent_extraction() -> None:
    history = GitHistoryAnalyzer(PAYGUARD_PATH).analyze()
    intents = IntentExtractor().extract_all(history.commits)

    assert len(intents) == history.total_commits >= 7
    assert all(i.evidence for i in intents)
    assert all(0.0 <= i.confidence <= 1.0 for i in intents)
    assert all(i.category in CATEGORIES for i in intents)

    # Match the known validation cases by their subject line.
    by_subject = {
        i.commit_message.splitlines()[0].strip(): i for i in intents
    }
    for message, expected_action, expected_categories in PAYGUARD_CASES:
        assert message in by_subject, message
        intent = by_subject[message]
        if expected_action is not None:
            assert intent.action == expected_action, message
        assert intent.category in expected_categories, message
