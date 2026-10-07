"""Intent drift detection for GhostCode.

This module implements the fourth stage of the GhostCode pipeline: comparing
the *developer intent* recorded in the Git history (stage 3,
:class:`~ghostcode.intent_extractor.Intent`) against the *current* Python
code structure (stage 2, :class:`~ghostcode.code_analyzer.RepositoryCodeAnalysis`)
across chronological commits, and reporting places where a previously
established behavior is no longer represented in later code.

Design rules (shared with every other GhostCode component):

* Deterministic — the same commits + same code always yield the same report.
* Explainable — every result carries a pairing reason, per-function coverage
  lines and a human-readable reason; nothing is a black box.
* Conservative — wording differences alone are never reported as drift;
  low-confidence intents are skipped instead of guessed at.
* Offline and LLM-free — plain Python string matching and call-graph walks
  only; no models, no network.

How detection works (high level):

1. Commits are sorted chronologically (``committed_at``).
2. An :class:`~ghostcode.intent_extractor.Intent` is extracted for every
   commit (or matched from caller-supplied intents by commit hash).
3. Original intents with confidence below
   :data:`DEFAULT_MIN_INTENT_CONFIDENCE` are skipped (documented heuristic,
   not a probability).
4. For every confident original intent the *nearest later related commit* is
   found — related by shared changed files or by a shared non-``unknown``
   category.
5. The original intent is checked against the current code with a behavior
   rule chosen from its action/category (logging coverage, validation
   presence, prevention guard, or generic presence). The check yields
   ``present`` / ``weakened`` / ``partial`` / ``absent``, which maps to a
   drift type and severity.
6. Related pairs always produce a comparison row (including
   ``severity="none"`` for preserved intents). Unrelated-but-confident
   intents are still checked against the current state as a safety net;
   they only produce a row when something was actually lost.

Intent extraction in this phase is deterministic and heuristic. It does not
use an LLM. It does not decide whether the drift is *risky* — risk scoring
is a later phase.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    List,
    Optional,
    Sequence,
    Set,
    Tuple,
)

from .code_analyzer import (
    FunctionInfo,
    PythonFileAnalysis,
    RepositoryCodeAnalysis,
)
from .intent_extractor import CATEGORY_SIGNALS, Intent, IntentExtractor

if TYPE_CHECKING:  # pragma: no cover - typing only, no runtime dependency
    from .git_analyzer import CommitInfo

__all__ = [
    "BehaviorCheck",
    "DriftResult",
    "DriftReport",
    "IntentDriftDetector",
    "DRIFT_TYPES",
    "SEVERITIES",
    "SEVERITY_BY_DRIFT_TYPE",
    "BEHAVIOR_STATUSES",
    "DEFAULT_MIN_INTENT_CONFIDENCE",
    "DEFAULT_MAX_CALL_DEPTH",
]

# ---------------------------------------------------------------------------
# Controlled vocabularies
# ---------------------------------------------------------------------------

#: Drift verdicts a comparison can reach. ``none`` means the established
#: behavior is still represented (a comparison happened; nothing drifted).
DRIFT_TYPES: Tuple[str, ...] = (
    "none",
    "behavior_weakened",
    "behavior_partial",
    "behavior_lost",
)

#: Severity levels (ordered from least to most significant).
SEVERITIES: Tuple[str, ...] = ("none", "low", "medium", "high")

#: Deterministic mapping from drift type to severity.
SEVERITY_BY_DRIFT_TYPE: Dict[str, str] = {
    "none": "none",
    "behavior_weakened": "low",
    "behavior_partial": "medium",
    "behavior_lost": "high",
}

#: Outcomes of a behavior check against the current code.
BEHAVIOR_STATUSES: Tuple[str, ...] = (
    "present",
    "weakened",
    "partial",
    "absent",
    "not-evaluable",
)

#: Original intents below this heuristic confidence are not used as drift
#: originals (their extraction was too weak to support a claim).
DEFAULT_MIN_INTENT_CONFIDENCE = 0.5

#: Maximum call hops followed when looking for a logging sink.
DEFAULT_MAX_CALL_DEPTH = 4

#: How much a given verdict contributes to the drift confidence, multiplied
#: by the original intent's own confidence. Documented heuristic — not a
#: probability.
_VERDICT_STRENGTH: Dict[str, float] = {
    "none": 0.9,
    "behavior_weakened": 0.85,
    "behavior_partial": 0.9,
    "behavior_lost": 1.0,
}

#: Cap so the detector never claims certainty.
_MAX_DRIFT_CONFIDENCE = 0.95

#: Call names that *write* collection/attribute state (``x.add(...)``).
_WRITE_METHODS = frozenset(
    {"add", "append", "extend", "insert", "update", "setdefault", "put", "push"}
)

#: Words that carry no meaning when matching an intent object against
#: function names.
_STOPWORDS = frozenset(
    {
        "a", "an", "the", "for", "of", "to", "in", "on", "at", "and", "or",
        "but", "with", "from", "by", "into", "via", "per", "is", "are", "be",
        "when", "then", "if", "every", "all", "each", "any", "some", "no",
        "this", "that", "these", "those", "its", "it", "as",
    }
)

#: Actions whose later occurrence lets a partial loss be phrased as
#: "during refactoring".
_REFACTORING_ACTIONS = frozenset(
    {
        "refactor", "improve", "clean", "simplify", "update", "change",
        "remove", "delete", "fix", "restructure", "rename",
    }
)

_TOKEN_RE = re.compile(r"[a-z0-9']+")

_STATUS_TO_DRIFT: Dict[str, str] = {
    "present": "none",
    "weakened": "behavior_weakened",
    "partial": "behavior_partial",
    "absent": "behavior_lost",
}


# ---------------------------------------------------------------------------
# Small deterministic helpers
# ---------------------------------------------------------------------------


def _tokens(text: Optional[str]) -> List[str]:
    """Lowercase word tokens of *text* (``"validate_email"`` -> names too)."""
    if not text:
        return []
    return _TOKEN_RE.findall(text.lower())


def _signal_words(category: str) -> Set[str]:
    """Single-word keyword signals of a controlled category."""
    return {
        signal
        for signal in CATEGORY_SIGNALS.get(category, ())
        if " " not in signal
    }


def _matches(words: Set[str], *texts: Optional[str]) -> bool:
    """True when any text shares at least one whole-word token with *words*."""
    if not words:
        return False
    for text in texts:
        if words.intersection(_tokens(text)):
            return True
    return False


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class BehaviorCheck:
    """Result of checking one intent against the current code.

    :attr:`status` is one of :data:`BEHAVIOR_STATUSES`; :attr:`evidence`
    explains the verdict line by line (shown as "Later implementation" in
    the demo output).
    """

    #: ``present`` / ``weakened`` / ``partial`` / ``absent`` / ``not-evaluable``
    status: str
    #: Which rule produced the verdict (e.g. ``"logging-coverage"``).
    rule: str
    #: Human-readable lines of structural evidence.
    evidence: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Plain-dict representation of this model."""
        return {
            "status": self.status,
            "rule": self.rule,
            "evidence": list(self.evidence),
        }


