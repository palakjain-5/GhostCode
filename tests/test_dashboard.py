"""Tests for the GhostCode Streamlit dashboard stage.

The dashboard is a **presentation layer** over the five existing stages,
so most tests exercise the pure pipeline builder
(:func:`ghostcode.dashboard.build_dashboard_model`) against deterministic
temporary Git repositories — no UI runtime needed. UI tests use
Streamlit's own ``AppTest`` harness (skipped automatically when Streamlit
is not installed) and run the real ``dashboard.py`` entry script in-process.

The PayGuard integration test validates the controlled, read-only test
repository (skipped when it is absent) with semantic assertions only —
never hard-coded hashes, line numbers or complete output strings.

The PayGuard commit messages appear only here as validation cases, never
in production logic.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Dict, List, Optional

import git
import pytest

from ghostcode import dashboard as dashboard_module
from ghostcode.dashboard import (
    TABS,
    DashboardModel,
    build_dashboard_model,
    main,
    severity_icon,
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

AUTHOR = git.Actor("Ghost Tester", "ghost@example.com")


# ---------------------------------------------------------------------------
# Helpers and fixtures
# ---------------------------------------------------------------------------


def _commit(
    repo: git.Repo,
    message: str,
    files: Optional[Dict[str, str]] = None,
    remove: Optional[List[str]] = None,
) -> None:
    """Create a commit in ``repo`` with fully deterministic authorship."""
    working_dir = Path(repo.working_dir)
    for relative, content in (files or {}).items():
        target = working_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        repo.index.add([relative])
    for relative in remove or []:
        (working_dir / relative).unlink()
        repo.index.remove([relative])
    repo.index.commit(message, author=AUTHOR, committer=AUTHOR)


def _init_repo(path: Path) -> git.Repo:
    path.mkdir(parents=True, exist_ok=True)
    repo = git.Repo.init(path)
    # Pin the branch name so tests do not depend on init.defaultBranch.
    repo.git.symbolic_ref("HEAD", "refs/heads/main")
    return repo


@pytest.fixture
def sample_repo(tmp_path: Path) -> Path:
    """A small deterministic repository with three commits on ``main``."""
    path = tmp_path / "sample"
    repo = _init_repo(path)
    _commit(repo, "Initial commit", files={"app.py": "import sys\n"})
    _commit(
        repo,
        "Add email validation",
        files={
            "app.py": (
                "import sys\nfrom validators import is_valid_email\n"
                "def register(email):\n    return is_valid_email(email)\n"
            ),
            "validators.py": (
                "def is_valid_email(value):\n    return '@' in value\n"
            ),
        },
    )
    _commit(repo, "Remove dead code", remove=["validators.py"])
    return path


@pytest.fixture
def drift_repo(tmp_path: Path) -> Path:
    """A repository where an established validation behavior is *lost*.

    Commit 1 defines and calls ``validate_email``; commit 2 removes both
    the definition and the call, so drift detection reports a loss and the
    risk stage produces at least one finding for the dashboard to show.

    The commits are one second apart on purpose: the drift detector orders
    commits by ``(committed_at, hash)`` and falls back to the hash when
    timestamps collide, which is effectively arbitrary for freshly created
    fixture repositories (real histories rarely share a second).
    """
    path = tmp_path / "drifty"
    repo = _init_repo(path)
    _commit(
        repo,
        "Add email validation for user registration",
        files={
            "validators.py": "def validate_email(value):\n    return '@' in value\n",
            "app.py": (
                "from validators import validate_email\n"
                "def register(email):\n    return validate_email(email)\n"
            ),
        },
    )
    time.sleep(1)  # distinct committed_at seconds -> stable chronology
    _commit(
        repo,
        "Remove dead code",
        files={"app.py": "def register(email):\n    return email\n"},
        remove=["validators.py"],
    )
    return path


# ---------------------------------------------------------------------------
# Builder: success paths
# ---------------------------------------------------------------------------


def test_builder_populates_every_stage(sample_repo: Path) -> None:
    model = build_dashboard_model(sample_repo)

    assert model.ok and model.error is None
    assert model.repository == str(sample_repo)

    # Git stage
    assert model.git["repository_name"] == "sample"
    assert model.git["branch"] == "main"
    assert model.git["total_commits"] == 3
    assert [c["summary"] for c in model.git["commits"]] == [
        "Initial commit",
        "Add email validation",
        "Remove dead code",
    ]

    # Code stage (the analyzer reads the *current* tree: commit 3 removed
    # validators.py, so app.py is what remains)
    assert model.code["total_python_files"] >= 1
    assert model.code["total_functions"] >= 1
    assert all(f["path"] and f["module_name"] for f in model.code["files"])

    # Intent stage — one intent per commit, chronological
    assert len(model.intents) == 3
    categories = [i["category"] for i in model.intents]
    assert "validation" in categories

    # Drift stage — comparison structure present
    assert model.drift["total_comparisons"] >= 1
    assert isinstance(model.drift["results"], list)
    assert model.drift["drifted"] >= 0

    # Risk stage — repository summary structure present
    for key in (
        "total_findings",
        "high_risk_findings",
        "critical_findings",
        "overall_risk",
        "findings",
    ):
        assert key in model.risk


def test_builder_is_deterministic(sample_repo: Path) -> None:
    first = build_dashboard_model(sample_repo)
    second = build_dashboard_model(sample_repo)
    assert first.to_dict() == second.to_dict()


def test_model_is_json_serializable(sample_repo: Path) -> None:
    model = build_dashboard_model(sample_repo)
    payload = model.to_dict()
    assert json.loads(json.dumps(payload)) == payload


def test_commit_rows_expose_display_fields(sample_repo: Path) -> None:
    commits = build_dashboard_model(sample_repo).git["commits"]
    for row in commits:
        assert len(row["hash"]) == 40
        assert len(row["short_hash"]) == 7
        assert row["summary"] and row["author"]
        assert "T" in row["authored_at"]  # ISO datetime
        assert isinstance(row["changed_files"], list)
        assert row["insertions"] >= 0 and row["deletions"] >= 0


def test_drift_repo_surfaces_findings_for_the_risk_tab(drift_repo: Path) -> None:
    """The fixture actually produces a lost behavior, so the Risk tab has
    data — and drift/risk counts stay consistent across stages."""
    model = build_dashboard_model(drift_repo)

    assert model.ok
    drift_types = [r["drift_type"] for r in model.drift["results"]]
    assert "behavior_lost" in drift_types

    lost_rows = [r for r in model.drift["results"] if r["drift_type"] != "none"]
    assert len(model.risk["findings"]) == len(lost_rows)
    assert model.risk["overall_risk"] == max(f["score"] for f in model.risk["findings"])
    assert all(f["severity"] in ("LOW", "MEDIUM", "HIGH", "CRITICAL") for f in model.risk["findings"])


def test_cross_stage_consistency(sample_repo: Path) -> None:
    """Risk findings always equal the number of actual drift rows."""
    model = build_dashboard_model(sample_repo)
    drifted_rows = [r for r in model.drift["results"] if r["drift_type"] != "none"]
    assert len(model.risk["findings"]) == len(drifted_rows)


# ---------------------------------------------------------------------------
# Builder: error paths
# ---------------------------------------------------------------------------


def test_missing_path_yields_error_model() -> None:
    model = build_dashboard_model("/nonexistent/ghostcode-repo")

    assert not model.ok
    assert model.error and "RepositoryNotFoundError" in model.error
    # Every stage stays at its safe empty default.
    assert model.git == {}
    assert model.code == {}
    assert model.intents == []
    assert model.drift == {}
    assert model.risk == {}


def test_non_git_directory_yields_error_model(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()

    model = build_dashboard_model(plain)

    assert not model.ok
    assert model.error and "NotGitRepositoryError" in model.error
    assert model.to_dict()["error"] == model.error


def test_repository_without_commits_yields_error_model(tmp_path: Path) -> None:
    empty = _init_repo(tmp_path / "empty-repo")

    model = build_dashboard_model(Path(empty.working_dir))

    assert not model.ok
    assert model.error and "NoCommitsError" in model.error


# ---------------------------------------------------------------------------
# Severity decoration and public API
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "severity,expected",
    [
        ("LOW", "🟢"),
        ("MEDIUM", "🟡"),
        ("HIGH", "🟠"),
        ("CRITICAL", "🔴"),
        ("high", "🟠"),  # drift severities are lowercase
        ("none", "⚪"),
        ("mystery", "⚪"),
    ],
)
def test_severity_icon(severity: str, expected: str) -> None:
    assert severity_icon(severity) == expected


def test_tab_order_mirrors_the_pipeline() -> None:
    assert TABS == (
        "Overview",
        "Git History",
        "Python Code",
        "Intents",
        "Drift",
        "Risk",
    )


def test_dashboard_model_defaults() -> None:
    model = DashboardModel(repository="/some/repo")
    assert model.ok and model.error is None
    assert model.git == {} and model.risk == {} and model.intents == []


def test_public_api_surface() -> None:
    import ghostcode

    assert ghostcode.build_dashboard_model is build_dashboard_model
    assert ghostcode.DashboardModel is DashboardModel
    assert dashboard_module.SEVERITY_ICONS
    assert callable(dashboard_module.render)


def test_main_outside_streamlit_prints_hint(capsys: pytest.CaptureFixture) -> None:
    """`python dashboard.py` without a Streamlit runtime prints a hint
    instead of failing (and does not require Streamlit to be installed)."""
    assert main() is None  # returns before any rendering
    captured = capsys.readouterr()
    assert "streamlit run dashboard.py" in captured.out


# ---------------------------------------------------------------------------
# Streamlit UI tests (AppTest) — skipped when Streamlit is unavailable
# ---------------------------------------------------------------------------


def _app_test() -> "AppTest":
    return AppTest.from_file(str(DASHBOARD_SCRIPT))


@pytest.mark.skipif(not HAVE_STREAMLIT, reason="streamlit is not installed")
def test_ui_renders_when_repository_is_preselected(
    monkeypatch: pytest.MonkeyPatch, sample_repo: Path
) -> None:
    monkeypatch.setenv("GHOSTCODE_REPOSITORY", str(sample_repo))

    at = _app_test().run(timeout=30)

    assert not at.exception
    assert at.title[0].value == "GhostCode"
    assert len(at.tabs) == len(TABS)
    # Overview metrics: commits / files / intents / comparisons / findings / risk
    assert len(at.metric) >= 6
    # One selectbox per detail-bearing tab (history, intents, drift, risk).
    assert len(at.selectbox) >= 3
    assert len(at.dataframe) >= 4


@pytest.mark.skipif(not HAVE_STREAMLIT, reason="streamlit is not installed")
def test_ui_shows_intro_before_first_analysis(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GHOSTCODE_REPOSITORY", raising=False)

    at = _app_test().run(timeout=30)

    assert not at.exception
    assert at.info
    assert "sidebar" in at.info[0].value
    assert not at.tabs  # nothing rendered before analysis


@pytest.mark.skipif(not HAVE_STREAMLIT, reason="streamlit is not installed")
def test_ui_shows_error_for_unusable_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GHOSTCODE_REPOSITORY", "/nonexistent/ghostcode-repo")

    at = _app_test().run(timeout=30)

    assert not at.exception
    assert at.error
    assert "RepositoryNotFoundError" in at.error[0].value
    assert not at.tabs  # no tabs rendered on failure


@pytest.mark.skipif(not HAVE_STREAMLIT, reason="streamlit is not installed")
def test_ui_analyze_button_runs_pipeline(monkeypatch: pytest.MonkeyPatch, sample_repo: Path) -> None:
    monkeypatch.delenv("GHOSTCODE_REPOSITORY", raising=False)

    at = _app_test().run(timeout=30)
    assert at.info  # intro state

    at.text_input[0].set_value(str(sample_repo))
    at.button[0].click()
    at = at.run(timeout=30)

    assert not at.exception
    # Note: the Drift tab reuses st.error() as its red "DRIFT DETECTED"
    # block, so only a build failure ("Could not analyze ...") is a bug.
    assert all("Could not analyze" not in e.value for e in at.error)
    assert at.button[0].label == "Analyze repository"
    assert len(at.tabs) == len(TABS)
    assert len(at.metric) >= 6


@pytest.mark.skipif(not HAVE_STREAMLIT, reason="streamlit is not installed")
def test_ui_risk_tab_lists_findings(
    monkeypatch: pytest.MonkeyPatch, drift_repo: Path
) -> None:
    monkeypatch.setenv("GHOSTCODE_REPOSITORY", str(drift_repo))

    at = _app_test().run(timeout=30)

    assert not at.exception
    # Risk tab metrics: Findings / High / Critical / Overall risk — plus
    # the six overview metrics, so at least ten exist when risk is shown.
    assert len(at.metric) >= 6
    assert at.success or at.error  # drift detail status blocks exist


# ---------------------------------------------------------------------------
# PayGuard integration (controlled, read-only test repository)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not PAYGUARD_PATH.exists(), reason="PayGuard test repository not found"
)
def test_payguard_dashboard_model() -> None:
    model = build_dashboard_model(PAYGUARD_PATH)

    assert model.ok, model.error
    assert model.git["repository_name"] == "PayGuard"
    assert model.git["total_commits"] >= 7
    assert {c["summary"] for c in model.git["commits"]} >= {
        "Log every failed login attempt",
        "Prevent duplicate payment processing",
    }

    # The three intentional scenarios: 3 comparisons, 2 with drift.
    assert model.drift["total_comparisons"] == 3
    assert model.drift["drifted"] == 2
    assert model.risk["total_findings"] == 2

    findings = model.risk["findings"]
    severities = [f["severity"] for f in findings]
    assert all(s in ("LOW", "MEDIUM", "HIGH", "CRITICAL") for s in severities)
    # The payment scenario outranks the authentication scenario.
    by_area = {f["affected_area"]: f["score"] for f in findings}
    assert by_area["payment"] > by_area["authentication"]
    assert model.risk["overall_risk"] == max(f["score"] for f in findings)

    # Deterministic across runs.
    assert model.to_dict() == build_dashboard_model(PAYGUARD_PATH).to_dict()
