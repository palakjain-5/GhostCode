"""End-to-end validation of the complete GhostCode pipeline.

This suite validates GhostCode as an **integrated application** rather than
as six isolated components: Git history → Python code analysis → intent
extraction → intent drift detection → risk scoring → dashboard view model.

Contents:

* Full-pipeline integration on purpose-built repositories, with cross-stage
  linkage checks (every referenced commit hash really exists) and a
  stage ↔ dashboard-model equivalence check.
* Determinism: the same repository analyzed repeatedly must produce
  byte-identical results at every stage.
* The controlled PayGuard test repository validated end-to-end against its
  three documented scenarios (preserved 0/LOW, authentication 70/HIGH,
  payment 100/CRITICAL) — from commit messages to dashboard numbers.
* Error isolation: invalid paths, non-Git directories, repositories without
  commits, repositories without Python files, malformed and unreadable
  Python sources, and low-confidence intents — none may crash an unrelated
  stage.
* Dashboard states via Streamlit's ``AppTest``: successful analysis shows
  the pipeline's real numbers; every failure kind shows a readable error
  instead of a traceback (skipped when Streamlit is unavailable).

Fixture commit timestamps are pinned explicitly (``commit_date``) so
chronology never depends on wall-clock time: the drift detector orders
commits by ``(committed_at, hash)``, and equal timestamps would introduce
hash-dependent, effectively arbitrary ordering.

The PayGuard commit messages appear only here as validation cases, never in
production logic.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

import git
import pytest

from ghostcode import (
    GitHistoryAnalyzer,
    IntentDriftDetector,
    IntentExtractor,
    NoCommitsError,
    NotGitRepositoryError,
    PythonCodeAnalyzer,
    RepositoryNotFoundError,
    build_dashboard_model,
    calculate_risk,
    calculate_risk_report,
    extract_intent,
)

try:  # Streamlit is an optional, UI-only dependency.
    from streamlit.testing.v1 import AppTest

    HAVE_STREAMLIT = True
except ImportError:  # pragma: no cover - environment dependent
    HAVE_STREAMLIT = False

#: Repository root (the dashboard entry script lives next to tests/).
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_SCRIPT = PROJECT_ROOT / "dashboard.py"

#: Controlled, read-only test repository (see README.md).
PAYGUARD_PATH = Path.home() / "Projects" / "PayGuard"

#: Chronological messages of PayGuard's seven commits (validation only).
PAYGUARD_MESSAGES = [
    "Initial PayGuard application",
    "Add email validation for user registration",
    "Log every failed login attempt",
    "Prevent duplicate payment processing",
    "Improve authentication flow",
    "Refactor payment processing",
    "Clean up application structure",
]

AUTHOR = git.Actor("Ghost Tester", "ghost@example.com")

#: Pinned base timestamp — fixture chronology is independent of wall-clock.
BASE_DATE = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)

#: Connected validation behavior: defined *and* called (a bare definition
#: with no call site counts as "present but not connected" — weakened).
VALIDATORS_PY = """\
def validate_email(email):
    return "@" in email


def register(email):
    if not validate_email(email):
        raise ValueError("invalid email")
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _init_repo(path: Path) -> git.Repo:
    path.mkdir(parents=True, exist_ok=True)
    repo = git.Repo.init(path)
    # Pin the branch name so tests do not depend on init.defaultBranch.
    repo.git.symbolic_ref("HEAD", "refs/heads/main")
    return repo


def _commit(
    repo: git.Repo,
    message: str,
    files: Dict[str, str] | None = None,
    remove: List[str] | None = None,
    minute: int = 0,
) -> None:
    """Commit with a pinned timestamp (minutes after :data:`BASE_DATE`)."""
    working_dir = Path(repo.working_dir)
    for relative, content in (files or {}).items():
        target = working_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        repo.index.add([relative])
    for relative in remove or []:
        (working_dir / relative).unlink()
        repo.index.remove([relative])
    when = BASE_DATE + timedelta(minutes=minute)
    repo.index.commit(
        message, author=AUTHOR, committer=AUTHOR,
        author_date=when, commit_date=when,
    )


