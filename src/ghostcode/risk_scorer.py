"""Deterministic, explainable risk scoring for GhostCode.

This module implements the fifth stage of the GhostCode pipeline: taking the
:class:`~ghostcode.drift_detector.DriftResult` rows produced by intent drift
detection and assigning each one an engineering-impact **risk score from
0 to 100**, together with a severity, the factors that produced the score
and a prose explanation.

Design rules (shared with every other GhostCode component):

* Deterministic — the same drift result always yields the same score.
* Explainable — every point is attributed to a named, inspectable factor;
  the factors always sum to the pre-cap total.
* Offline and LLM-free — arithmetic over data the previous stages already
  produced; no models, no network, no repository access.
* Explicitly **not a probability** — the weights below are a hand-written,
  documented heuristic describing *potential engineering impact*, not a
  statistically calibrated likelihood. Nothing here claims "80% likely to
  fail".

How scoring works (high level):

1. **Baseline** from the drift verdict (highest applicable tier wins):

   =============================================  =======
   no significant drift / preserved / benign      0
   weakened or partial drift                     35
   lost drift (not classified ``high``)          60
   significant/high drift                        70
   =============================================  =======

   When the baseline is 0 there is no drift, so **no contextual factor is
   applied at all** — the score is exactly 0 (never a false positive).

2. **Contextual factors** are then added (only for actual drift):

   * *Impact area* (from the original intent's category): cleanup/refactor/
     general ``+0``, validation ``+5``, authentication ``+10``,
     logging/audit ``+10``, payment/transaction ``+20``,
     security/access-control ``+20``.
   * *Scope*: multiple files ``+10``, otherwise multiple functions ``+5``,
     otherwise single function/file ``+0``.
   * *Confidence*: ``>= 0.90`` → ``+10``, ``>= 0.75`` → ``+5``, else ``+0``.
   * *Evidence strength*: a direct-AST rule (per-function coverage, named
     marker function, guard/state inspection, file presence) → ``+10``; the
     heuristic ``generic-presence`` rule (or unknown provenance) → ``+0``.

3. The raw total is **capped at 100**.

4. **Severity mapping**: ``0–19 LOW``, ``20–49 MEDIUM``, ``50–74 HIGH``,
   ``75–100 CRITICAL`` — plus an explicit **floor rule**: a drift whose own
   severity is ``high`` in a security/payment-sensitive area is never
   mapped below ``HIGH``, regardless of how few contextual factors applied.

Repository-level scoring rolls individual findings up into a
:class:`RiskReport` (finding counts and an ``overall_risk`` equal to the
worst finding — the highest-score finding drives repository risk).

Risk scoring does not decide what to *do* about a finding; presenting the
results (dashboard) is a later phase.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from .drift_detector import DriftReport, DriftResult

__all__ = [
    "RiskFactor",
    "RiskScore",
    "RiskReport",
    "calculate_risk",
    "calculate_risk_report",
    "resolve_severity",
    "RISK_SEVERITIES",
    "SENSITIVE_AREAS",
    "AREA_IMPACT",
    "DIRECT_EVIDENCE_RULES",
]

# ---------------------------------------------------------------------------
# Controlled vocabularies
# ---------------------------------------------------------------------------

#: Severity levels risk scores can map to (ordered least to most severe).
RISK_SEVERITIES: Tuple[str, ...] = ("LOW", "MEDIUM", "HIGH", "CRITICAL")

#: Areas where a lost behavior is security- or money-sensitive. Combined
#: with a ``high`` drift severity this triggers the severity floor rule.
SENSITIVE_AREAS = frozenset({"payment", "security", "authorization"})

#: Impact weight per intent category. Every category of
#: ``ghostcode.intent_extractor.CATEGORIES`` is listed explicitly — nothing
#: is guessed from a default. These are hand-written heuristic weights,
#: not learned probabilities.
AREA_IMPACT: Dict[str, int] = {
    "validation": 5,
    "authentication": 10,
    "logging": 10,
    "payment": 20,
    "security": 20,
    "authorization": 20,
    "refactoring": 0,
    "cleanup": 0,
    "feature": 0,
    "bug_fix": 0,
    "data_processing": 0,
    "testing": 0,
    "configuration": 0,
    "unknown": 0,
}

#: Human phrases used in explanations and factor labels.
_AREA_LABELS: Dict[str, str] = {
    "validation": "validation impact",
    "authentication": "authentication impact",
    "logging": "logging/audit impact",
    "payment": "payment/transaction impact",
    "security": "security impact",
    "authorization": "access-control impact",
}

_AREA_PHRASES: Dict[str, str] = {
    "validation": "input validation",
    "authentication": "authentication",
    "logging": "logging/audit",
    "payment": "payment/transaction processing",
    "security": "security",
    "authorization": "access control",
}

#: Behavior rules whose verdicts rest on concrete AST structure (which
#: function reaches a sink, whether a named marker function is called,
#: whether guard state is consulted, whether files exist). The remaining
#: rule (``generic-presence``, name matching across the repository — or an
#: unknown rule) counts as indirect/heuristic evidence.
DIRECT_EVIDENCE_RULES = frozenset(
    {"logging-coverage", "validation-presence", "prevention-guard", "file-presence"}
)

# Baseline tiers (highest applicable wins).
_BASELINE_NONE = (0, "no significant drift")
_BASELINE_PARTIAL = (35, "weakened or partial drift")
_BASELINE_LOST = (60, "lost drift")
_BASELINE_HIGH = (70, "significant/high drift")

# Contextual factor constants in (code, label, points) order.
_CONFIDENCE_HIGH = ("confidence_high", "high confidence", 10)
_CONFIDENCE_MODERATE = ("confidence_moderate", "moderate confidence", 5)
_EVIDENCE_DIRECT = ("evidence_direct", "direct AST evidence", 10)

_SCOPE_FILES = ("scope_files", "multiple files affected", 10)
_SCOPE_FUNCTIONS = ("scope_functions", "multiple functions affected", 5)

#: Where a drift's implementation evidence names concrete functions.
_FUNCTION_LIST_PREFIXES: Tuple[str, ...] = (
    "marker function(s) defined: ",
    "marker function(s): ",
    "related functions present: ",
    "related functions remain: ",
    "guard function defined but never called: ",
    "guard call present: ",
)

_SHARED_FILES_MARKER = "shares changed file(s): "
_RULE_MARKER = "behavior rule: "

_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class RiskFactor:
    """One weighted contribution to a risk score."""

    #: Stable identifier (``baseline``, ``impact_payment``, ...).
    code: str
    #: Human-readable description shown in reports.
    label: str
    #: Points contributed to the raw total.
    points: int

    def __str__(self) -> str:
        return f"+{self.points} {self.label}"

    def to_dict(self) -> Dict[str, Any]:
        """Plain-dict representation of this model."""
        return {"code": self.code, "label": self.label, "points": self.points}


@dataclass
class RiskScore:
    """The risk assigned to a single drift finding."""

    #: Final score in ``[0, 100]`` (raw total capped at 100).
    score: int
    #: One of :data:`RISK_SEVERITIES` (``LOW`` / ``MEDIUM`` / ``HIGH`` /
    #: ``CRITICAL``).
    severity: str
    #: Every applied factor, in application order; their points sum to
    #: :attr:`raw_score`.
    factors: List[RiskFactor] = field(default_factory=list)
    #: Prose explanation of why the score was produced.
    explanation: str = ""
    #: Domain the drift affects (an :data:`AREA_IMPACT` key, or
    #: ``"general"``).
    affected_area: str = "general"
    #: Confidence of the underlying drift detection result.
    confidence: float = 0.0
    #: Drift type of the underlying finding (``none``,
    #: ``behavior_weakened``, ``behavior_partial``, ``behavior_lost``).
    drift_type: str = "none"
    #: Hash of the commit where the original intent was established.
    source_commit: str = ""
    #: Hash of the later commit where the drift was detected.
    detected_commit: str = ""
    #: Sum of all factor points *before* the cap at 100.
    raw_score: int = 0

    @property
    def is_capped(self) -> bool:
        """True when the raw total exceeded 100."""
        return self.raw_score > self.score

    def to_dict(self) -> Dict[str, Any]:
        """Plain-dict (JSON-friendly) representation."""
        return {
            "score": self.score,
            "severity": self.severity,
            "raw_score": self.raw_score,
            "factors": [f.to_dict() for f in self.factors],
            "explanation": self.explanation,
            "affected_area": self.affected_area,
            "confidence": self.confidence,
            "drift_type": self.drift_type,
            "source_commit": self.source_commit,
            "detected_commit": self.detected_commit,
        }


@dataclass
class RiskReport:
    """Repository-level rollup of every risk finding."""

    #: Number of findings (drift rows with actual drift; preserved
    #: comparisons are not findings).
    total_findings: int = 0
    #: Findings rated ``HIGH`` or ``CRITICAL`` (i.e. at least HIGH).
    high_risk_findings: int = 0
    #: Findings rated ``CRITICAL`` (subset of :attr:`high_risk_findings`).
    critical_findings: int = 0
    #: Worst finding score — the highest finding drives repository risk.
    overall_risk: int = 0
    #: One :class:`RiskScore` per finding, in chronological order.
    findings: List[RiskScore] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Plain-dict (JSON-friendly) representation."""
        return {
            "total_findings": self.total_findings,
            "high_risk_findings": self.high_risk_findings,
            "critical_findings": self.critical_findings,
            "overall_risk": self.overall_risk,
            "findings": [f.to_dict() for f in self.findings],
        }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def calculate_risk(drift: DriftResult) -> RiskScore:
    """Assign a 0-100 risk score to a single drift finding.

    Deterministic and fully explained: the returned
    :class:`RiskScore.factors` always sum to ``raw_score``, and
    ``score == min(100, raw_score)``. A drift with ``drift_type == "none"``
    scores exactly 0 with no contextual factors.
    """
    baseline, baseline_label = _baseline(drift)
    factors: List[RiskFactor] = [RiskFactor("baseline", baseline_label, baseline)]
    area, impact = _affected_area(drift)

    if baseline == 0:
        # No drift: contextual factors are deliberately not applied, so a
        # preserved/benign comparison can never look risky (and the
        # sensitive-area floor never fires without a real high drift).
        return RiskScore(
            score=0,
            severity=resolve_severity(0),
            factors=factors,
            explanation=(
                f"{_sentence(drift.reason)} No risk factors are applied, "
                "so the risk score is 0."
            ),
            affected_area=area,
            confidence=drift.confidence,
            drift_type=drift.drift_type,
            source_commit=drift.original_commit_hash,
            detected_commit=drift.later_commit_hash,
            raw_score=0,
        )

    # 1. Impact area.
    if impact > 0:
        factors.append(
            RiskFactor(f"impact_{area}", _AREA_LABELS[area], impact)
        )

    # 2. Scope (multiple files outrank multiple functions).
    scope = _scope_factor(drift)
    if scope is not None:
        factors.append(scope)

    # 3. Confidence adjustment.
    if drift.confidence >= 0.90:
        code, label, points = _CONFIDENCE_HIGH
        factors.append(RiskFactor(code, label, points))
    elif drift.confidence >= 0.75:
        code, label, points = _CONFIDENCE_MODERATE
        factors.append(RiskFactor(code, label, points))

    # 4. Evidence strength.
    rule = _behavior_rule(drift)
    if rule in DIRECT_EVIDENCE_RULES:
        code, label, points = _EVIDENCE_DIRECT
        factors.append(RiskFactor(code, label, points))

    raw = sum(f.points for f in factors)
    score = min(100, raw)
    severity = resolve_severity(score, drift.severity, area)

    return RiskScore(
        score=score,
        severity=severity,
        factors=factors,
        explanation=_explanation(drift, area, rule, raw, score),
        affected_area=area,
        confidence=drift.confidence,
        drift_type=drift.drift_type,
        source_commit=drift.original_commit_hash,
        detected_commit=drift.later_commit_hash,
        raw_score=raw,
    )


