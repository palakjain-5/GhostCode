"""GhostCode — Developer Intent Drift Detection System.

GhostCode is under active development. The Git history analyzer and the
Python code analyzer are implemented so far; intent inference and drift
detection are later phases. See README.md for the current status.
"""

from .code_analyzer import (
    CallInfo,
    ClassInfo,
    CodeAnalyzerError,
    ConditionalInfo,
    DEFAULT_EXCLUDED_DIRS,
    ExceptionInfo,
    FileAnalysisError,
    FunctionInfo,
    ImportInfo,
    InvalidPathError,
    InvalidSourceError,
    LoopInfo,
    PathNotFoundError,
    PythonCodeAnalyzer,
    PythonFileAnalysis,
    RepositoryCodeAnalysis,
    ReturnInfo,
    UnreadableFileError,
)
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
    # Git history analyzer (phase: completed)
    "CommitInfo",
    "RepositoryHistory",
    "GitHistoryAnalyzer",
    "GitAnalyzerError",
    "RepositoryNotFoundError",
    "NotGitRepositoryError",
    "RepositoryAccessError",
    "NoCommitsError",
    "GitOperationError",
    # Python code analyzer (phase: completed)
    "PythonCodeAnalyzer",
    "PythonFileAnalysis",
    "RepositoryCodeAnalysis",
    "FileAnalysisError",
    "FunctionInfo",
    "ClassInfo",
    "CallInfo",
    "ReturnInfo",
    "ConditionalInfo",
    "LoopInfo",
    "ExceptionInfo",
    "ImportInfo",
    "DEFAULT_EXCLUDED_DIRS",
    "CodeAnalyzerError",
    "PathNotFoundError",
    "InvalidPathError",
    "InvalidSourceError",
    "UnreadableFileError",
]