def run_pipeline(repository: Path) -> Dict[str, Any]:
    """Run stages 1–5 through their public APIs, exactly as the dashboard
    builder does, and return every stage's raw output."""
    history = GitHistoryAnalyzer(repository).analyze()
    code = PythonCodeAnalyzer(history.repository_path).analyze()
    intents = IntentExtractor().extract_all(history.commits)
    drift = IntentDriftDetector().detect(history.commits, code, intents)
    risk = calculate_risk_report(drift)
    return {
        "history": history,
        "code": code,
        "intents": intents,
        "drift": drift,
        "risk": risk,
    }


def _snapshot(stages: Dict[str, Any]) -> str:
    """JSON snapshot of every stage output (for determinism checks)."""
    history = stages["history"]
    return json.dumps(
        {
            "branch": history.branch,
            "commits": [
                (
                    c.hash, c.summary, c.author,
                    c.insertions, c.deletions, tuple(c.changed_files),
                )
                for c in history.commits
            ],
            "code": stages["code"].to_dict(),
            "intents": [i.to_dict() for i in stages["intents"]],
            "drift": stages["drift"].to_dict(),
            "risk": stages["risk"].to_dict(),
        },
        sort_keys=True,
    )


# ---------------------------------------------------------------------------
# Fixtures — purpose-built repositories for each validation scenario
# ---------------------------------------------------------------------------


@pytest.fixture
def lost_repo(tmp_path: Path) -> Path:
    """Validation behavior established, then removed → significant drift."""
    path = tmp_path / "lost"
    repo = _init_repo(path)
    _commit(
        repo,
        "Add email validation for user registration",
        files={
            "validators.py": VALIDATORS_PY,
            "app.py": (
                "from validators import validate_email\n"
                "\n"
                "\n"
                "def register(email):\n"
                "    return validate_email(email)\n"
            ),
        },
        minute=0,
    )
    _commit(
        repo,
        "Remove dead code",
        files={"app.py": "def register(email):\n    return email\n"},
        remove=["validators.py"],
        minute=1,
    )
    return path


@pytest.fixture
def preserved_repo(tmp_path: Path) -> Path:
    """A related later commit keeps the behavior → no drift, risk exactly 0."""
    path = tmp_path / "preserved"
    repo = _init_repo(path)
    _commit(repo, "Add email validation for user registration",
            files={"validators.py": VALIDATORS_PY}, minute=0)
    _commit(
        repo,
        "Clean up application structure",
        files={"validators.py": VALIDATORS_PY
               + "\n\n\ndef extra_check(value):\n    return bool(value)\n"},
        minute=1,
    )
    return path


@pytest.fixture
def multi_loss_repo(tmp_path: Path) -> Path:
    """Two independent behaviors (validation + logging) lost over history →
    multiple risk findings."""
    path = tmp_path / "multi"
    repo = _init_repo(path)
    _commit(
        repo,
        "Add email validation for user registration",
        files={
            "validators.py": VALIDATORS_PY,
            "app.py": (
                "from validators import validate_email\n"
                "\n"
                "\n"
                "def register(email):\n"
                "    return validate_email(email)\n"
            ),
        },
        minute=0,
    )
    _commit(
        repo,
        "Log every failed login attempt",
        files={
            "app.py": (
                "from validators import validate_email\n"
                "\n"
                "\n"
                "def audit_log(event):\n"
                "    return f'audit: {event}'\n"
                "\n"
                "\n"
                "def register(email):\n"
                "    return validate_email(email)\n"
                "\n"
                "\n"
                "def authenticate(user):\n"
                "    audit_log(f'login: {user}')\n"
                "    return user\n"
            ),
        },
        minute=1,
    )
    _commit(
        repo,
        "Remove dead code",
        files={
            "app.py": (
                "def register(email):\n"
                "    return email\n"
                "\n"
                "\n"
                "def authenticate(user):\n"
                "    return user\n"
            ),
        },
        remove=["validators.py"],
        minute=2,
    )
    return path