def calculate_risk_report(drift_report: DriftReport) -> RiskReport:
    """Score every finding in a drift report and roll them up.

    Preserved comparisons (``drift_type == "none"``) are not findings and
    are excluded from the rollup. ``overall_risk`` is the highest finding
    score — a repository is never reported as less risky than its worst
    finding.
    """
    findings: List[RiskScore] = []
    for result in drift_report.results:
        if result.drift_type == "none" or result.severity == "none":
            continue
        findings.append(calculate_risk(result))

    return RiskReport(
        total_findings=len(findings),
        high_risk_findings=sum(
            f.severity in ("HIGH", "CRITICAL") for f in findings
        ),
        critical_findings=sum(f.severity == "CRITICAL" for f in findings),
        overall_risk=max((f.score for f in findings), default=0),
        findings=findings,
    )


def resolve_severity(
    score: int,
    drift_severity: str = "none",
    affected_area: str = "general",
) -> str:
    """Map a 0-100 score to a risk severity.

    Ranges: ``0-19 LOW``, ``20-49 MEDIUM``, ``50-74 HIGH``,
    ``75-100 CRITICAL``.

    **Floor rule:** a drift whose own severity is ``high`` in a sensitive
    area (payment / security / access control) is never mapped below
    ``HIGH`` — a significant loss in those areas cannot become ``LOW``
    merely because few contextual factors applied. The floor only ever
    raises the severity; it never lowers it.
    """
    if score >= 75:
        return "CRITICAL"
    if score >= 50:
        return "HIGH"
    if score >= 20:
        return "MEDIUM"
    if drift_severity == "high" and affected_area in SENSITIVE_AREAS:
        return "HIGH"
    return "LOW"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _baseline(drift: DriftResult) -> Tuple[int, str]:
    """Baseline tier for a drift verdict — highest applicable tier wins."""
    if drift.drift_type == "none" or drift.severity == "none":
        return _BASELINE_NONE
    if drift.severity == "high":
        # "significant/high" outranks the plain "lost" tier: a loss that
        # detection already classified high starts at 70.
        return _BASELINE_HIGH
    if drift.drift_type == "behavior_lost":
        return _BASELINE_LOST
    # behavior_partial (medium) and behavior_weakened (low), plus any other
    # detected non-high drift.
    return _BASELINE_PARTIAL