@dataclass
class DriftResult:
    """One comparison between an original intent and a later commit."""

    #: Hash of the commit where the original intent was established.
    original_commit_hash: str
    #: Message of the original commit.
    original_commit_message: str
    #: Hash of the later commit the drift is anchored to.
    later_commit_hash: str
    #: Message of the later commit.
    later_commit_message: str
    #: Intent extracted from the original commit.
    original_intent: Intent
    #: Intent extracted from the later commit.
    later_intent: Intent
    #: One of :data:`DRIFT_TYPES`.
    drift_type: str
    #: One of :data:`SEVERITIES`.
    severity: str
    #: One-sentence explanation of the verdict.
    reason: str
    #: Evidence about the *comparison* (pairing, intent continuity).
    evidence: List[str] = field(default_factory=list)
    #: Evidence about the *current implementation* (per-function coverage,
    #: guard presence, ...) — the "Later implementation" block.
    implementation: List[str] = field(default_factory=list)
    #: Heuristic confidence of this verdict in ``[0.0, 1.0]`` (not a
    #: probability): original intent confidence scaled by evidence strength.
    confidence: float = 0.0

    @property
    def is_drift(self) -> bool:
        """True when this row reports meaningful drift."""
        return self.severity != "none"

    @property
    def is_significant(self) -> bool:
        """True for medium/high drift (the scenarios that matter most)."""
        return self.severity in ("medium", "high")

    def to_dict(self) -> Dict[str, Any]:
        """Plain-dict representation, including nested intents."""
        return {
            "original_commit_hash": self.original_commit_hash,
            "original_commit_message": self.original_commit_message,
            "later_commit_hash": self.later_commit_hash,
            "later_commit_message": self.later_commit_message,
            "original_intent": self.original_intent.to_dict(),
            "later_intent": self.later_intent.to_dict(),
            "drift_type": self.drift_type,
            "severity": self.severity,
            "reason": self.reason,
            "evidence": list(self.evidence),
            "implementation": list(self.implementation),
            "confidence": self.confidence,
        }


