"""Git history analyzer for GhostCode.

This module implements the first stage of the GhostCode pipeline: reading a
local Git repository and converting its commit history into plain, structured
Python objects (dataclasses) that later stages can consume.

Design rules:

* Read-only: the target repository is never modified.
* Offline: no network calls, no cloning, no remotes are contacted.
* Deterministic: the same repository always yields the same structured data.
* UI-independent: no Streamlit (or any other framework) code lives here.
* LLM-independent: no model or API dependency exists in this stage.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple, Union

import git
from git import Commit as GitCommit
from git.exc import BadName, GitCommandError, InvalidGitRepositoryError, NoSuchPathError

__all__ = [
    "GitAnalyzerError",
    "RepositoryNotFoundError",
    "NotGitRepositoryError",
    "RepositoryAccessError",
    "NoCommitsError",
    "GitOperationError",
    "CommitInfo",
    "RepositoryHistory",
    "GitHistoryAnalyzer",
]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class GitAnalyzerError(Exception):
    """Base class for every error raised by the Git history analyzer."""


class RepositoryNotFoundError(GitAnalyzerError):
    """Raised when the supplied path does not exist."""


class NotGitRepositoryError(GitAnalyzerError):
    """Raised when the supplied path exists but is not a Git repository."""


class RepositoryAccessError(GitAnalyzerError):
    """Raised when the repository exists but cannot be opened (for example
    due to missing permissions)."""


class NoCommitsError(GitAnalyzerError):
    """Raised when the repository contains no commits yet."""


class GitOperationError(GitAnalyzerError):
    """Raised when an underlying ``git`` command fails unexpectedly."""


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class CommitInfo:
    """Structured information about a single commit.

    All fields are plain Python types; no GitPython objects are exposed.
    """

    #: Full 40-character commit hash.
    hash: str
    #: Abbreviated commit hash (first 7 characters, as printed by ``git log``).
    short_hash: str
    #: Full commit message, stripped of surrounding whitespace.
    message: str
    #: First line of the commit message.
    summary: str
    #: Name of the commit author.
    author: str
    #: Email address of the commit author.
    author_email: str
    #: When the change was originally authored.
    authored_at: datetime
    #: When the change was committed to the repository.
    committed_at: datetime
    #: Paths (relative to the repository root) changed by this commit.
    changed_files: List[str] = field(default_factory=list)
    #: Number of lines added by this commit (0 when not available).
    insertions: int = 0
    #: Number of lines removed by this commit (0 when not available).
    deletions: int = 0

    @property
    def files_changed(self) -> int:
        """Number of files touched by this commit."""
        return len(self.changed_files)

    @property
    def net_change(self) -> int:
        """Net number of lines added (insertions minus deletions)."""
        return self.insertions - self.deletions


@dataclass
class RepositoryHistory:
    """Structured history of a repository, produced by
    :meth:`GitHistoryAnalyzer.analyze`."""

    #: Absolute path of the analyzed repository.
    repository_path: Path
    #: Current branch name, or ``None`` when HEAD is detached (or the branch
    #: cannot be determined, for example in a repository without commits).
    branch: Optional[str]
    #: Total number of commits returned in :attr:`commits`.
    total_commits: int
    #: Every commit in chronological order (oldest first).
    commits: List[CommitInfo] = field(default_factory=list)

    @property
    def repository_name(self) -> str:
        """The repository's directory name (for example ``PayGuard``)."""
        return self.repository_path.name


# ---------------------------------------------------------------------------
# Analyzer
# ---------------------------------------------------------------------------