def _affected_area(drift: DriftResult) -> Tuple[str, int]:
    """Resolve the affected area (and its impact weight) for a finding.

    The original intent's category names the domain where the behavior was
    established; an ``unknown`` original falls back to the later commit's
    category; anything unresolvable is ``general`` (weight 0).
    """
    category = drift.original_intent.category
    if category == "unknown" or category not in AREA_IMPACT:
        category = drift.later_intent.category
    if category == "unknown" or category not in AREA_IMPACT:
        return "general", 0
    return category, AREA_IMPACT[category]


def _behavior_rule(drift: DriftResult) -> str:
    """The behavior rule recorded by the drift detector (or ``""``)."""
    for line in drift.evidence:
        if line.startswith(_RULE_MARKER):
            return line[len(_RULE_MARKER):].strip()
    return ""


def _shared_files(drift: DriftResult) -> List[str]:
    """Changed files named in the pairing evidence line."""
    for line in drift.evidence:
        if _SHARED_FILES_MARKER in line:
            tail = line.split(_SHARED_FILES_MARKER, 1)[1]
            return [part.strip() for part in tail.split(",") if part.strip()]
    return []


def _mentioned_functions(drift: DriftResult) -> Set[str]:
    """Concrete function names cited by the implementation evidence.

    Only lines the drift detector generates to name functions are read
    (``fn -> ...`` verdicts and ``prefix: a, b`` lists) — never state
    attributes, modules or condition text.
    """
    names: Set[str] = set()
    for line in drift.implementation:
        if " -> " in line:
            left = line.split(" -> ", 1)[0].strip()
            if _IDENTIFIER_RE.fullmatch(left):
                names.add(left)
            continue
        for prefix in _FUNCTION_LIST_PREFIXES:
            if line.startswith(prefix):
                for part in line[len(prefix):].split(","):
                    candidate = part.strip()
                    if _IDENTIFIER_RE.fullmatch(candidate):
                        names.add(candidate)
                break
    return names