@dataclass
class DriftReport:
    """Every comparison produced for one repository."""

    #: Repository name (basename of the analyzed code root).
    repository_name: str
    #: Comparisons in chronological order of their original commit.
    results: List[DriftResult] = field(default_factory=list)

    @property
    def drifted(self) -> List[DriftResult]:
        """Rows with any drift (severity ``low`` and above)."""
        return [r for r in self.results if r.is_drift]

    @property
    def significant(self) -> List[DriftResult]:
        """Rows with medium/high drift."""
        return [r for r in self.results if r.is_significant]

    def to_dict(self) -> Dict[str, Any]:
        """Plain-dict representation, including summary counts."""
        return {
            "repository_name": self.repository_name,
            "results": [r.to_dict() for r in self.results],
            "total_comparisons": len(self.results),
            "drifted": len(self.drifted),
            "significant": len(self.significant),
        }


# ---------------------------------------------------------------------------
# Detector
# ---------------------------------------------------------------------------


class IntentDriftDetector:
    """Detects drift between historical intent and current code structure.

    Usage::

        detector = IntentDriftDetector()
        report = detector.detect(history.commits, code_analysis)

        for result in report.results:
            print(result.severity, result.reason)
    """

    def __init__(
        self,
        min_intent_confidence: float = DEFAULT_MIN_INTENT_CONFIDENCE,
        max_call_depth: int = DEFAULT_MAX_CALL_DEPTH,
    ) -> None:
        if not 0.0 <= min_intent_confidence <= 1.0:
            raise ValueError("min_intent_confidence must be within [0.0, 1.0]")
        if max_call_depth < 1:
            raise ValueError("max_call_depth must be at least 1")
        self._min_confidence = float(min_intent_confidence)
        self._max_call_depth = int(max_call_depth)
        self._extractor = IntentExtractor()

    @property
    def min_intent_confidence(self) -> float:
        """Confidence threshold below which originals are skipped."""
        return self._min_confidence

    @property
    def max_call_depth(self) -> int:
        """Maximum call hops followed when searching for a logging sink."""
        return self._max_call_depth

    def __repr__(self) -> str:
        return (
            f"IntentDriftDetector(min_intent_confidence="
            f"{self._min_confidence}, max_call_depth={self._max_call_depth})"
        )

    # -- public API --------------------------------------------------------

    def detect(
        self,
        commits: Sequence["CommitInfo"],
        code: RepositoryCodeAnalysis,
        intents: Optional[Sequence[Intent]] = None,
    ) -> DriftReport:
        """Compare intents across chronological commits against *code*.

        :param commits: Commits from the Git History Analyzer (any order —
            they are sorted chronologically first).
        :param code: Structural analysis of the current repository state.
        :param intents: Optional pre-extracted intents; matched to commits
            by ``commit_hash`` (missing ones are extracted on the fly).
        """
        ordered = sorted(
            commits, key=lambda c: (c.committed_at.timestamp(), c.hash)
        )
        provided: Dict[str, Intent] = {}
        for intent in intents or ():
            if intent.commit_hash:
                provided[intent.commit_hash] = intent

        pairs: List[Tuple["CommitInfo", Intent]] = []
        for commit in ordered:
            intent = provided.get(commit.hash) or self._extractor.extract(commit)
            pairs.append((commit, intent))

        results: List[DriftResult] = []
        for index, (commit, intent) in enumerate(pairs):
            if intent.confidence < self._min_confidence:
                # Too weak to anchor a drift claim; documented and visible.
                continue

            later = self._nearest_later_related(pairs, index)
            if later is not None:
                later_commit, later_intent, basis = later
                result = self._compare(
                    intent,
                    commit,
                    later_intent,
                    later_commit,
                    code,
                    pairing=f"related later commit {basis}",
                )
                if result is not None:
                    results.append(result)
                continue

            # Safety net: no later commit touches this intent's area, but a
            # confident intent still deserves a check against the current
            # state — problems are reported, preserved intents stay silent.
            last_commit, last_intent = pairs[-1]
            if last_commit.hash == commit.hash:
                continue
            check = self.check_behavior(intent, commit, code)
            if check.status in ("present", "not-evaluable"):
                continue
            result = self._compare(
                intent,
                commit,
                last_intent,
                last_commit,
                code,
                pairing="no related later commit; checked against current state",
                check=check,
            )
            if result is not None:
                results.append(result)

        return DriftReport(
            repository_name=code.repository_path.name, results=results
        )

    def check_behavior(
        self,
        intent: Intent,
        commit: "CommitInfo",
        code: RepositoryCodeAnalysis,
    ) -> BehaviorCheck:
        """Check whether *intent*'s behavior is represented in *code*.

        The commit's ``changed_files`` anchor where the behavior was
        established. This is the structural heart of drift detection and is
        public so tests (and later phases) can verify a single intent
        without running a full comparison.
        """
        established = [p for p in commit.changed_files if p.endswith(".py")]
        if not established:
            return BehaviorCheck(
                status="not-evaluable",
                rule="file-presence",
                evidence=["commit changed no Python source files"],
            )

        files = self._match_files(established, code)
        if not files:
            return BehaviorCheck(
                status="absent",
                rule="file-presence",
                evidence=[
                    "established file(s) no longer exist: "
                    + ", ".join(sorted(established))
                ],
            )

        status, rule, evidence = self._dispatch(intent, code, files)
        return BehaviorCheck(status=status, rule=rule, evidence=evidence)

    # -- pairing -----------------------------------------------------------

    def _nearest_later_related(
        self,
        pairs: List[Tuple["CommitInfo", Intent]],
        index: int,
    ) -> Optional[Tuple["CommitInfo", Intent, str]]:
        """First later commit related to ``pairs[index]``, with the basis."""
        original_commit, original_intent = pairs[index]
        original_files = set(original_commit.changed_files)
        for later_commit, later_intent in pairs[index + 1 :]:
            shared = sorted(original_files & set(later_commit.changed_files))
            if shared:
                return (
                    later_commit,
                    later_intent,
                    f"shares changed file(s): {', '.join(shared)}",
                )
            if (
                original_intent.category != "unknown"
                and original_intent.category == later_intent.category
            ):
                return (
                    later_commit,
                    later_intent,
                    f"shares category '{original_intent.category}'",
                )
        return None

    # -- comparison --------------------------------------------------------

    def _compare(
        self,
        original_intent: Intent,
        original_commit: "CommitInfo",
        later_intent: Intent,
        later_commit: "CommitInfo",
        code: RepositoryCodeAnalysis,
        pairing: str,
        check: Optional[BehaviorCheck] = None,
    ) -> Optional[DriftResult]:
        """Turn a behavior check into a fully explained drift result."""
        check = check or self.check_behavior(
            original_intent, original_commit, code
        )
        if check.status == "not-evaluable":
            return None

        drift_type = _STATUS_TO_DRIFT[check.status]
        severity = SEVERITY_BY_DRIFT_TYPE[drift_type]
        evidence = [
            f"pairing: {pairing}",
            "original intent: "
            f"action={original_intent.action}, "
            f"category={original_intent.category}, "
            f"object={original_intent.object}",
            "later intent: "
            f"action={later_intent.action}, "
            f"category={later_intent.category}, "
            f"object={later_intent.object}",
            "category continuity: "
            f"{original_intent.category} -> {later_intent.category}",
            "action continuity: "
            f"{original_intent.action} -> {later_intent.action}",
            f"behavior rule: {check.rule}",
        ]
        confidence = round(
            min(
                _MAX_DRIFT_CONFIDENCE,
                original_intent.confidence * _VERDICT_STRENGTH[drift_type],
            ),
            2,
        )
        return DriftResult(
            original_commit_hash=original_commit.hash,
            original_commit_message=original_commit.message,
            later_commit_hash=later_commit.hash,
            later_commit_message=later_commit.message,
            original_intent=original_intent,
            later_intent=later_intent,
            drift_type=drift_type,
            severity=severity,
            reason=self._reason(drift_type, original_intent, later_intent),
            evidence=evidence,
            implementation=list(check.evidence),
            confidence=confidence,
        )

    @staticmethod
    def _reason(
        drift_type: str, original: Intent, later: Intent
    ) -> str:
        """One deterministic sentence explaining the verdict."""
        category = original.category
        if drift_type == "none":
            return (
                f"A previously established {category} behavior remains "
                "represented in the current code."
            )
        if drift_type == "behavior_weakened":
            return (
                f"A previously established {category} behavior is present "
                "but no longer connected to the code paths that use it."
            )
        if drift_type == "behavior_partial":
            where = (
                "during refactoring"
                if later.action in _REFACTORING_ACTIONS
                else "in later code"
            )
            return (
                f"A previously established {category} behavior was "
                f"partially lost {where}."
            )
        return (
            f"A previously established {category} behavior is no longer "
            "represented in later code."
        )

    # -- behavior rules ----------------------------------------------------

    def _dispatch(
        self,
        intent: Intent,
        code: RepositoryCodeAnalysis,
        files: List[PythonFileAnalysis],
    ) -> Tuple[str, str, List[str]]:
        """Pick the rule from the intent's action/category."""
        action = intent.action or ""
        if action == "log" or intent.category == "logging":
            return self._rule_logging(intent, code, files)
        if action == "validate" or intent.category == "validation":
            return self._rule_validation(intent, code)
        if action in ("prevent", "restrict"):
            return self._rule_prevent(intent, code, files)
        return self._rule_generic(intent, code)

    # Rule 1: logging coverage across sibling functions -------------------

    def _rule_logging(
        self,
        intent: Intent,
        code: RepositoryCodeAnalysis,
        files: List[PythonFileAnalysis],
    ) -> Tuple[str, str, List[str]]:
        """For ``log`` intents: do all sibling flows still reach a log sink?

        Peers are discovered by name-stem families (``authenticate_*``),
        falling back to category-related functions with branches, falling
        back to every function in the established files. Coverage means the
        function can reach a logging call (``record``, ``audit``, ... )
        within :attr:`max_call_depth` call hops.
        """
        category_words = _signal_words(intent.category)
        sink_words = _signal_words("logging")
        funcs = [fn for _, fn in _behavior_functions(files)]
        if not funcs:
            return (
                "absent",
                "logging-coverage",
                ["established file(s) contain no function definitions"],
            )

        peers, tier = _logging_peers(funcs, category_words)
        graph = _call_graph(code)
        evidence = [
            f"rule: logging coverage over {len(peers)} function(s) [{tier}]"
        ]
        covered = 0
        for fn in peers:
            sink = _reaches_sink(
                fn.name, graph, sink_words, self._max_call_depth
            )
            if sink is not None:
                covered += 1
                evidence.append(
                    f"{fn.name} -> logging present (reaches '{sink}')"
                )
            else:
                evidence.append(f"{fn.name} -> logging absent")

        if covered == len(peers):
            return "present", "logging-coverage", evidence
        if covered == 0:
            return "absent", "logging-coverage", evidence
        return "partial", "logging-coverage", evidence

    # Rule 2: validation (and friends) presence ----------------------------

    def _rule_validation(
        self, intent: Intent, code: RepositoryCodeAnalysis
    ) -> Tuple[str, str, List[str]]:
        """For ``validation`` intents: the marker function must exist and
        actually be called somewhere in the repository."""
        markers = _object_markers(intent)
        category_words = _signal_words(intent.category)
        funcs = _behavior_functions(code.files)
        evidence = [f"rule: validation presence (markers: {sorted(markers)})"]

        marker_funcs = [fn for _, fn in funcs if _matches(markers, fn.name)]
        if marker_funcs:
            called_in = _call_sites(code, {fn.name for fn in marker_funcs})
            names = sorted({fn.name for fn in marker_funcs})
            evidence.append(
                "marker function(s) defined: " + ", ".join(names)
            )
            if called_in:
                evidence.append(
                    "called from module(s): " + ", ".join(sorted(called_in))
                )
                return "present", "validation-presence", evidence
            evidence.append("marker function(s) are never called")
            return "weakened", "validation-presence", evidence

        domain_funcs = sorted(
            {fn.name for _, fn in funcs if _matches(category_words, fn.name)}
        )
        if domain_funcs:
            evidence.append(
                "intent markers not found in any function name; only "
                "category-related functions remain: "
                + ", ".join(domain_funcs)
            )
            return "weakened", "validation-presence", evidence
        evidence.append(
            f"no function matches markers {sorted(markers)} or category "
            f"'{intent.category}' in the current code"
        )
        return "absent", "validation-presence", evidence

    # Rule 3: prevention guard ---------------------------------------------

    def _rule_prevent(
        self,
        intent: Intent,
        code: RepositoryCodeAnalysis,
        files: List[PythonFileAnalysis],
    ) -> Tuple[str, str, List[str]]:
        """For ``prevent``/``restrict`` intents: a guard must consult the
        state that protects against the problem.

        Evidence considered (all deterministic): a guard function or call
        whose name matches the *distinctive* object marker (e.g.
        ``duplicate``), a condition mentioning it, or tracked domain state
        that is written **and** consulted by a condition. State that is
        written but never read means the protection exists only on paper.
        """
        markers = _object_markers(intent)
        category_words = _signal_words(intent.category)
        distinctive = {m for m in markers if m not in category_words} or markers
        evidence = [
            "rule: prevention guard "
            f"(distinctive markers: {sorted(distinctive)})"
        ]

        repo_funcs = _behavior_functions(code.files)
        guard_defs = sorted(
            {fn.name for _, fn in repo_funcs if _matches(distinctive, fn.name)}
        )
        guard_calls = sorted(
            {
                c.function_name
                for fa in code.files
                for c in fa.function_calls
                if _matches(distinctive, c.function_name, c.qualified_name)
            }
        )
        guard_conds = [
            cond
            for fa in code.files
            for cond in fa.conditionals
            if _matches(distinctive, cond.expression)
        ]

        writes, reads, cond_text = _state_usage(files)
        domain_words = markers | category_words
        domain_state = {a for a in writes if _matches(domain_words, a)}
        consulted = {
            a
            for a in domain_state
            if a in reads or a in cond_text
        }

        if guard_calls:
            evidence.append("guard call present: " + ", ".join(guard_calls))
            return "present", "prevention-guard", evidence
        if guard_conds:
            lines = sorted(
                f"line {c.line_number}: {c.expression}"
                for c in guard_conds
                if c.expression
            )
            evidence.append("guard condition present: " + "; ".join(lines))
            return "present", "prevention-guard", evidence
        if domain_state and domain_state <= consulted:
            evidence.append(
                "tracked state consulted: " + ", ".join(sorted(consulted))
            )
            return "present", "prevention-guard", evidence
        if guard_defs:
            evidence.append(
                "guard function defined but never called: "
                + ", ".join(guard_defs)
            )
            return "weakened", "prevention-guard", evidence
        if domain_state and not (domain_state & consulted):
            evidence.append(
                "state written but never consulted: "
                + ", ".join(sorted(domain_state))
            )
        evidence.append(
            "no guard function, call or condition matching "
            + str(sorted(distinctive))
            + " in the current code"
        )
        domain_funcs = sorted(
            {fn.name for _, fn in repo_funcs if _matches(markers, fn.name)}
        )
        if domain_funcs:
            evidence.append(
                "related functions remain: " + ", ".join(domain_funcs)
            )
        return "absent", "prevention-guard", evidence

    # Rule 4: generic presence ---------------------------------------------

    def _rule_generic(
        self, intent: Intent, code: RepositoryCodeAnalysis
    ) -> Tuple[str, str, List[str]]:
        """Default rule: functions matching the intent's object (or, failing
        that, its category) should still exist and be wired up."""
        markers = _object_markers(intent)
        category_words = _signal_words(intent.category)
        funcs = _behavior_functions(code.files)
        evidence = [f"rule: generic presence (markers: {sorted(markers)})"]

        marker_funcs = [fn for _, fn in funcs if _matches(markers, fn.name)]
        if marker_funcs:
            names = sorted({fn.name for fn in marker_funcs})
            evidence.append("marker function(s): " + ", ".join(names))
            called_in = _call_sites(code, set(names))
            if called_in:
                evidence.append(
                    "called from module(s): " + ", ".join(sorted(called_in))
                )
                return "present", "generic-presence", evidence
            evidence.append("marker function(s) are never called")
            return "weakened", "generic-presence", evidence

        domain_funcs = sorted(
            {fn.name for _, fn in funcs if _matches(category_words, fn.name)}
        )
        if domain_funcs:
            evidence.append(
                "related functions present: " + ", ".join(domain_funcs)
            )
            return "present", "generic-presence", evidence

        evidence.append(
            f"no function matches markers {sorted(markers)} or category "
            f"'{intent.category}' in the current code"
        )
        return "weakened", "generic-presence", evidence

    # -- shared helpers ----------------------------------------------------

    @staticmethod
    def _match_files(
        established: Sequence[str], code: RepositoryCodeAnalysis
    ) -> List[PythonFileAnalysis]:
        """Resolve repo-relative changed files to analyzed files."""
        matched: List[PythonFileAnalysis] = []
        root = code.repository_path
        for relative in established:
            target = (root / relative).resolve()
            for fa in code.files:
                if fa.file_path == target or str(fa.file_path).endswith(
                    "/" + relative
                ):
                    matched.append(fa)
                    break
        return matched


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _object_markers(intent: Intent) -> Set[str]:
    """Meaningful words of the intent object (determiners removed)."""
    return {t for t in _tokens(intent.object) if t not in _STOPWORDS}