@pytest.fixture
def no_python_repo(tmp_path: Path) -> Path:
    """Commits exist but there is no Python code at all."""
    path = tmp_path / "docs-only"
    repo = _init_repo(path)
    _commit(
        repo,
        "Initial application scaffold",
        files={"README.md": "# Documentation only\n", "notes.txt": "no code\n"},
        minute=0,
    )
    return path


@pytest.fixture
def malformed_repo(tmp_path: Path) -> Path:
    """One valid and one syntactically broken Python file."""
    path = tmp_path / "malformed"
    repo = _init_repo(path)
    _commit(repo, "Initial application scaffold",
            files={"app.py": "def register(email):\n    return email\n"},
            minute=0)
    _commit(repo, "Add new module",
            files={"broken.py": "def broken(:\n    pass\n"}, minute=1)
    return path


@pytest.fixture
def low_conf_repo(tmp_path: Path) -> Path:
    """The first commit extracts below the drift threshold; the second is
    last, so nothing can be compared."""
    path = tmp_path / "lowconf"
    repo = _init_repo(path)
    _commit(repo, "Initial application scaffold",
            files={"app.py": "import os\n"}, minute=0)
    _commit(repo, "Refactor application structure",
            files={"app.py": "import os\nimport sys\n"}, minute=1)
    return path


@pytest.fixture
def no_commit_repo(tmp_path: Path) -> Path:
    """A freshly initialized repository with zero commits."""
    repo = _init_repo(tmp_path / "no-commits")
    return Path(repo.working_dir)


@pytest.fixture
def non_git_dir(tmp_path: Path) -> Path:
    path = tmp_path / "plain"
    path.mkdir()
    return path


# ---------------------------------------------------------------------------
# 1. Full-pipeline integration
# ---------------------------------------------------------------------------


def test_full_pipeline_chain_is_consistent(lost_repo: Path) -> None:
    stages = run_pipeline(lost_repo)
    history, code = stages["history"], stages["code"]
    intents, drift, risk = stages["intents"], stages["drift"], stages["risk"]

    # Stage 1 — Git history: chronological, deterministic inputs.
    assert history.total_commits == 2
    assert [c.summary for c in history.commits] == [
        "Add email validation for user registration",
        "Remove dead code",
    ]
    hashes = {c.hash for c in history.commits}

    # Stage 2 — Code analysis reflects the *current* tree (validators.py
    # was deleted in commit 2).
    assert code.total_python_files == 1
    assert code.total_functions >= 1
    assert code.total_errors == 0

    # Stage 3 — Intent extraction: one intent per commit, linked to real
    # commit hashes; the behavior-adding commit is confidently extracted.
    assert len(intents) == len(history.commits)
    for intent in intents:
        assert intent.commit_hash in hashes
        assert intent.category
        assert intent.evidence
    assert intents[0].confidence >= 0.5

    # Stage 4 — Drift detection: comparisons reference real commits and the
    # removed behavior is detected as drift.
    assert len(drift.results) >= 1
    for result in drift.results:
        assert result.original_commit_hash in hashes
        assert (
            result.later_commit_hash is None
            or result.later_commit_hash in hashes
        )
    drifted = [r for r in drift.results if r.drift_type != "none"]
    assert drifted, "removing validate_email must register as drift"

    # Stage 5 — Risk scoring: findings mirror the drift rows exactly, with
    # values inside the documented ranges.
    assert len(risk.findings) == len(drifted)
    for finding in risk.findings:
        assert 0 <= finding.score <= 100
        assert finding.severity in ("LOW", "MEDIUM", "HIGH", "CRITICAL")
        assert finding.drift_type != "none"
        assert finding.factors, "a scored finding must show its factors"
        assert finding.explanation
        assert finding.raw_score >= finding.score
    assert risk.overall_risk == max(f.score for f in risk.findings)

    # Stage 6 — The dashboard model presents the very same chain.
    model = build_dashboard_model(lost_repo)
    assert model.ok
    assert model.git["total_commits"] == history.total_commits
    assert model.code["total_python_files"] == code.total_python_files
    assert model.drift == drift.to_dict()
    assert model.risk == risk.to_dict()


