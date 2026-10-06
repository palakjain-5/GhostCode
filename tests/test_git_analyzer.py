"""Tests for the GhostCode Git history analyzer.

The suite is split into two groups:

* Unit tests that run against small temporary Git repositories created on
  the fly — fully deterministic, no external dependencies.
* An integration test against the controlled PayGuard test repository
  (skipped automatically when that repository is not present).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import git
import pytest

from ghostcode.git_analyzer import (
    CommitInfo,
    GitHistoryAnalyzer,
    NoCommitsError,
    NotGitRepositoryError,
    RepositoryHistory,
    RepositoryNotFoundError,
)

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

AUTHOR = git.Actor("Ghost Tester", "ghost@example.com")

#: Controlled, read-only test repository (see README.md).
PAYGUARD_PATH = Path.home() / "Projects" / "PayGuard"

#: Commit messages that must exist in PayGuard's history. Hashes are
#: deliberately not asserted: they differ between environments.
EXPECTED_PAYGUARD_MESSAGES = [
    "Initial PayGuard application",
    "Add email validation for user registration",
    "Log every failed login attempt",
    "Prevent duplicate payment processing",
    "Improve authentication flow",
    "Refactor payment processing",
    "Clean up application structure",
]

#: Chronological messages of the temporary sample repository.
EXPECTED_SAMPLE_MESSAGES = [
    "Initial commit",
    "Add email validation",
    "Remove dead code",
]


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


@pytest.fixture
def sample_repo(tmp_path: Path) -> Path:
    """A small deterministic Git repository with three commits on ``main``.

    Ground truth per commit:

    1. "Initial commit"          app.py              +1  -0
    2. "Add email validation"    app.py, validators.py  +3  -0
    3. "Remove dead code"        validators.py       +0  -2
    """
    path = tmp_path / "sample"
    repo = git.Repo.init(path)
    # Pin the branch name so the test does not depend on the host's
    # `init.defaultBranch` configuration.
    repo.git.symbolic_ref("HEAD", "refs/heads/main")

    _commit(repo, "Initial commit", files={"app.py": "import sys\n"})
    _commit(
        repo,
        "Add email validation",
        files={
            "app.py": "import sys\nfrom validators import is_valid_email\n",
            "validators.py": "def is_valid_email(value):\n    return '@' in value\n",
        },
    )
    _commit(repo, "Remove dead code", remove=["validators.py"])
    return path


# ---------------------------------------------------------------------------
# 1-3. Repository validation and error handling
# ---------------------------------------------------------------------------


def test_valid_repository_loads_successfully(sample_repo: Path) -> None:
    analyzer = GitHistoryAnalyzer(sample_repo)
    assert analyzer.is_valid_repository() is True

    history = analyzer.analyze()
    assert isinstance(history, RepositoryHistory)
    assert history.total_commits == 3
    assert len(history.commits) == 3
    assert all(isinstance(c, CommitInfo) for c in history.commits)
    assert history.repository_name == "sample"
    assert history.repository_path.is_absolute()


def test_non_existent_path_raises_clear_error(tmp_path: Path) -> None:
    analyzer = GitHistoryAnalyzer(tmp_path / "does-not-exist")
    assert analyzer.is_valid_repository() is False

    with pytest.raises(RepositoryNotFoundError, match="does not exist"):
        analyzer.analyze()


def test_non_git_directory_raises_clear_error(tmp_path: Path) -> None:
    plain = tmp_path / "plain_directory"
    plain.mkdir()
    (plain / "notes.txt").write_text("just a folder, not a repository\n")

    analyzer = GitHistoryAnalyzer(plain)
    assert analyzer.is_valid_repository() is False

    with pytest.raises(NotGitRepositoryError, match="not a Git repository"):
        analyzer.analyze()


def test_repository_without_commits_raises_clear_error(tmp_path: Path) -> None:
    path = tmp_path / "empty"
    git.Repo.init(path)

    analyzer = GitHistoryAnalyzer(path)
    # A fresh repository is still a valid repository...
    assert analyzer.is_valid_repository() is True
    # ...but it has no history to analyze.
    with pytest.raises(NoCommitsError, match="no commits"):
        analyzer.analyze()


# ---------------------------------------------------------------------------
# 4. Branch detection (including detached HEAD)
# ---------------------------------------------------------------------------


def test_current_branch_is_detected(sample_repo: Path) -> None:
    assert GitHistoryAnalyzer(sample_repo).get_current_branch() == "main"

    history = GitHistoryAnalyzer(sample_repo).analyze()
    assert history.branch == "main"


def test_detached_head_is_handled_gracefully(sample_repo: Path) -> None:
    repo = git.Repo(sample_repo)
    repo.git.checkout("--detach", "HEAD")

    analyzer = GitHistoryAnalyzer(sample_repo)
    assert analyzer.get_current_branch() is None

    history = analyzer.analyze()
    assert history.branch is None
    assert history.total_commits == 3
    assert [c.summary for c in history.commits] == EXPECTED_SAMPLE_MESSAGES


# ---------------------------------------------------------------------------
# 5-7. Commit history, messages and hashes
# ---------------------------------------------------------------------------


def test_commit_history_is_returned(sample_repo: Path) -> None:
    history = GitHistoryAnalyzer(sample_repo).analyze()
    assert history.total_commits == len(history.commits) == 3
    assert all(isinstance(c, CommitInfo) for c in history.commits)
    # History is chronological: oldest commit first.
    assert history.commits[0].summary == "Initial commit"
    assert history.commits[-1].summary == "Remove dead code"


def test_commit_messages_are_extracted(sample_repo: Path) -> None:
    history = GitHistoryAnalyzer(sample_repo).analyze()
    assert [c.summary for c in history.commits] == EXPECTED_SAMPLE_MESSAGES
    # `message` carries the full message text.
    assert history.commits[1].message == "Add email validation"


def test_commit_hashes_are_extracted(sample_repo: Path) -> None:
    history = GitHistoryAnalyzer(sample_repo).analyze()

    expected = [c.hexsha for c in git.Repo(sample_repo).iter_commits()]
    expected.reverse()
    assert [c.hash for c in history.commits] == expected

    assert all(len(c.hash) == 40 for c in history.commits)
    assert all(c.short_hash == c.hash[:7] for c in history.commits)
    assert len({c.hash for c in history.commits}) == 3


def test_get_commit_details_accepts_hash_references(sample_repo: Path) -> None:
    analyzer = GitHistoryAnalyzer(sample_repo)
    history = analyzer.analyze()
    target = history.commits[1]

    assert analyzer.get_commit_details(target.hash) == target
    assert analyzer.get_commit_details(target.short_hash) == target
    # Passing an already-converted CommitInfo is idempotent.
    assert analyzer.get_commit_details(target) is target


def test_commit_metadata_is_extracted(sample_repo: Path) -> None:
    history = GitHistoryAnalyzer(sample_repo).analyze()
    first = history.commits[0]

    assert first.author == "Ghost Tester"
    assert first.author_email == "ghost@example.com"
    assert isinstance(first.authored_at, datetime)
    assert isinstance(first.committed_at, datetime)
    assert first.authored_at <= first.committed_at


# ---------------------------------------------------------------------------
# 8-9. Changed files and insertion/deletion statistics
# ---------------------------------------------------------------------------


def test_changed_files_are_extracted(sample_repo: Path) -> None:
    history = GitHistoryAnalyzer(sample_repo).analyze()
    first, second, third = history.commits

    assert first.changed_files == ["app.py"]
    assert sorted(second.changed_files) == ["app.py", "validators.py"]
    assert third.changed_files == ["validators.py"]

    assert first.files_changed == 1
    assert second.files_changed == 2
    assert third.files_changed == 1


def test_insertions_and_deletions_are_extracted(sample_repo: Path) -> None:
    history = GitHistoryAnalyzer(sample_repo).analyze()
    first, second, third = history.commits

    # 1. app.py created with a single line.
    assert (first.insertions, first.deletions) == (1, 0)
    # 2. validators.py added (2 lines) + one line appended to app.py.
    assert (second.insertions, second.deletions) == (3, 0)
    # 3. validators.py deleted (2 lines).
    assert (third.insertions, third.deletions) == (0, 2)

    assert first.net_change == 1
    assert third.net_change == -2


# ---------------------------------------------------------------------------
# 10. PayGuard integration test
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not PAYGUARD_PATH.exists(),
    reason="PayGuard test repository not found at ~/Projects/PayGuard",
)
def test_payguard_known_commits_are_detected() -> None:
    history = GitHistoryAnalyzer(PAYGUARD_PATH).analyze()

    # Do not hard-code hashes: they differ between environments.
    assert history.total_commits >= 7

    summaries = {commit.summary for commit in history.commits}
    missing = [m for m in EXPECTED_PAYGUARD_MESSAGES if m not in summaries]
    assert not missing, f"Missing expected PayGuard commit messages: {missing}"