def _is_test_path(path_parts: Sequence[str]) -> bool:
    return any(part in ("test", "tests") for part in path_parts)


def _behavior_functions(
    files: Sequence[PythonFileAnalysis],
) -> List[Tuple[PythonFileAnalysis, FunctionInfo]]:
    """Every non-test function/method in *files*, in source order."""
    result: List[Tuple[PythonFileAnalysis, FunctionInfo]] = []
    for fa in files:
        parts = fa.file_path.parts
        if _is_test_path(parts) or fa.file_path.name.startswith("test_"):
            continue
        for fn in list(fa.functions) + [
            m for c in fa.classes for m in c.methods
        ]:
            if fn.name.startswith("test_"):
                continue
            result.append((fa, fn))
    return result


def _all_functions(
    files: Sequence[PythonFileAnalysis],
) -> List[FunctionInfo]:
    return [fn for fa in files for fn in _file_functions(fa)]


def _file_functions(fa: PythonFileAnalysis) -> List[FunctionInfo]:
    return list(fa.functions) + [m for c in fa.classes for m in c.methods]


def _call_graph(code: RepositoryCodeAnalysis) -> Dict[str, List[str]]:
    """Function name -> names it calls (repo-wide, deterministic order)."""
    graph: Dict[str, List[str]] = {}
    for fa in code.files:
        for fn in _file_functions(fa):
            callees = [c.function_name for c in fn.calls]
            graph.setdefault(fn.name, []).extend(callees)
    return {name: list(dict.fromkeys(callees)) for name, callees in graph.items()}


