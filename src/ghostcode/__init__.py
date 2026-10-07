"""GhostCode — Developer Intent Drift Detection System.

GhostCode is under active development. Six pipeline stages are implemented
so far: the Git history analyzer, the Python code analyzer, intent
extraction, intent drift detection, risk scoring and the Streamlit
dashboard. See README.md for the current status.
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
from .intent_extractor import (
    ACTION_ALIASES,
    ACTION_VERBS,
    CATEGORIES,
    CATEGORY_SIGNALS,
    CONFIDENCE_WEIGHTS,
    Intent,
    IntentExtractor,
    extract_intent,
)
from .drift_detector import (
    BEHAVIOR_STATUSES,
    DEFAULT_MAX_CALL_DEPTH,
    DEFAULT_MIN_INTENT_CONFIDENCE,
    DRIFT_TYPES,
    SEVERITIES,
    SEVERITY_BY_DRIFT_TYPE,
    BehaviorCheck,
    DriftReport,
    DriftResult,
    IntentDriftDetector,
)
from .risk_scorer import (
    AREA_IMPACT,
    DIRECT_EVIDENCE_RULES,
    RISK_SEVERITIES,
    SENSITIVE_AREAS,
    RiskFactor,
    RiskReport,
    RiskScore,
    calculate_risk,
    calculate_risk_report,
    resolve_severity,
)
from .dashboard import (
    DashboardModel,
    build_dashboard_model,
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
    # Intent extraction (phase: completed)
    "Intent",
    "IntentExtractor",
    "extract_intent",
    "CATEGORIES",
    "ACTION_VERBS",
    "ACTION_ALIASES",
    "CATEGORY_SIGNALS",
    "CONFIDENCE_WEIGHTS",
    # Intent drift detection (phase: completed)
    "IntentDriftDetector",
    "DriftReport",
    "DriftResult",
    "BehaviorCheck",
    "DRIFT_TYPES",
    "SEVERITIES",
    "SEVERITY_BY_DRIFT_TYPE",
    "BEHAVIOR_STATUSES",
    "DEFAULT_MIN_INTENT_CONFIDENCE",
    "DEFAULT_MAX_CALL_DEPTH",
    # Risk scoring (phase: completed)
    "RiskScore",
    "RiskFactor",
    "RiskReport",
    "calculate_risk",
    "calculate_risk_report",
    "resolve_severity",
    "RISK_SEVERITIES",
    "SENSITIVE_AREAS",
    "AREA_IMPACT",
    "DIRECT_EVIDENCE_RULES",
    # Dashboard (phase: completed)
    "DashboardModel",
    "build_dashboard_model",
]