def test_dashboard_model_equals_independent_pipeline(lost_repo: Path) -> None:
    """The builder's output must equal an independent run of stages 1–5 —
    the UI can never disagree with the analysis stages."""
    stages = run_pipeline(lost_repo)
    model = build_dashboard_model(lost_repo)

    assert model.ok and model.error is None
    assert model.git["repository_path"] == str(
        stages["history"].repository_path
    )
    assert model.git["branch"] == stages["history"].branch
    assert [c["hash"] for c in model.git["commits"]] == [
        c.hash for c in stages["history"].commits
    ]
    assert model.code["total_python_files"] == stages["code"].total_python_files
    assert model.code["total_functions"] == stages["code"].total_functions
    assert model.code["total_errors"] == stages["code"].total_errors
    assert len(model.code["files"]) == len(stages["code"].files)
    assert model.intents == [i.to_dict() for i in stages["intents"]]
    assert model.drift == stages["drift"].to_dict()
    assert model.risk == stages["risk"].to_dict()
    # The whole model survives a JSON round-trip.
    assert json.loads(json.dumps(model.to_dict())) == model.to_dict()


def test_pipeline_is_deterministic(lost_repo: Path) -> None:
    """Three full runs over the same repository are byte-identical."""
    snapshots = {_snapshot(run_pipeline(lost_repo)) for _ in range(3)}
    assert len(snapshots) == 1


def test_dashboard_builder_is_deterministic(lost_repo: Path) -> None:
    """Three dashboard builds over the same repository are byte-identical."""
    payloads = {
        json.dumps(build_dashboard_model(lost_repo).to_dict(), sort_keys=True)
        for _ in range(3)
    }
    assert len(payloads) == 1


# ---------------------------------------------------------------------------
# 2. PayGuard end-to-end (controlled, read-only test repository)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not PAYGUARD_PATH.exists(), reason="PayGuard test repository not found"
)
def test_payguard_end_to_end_counts() -> None:
    """The complete pipeline over PayGuard yields the documented results."""
    stages = run_pipeline(PAYGUARD_PATH)
    history, code = stages["history"], stages["code"]
    intents, drift, risk = stages["intents"], stages["drift"], stages["risk"]

    # Git history — exactly the seven documented commits, in order.
    assert history.total_commits == 7
    assert [c.summary for c in history.commits] == PAYGUARD_MESSAGES
    assert history.branch == "main"

    # Code — a real, clean Python project.
    assert code.total_python_files >= 1
    assert code.total_errors == 0

    # Intent extraction — one intent per commit.
    assert len(intents) == 7
    assert all(i.category for i in intents)
    assert all(i.commit_hash for i in intents)

    # Drift detection — three comparisons; two report actual drift, both
    # significant (medium/high).
    assert len(drift.results) == 3
    assert len(drift.drifted) == 2
    assert len(drift.significant) == 2

    # Risk scoring — two findings, both high-risk or above, one critical,
    # overall risk 100; findings mirror the drifted comparisons.
    assert risk.total_findings == 2
    assert risk.high_risk_findings == 2
    assert risk.critical_findings == 1
    assert risk.overall_risk == 100
    assert len(risk.findings) == len(drift.drifted)
    assert {f.affected_area for f in risk.findings} == {
        "authentication",
        "payment",
    }

    # Dashboard — the view model mirrors every stage.
    model = build_dashboard_model(PAYGUARD_PATH)
    assert model.ok
    assert model.git["total_commits"] == 7
    assert len(model.intents) == 7
    assert model.drift == drift.to_dict()
    assert model.risk == risk.to_dict()


