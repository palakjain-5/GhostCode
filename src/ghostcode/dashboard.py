"""Streamlit dashboard for GhostCode — the presentation stage.

This module is the sixth pipeline stage: a **read-only presentation layer**
over the five existing stages (Git history analyzer, Python code analyzer,
intent extraction, intent drift detection and risk scoring). It contains
*no analysis logic of its own* — every number shown is produced by the
existing components, so the dashboard can never disagree with the CLI or
the tests.

Design rules (shared with every other GhostCode component):

* **Deterministic** — :func:`build_dashboard_model` runs the same pipeline
  the demo CLI runs and returns plain, JSON-serializable dictionaries; the
  same repository always yields the same model.
* **Read-only and offline** — the analyzed repository is never modified;
  no network, no LLM, no database, no deployment concerns.
* **No rewriting of existing stages** — the dashboard only *consumes*
  their public APIs (``analyze()`` / ``extract_all()`` / ``detect()`` /
  ``calculate_risk_report()``).

The module is split into two halves:

* :func:`build_dashboard_model` / :class:`DashboardModel` — pure Python,
  **no Streamlit import**, fully unit-testable without a UI runtime.
* :func:`render` / :func:`main` — the Streamlit view, which imports
  ``streamlit`` lazily inside the functions so that importing this module
  (for the builder, for tests, or from the package root) never requires
  Streamlit to be installed.

Running the dashboard::

    streamlit run dashboard.py

Optionally set ``GHOSTCODE_REPOSITORY`` to preselect a repository path.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from .code_analyzer import CodeAnalyzerError, PythonCodeAnalyzer
from .drift_detector import IntentDriftDetector
from .git_analyzer import GitAnalyzerError, GitHistoryAnalyzer
from .intent_extractor import IntentExtractor
from .risk_scorer import calculate_risk_report

__all__ = [
    "DashboardModel",
    "build_dashboard_model",
    "render",
    "main",
    "severity_icon",
    "TABS",
    "SEVERITY_ICONS",
]

#: The dashboard's tab order (left to right), mirroring the pipeline.
TABS: tuple = (
    "Overview",
    "Git History",
    "Python Code",
    "Intents",
    "Drift",
    "Risk",
)

#: Deterministic severity decoration, keyed by the *uppercase* severity.
SEVERITY_ICONS: Dict[str, str] = {
    "LOW": "🟢",
    "MEDIUM": "🟡",
    "HIGH": "🟠",
    "CRITICAL": "🔴",
    "NONE": "⚪",
}


def severity_icon(severity: str) -> str:
    """Return the icon for a risk or drift severity (case-insensitive)."""
    return SEVERITY_ICONS.get(str(severity).upper(), "⚪")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class DashboardModel:
    """Everything the dashboard renders, assembled by the existing stages.

    On success (``error is None``) every stage section is populated. On
    failure all sections stay at their empty defaults so the view can
    render safely after checking :attr:`error` once.
    """

    #: Repository path exactly as requested by the caller.
    repository: str
    #: Human-readable failure message, or ``None`` when analysis succeeded.
    error: Optional[str] = None
    #: Git history summary: repository_name/branch/commits/...
    git: Dict[str, Any] = field(default_factory=dict)
    #: Python code summary: totals + per-file rows + parse errors.
    code: Dict[str, Any] = field(default_factory=dict)
    #: One ``Intent.to_dict()`` per commit, in chronological order.
    intents: List[Dict[str, Any]] = field(default_factory=list)
    #: ``DriftReport.to_dict()`` — comparisons, drifted, significant.
    drift: Dict[str, Any] = field(default_factory=dict)
    #: ``RiskReport.to_dict()`` — findings + repository summary.
    risk: Dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """True when the pipeline ran without error."""
        return self.error is None

    def to_dict(self) -> Dict[str, Any]:
        """Plain-dict (JSON-friendly) representation."""
        return {
            "repository": self.repository,
            "error": self.error,
            "git": self.git,
            "code": self.code,
            "intents": self.intents,
            "drift": self.drift,
            "risk": self.risk,
        }


# ---------------------------------------------------------------------------
# Pipeline assembly (pure — no Streamlit)
# ---------------------------------------------------------------------------


def _commit_row(commit: Any) -> Dict[str, Any]:
    """Serialize one ``CommitInfo`` for tabular display."""
    return {
        "short_hash": commit.short_hash,
        "hash": commit.hash,
        "summary": commit.summary,
        "message": commit.message,
        "author": commit.author,
        "authored_at": commit.authored_at.isoformat(timespec="minutes"),
        "files_changed": commit.files_changed,
        "insertions": commit.insertions,
        "deletions": commit.deletions,
        "net_change": commit.net_change,
        "changed_files": list(commit.changed_files),
    }


def _relative(path: Path, root: Path) -> str:
    """Repo-relative POSIX path (falls back to the absolute path)."""
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def build_dashboard_model(
    repository: Union[str, Path],
) -> DashboardModel:
    """Run the full GhostCode pipeline and package it for the dashboard.

    The five stages are executed in pipeline order — git history →
    Python code → intent extraction → drift detection → risk scoring —
    with the extracted intents handed to the detector so every tab shows
    one consistent analysis.

    Failures (missing path, not a Git repository, unreadable code) never
    raise: they are captured as :attr:`DashboardModel.error` so the UI can
    render a readable message instead of a traceback.
    """
    repository_str = str(repository)
    try:
        history = GitHistoryAnalyzer(repository).analyze()
        code = PythonCodeAnalyzer(history.repository_path).analyze()
        intents = IntentExtractor().extract_all(history.commits)
        drift = IntentDriftDetector().detect(history.commits, code, intents)
        risk = calculate_risk_report(drift)
    except (GitAnalyzerError, CodeAnalyzerError, OSError, ValueError) as exc:
        return DashboardModel(
            repository=repository_str,
            error=f"{type(exc).__name__}: {exc}",
        )

    root = code.repository_path
    return DashboardModel(
        repository=repository_str,
        git={
            "repository_name": history.repository_name,
            "repository_path": str(history.repository_path),
            "branch": history.branch,
            "total_commits": history.total_commits,
            "commits": [_commit_row(c) for c in history.commits],
        },
        code={
            "repository_path": str(root),
            "total_python_files": code.total_python_files,
            "total_functions": code.total_functions,
            "total_classes": code.total_classes,
            "total_imports": code.total_imports,
            "total_function_calls": code.total_function_calls,
            "total_errors": code.total_errors,
            "files": [
                {
                    "path": _relative(f.file_path, root),
                    "module_name": f.module_name,
                    "lines_of_code": f.lines_of_code,
                    "functions": len(f.functions),
                    "classes": len(f.classes),
                    "imports": len(f.imports),
                    "function_calls": len(f.function_calls),
                }
                for f in code.files
            ],
            "errors": [e.to_dict() for e in code.errors],
        },
        intents=[intent.to_dict() for intent in intents],
        drift=drift.to_dict(),
        risk=risk.to_dict(),
    )


# ---------------------------------------------------------------------------
# Streamlit view (Streamlit is imported lazily inside each entry point)
# ---------------------------------------------------------------------------


def _render_overview(st: Any, model: DashboardModel) -> None:
    git, code, drift, risk = model.git, model.code, model.drift, model.risk

    st.subheader(git["repository_name"])
    st.caption(
        f"{git['repository_path']} · branch: {git['branch'] or '(detached HEAD)'}"
    )

    columns = st.columns(6)
    columns[0].metric("Commits", git["total_commits"])
    columns[1].metric("Python files", code["total_python_files"])
    columns[2].metric("Intents", len(model.intents))
    columns[3].metric("Comparisons", drift["total_comparisons"])
    columns[4].metric("Risk findings", risk["total_findings"])
    columns[5].metric("Overall risk", risk["overall_risk"])

    st.markdown("#### Pipeline")
    st.write(
        f"1. ✅ **Git History Analyzer** — {git['total_commits']} commit(s), "
        f"branch `{git['branch'] or 'detached'}`"
    )
    st.write(
        f"2. ✅ **Python Code Analyzer** — {code['total_python_files']} file(s), "
        f"{code['total_functions']} function(s), {code['total_classes']} class(es)"
    )
    st.write(
        f"3. ✅ **Intent Extraction** — {len(model.intents)} intent(s) "
        "from commit messages"
    )
    st.write(
        f"4. ✅ **Intent Drift Detection** — {drift['total_comparisons']} "
        f"comparison(s), {drift['drifted']} with drift, "
        f"{drift['significant']} significant"
    )
    st.write(
        f"5. ✅ **Risk Scoring** — {risk['total_findings']} finding(s), "
        f"overall risk {risk['overall_risk']}/100"
    )
    st.write("6. 🖥️ **Streamlit Dashboard** — this view (read-only)")
    st.caption(
        "Deterministic and offline: no LLM, no database, and the analyzed "
        "repository is never modified."
    )


def _render_history(st: Any, model: DashboardModel) -> None:
    commits = model.git["commits"]
    st.caption(f"{len(commits)} commit(s), oldest first (chronological).")

    rows = [
        {
            "#": index,
            "commit": c["short_hash"],
            "summary": c["summary"],
            "author": c["author"],
            "authored": c["authored_at"],
            "files": c["files_changed"],
            "+": c["insertions"],
            "-": c["deletions"],
        }
        for index, c in enumerate(commits, start=1)
    ]
    st.dataframe(rows, hide_index=True)

    options = [
        f"{index}. {c['short_hash']} {c['summary']}"
        for index, c in enumerate(commits, start=1)
    ]
    choice = st.selectbox("Commit detail", options)
    index = options.index(choice)
    commit = commits[index]
    st.code(commit["message"], language="text")
    st.markdown("**Changed files:**")
    for path in commit["changed_files"]:
        st.write(f"- `{path}`")


def _render_code(st: Any, model: DashboardModel) -> None:
    code = model.code

    columns = st.columns(6)
    columns[0].metric("Files", code["total_python_files"])
    columns[1].metric("Functions", code["total_functions"])
    columns[2].metric("Classes", code["total_classes"])
    columns[3].metric("Imports", code["total_imports"])
    columns[4].metric("Calls", code["total_function_calls"])
    columns[5].metric("Parse errors", code["total_errors"])

    rows = [
        {
            "module": f["module_name"],
            "path": f["path"],
            "loc": f["lines_of_code"],
            "functions": f["functions"],
            "classes": f["classes"],
            "imports": f["imports"],
            "calls": f["function_calls"],
        }
        for f in code["files"]
    ]
    st.dataframe(rows, hide_index=True)

    if code["errors"]:
        st.warning(f"{len(code['errors'])} file(s) could not be parsed:")
        st.dataframe(
            [
                {"path": e["file_path"], "type": e["error_type"], "message": e["message"]}
                for e in code["errors"]
            ],
            hide_index=True,
        )


def _render_intents(st: Any, model: DashboardModel) -> None:
    intents = model.intents
    st.caption(
        "Deterministic, heuristic intent extraction — no LLM. "
        "Confidence is a checklist score, not a probability."
    )

    rows = [
        {
            "commit": str(intent["commit_message"]).splitlines()[0],
            "action": intent["action"] or "—",
            "object": intent["object"],
            "category": intent["category"],
            "confidence": intent["confidence"],
        }
        for intent in intents
    ]
    st.dataframe(rows, hide_index=True)

    options = [
        f"{index}. {str(intent['commit_message']).splitlines()[0]}"
        for index, intent in enumerate(intents, start=1)
    ]
    choice = st.selectbox("Intent detail", options)
    intent = intents[options.index(choice)]
    st.write(f"**Action:** {intent['action'] or '—'}")
    st.write(f"**Object:** {intent['object']}")
    st.write(f"**Scope:** {intent['scope'] or '—'}")
    st.write(f"**Category:** {intent['category']}")
    st.write(f"**Confidence:** {intent['confidence']:.2f} (heuristic)")
    st.markdown("**Evidence:**")
    for line in intent["evidence"]:
        st.write(f"- {line}")


def _render_drift(st: Any, model: DashboardModel) -> None:
    drift = model.drift
    results = drift["results"]

    columns = st.columns(3)
    columns[0].metric("Comparisons", drift["total_comparisons"])
    columns[1].metric("Drift detected", drift["drifted"])
    columns[2].metric("Significant (medium/high)", drift["significant"])

    if not results:
        st.info(
            "No comparisons were produced — the history contains no "
            "confident intent that could be paired or safety-net checked."
        )
        return

    rows = [
        {
            "original": r["original_commit_message"].splitlines()[0],
            "later": r["later_commit_message"].splitlines()[0],
            "drift_type": r["drift_type"],
            "severity": r["severity"],
            "category": r["original_intent"]["category"],
            "confidence": r["confidence"],
        }
        for r in results
    ]
    st.dataframe(rows, hide_index=True)

    options = [
        f"{index}. {r['original_commit_message'].splitlines()[0]}"
        for index, r in enumerate(results, start=1)
    ]
    choice = st.selectbox("Comparison detail", options)
    result = results[options.index(choice)]

    is_drift = result["drift_type"] != "none"
    header = (
        f"{severity_icon(result['severity'])} "
        f"{result['severity'].upper()} — {result['drift_type']}"
    )
    if is_drift:
        st.error(f"**DRIFT DETECTED** · {header}")
    else:
        st.success(f"**No significant drift** · {header}")

    st.write(f"**Original:** {result['original_commit_message'].splitlines()[0]}")
    st.write(f"**Later:** {result['later_commit_message'].splitlines()[0]}")
    st.info(result["reason"])
    st.markdown("**Later implementation (structural evidence):**")
    for line in result["implementation"]:
        st.write(f"- {line}")
    with st.expander("Comparison evidence"):
        for line in result["evidence"]:
            st.write(f"- {line}")
    st.caption(f"Confidence: {result['confidence']:.2f} (heuristic, not a probability)")


def _render_risk(st: Any, model: DashboardModel) -> None:
    risk = model.risk
    findings = risk["findings"]

    columns = st.columns(4)
    columns[0].metric("Findings", risk["total_findings"])
    columns[1].metric("High risk (or above)", risk["high_risk_findings"])
    columns[2].metric("Critical", risk["critical_findings"])
    columns[3].metric("Overall risk", f"{risk['overall_risk']}/100")

    if not findings:
        st.info(
            "No risk findings — preserved comparisons and no-drift rows are "
            "never scored as risk (score 0 / LOW)."
        )
        return

    st.caption(
        "Scores are a deterministic weighted heuristic describing potential "
        "engineering impact — **not probabilities**."
    )
    rows = [
        {
            "score": f["score"],
            "severity": f"{severity_icon(f['severity'])} {f['severity']}",
            "area": f["affected_area"],
            "drift_type": f["drift_type"],
            "source": f["source_commit"][:7],
            "detected": f["detected_commit"][:7],
        }
        for f in findings
    ]
    st.dataframe(rows, hide_index=True)

    options = [
        f"{index}. {f['score']}/100 {f['severity']} — {f['affected_area']}"
        for index, f in enumerate(findings, start=1)
    ]
    choice = st.selectbox("Finding detail", options)
    finding = findings[options.index(choice)]

    st.markdown(
        f"### {severity_icon(finding['severity'])} "
        f"RISK SCORE: {finding['score']}/100 — {finding['severity']}"
    )
    st.markdown("**Factors:**")
    for risk_factor in finding["factors"]:
        st.write(f"- +{risk_factor['points']} {risk_factor['label']}")
    st.caption(
        f"Raw total: {finding['raw_score']} · Final score: {finding['score']} · "
        f"confidence {finding['confidence']:.2f} · area `{finding['affected_area']}`"
    )
    st.info(finding["explanation"])


def render(model: DashboardModel) -> None:
    """Render one analyzed repository as the six dashboard tabs."""
    import streamlit as st

    tab_overview, tab_history, tab_code, tab_intents, tab_drift, tab_risk = st.tabs(
        list(TABS)
    )
    with tab_overview:
        _render_overview(st, model)
    with tab_history:
        _render_history(st, model)
    with tab_code:
        _render_code(st, model)
    with tab_intents:
        _render_intents(st, model)
    with tab_drift:
        _render_drift(st, model)
    with tab_risk:
        _render_risk(st, model)


def _running_in_streamlit() -> bool:
    """True when executed inside a Streamlit runtime (incl. AppTest)."""
    try:
        from streamlit.runtime.scriptrunner import get_script_run_ctx
    except Exception:  # pragma: no cover - streamlit API variations
        return False
    try:
        # suppress_warning keeps the bare-mode invocation silent; older
        # Streamlit releases only accept the no-argument form.
        ctx = get_script_run_ctx(suppress_warning=True)
    except TypeError:  # pragma: no cover - streamlit API variations
        ctx = get_script_run_ctx()
    return ctx is not None


def main() -> None:
    """Dashboard entry point (used by ``streamlit run dashboard.py``).

    When executed outside a Streamlit runtime (plain ``python
    dashboard.py``) a short usage hint is printed instead of failing —
    the runtime is checked *before* Streamlit is imported, so the hint
    also works in an environment where Streamlit is not installed.
    """
    if not _running_in_streamlit():
        print(
            "The GhostCode dashboard runs inside Streamlit:\n"
            "\n"
            "    streamlit run dashboard.py\n"
            "\n"
            "Optionally preselect a repository with the "
            "GHOSTCODE_REPOSITORY environment variable."
        )
        return

    import streamlit as st

    st.set_page_config(
        page_title="GhostCode — Intent Drift Dashboard",
        layout="wide",
    )
    st.title("GhostCode")
    st.caption(
        "Developer Intent Drift Detection — deterministic pipeline dashboard"
    )

    with st.sidebar:
        st.header("Repository")
        default_repository = os.environ.get("GHOSTCODE_REPOSITORY", "")
        repository = st.text_input(
            "Local Git repository path",
            value=default_repository,
            placeholder="~/Projects/my-repo",
        )
        analyze_clicked = st.button("Analyze repository", type="primary")
        st.caption(
            "Read-only · offline · no LLM · the analyzed repository is "
            "never modified."
        )

    repository = repository.strip()
    should_build = False
    if analyze_clicked:
        if repository:
            should_build = True
        else:
            st.warning("Enter a local Git repository path first.")
    elif "model" not in st.session_state and repository:
        should_build = True

    if should_build:
        with st.spinner("Running the GhostCode pipeline..."):
            st.session_state["model"] = build_dashboard_model(repository)

    model: Optional[DashboardModel] = st.session_state.get("model")
    if model is None:
        st.info(
            "Enter a local Git repository path in the sidebar and click "
            "**Analyze repository** to run the pipeline."
        )
        return
    if model.error:
        st.error(f"Could not analyze `{model.repository}` — {model.error}")
        return

    render(model)