def _scope_factor(drift: DriftResult) -> Optional[RiskFactor]:
    """Scope tier: multiple files > multiple functions > single."""
    if len(_shared_files(drift)) >= 2:
        return RiskFactor(*_SCOPE_FILES)
    if len(_mentioned_functions(drift)) >= 2:
        return RiskFactor(*_SCOPE_FUNCTIONS)
    return None


def _sentence(text: str) -> str:
    """A single well-terminated prose sentence."""
    cleaned = text.strip()
    if cleaned and cleaned[-1] not in ".!?":
        cleaned += "."
    return cleaned


def _explanation(
    drift: DriftResult, area: str, rule: str, raw: int, score: int
) -> str:
    """Build the prose explanation for a scored finding."""
    parts = [_sentence(drift.reason)]
    parts.append(f"The affected area is {_AREA_PHRASES.get(area, area)}.")
    later_summary = drift.later_commit_message.splitlines()[0]
    parts.append(f"Drift was detected at the later commit '{later_summary}'.")
    if rule in DIRECT_EVIDENCE_RULES:
        parts.append(
            f"The {rule or 'structural'} rule inspects concrete AST "
            "structure (functions, calls and state), so the evidence is "
            "direct."
        )
    else:
        parts.append(
            f"The {rule or 'structural'} rule relies on name-presence "
            "heuristics, so the evidence is indirect."
        )
    if raw > score:
        parts.append(f"Weighted factors total {raw}, capped to {score}.")
    else:
        parts.append(f"Weighted factors total {raw}.")
    return " ".join(parts)