@pytest.mark.skipif(
    not PAYGUARD_PATH.exists(), reason="PayGuard test repository not found"
)
def test_payguard_scenarios_abc() -> None:
    """The three intentional scenarios score exactly as documented."""
    stages = run_pipeline(PAYGUARD_PATH)
    drift, risk = stages["drift"], stages["risk"]

    def row(prefix: str) -> Any:
        matches = [
            r for r in drift.results
            if r.original_commit_message.startswith(prefix)
        ]
        assert len(matches) == 1, f"expected exactly one row for {prefix!r}"
        return matches[0]

    scenario_b = row("Log every failed login")     # partial logging drift
    scenario_c = row("Prevent duplicate payment")  # lost prevention guard

    score_b = calculate_risk(scenario_b)
    score_c = calculate_risk(scenario_c)

    # Scenario A — preserved behavior: the report contains no validation
    # finding at all, and every preserved comparison scores exactly 0/LOW
    # with no positive factors (no false positive).
    preserved = [r for r in drift.results if r.drift_type == "none"]
    assert preserved, "expected a preserved comparison row (scenario A)"
    for preserved_row in preserved:
        score = calculate_risk(preserved_row)
        assert (score.score, score.severity) == (0, "LOW")
        assert not any(f.points > 0 for f in score.factors)
    assert not any(f.affected_area == "validation" for f in risk.findings)
    score_a = calculate_risk(preserved[0])

    # Scenario B — partial logging drift: the documented 70 / HIGH.
    assert (score_b.score, score_b.severity) == (70, "HIGH")

    # Scenario C — lost duplicate-payment guard: capped at 100 / CRITICAL.
    assert (score_c.score, score_c.severity) == (100, "CRITICAL")
    assert score_c.raw_score >= 100  # the cap engaged; raw total preserved

    # Strict ordering: preserved < authentication < payment.
    assert score_a.score < score_b.score < score_c.score
    assert {f.affected_area for f in risk.findings} == {
        "authentication",
        "payment",
    }


@pytest.mark.skipif(
    not PAYGUARD_PATH.exists(), reason="PayGuard test repository not found"
)
def test_payguard_dashboard_numbers() -> None:
    """The dashboard displays PayGuard's documented headline numbers."""
    model = build_dashboard_model(PAYGUARD_PATH)

    assert model.ok and model.error is None
    assert model.git["total_commits"] == 7
    assert len(model.intents) == 7
    assert model.drift["total_comparisons"] == 3
    assert model.drift["drifted"] == 2
    assert model.risk["total_findings"] == 2
    assert model.risk["high_risk_findings"] == 2
    assert model.risk["critical_findings"] == 1
    assert model.risk["overall_risk"] == 100
    # Scenario B (70) and scenario C (100); scenario A adds no finding.
    assert sorted(f["score"] for f in model.risk["findings"]) == [70, 100]
    # Repeated builds stay identical.
    assert model.to_dict() == build_dashboard_model(PAYGUARD_PATH).to_dict()


# ---------------------------------------------------------------------------
# 3. Edge cases and error isolation
# ---------------------------------------------------------------------------


def test_invalid_repository_path_fails_gracefully(tmp_path: Path) -> None:
    missing = tmp_path / "nope"

    # Stage 1 raises its documented error...
    with pytest.raises(RepositoryNotFoundError):
        GitHistoryAnalyzer(missing).analyze()

    # ...and the dashboard captures it instead of crashing any stage.
    model = build_dashboard_model(missing)
    assert not model.ok
    assert "RepositoryNotFoundError" in (model.error or "")
    assert model.git == {} and model.code == {}
    assert model.intents == [] and model.drift == {} and model.risk == {}
    assert json.dumps(model.to_dict())  # still serializable


def test_non_git_directory_fails_gracefully(non_git_dir: Path) -> None:
    with pytest.raises(NotGitRepositoryError):
        GitHistoryAnalyzer(non_git_dir).analyze()

    model = build_dashboard_model(non_git_dir)
    assert not model.ok
    assert "NotGitRepositoryError" in (model.error or "")


def test_repository_without_commits_fails_gracefully(
    no_commit_repo: Path,
) -> None:
    with pytest.raises(NoCommitsError):
        GitHistoryAnalyzer(no_commit_repo).analyze()

    model = build_dashboard_model(no_commit_repo)
    assert not model.ok
    assert "NoCommitsError" in (model.error or "")


