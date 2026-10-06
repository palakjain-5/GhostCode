"""GhostCode — Developer Intent Drift Detection System.

GhostCode is under active development. Only the Git history analysis stage
is implemented so far; see README.md for the current status.
"""

from .git_analyzer import (
    CommitInfo,
    GitAnalyzerError,
    GitHistoryAnalyzer,
    GitOperationError,
    NoCommitsError,
    NotGitRepositoryError,
    RepositoryAccessError,
    RepositoryHistory,
    RepositoryNotFoundError,
)

__version__ = "0.1.0"

__all__ = [
    "__version__",
    # Data models
    "CommitInfo",
    "RepositoryHistory",
    # Analyzer
    "GitHistoryAnalyzer",
    # Errors
    "GitAnalyzerError",
    "RepositoryNotFoundError",
    "NotGitRepositoryError",
    "RepositoryAccessError",
    "NoCommitsError",
    "GitOperationError",
]