class GitHistoryAnalyzer:
    """Reads a local Git repository and returns structured history objects.

    Usage::

        analyzer = GitHistoryAnalyzer("/Users/me/Projects/PayGuard")
        history = analyzer.analyze()
        history.total_commits   # e.g. 7
        history.branch          # e.g. "main"
        history.commits         # list[CommitInfo], oldest commit first
    """

    def __init__(self, repository_path: Union[str, Path]) -> None:
        self._repository_path = Path(repository_path).expanduser().resolve()
        self._repo: Optional[git.Repo] = None

    @property
    def repository_path(self) -> Path:
        """The (resolved) path the analyzer was created with."""
        return self._repository_path

    def __repr__(self) -> str:
        return f"GitHistoryAnalyzer({str(self._repository_path)!r})"

    # -- repository access -------------------------------------------------

    def _open_repository(self) -> git.Repo:
        """Open (once) and return the underlying repository.

        :raises RepositoryNotFoundError: the path does not exist.
        :raises NotGitRepositoryError: the path is not a Git repository.
        :raises RepositoryAccessError: the repository cannot be opened.
        :raises GitOperationError: an underlying git command failed.
        """
        if self._repo is not None:
            return self._repo

        path = self._repository_path
        if not path.exists():
            raise RepositoryNotFoundError(f"Path does not exist: {path}")
        if not path.is_dir():
            raise NotGitRepositoryError(
                f"Path is a file, not a Git repository: {path}"
            )

        try:
            repo = git.Repo(path)
        except NoSuchPathError as exc:
            raise RepositoryNotFoundError(f"Path does not exist: {path}") from exc
        except InvalidGitRepositoryError as exc:
            raise NotGitRepositoryError(f"Path is not a Git repository: {path}") from exc
        except PermissionError as exc:
            raise RepositoryAccessError(
                f"Repository is not accessible (permission denied): {path}"
            ) from exc
        except OSError as exc:
            raise RepositoryAccessError(f"Repository could not be opened: {path} ({exc})") from exc
        except GitCommandError as exc:
            raise GitOperationError(f"Git failed while opening {path}: {exc}") from exc

        self._repo = repo
        return repo

    def is_valid_repository(self) -> bool:
        """Return ``True`` when the path exists and is a Git repository.

        This is a predicate: it never raises. Use :meth:`analyze` (or the
        other methods) when you need a descriptive error.
        """
        try:
            self._open_repository()
        except GitAnalyzerError:
            return False
        return True

    def get_current_branch(self) -> Optional[str]:
        """Return the name of the checked-out branch.

        Returns ``None`` when HEAD is detached or when the branch cannot be
        determined (for example a repository without commits).
        """
        repo = self._open_repository()
        try:
            if repo.head.is_detached:
                return None
            return repo.active_branch.name
        except (TypeError, ValueError):
            # GitPython raises TypeError for a detached HEAD and ValueError
            # when the branch reference does not exist yet (unborn branch).
            return None
        except GitCommandError as exc:
            raise GitOperationError(
                f"Failed to determine the current branch of "
                f"{self._repository_path}: {exc}"
            ) from exc

    def get_commits(self) -> List[CommitInfo]:
        """Return every commit as a :class:`CommitInfo`, oldest commit first.

        :raises RepositoryNotFoundError: the path does not exist.
        :raises NotGitRepositoryError: the path is not a Git repository.
        :raises NoCommitsError: the repository has no commits yet.
        :raises GitOperationError: an underlying git command failed.
        """
        repo = self._open_repository()
        try:
            raw_commits = list(repo.iter_commits())
        except ValueError as exc:
            # GitPython raises ValueError when HEAD points to an unborn
            # branch, which means the repository has no commits yet.
            raise NoCommitsError(
                f"Repository at {self._repository_path} has no commits yet."
            ) from exc
        except (GitCommandError, OSError) as exc:
            raise GitOperationError(
                f"Failed to read commit history from {self._repository_path}: {exc}"
            ) from exc

        if not raw_commits:
            raise NoCommitsError(
                f"Repository at {self._repository_path} has no commits yet."
            )

        details = [self.get_commit_details(raw) for raw in raw_commits]
        details.reverse()  # git log is newest-first; history is chronological
        return details

    def get_commit_details(
        self, commit: Union[str, GitCommit, CommitInfo]
    ) -> CommitInfo:
        """Return a :class:`CommitInfo` for a commit reference.

        ``commit`` may be a GitPython ``Commit``, a full or abbreviated hash,
        or an already-converted :class:`CommitInfo` (returned unchanged).
        """
        if isinstance(commit, CommitInfo):
            return commit

        repo = self._open_repository()
        git_commit = self._resolve_commit(repo, commit)
        changed_files, insertions, deletions = self._extract_statistics(git_commit)

        message = (git_commit.message or "").strip()
        summary = message.splitlines()[0] if message else ""
        author = git_commit.author
        return CommitInfo(
            hash=git_commit.hexsha,
            short_hash=git_commit.hexsha[:7],
            message=message,
            summary=summary,
            author=author.name or "",
            author_email=author.email or "",
            authored_at=git_commit.authored_datetime,
            committed_at=git_commit.committed_datetime,
            changed_files=changed_files,
            insertions=insertions,
            deletions=deletions,
        )

    def analyze(self) -> RepositoryHistory:
        """Open the repository and return its full structured history.

        This is the main entry point of the component::

            history = GitHistoryAnalyzer("/path/to/repo").analyze()
        """
        self._open_repository()
        branch = self.get_current_branch()
        commits = self.get_commits()
        return RepositoryHistory(
            repository_path=self._repository_path,
            branch=branch,
            total_commits=len(commits),
            commits=commits,
        )

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _resolve_commit(
        repo: git.Repo, commit: Union[str, GitCommit]
    ) -> GitCommit:
        if isinstance(commit, GitCommit):
            return commit
        if isinstance(commit, str):
            try:
                return repo.commit(commit)
            except (BadName, GitCommandError, ValueError, OSError) as exc:
                raise GitOperationError(
                    f"Could not resolve commit {commit!r}: {exc}"
                ) from exc
        raise TypeError(
            f"Unsupported commit reference of type {type(commit).__name__}; "
            "expected a hash, a git.Commit or a CommitInfo."
        )

    @staticmethod
    def _extract_statistics(commit: GitCommit) -> Tuple[List[str], int, int]:
        """Return ``(changed_files, insertions, deletions)`` for a commit.

        Uses GitPython's ``Commit.stats`` (backed by ``git diff-tree``), which
        diffs against the first parent (or the empty tree for a root commit).
        """
        try:
            stats = commit.stats
        except (GitCommandError, OSError) as exc:
            raise GitOperationError(
                f"Failed to read change statistics for commit "
                f"{commit.hexsha[:7]}: {exc}"
            ) from exc

        changed_files = list(stats.files.keys())
        totals = stats.total or {}
        insertions = int(totals.get("insertions", 0))
        deletions = int(totals.get("deletions", 0))
        return changed_files, insertions, deletions