def test_repository_without_python_files_completes(
    no_python_repo: Path,
) -> None:
    stages = run_pipeline(no_python_repo)

    # Git and intent stages still work; code analysis is legitimately empty.
    assert stages["history"].total_commits == 1
    assert stages["code"].total_python_files == 0
    assert stages["code"].files == []
    assert stages["code"].total_errors == 0
    assert len(stages["intents"]) == 1

    # Drift/risk degrade to "nothing to compare" instead of crashing.
    drifted = [
        r for r in stages["drift"].results if r.drift_type != "none"
    ]
    assert len(stages["risk"].findings) == len(drifted)

    # The dashboard still produces a complete, successful model.
    model = build_dashboard_model(no_python_repo)
    assert model.ok
    assert model.code["total_python_files"] == 0
    assert model.risk["overall_risk"] == max(
        (f["score"] for f in model.risk["findings"]), default=0
    )


def test_malformed_python_file_does_not_break_pipeline(
    malformed_repo: Path,
) -> None:
    stages = run_pipeline(malformed_repo)
    code = stages["code"]

    # The broken file is reported; the valid one is still analyzed.
    assert code.total_errors == 1
    assert code.errors[0].error_type == "invalid-source"
    assert code.errors[0].file_path.name == "broken.py"
    assert code.total_python_files == 1

    # Every downstream stage still completes.
    assert len(stages["intents"]) == 2
    assert isinstance(stages["drift"].results, list)
    assert isinstance(stages["risk"].findings, list)

    # The dashboard surfaces the parse error without failing.
    model = build_dashboard_model(malformed_repo)
    assert model.ok
    assert model.code["total_errors"] == 1
    assert model.code["errors"][0]["error_type"] == "invalid-source"
    json.dumps(model.to_dict())


def test_unreadable_python_file_does_not_break_pipeline(
    tmp_path: Path,
) -> None:
    path = tmp_path / "unreadable"
    repo = _init_repo(path)
    _commit(repo, "Initial application scaffold",
            files={"app.py": "def register(email):\n    return email\n"},
            minute=0)

    locked = path / "locked.py"
    locked.write_text("def secret():\n    return 42\n")
    locked.chmod(0o000)
    if os.access(locked, os.R_OK):  # e.g. running as root
        locked.chmod(0o644)
        pytest.skip("cannot create an unreadable file (running as root)")

    try:
        stages = run_pipeline(path)
        code = stages["code"]

        # The unreadable file is reported; the readable one is unaffected.
        assert code.total_errors == 1
        assert code.errors[0].error_type == "unreadable"
        assert code.total_python_files == 1

        # Downstream stages are unaffected, and the dashboard still works.
        assert len(stages["intents"]) == 1
        model = build_dashboard_model(path)
        assert model.ok
        assert model.code["total_errors"] == 1
    finally:
        locked.chmod(0o644)


def test_low_confidence_intent_produces_no_drift(
    low_conf_repo: Path,
) -> None:
    stages = run_pipeline(low_conf_repo)
    detector = IntentDriftDetector()

    # The weak original really is below the threshold...
    assert stages["intents"][0].confidence < detector.min_intent_confidence
    # ...so nothing is compared, and risk is empty.
    assert stages["drift"].results == []
    assert stages["risk"].findings == []
    assert stages["risk"].total_findings == 0
    assert stages["risk"].overall_risk == 0

    model = build_dashboard_model(low_conf_repo)
    assert model.ok
    assert model.drift["total_comparisons"] == 0
    assert model.risk["overall_risk"] == 0


def test_no_drift_yields_empty_risk_report(preserved_repo: Path) -> None:
    stages = run_pipeline(preserved_repo)
    drift, risk = stages["drift"], stages["risk"]

    # A comparison exists (the commits are related) but reports no drift.
    assert len(drift.results) >= 1
    assert all(r.drift_type == "none" for r in drift.results)
    assert drift.drifted == []

    # No drift → no findings; repository risk is exactly zero.
    assert risk.findings == []
    assert risk.total_findings == 0
    assert risk.high_risk_findings == 0
    assert risk.overall_risk == 0

    model = build_dashboard_model(preserved_repo)
    assert model.ok
    assert model.risk["findings"] == []
    assert model.risk["overall_risk"] == 0


