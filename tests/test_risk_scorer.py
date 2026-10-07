"""Tests for GhostCode's deterministic risk scoring layer.

Synthetic drift results are built directly from the public data models so
every scoring factor can be isolated and asserted (factor presence,
ordering, score ranges, severity, relative risk). The PayGuard integration
tests validate the three intentional scenarios of the controlled,
read-only test repository (skipped when it is absent) with semantic
assertions only — never hard-coded repository hashes or exact scores that
the formula does not itself require.

The PayGuard commit messages appear only here as validation cases, never
in production logic.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional, Sequence, Tuple

import pytest

from ghostcode import risk_scorer
from ghostcode.code_analyzer import PythonCodeAnalyzer
from ghostcode.drift_detector import DriftReport, DriftResult, IntentDriftDetector
from ghostcode.git_analyzer import GitHistoryAnalyzer
from ghostcode.intent_extractor import CATEGORIES, Intent
from ghostcode.risk_scorer import (
    AREA_IMPACT,
    RISK_SEVERITIES,
    RiskFactor,
    RiskReport,
    RiskScore,
    calculate_risk,
    calculate_risk_report,
    resolve_severity,
)

#: Controlled, read-only test repository (see README.md).
PAYGUARD_PATH = Path.home() / "Projects" / "PayGuard"

SOURCE_HASH = "5" * 40
DETECTED_HASH = "6" * 40


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_drift(
    *,
    drift_type: str = "behavior_partial",
    severity: str = "medium",
    confidence: float = 0.5,
    category: str = "cleanup",
    later_category: str = "cleanup",
    action: str = "refactor",
    later_action: str = "refactor",
    obj: str = "application structure",
    later_obj: str = "application structure",
    message: str = "Clean up application structure",
    later_message: str = "Clean up application structure",
    files: Sequence[str] = ("app.py",),
    rule: Optional[str] = None,
    implementation: Sequence[str] = ("related functions present: do_thing",),
    reason: Optional[str] = None,
) -> DriftResult:
    """A synthetic DriftResult whose *context defaults to zero*.

    Default category ``cleanup`` (impact 0), confidence 0.5 (no factor),
    no behavior-rule line (no evidence factor), one shared file and one
    cited function (no scope factor) — so each test opts in to exactly the
    factor it wants to observe.
    """
    if files:
        pairing = (
            "pairing: related later commit shares changed file(s): "
            + ", ".join(files)
        )
    else:
        pairing = "pairing: no related later commit; checked against current state"

    evidence = [
        pairing,
        f"original intent: action={action}, category={category}, object={obj}",
        f"later intent: action={later_action}, category={later_category}, "
        f"object={later_obj}",
        f"category continuity: {category} -> {later_category}",
        f"action continuity: {action} -> {later_action}",
    ]
    if rule:
        evidence.append(f"behavior rule: {rule}")

    original = Intent(
        commit_hash=SOURCE_HASH,
        commit_message=message,
        action=action,
        object=obj,
        scope=obj,
        category=category,
        keywords=[],
        confidence=1.0,
        evidence=[],
    )
    later = Intent(
        commit_hash=DETECTED_HASH,
        commit_message=later_message,
        action=later_action,
        object=later_obj,
        scope=later_obj,
        category=later_category,
        keywords=[],
        confidence=1.0,
        evidence=[],
    )
    return DriftResult(
        original_commit_hash=SOURCE_HASH,
        original_commit_message=message,
        later_commit_hash=DETECTED_HASH,
        later_commit_message=later_message,
        original_intent=original,
        later_intent=later,
        drift_type=drift_type,
        severity=severity,
        reason=reason
        or f"A previously established {category} behavior ({drift_type}) test fixture",
        evidence=evidence,
        implementation=list(implementation),
        confidence=confidence,
    )


def factor(score: RiskScore, code: str) -> Optional[RiskFactor]:
    """The factor with *code*, or None when it was not applied."""
    return next((f for f in score.factors if f.code == code), None)


# ---------------------------------------------------------------------------
# 1-2. No drift / preserved intent
# ---------------------------------------------------------------------------


def test_no_drift_scores_zero_and_never_looks_risky() -> None:
    """Even a payment-area, high-confidence, direct-evidence *preserved*
    row scores exactly 0: without drift no contextual factor applies."""
    drift = make_drift(
        drift_type="none",
        severity="none",
        category="payment",
        confidence=0.95,
        rule="prevention-guard",
        implementation=["guard call present: is_duplicate"],
    )

    score = calculate_risk(drift)

    assert score.score == 0
    assert score.raw_score == 0
    assert score.severity == "LOW"
    assert score.drift_type == "none"
    assert [f.code for f in score.factors] == ["baseline"]
    assert score.factors[0].points == 0
    assert factor(score, "impact_payment") is None
    assert factor(score, "confidence_high") is None
    assert factor(score, "evidence_direct") is None


def test_preserved_comparisons_are_not_reported_as_findings() -> None:
    """Preserved rows can be scored (0/LOW) but never enter the rollup."""
    preserved = make_drift(drift_type="none", severity="none")
    real = make_drift(category="validation")

    report = calculate_risk_report(
        DriftReport(repository_name="demo", results=[preserved, real])
    )

    assert calculate_risk(preserved).score == 0
    assert calculate_risk(preserved).severity == "LOW"
    assert report.total_findings == 1
    assert len(report.findings) == 1
    assert report.findings[0].source_commit == SOURCE_HASH


# ---------------------------------------------------------------------------
# 3-4. Baseline tiers (spec weights)
# ---------------------------------------------------------------------------


def test_partial_drift_baseline_is_35() -> None:
    """Weakened/partial drift starts at +35 (no context applied)."""
    score = calculate_risk(
        make_drift(
            drift_type="behavior_partial",
            severity="medium",
            category="cleanup",
            confidence=0.5,
        )
    )

    assert score.factors[0].code == "baseline"
    assert score.factors[0].points == 35
    assert score.raw_score == 35
    assert 20 <= score.score <= 49  # MEDIUM band
    assert score.severity == "MEDIUM"


def test_significant_high_drift_baseline_is_70() -> None:
    score = calculate_risk(
        make_drift(
            drift_type="behavior_lost",
            severity="high",
            category="cleanup",
            confidence=0.5,
        )
    )

    assert score.factors[0].points == 70
    assert score.raw_score == 70
    assert score.severity == "HIGH"


def test_lost_drift_not_classified_high_starts_at_60() -> None:
    """The plain 'lost drift' tier (60) applies when detection did not
    classify the loss as high; 'significant/high' (70) outranks it."""
    score = calculate_risk(
        make_drift(
            drift_type="behavior_lost",
            severity="medium",
            category="cleanup",
            confidence=0.5,
        )
    )

    assert score.factors[0].points == 60
    assert score.severity == "HIGH"


def test_weakened_drift_uses_the_35_tier() -> None:
    score = calculate_risk(
        make_drift(drift_type="behavior_weakened", severity="low")
    )
    assert score.factors[0].points == 35


# ---------------------------------------------------------------------------
# 5-8. Impact area factors
# ---------------------------------------------------------------------------


def test_authentication_drift_applies_ten_points() -> None:
    score = calculate_risk(make_drift(category="authentication"))

    assert score.affected_area == "authentication"
    impact = factor(score, "impact_authentication")
    assert impact is not None and impact.points == 10
    assert impact.label == "authentication impact"


def test_payment_drift_applies_twenty_points() -> None:
    score = calculate_risk(make_drift(category="payment"))

    assert score.affected_area == "payment"
    impact = factor(score, "impact_payment")
    assert impact is not None and impact.points == 20
    assert impact.label == "payment/transaction impact"


def test_logging_drift_applies_ten_points() -> None:
    score = calculate_risk(make_drift(category="logging"))

    assert score.affected_area == "logging"
    impact = factor(score, "impact_logging")
    assert impact is not None and impact.points == 10
    assert impact.label == "logging/audit impact"


def test_validation_drift_applies_five_points() -> None:
    score = calculate_risk(make_drift(category="validation"))

    assert score.affected_area == "validation"
    impact = factor(score, "impact_validation")
    assert impact is not None and impact.points == 5


def test_security_and_access_control_apply_twenty_points() -> None:
    for category, code in (
        ("security", "impact_security"),
        ("authorization", "impact_authorization"),
    ):
        score = calculate_risk(make_drift(category=category))
        impact = factor(score, code)
        assert impact is not None and impact.points == 20
        assert score.affected_area == category


def test_general_areas_add_no_impact_factor() -> None:
    """cleanup/refactor/general stay at +0 — and the area is still named."""
    for category in ("refactoring", "cleanup", "feature"):
        score = calculate_risk(make_drift(category=category))
        assert score.affected_area == category
        assert not any(f.code.startswith("impact_") for f in score.factors)


def test_area_impact_covers_every_controlled_category() -> None:
    """Every intent category has an explicit, spec-conformant weight."""
    assert set(CATEGORIES) <= set(AREA_IMPACT)
    assert AREA_IMPACT["validation"] == 5
    assert AREA_IMPACT["authentication"] == 10
    assert AREA_IMPACT["logging"] == 10
    assert AREA_IMPACT["payment"] == 20
    assert AREA_IMPACT["security"] == 20
    assert AREA_IMPACT["authorization"] == 20
    assert AREA_IMPACT["refactoring"] == 0
    assert AREA_IMPACT["cleanup"] == 0
    assert AREA_IMPACT["unknown"] == 0


# ---------------------------------------------------------------------------
# 9-10. Scope factors
# ---------------------------------------------------------------------------


def test_single_file_single_function_scope_adds_nothing() -> None:
    score = calculate_risk(
        make_drift(
            files=("payments.py",),
            implementation=["related functions remain: process_payment"],
        )
    )
    assert factor(score, "scope_files") is None
    assert factor(score, "scope_functions") is None


def test_multiple_shared_files_add_ten_points() -> None:
    score = calculate_risk(make_drift(files=("a.py", "b.py")))

    scope = factor(score, "scope_files")
    assert scope is not None and scope.points == 10
    assert factor(score, "scope_functions") is None  # files outrank functions


def test_multiple_cited_functions_add_five_points() -> None:
    score = calculate_risk(
        make_drift(
            files=("auth.py",),
            implementation=[
                "rule: logging coverage over 2 function(s)",
                "authenticate_password -> logging present (reaches 'record')",
                "authenticate_token -> logging absent",
            ],
        )
    )

    scope = factor(score, "scope_functions")
    assert scope is not None and scope.points == 5
    assert factor(score, "scope_files") is None


def test_state_attributes_and_modules_are_not_counted_as_functions() -> None:
    """Scope counts only lines that name functions, not state/modules."""
    score = calculate_risk(
        make_drift(
            files=("payments.py",),
            implementation=[
                "state written but never consulted: processed_transaction_ids",
                "called from module(s): app, audit",
            ],
        )
    )
    assert factor(score, "scope_functions") is None


# ---------------------------------------------------------------------------
# 11-12. Confidence adjustment
# ---------------------------------------------------------------------------


def test_high_confidence_adds_ten_points() -> None:
    for confidence in (0.90, 0.95, 1.0):
        score = calculate_risk(make_drift(confidence=confidence))
        hit = factor(score, "confidence_high")
        assert hit is not None and hit.points == 10
        assert factor(score, "confidence_moderate") is None


def test_moderate_confidence_adds_five_points() -> None:
    for confidence in (0.75, 0.89):
        score = calculate_risk(make_drift(confidence=confidence))
        hit = factor(score, "confidence_moderate")
        assert hit is not None and hit.points == 5
        assert factor(score, "confidence_high") is None


def test_low_confidence_adds_nothing() -> None:
    score = calculate_risk(make_drift(confidence=0.7))
    assert factor(score, "confidence_high") is None
    assert factor(score, "confidence_moderate") is None


# ---------------------------------------------------------------------------
# 13. Evidence strength
# ---------------------------------------------------------------------------


def test_direct_ast_evidence_adds_ten_points() -> None:
    for rule in (
        "logging-coverage",
        "validation-presence",
        "prevention-guard",
        "file-presence",
    ):
        score = calculate_risk(make_drift(rule=rule))
        hit = factor(score, "evidence_direct")
        assert hit is not None and hit.points == 10


def test_indirect_or_unknown_evidence_adds_nothing() -> None:
    for rule in ("generic-presence", None):
        score = calculate_risk(make_drift(rule=rule))
        assert factor(score, "evidence_direct") is None


# ---------------------------------------------------------------------------
# 14-16. Cap, severity mapping, serialization
# ---------------------------------------------------------------------------


def test_score_is_capped_at_100_with_raw_total_preserved() -> None:
    """70 + 20 + 10 (files) + 10 (confidence) + 10 (evidence) = 120."""
    score = calculate_risk(
        make_drift(
            drift_type="behavior_lost",
            severity="high",
            category="payment",
            files=("payments.py", "ledger.py"),
            confidence=0.95,
            rule="prevention-guard",
            implementation=[
                "related functions remain: process_payment, check_ledger"
            ],
        )
    )

    assert score.raw_score == 120
    assert score.score == 100
    assert score.is_capped
    assert sum(f.points for f in score.factors) == score.raw_score


@pytest.mark.parametrize(
    "score,expected",
    [
        (0, "LOW"),
        (19, "LOW"),
        (20, "MEDIUM"),
        (49, "MEDIUM"),
        (50, "HIGH"),
        (74, "HIGH"),
        (75, "CRITICAL"),
        (100, "CRITICAL"),
    ],
)
def test_severity_mapping_boundaries(score: int, expected: str) -> None:
    assert resolve_severity(score) == expected
    assert expected in RISK_SEVERITIES


def test_severity_floor_protects_sensitive_areas() -> None:
    """A high drift in a payment/security/access-control area never maps
    below HIGH — the floor only raises, never lowers."""
    assert resolve_severity(10, "high", "payment") == "HIGH"
    assert resolve_severity(10, "high", "security") == "HIGH"
    assert resolve_severity(10, "high", "authorization") == "HIGH"
    # Not a sensitive area -> plain range mapping applies.
    assert resolve_severity(10, "high", "validation") == "LOW"
    # Floor requires a *high* drift severity.
    assert resolve_severity(10, "medium", "payment") == "LOW"
    # The numeric ranges always win when they are higher.
    assert resolve_severity(60, "high", "validation") == "HIGH"


def test_json_serialization_roundtrips() -> None:
    score = calculate_risk(
        make_drift(
            drift_type="behavior_lost",
            severity="high",
            category="payment",
            confidence=0.95,
            rule="prevention-guard",
        )
    )
    report = calculate_risk_report(
        DriftReport(repository_name="demo", results=[score_source_drift(score)])
    )

    score_data = score.to_dict()
    assert json.loads(json.dumps(score_data)) == score_data
    assert set(score_data) == {
        "score",
        "severity",
        "raw_score",
        "factors",
        "explanation",
        "affected_area",
        "confidence",
        "drift_type",
        "source_commit",
        "detected_commit",
    }

    report_data = report.to_dict()
    assert json.loads(json.dumps(report_data)) == report_data
    assert set(report_data) == {
        "total_findings",
        "high_risk_findings",
        "critical_findings",
        "overall_risk",
        "findings",
    }


def score_source_drift(score: RiskScore) -> DriftResult:
    """Rebuild the drift row a score came from (same hashes/drift type)."""
    return make_drift(
        drift_type=score.drift_type,
        severity="high" if score.drift_type == "behavior_lost" else "medium",
        category=score.affected_area if score.affected_area != "general" else "cleanup",
        confidence=score.confidence,
    )


# ---------------------------------------------------------------------------
# Report rollup, explanations, invariants
# ---------------------------------------------------------------------------


def test_report_counts_and_overall_risk() -> None:
    preserved = make_drift(drift_type="none", severity="none")
    medium = make_drift(category="validation")  # 35 + 5 = 40
    critical = make_drift(
        drift_type="behavior_lost",
        severity="high",
        category="payment",
        files=("a.py", "b.py"),
        confidence=0.95,
        rule="prevention-guard",
    )

    report = calculate_risk_report(
        DriftReport(
            repository_name="demo",
            results=[preserved, medium, critical],
        )
    )

    assert report.total_findings == 2
    assert report.critical_findings == 1
    # high_risk_findings counts HIGH *or above*: only the critical finding
    # qualifies here (the other is MEDIUM).
    assert report.high_risk_findings == 1
    assert report.overall_risk == max(f.score for f in report.findings)
    assert report.overall_risk == 100
    assert [f.drift_type for f in report.findings] == [
        "behavior_partial",
        "behavior_lost",
    ]


def test_empty_report_rolls_up_to_zero() -> None:
    report = calculate_risk_report(
        DriftReport(repository_name="empty", results=[])
    )
    assert report.total_findings == 0
    assert report.overall_risk == 0
    assert report.findings == []
    assert report.to_dict()["findings"] == []


def test_explanation_names_area_commit_rule_and_total() -> None:
    score = calculate_risk(
        make_drift(
            drift_type="behavior_lost",
            severity="high",
            category="payment",
            later_message="Refactor payment processing",
            confidence=0.95,
            rule="prevention-guard",
        )
    )

    assert score.explanation
    assert "payment/transaction processing" in score.explanation
    assert "Refactor payment processing" in score.explanation
    assert "prevention-guard" in score.explanation
    assert "direct" in score.explanation
    assert "total" in score.explanation


def test_explanation_for_preserved_row_states_zero() -> None:
    score = calculate_risk(make_drift(drift_type="none", severity="none"))
    assert "No risk factors are applied" in score.explanation
    assert "risk score is 0" in score.explanation


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"drift_type": "behavior_lost", "severity": "high", "category": "payment"},
        {"category": "validation", "confidence": 0.8},
        {"category": "authentication", "files": ("a.py", "b.py")},
        {"drift_type": "none", "severity": "none"},
        {"category": "logging", "confidence": 0.95, "rule": "logging-coverage"},
    ],
)
def test_factor_sum_invariant(kwargs: dict) -> None:
    score = calculate_risk(make_drift(**kwargs))
    assert sum(f.points for f in score.factors) == score.raw_score
    assert score.score == min(100, score.raw_score)
    assert score.severity in RISK_SEVERITIES
    assert score.score <= 100 and score.score >= 0
    assert score.affected_area


def test_public_api_surface() -> None:
    """calculate_risk / calculate_risk_report / resolve_severity are the
    documented entry points (also re-exported from the package root)."""
    assert callable(risk_scorer.calculate_risk)
    assert callable(risk_scorer.calculate_risk_report)
    assert callable(risk_scorer.resolve_severity)

    import ghostcode

    assert ghostcode.calculate_risk is calculate_risk
    assert ghostcode.calculate_risk_report is calculate_risk_report
    assert ghostcode.RiskScore is RiskScore
    assert ghostcode.RiskReport is RiskReport


# ---------------------------------------------------------------------------
# PayGuard integration: the three intentional scenarios
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def payguard_reports() -> Tuple[DriftReport, RiskReport]:
    if not PAYGUARD_PATH.exists():
        pytest.skip("PayGuard test repository not found at ~/Projects/PayGuard")
    history = GitHistoryAnalyzer(PAYGUARD_PATH).analyze()
    code = PythonCodeAnalyzer(PAYGUARD_PATH).analyze()
    drift = IntentDriftDetector().detect(history.commits, code)
    return drift, calculate_risk_report(drift)


def _finding_by_message(
    drift: DriftReport, risk: RiskReport, summary_prefix: str
) -> RiskScore:
    row = next(
        r
        for r in drift.results
        if r.original_commit_message.startswith(summary_prefix)
    )
    return next(f for f in risk.findings if f.source_commit == row.original_commit_hash)


def test_payguard_scenario_b_authentication_risk(
    payguard_reports: Tuple[DriftReport, RiskReport],
) -> None:
    """Lost token-path logging: at least MEDIUM, authentication factors,
    and strictly more risk than the preserved scenario."""
    drift, risk = payguard_reports
    finding = _finding_by_message(drift, risk, "Log every failed login")

    assert finding.severity in ("MEDIUM", "HIGH", "CRITICAL")  # at least MEDIUM
    assert finding.score >= 20
    assert finding.affected_area == "authentication"
    impact = factor(finding, "impact_authentication")
    assert impact is not None and impact.points == 10
    assert factor(finding, "confidence_high") is not None
    assert factor(finding, "evidence_direct") is not None


def test_payguard_scenario_c_payment_risk(
    payguard_reports: Tuple[DriftReport, RiskReport],
) -> None:
    """Lost duplicate-payment guard: highest risk, payment factor applied,
    HIGH or CRITICAL."""
    drift, risk = payguard_reports
    finding = _finding_by_message(drift, risk, "Prevent duplicate payment")

    assert finding.severity in ("HIGH", "CRITICAL")
    assert finding.score >= 50
    assert finding.affected_area == "payment"
    impact = factor(finding, "impact_payment")
    assert impact is not None and impact.points == 20
    assert factor(finding, "baseline") is not None
    # It is the worst finding in the repository.
    assert risk.overall_risk == finding.score


def test_payguard_scenario_c_outranks_scenario_b(
    payguard_reports: Tuple[DriftReport, RiskReport],
) -> None:
    drift, risk = payguard_reports
    scenario_b = _finding_by_message(drift, risk, "Log every failed login")
    scenario_c = _finding_by_message(drift, risk, "Prevent duplicate payment")

    assert scenario_c.score > scenario_b.score
    rank = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}
    assert rank[scenario_c.severity] > rank[scenario_b.severity]


def test_payguard_scenario_a_no_false_positive(
    payguard_reports: Tuple[DriftReport, RiskReport],
) -> None:
    """Preserved email validation: no validation finding, and preserved
    comparisons score 0/LOW."""
    drift, risk = payguard_reports

    # No finding anywhere for the preserved validation intent.
    assert not any(f.affected_area == "validation" for f in risk.findings)
    assert risk.total_findings == 2  # only the two intentional scenarios

    # The preserved comparison that *is* in the drift report scores 0/LOW.
    preserved_rows = [r for r in drift.results if r.drift_type == "none"]
    assert preserved_rows, "expected a preserved comparison row"
    for row in preserved_rows:
        score = calculate_risk(row)
        assert score.score == 0
        assert score.severity == "LOW"
        assert not any(f.points > 0 for f in score.factors)