def _reaches_sink(
    start: str, graph: Dict[str, List[str]], sink_words: Set[str], depth: int
) -> Optional[str]:
    """Breadth-first search for a call name matching *sink_words*."""
    if _matches(sink_words, start):
        return start
    seen: Set[str] = {start}
    frontier: List[Tuple[str, int]] = [(start, 0)]
    while frontier:
        name, level = frontier.pop(0)
        if level >= depth:
            continue
        for callee in graph.get(name, ()):
            if callee in seen:
                continue
            seen.add(callee)
            if _matches(sink_words, callee):
                return callee
            frontier.append((callee, level + 1))
    return None


def _logging_peers(
    funcs: List[FunctionInfo], category_words: Set[str]
) -> Tuple[List[FunctionInfo], str]:
    """Choose which functions a logging intent should cover.

    Tier 1 — sibling families sharing a leading name stem (this is what
    separates ``authenticate_password`` from ``hash_password``).
    Tier 2 — category-related functions that contain branches.
    Tier 3 — every function in the established files.
    """
    groups: Dict[str, List[FunctionInfo]] = {}
    for fn in funcs:
        parts = [p for p in fn.name.split("_") if p]
        key = parts[0].lower() if parts else fn.name.lower()
        groups.setdefault(key, []).append(fn)

    tier1: List[FunctionInfo] = []
    for members in groups.values():
        if len(members) >= 2 and any(
            _matches(category_words, m.name) for m in members
        ):
            tier1.extend(members)
    if tier1:
        return tier1, "sibling name-stem family"

    tier2 = [
        fn
        for fn in funcs
        if fn.has_conditions and _matches(category_words, fn.name)
    ]
    if tier2:
        return tier2, "category-related functions with branches"

    return funcs, "all functions in the established files"


