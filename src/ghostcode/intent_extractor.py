"""Deterministic intent extraction for GhostCode.

This module implements the third stage of the GhostCode pipeline: turning
Git commit messages (the :class:`~ghostcode.git_analyzer.CommitInfo`
objects produced by the Git History Analyzer) into structured, explainable
:class:`Intent` objects.

Design rules:

* Deterministic and heuristic — the same message always yields the same
  Intent, with no randomness.
* No LLM, no API, no network, no embeddings, no machine learning: only
  controlled vocabularies and plain Python string matching.
* Explainable — every classification records its evidence; nothing is a
  black box.
* Conservative — when the message does not carry enough information the
  extractor falls back to the message itself instead of inventing content.
* Independent — it accepts clean data (a message or a CommitInfo) and
  returns clean Intent objects; no GitPython or AST internals involved.

Intent extraction does **not** judge whether any code satisfies the
intent; that comparison belongs to
:mod:`~ghostcode.drift_detector`, which performs it against the current
code structure.
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
    Tuple,
    Union,
)

if TYPE_CHECKING:  # pragma: no cover - typing only, no runtime dependency
    from .git_analyzer import CommitInfo

__all__ = [
    "Intent",
    "IntentExtractor",
    "extract_intent",
    "CATEGORIES",
    "ACTION_VERBS",
    "ACTION_ALIASES",
    "CATEGORY_SIGNALS",
    "CONFIDENCE_WEIGHTS",
]


# ---------------------------------------------------------------------------
# Controlled vocabularies
# ---------------------------------------------------------------------------

#: Supported intent categories. Order matters: on equal signal scores the
#: earlier category wins (deterministic tie-breaking). ``unknown`` is the
#: fallback when nothing matches.
CATEGORIES: Tuple[str, ...] = (
    "validation",
    "authentication",
    "authorization",
    "logging",
    "payment",
    "security",
    "refactoring",
    "bug_fix",
    "feature",
    "data_processing",
    "testing",
    "configuration",
    "cleanup",
    "unknown",
)

#: Canonical development verbs recognized as the intent *action*.
ACTION_VERBS: Tuple[str, ...] = (
    "add",
    "implement",
    "create",
    "introduce",
    "prevent",
    "fix",
    "remove",
    "delete",
    "refactor",
    "improve",
    "update",
    "change",
    "log",
    "validate",
    "handle",
    "support",
    "allow",
    "restrict",
    "secure",
    "clean",
    "simplify",
)

#: Explicit irregular/synonym normalizations (regular -s/-ed/-ing forms are
#: handled by morphology rules instead).
ACTION_ALIASES: Dict[str, str] = {
    "cleanup": "clean",
    "bugfix": "fix",
}

#: Domain keywords per category. Matching is done on lowercased whole words
#: (or consecutive word phrases such as ``"clean up"``), so ``log`` in
#: "logins" can never match accidentally.
CATEGORY_SIGNALS: Dict[str, Tuple[str, ...]] = {
    "validation": (
        "validate",
        "validation",
        "validator",
        "validators",
        "verify",
        "verification",
        "email",
        "emails",
    ),
    "authentication": (
        "login",
        "logins",
        "logout",
        "authentication",
        "authenticate",
        "password",
        "passwords",
        "credential",
        "credentials",
        "token",
        "tokens",
        "session",
        "sessions",
        "signin",
        "mfa",
        "2fa",
    ),
    "authorization": (
        "authorization",
        "authorize",
        "permission",
        "permissions",
        "role",
        "roles",
        "privilege",
        "privileges",
        "access",
        "acl",
    ),
    "logging": (
        "log",
        "logs",
        "logging",
        "audit",
        "auditing",
        "logger",
        "record",
        "records",
        "trace",
        "tracing",
        "telemetry",
    ),
    "payment": (
        "payment",
        "payments",
        "transaction",
        "transactions",
        "settlement",
        "settle",
        "refund",
        "refunds",
        "invoice",
        "invoices",
        "billing",
        "charge",
        "charges",
        "checkout",
        "processing",
    ),
    "security": (
        "security",
        "secure",
        "vulnerability",
        "vulnerabilities",
        "encryption",
        "encrypt",
        "hashing",
        "hash",
        "sanitize",
        "xss",
        "csrf",
        "injection",
        "cors",
    ),
    "refactoring": (
        "refactor",
        "refactoring",
        "restructure",
        "reorganize",
        "reorganise",
        "rename",
        "structure",
        "structures",
        "rework",
    ),
    "bug_fix": (
        "fix",
        "fixes",
        "fixed",
        "fixing",
        "bug",
        "bugs",
        "broken",
        "issue",
        "issues",
        "error",
        "errors",
        "crash",
        "crashes",
        "regression",
        "regressions",
        "hotfix",
        "defect",
    ),
    "feature": (
        "feature",
        "features",
        "initial",
        "application",
        "applications",
        "new",
        "launch",
        "enable",
        "support",
    ),
    "data_processing": (
        "data",
        "processing",
        "etl",
        "pipeline",
        "pipelines",
        "transform",
        "transformation",
        "import",
        "export",
        "migration",
        "migrate",
    ),
    "testing": (
        "test",
        "tests",
        "testing",
        "coverage",
        "assert",
        "asserts",
        "pytest",
        "mock",
        "mocks",
    ),
    "configuration": (
        "config",
        "configs",
        "configuration",
        "configure",
        "setting",
        "settings",
        "environment",
        "env",
        "option",
        "options",
        "flag",
        "flags",
        "defaults",
    ),
    "cleanup": (
        "cleanup",
        "clean up",
        "clean",
        "tidy",
        "dead code",
        "unused",
    ),
}

#: Deterministic confidence weights (heuristic, NOT probabilities).
#: They simply add up when the corresponding signal was found.
CONFIDENCE_WEIGHTS: Dict[str, float] = {
    "action": 0.30,
    "category": 0.30,
    "object": 0.25,
    "scope": 0.15,
}

#: Words dropped from the start of an object phrase.
_DETERMINERS = frozenset(
    {
        "a",
        "an",
        "the",
        "every",
        "all",
        "each",
        "any",
        "some",
        "no",
        "this",
        "that",
        "these",
        "those",
    }
)

_WORD_RE = re.compile(r"([A-Za-z0-9']+)")
_DETERMINER_RE = re.compile(
    r"^(?:a|an|the|every|all|each|any|some|no|this|that|these|those)\s+",
    re.IGNORECASE,
)
_FOR_RE = re.compile(r"\bfor\b", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class Intent:
    """A structured representation of one commit's developer intent."""

    #: Hash of the source commit (``None`` for a bare message).
    commit_hash: Optional[str]
    #: The commit message the intent was extracted from.
    commit_message: str
    #: Normalized action verb (e.g. ``"prevent"``), ``None`` when no known
    #: verb was found.
    action: Optional[str]
    #: Main thing affected by the commit (conservatively extracted; may
    #: fall back to the commit message itself).
    object: str
    #: Where the intent applies (explicit ``"for ..."`` phrase, otherwise
    #: the object phrase), ``None`` when nothing could be identified.
    scope: Optional[str]
    #: One of :data:`CATEGORIES`.
    category: str
    #: Domain keywords that justified the category (lowercased, in message
    #: order).
    keywords: List[str] = field(default_factory=list)
    #: Heuristic confidence in ``[0.0, 1.0]`` — see README. This is a
    #: deterministic score, not a statistical probability.
    confidence: float = 0.0
    #: Human-readable reasons for every extraction decision.
    evidence: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Return a plain, JSON-friendly dictionary of this intent."""
        return {
            "commit_hash": self.commit_hash,
            "commit_message": self.commit_message,
            "action": self.action,
            "object": self.object,
            "scope": self.scope,
            "category": self.category,
            "keywords": list(self.keywords),
            "confidence": self.confidence,
            "evidence": list(self.evidence),
        }


# ---------------------------------------------------------------------------
# Extractor
# ---------------------------------------------------------------------------


class IntentExtractor:
    """Extracts :class:`Intent` objects from commit messages.

    Usage::

        extractor = IntentExtractor()
        intent = extractor.extract(commit)                 # CommitInfo
        intent = extractor.extract_from_message("Prevent duplicate payment processing")
        intents = extractor.extract_all(history.commits)
    """

    def extract(
        self,
        commit: Union["CommitInfo", str],
        commit_hash: Optional[str] = None,
    ) -> Intent:
        """Extract an intent from a CommitInfo (or a raw message string)."""
        if isinstance(commit, str):
            return self.extract_from_message(commit, commit_hash=commit_hash)
        message = getattr(commit, "message", "") or ""
        resolved_hash = getattr(commit, "hash", None) or commit_hash
        return self.extract_from_message(message, commit_hash=resolved_hash)

    def extract_all(
        self, commits: Sequence[Union["CommitInfo", str]]
    ) -> List[Intent]:
        """Extract intents for a sequence of commits, in input order."""
        return [self.extract(commit) for commit in commits]

    def extract_from_message(
        self, message: str, commit_hash: Optional[str] = None
    ) -> Intent:
        """Extract an intent from a raw commit message (unit-test friendly)."""
        message = (message or "").strip()
        subject = self._subject_line(message)
        evidence: List[str] = []

        # 1. Action -------------------------------------------------------
        action, matched_token, action_end = self._extract_action(subject)
        if action is not None:
            evidence.append(f"action keyword: {matched_token}")
        else:
            evidence.append("no known action verb found")

        # 2. Category -----------------------------------------------------
        category, keywords, scores = self._extract_category(subject)
        evidence.extend(f"category keyword: {kw}" for kw in keywords)
        if category == "unknown":
            evidence.append(
                "no category keyword matched; default category: unknown"
            )
        elif len(scores) > 1:
            evidence.append(
                "category scores: "
                + ", ".join(f"{name}={value}" for name, value in scores)
            )

        # 3. Object and scope ---------------------------------------------
        obj, scope = self._extract_object_and_scope(
            subject, action, action_end
        )

        # 4. Confidence ----------------------------------------------------
        object_extracted = bool(obj) and obj.lower() != subject.lower()
        confidence = 0.0
        if action is not None:
            confidence += CONFIDENCE_WEIGHTS["action"]
        if category != "unknown":
            confidence += CONFIDENCE_WEIGHTS["category"]
        if object_extracted:
            confidence += CONFIDENCE_WEIGHTS["object"]
            evidence.append(f"object phrase: {obj}")
        else:
            evidence.append("object falls back to full commit message")
        if scope is not None:
            confidence += CONFIDENCE_WEIGHTS["scope"]
            evidence.append(f"scope phrase: {scope}")
        else:
            evidence.append("no scope phrase found")

        return Intent(
            commit_hash=commit_hash,
            commit_message=message,
            action=action,
            object=obj,
            scope=scope,
            category=category,
            keywords=keywords,
            confidence=round(confidence, 2),
            evidence=evidence,
        )

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _subject_line(message: str) -> str:
        """First non-empty line: commit subjects carry the intent."""
        for line in message.splitlines():
            if line.strip():
                return line.strip()
        return ""

    @staticmethod
    def _normalize_verb(token: str) -> Optional[str]:
        """Map a word to its canonical action verb, if any."""
        word = token.lower().strip("'\",.;:!?")
        if not word:
            return None
        if word in ACTION_VERBS:
            return word
        if word in ACTION_ALIASES:
            return ACTION_ALIASES[word]
        candidates: List[str] = []
        if len(word) > 5 and word.endswith("ing"):
            candidates += [word[:-3], word[:-3] + "e", word[:-3] + "y"]
        if len(word) > 4 and word.endswith("ed"):
            # Regular past tense, incl. the y -> i + ed spelling
            # ("simplified" -> "simplify").
            candidates += [word[:-1], word[:-2]]
            if word.endswith("ied"):
                candidates.append(word[:-3] + "y")
        if len(word) > 4 and word.endswith("es"):
            candidates.append(word[:-2])
            if word.endswith("ies"):
                candidates.append(word[:-3] + "y")
        if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
            candidates.append(word[:-1])
        for candidate in candidates:
            if candidate in ACTION_VERBS:
                return candidate
            if candidate in ACTION_ALIASES:
                return ACTION_ALIASES[candidate]
        return None

    @classmethod
    def _extract_action(
        cls, subject: str
    ) -> Tuple[Optional[str], Optional[str], int]:
        """Return ``(action, matched_token, character_end_index)``.

        The first word (in reading order) that normalizes to a known verb
        wins. ``character_end_index`` points just past the action (plus a
        ``"up"`` particle for e.g. "clean up"), so the object phrase can be
        cut from the subject.
        """
        matches = list(_WORD_RE.finditer(subject))
        for index, match in enumerate(matches):
            verb = cls._normalize_verb(match.group(1))
            if verb is None:
                continue
            end = match.end()
            # "clean up" keeps its particle out of the object phrase.
            if verb == "clean" and index + 1 < len(matches):
                following = matches[index + 1].group(1).lower()
                if following == "up":
                    end = matches[index + 1].end()
            return verb, match.group(1).lower(), end
        return None, None, 0

    @classmethod
    def _extract_category(
        cls, subject: str
    ) -> Tuple[str, List[str], List[Tuple[str, int]]]:
        """Return ``(category, keywords_in_message_order, scores)``.

        Scores count distinct matching signals per category; the highest
        score wins and ties are broken by the order of :data:`CATEGORIES`.
        """
        tokens = [m.group(1).lower() for m in _WORD_RE.finditer(subject)]
        matched: Dict[str, List[Tuple[int, str]]] = {}
        for category in CATEGORIES:
            if category == "unknown":
                continue
            hits: List[Tuple[int, str]] = []
            for signal in CATEGORY_SIGNALS[category]:
                position = cls._signal_position(signal, tokens)
                if position is not None:
                    hits.append((position, signal))
            if hits:
                matched[category] = hits

        if not matched:
            return "unknown", [], []

        scores = sorted(
            ((name, len(hits)) for name, hits in matched.items()),
            key=lambda item: (-item[1], CATEGORIES.index(item[0])),
        )
        winner = scores[0][0]
        keywords = [
            signal for _, signal in sorted(matched[winner], key=lambda h: h[0])
        ]
        return winner, keywords, scores

    @staticmethod
    def _signal_position(signal: str, tokens: List[str]) -> Optional[int]:
        """First token index where *signal* appears (word or phrase)."""
        parts = signal.split()
        width = len(parts)
        for index in range(len(tokens) - width + 1):
            if tokens[index : index + width] == parts:
                return index
        return None

    @staticmethod
    def _extract_object_and_scope(
        subject: str, action: Optional[str], action_end: int
    ) -> Tuple[str, Optional[str]]:
        """Conservatively split the subject into ``(object, scope)``.

        * Explicit ``"for <phrase>"`` yields the scope.
        * Leading determiners (``the``, ``every``, ...) are dropped from
          the object.
        * With a known action, the object itself doubles as the implicit
          scope; with no action and no ``for``-phrase, there is no scope.
        * If nothing can be identified, the subject itself is used as the
          object — never an invented phrase.
        """
        remainder = subject[action_end:] if action is not None else subject
        head, scope = subject, None
        for_match = _FOR_RE.search(remainder)
        if for_match is not None:
            head = remainder[: for_match.start()]
            scope = _clean_phrase(remainder[for_match.end() :])
        else:
            head = remainder

        obj = _strip_determiners(head)
        if not obj:
            # e.g. "Support for dark mode": the object IS the for-phrase.
            obj = scope if scope is not None else _clean_phrase(subject)
        if not obj:
            obj = _clean_phrase(subject)

        if scope is None and action is not None:
            scope = obj or None
        if scope is not None and not scope:
            scope = None
        return obj, scope


def _clean_phrase(text: str) -> str:
    """Trim whitespace and a single trailing sentence period."""
    return text.strip().rstrip(".").strip()


def _strip_determiners(text: str) -> str:
    """Repeatedly drop leading determiners/quantifiers."""
    result = text.strip()
    while True:
        stripped = _DETERMINER_RE.sub("", result, count=1).strip()
        if stripped == result:
            return _clean_phrase(result)
        result = stripped


def extract_intent(
    message: str, commit_hash: Optional[str] = None
) -> Intent:
    """Convenience wrapper: extract one intent from a raw message."""
    return IntentExtractor().extract_from_message(message, commit_hash=commit_hash)