def test_significant_drift_yields_high_risk_findings(
    lost_repo: Path,
) -> None:
    stages = run_pipeline(lost_repo)
    drift, risk = stages["drift"], stages["risk"]

    assert len(drift.significant) >= 1
    assert risk.total_findings >= 1
    assert risk.total_findings == len(
        [r for r in drift.results if r.drift_type != "none"]
    )
    for finding in risk.findings:
        assert finding.severity in ("MEDIUM", "HIGH", "CRITICAL")
    assert risk.high_risk_findings == sum(
        f.severity in ("HIGH", "CRITICAL") for f in risk.findings
    )


def test_multiple_risk_findings(multi_loss_repo: Path) -> None:
    stages = run_pipeline(multi_loss_repo)
    drift, risk = stages["drift"], stages["risk"]

    drifted = [r for r in drift.results if r.drift_type != "none"]
    assert len(drifted) == 2
    assert risk.total_findings == 2
    # One finding per lost behavior, each attributed to its own area.
    assert len({f.affected_area for f in risk.findings}) == 2
    assert risk.overall_risk == max(f.score for f in risk.findings)
    assert risk.high_risk_findings == sum(
        f.severity in ("HIGH", "CRITICAL") for f in risk.findings
    )

    # The dashboard mirrors both findings.
    model = build_dashboard_model(multi_loss_repo)
    assert model.ok
    assert len(model.risk["findings"]) == 2
    assert model.risk["overall_risk"] == risk.overall_risk


# ---------------------------------------------------------------------------
# 4. Dashboard states (Streamlit AppTest)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not HAVE_STREAMLIT, reason="streamlit is not installed")
def test_dashboard_success_state_shows_pipeline_numbers(
    monkeypatch: pytest.MonkeyPatch, lost_repo: Path
) -> None:
    """After a successful analysis the Overview metrics equal the model —
    the UI displays the pipeline's real numbers, not placeholders."""
    expected = build_dashboard_model(lost_repo)
    assert expected.ok
    monkeypatch.setenv("GHOSTCODE_REPOSITORY", str(lost_repo))

    at = AppTest.from_file(str(DASHBOARD_SCRIPT)).run(timeout=30)

    assert not at.exception
    assert not any("Could not analyze" in e.value for e in at.error)
    assert len(at.tabs) == 6

    overview = list(at.metric)[:6]
    assert [m.label for m in overview] == [
        "Commits",
        "Python files",
        "Intents",
        "Comparisons",
        "Risk findings",
        "Overall risk",
    ]
    assert [m.value for m in overview] == [
        str(expected.git["total_commits"]),
        str(expected.code["total_python_files"]),
        str(len(expected.intents)),
        str(expected.drift["total_comparisons"]),
        str(expected.risk["total_findings"]),
        str(expected.risk["overall_risk"]),
    ]


@pytest.mark.skipif(not HAVE_STREAMLIT, reason="streamlit is not installed")
def test_dashboard_error_states_do_not_crash(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    non_git_dir: Path,
    no_commit_repo: Path,
) -> None:
    """Every failure kind renders a readable error and no analysis tabs."""
    failing = {
        "invalid path": str(tmp_path / "missing-repository"),
        "non-Git directory": str(non_git_dir),
        "repository without commits": str(no_commit_repo),
    }
    for description, repository in failing.items():
        monkeypatch.setenv("GHOSTCODE_REPOSITORY", repository)
        at = AppTest.from_file(str(DASHBOARD_SCRIPT)).run(timeout=30)

        assert not at.exception, description
        assert at.error, description
        assert "Could not analyze" in at.error[0].value, description
        assert len(list(at.tabs)) == 0, description
        assert len(list(at.metric)) == 0, description