def _call_sites(
    code: RepositoryCodeAnalysis, names: Set[str]
) -> Set[str]:
    """Modules containing a call to any of *names* (tests included)."""
    sites: Set[str] = set()
    for fa in code.files:
        if any(c.function_name in names for c in fa.function_calls):
            sites.add(fa.module_name)
    return sites


def _state_usage(
    files: Sequence[PythonFileAnalysis],
) -> Tuple[Set[str], Set[str], str]:
    """Written attrs, read attrs and all condition text in *files*.

    A call like ``self.processed.add(x)`` writes ``processed``; any other
    call on the same attribute (and any condition mentioning it) reads it.
    """
    writes: Set[str] = set()
    reads: Set[str] = set()
    condition_texts: List[str] = []
    for fa in files:
        for cond in fa.conditionals:
            if cond.expression:
                condition_texts.append(cond.expression)
        for call in fa.function_calls:
            parts = call.qualified_name.split(".")
            if len(parts) >= 3 and parts[0] in ("self", "cls"):
                attr, method = parts[1], call.function_name
            elif len(parts) >= 2 and parts[0] not in ("self", "cls"):
                attr, method = parts[0], call.function_name
            else:
                continue  # plain function call, not a state access
            (writes if method in _WRITE_METHODS else reads).add(attr)
    return writes, reads, " || ".join(condition_texts)
