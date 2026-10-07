# Contributing to GhostCode

Short developer guide: how to set the project up, where each stage lives,
and how tests are written. The full user documentation is in
[README.md](README.md).

## Project setup

```bash
git clone <your-fork-url>
cd GhostCode
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt   # runtime + pytest
```

Runtime-only installs (`pip install -r requirements.txt`) are enough to run
the dashboard but not the test suite.

## Running the tool

```bash
streamlit run dashboard.py                              # web UI
GHOSTCODE_REPOSITORY=~/SomeRepo streamlit run dashboard.py
python3 demo.py git|code|intent|drift|risk <path> [-v]  # stage-by-stage CLI
pytest -q                                               # full test suite
```

## Where each analysis stage lives

| Stage | Module | Main API | Tests |
| --- | --- | --- | --- |
| Git history | `src/ghostcode/git_analyzer.py` | `GitHistoryAnalyzer.analyze()` | `tests/test_git_analyzer.py` |
| Python code (AST) | `src/ghostcode/code_analyzer.py` | `PythonCodeAnalyzer.analyze()` | `tests/test_code_analyzer.py` |
| Intent extraction | `src/ghostcode/intent_extractor.py` | `extract_intent()` / `IntentExtractor.extract_all()` | `tests/test_intent_extractor.py` |
| Drift detection | `src/ghostcode/drift_detector.py` | `IntentDriftDetector.detect()` | `tests/test_drift_detector.py` |
| Risk scoring | `src/ghostcode/risk_scorer.py` | `calculate_risk()` / `calculate_risk_report()` | `tests/test_risk_scorer.py` |
| Dashboard | `src/ghostcode/dashboard.py` | `build_dashboard_model()` / `render()` | `tests/test_dashboard.py` |
| End-to-end | (whole pipeline) | `run_pipeline()` in the test module | `tests/test_pipeline_e2e.py` |

The package root (`src/ghostcode/__init__.py`) re-exports the public API of
every stage.

## Development workflow

* **One stage per change.** Keep stages independent: a stage consumes the
  public models/dataclasses of the stages before it, never their internals.
* **Models are dataclasses with `to_dict()`** returning plain, JSON-friendly
  types (`str`, `int`, `float`, `bool`, `list`, `dict`, `None`).
* **Analysis stages use only the standard library** (plus GitPython for the
  Git stage). No network calls, no LLMs, no credentials, and never any
  logic that special-cases a specific repository.
* **Determinism is a requirement.** Anything that iterates collections must
  have a stable, documented order; the same input must always produce the
  same output (the E2E suite asserts this by running stages three times).
* **The dashboard is presentation-only.** Put logic in the pure
  `build_dashboard_model()` function; `streamlit` is imported lazily inside
  the view functions, never at module import time.
* **Keep documentation honest.** Update README.md when behavior, commands,
  or documented results change; remove stale "future phase" wording instead
  of leaving it to rot.

## Running tests

```bash
pytest -q                            # everything
pytest tests/test_pipeline_e2e.py -q # end-to-end validation only
```

* Tests build their own repositories in `tmp_path` — never modify
  `~/Projects/PayGuard` or any other real repository.
* PayGuard integration tests skip automatically when that repository is
  absent; Streamlit `AppTest` UI tests skip when Streamlit is not installed.

## Writing tests

* **One test = one behavior.** Use the fixtures in `tests/test_pipeline_e2e.py`
  (lost / preserved / multi-loss / no-Python / malformed / low-confidence /
  no-commit / non-git repositories) as patterns for new scenarios.
* **Create Git history with GitPython** inside the fixture:

  ```python
  repo.index.commit(
      message,
      author_date=timestamp,   # pins committed_datetime ...
      commit_date=timestamp,   # ... so drift ordering is deterministic
  )
  ```

* **Assert semantics, not literals:** counts, drift types, severities,
  score bands, evidence content, `to_dict()` shapes. Never assert commit
  hashes, line numbers, complete output strings, or values that depend on
  a specific repository's history.
* **No network, no sleeps, no polling** — tests must pass offline in a few
  seconds (the whole suite currently runs in ~16 s).

## Style

* Python 3.9+ compatible syntax; use `from __future__ import annotations`
  when writing PEP 604 unions (`str | None`) in annotations.
* Docstrings describe what the code *actually* does — update them in the
  same change as the behavior.
* Run the complete suite before committing; commit complete, tested units
  of work with a message that says what the change delivers.
