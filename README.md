# GhostCode

**Developer Intent Drift Detection System** — *under active development.*

GhostCode analyses a local Git repository and tries to understand the
*intent* behind the code — as recorded in commit history, commit messages and
code structure — and then compares that intent against the current Python
implementation. The goal is to flag places where the code may have drifted
away from what the developers originally intended.

> ⚠️ **Project status:** GhostCode is a work in progress. **Intent drift
> detection is not implemented yet.** Only the first stage of the pipeline —
> the Git History Analyzer — currently exists. Intent extraction, the Python
> code analyzer, drift detection, risk scoring and the dashboard are all
> future work.

## Intended pipeline

```
Git Repository
      ↓
Git History Analyzer        ← implemented in this phase
      ↓
Python Code Analyzer        ← not yet implemented
      ↓
Intent Extraction           ← not yet implemented
      ↓
Intent vs Implementation Comparison   ← not yet implemented
      ↓
Drift Detection             ← not yet implemented
      ↓
Risk Scoring                ← not yet implemented
      ↓
Streamlit Dashboard         ← not yet implemented
```

## Current development phase

| Stage | Status |
| --- | --- |
| Git History Analyzer | ✅ implemented (this phase) |
| Python Code Analyzer (AST) | ⏳ planned |
| Intent Extraction | ⏳ planned |
| Intent vs Implementation Comparison | ⏳ planned |
| Drift Detection | ⏳ planned |
| Risk Scoring | ⏳ planned |
| Streamlit Dashboard | ⏳ planned |

## Purpose of `GitHistoryAnalyzer`

`GitHistoryAnalyzer` reliably extracts structured historical information from
a **local** Git repository and returns plain Python dataclasses instead of
raw GitPython objects. It is:

* **Read-only** — it never modifies the analyzed repository.
* **Offline** — no network calls, no cloning, no remotes.
* **Deterministic** — the same repository always yields the same data.
* **Independent of any UI or LLM** — reusable by every future stage.

For each commit it extracts: full and short hash, full message and summary
line, author name/email, authored and committed timestamps, changed files,
and insertion/deletion statistics.

## Installation

Requires Python 3.

```bash
cd GhostCode
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Dependencies for this phase: `GitPython` and `pytest` (nothing else).

## Running the tests

```bash
pytest
```

The suite covers repository validation, error handling, branch detection
(including detached HEAD), commit messages/hashes, changed files and
insertion/deletion statistics, plus an integration test against the PayGuard
test repository (automatically skipped if PayGuard is not present).

## Usage example

```python
from ghostcode.git_analyzer import GitHistoryAnalyzer

history = GitHistoryAnalyzer("/Users/you/Projects/PayGuard").analyze()

print(history.repository_name)   # PayGuard
print(history.branch)            # main
print(history.total_commits)     # 7

for commit in history.commits:   # chronological order, oldest first
    print(commit.short_hash, commit.summary)
    print("   files:", commit.changed_files)
    print("   change:", f"+{commit.insertions} -{commit.deletions}")
```

There is also a small demo script:

```bash
python3 demo.py ~/Projects/PayGuard
```

which prints:

```
Repository: PayGuard
Branch: main
Commits: 7

1. Initial PayGuard application
2. Add email validation for user registration
3. Log every failed login attempt
4. Prevent duplicate payment processing
5. Improve authentication flow
6. Refactor payment processing
7. Clean up application structure
```

Add `--verbose` for per-commit hashes, authors and change statistics.

## PayGuard — the controlled test repository

`~/Projects/PayGuard` is a small local Git repository with seven known
commits, used as the **controlled, read-only test repository** for GhostCode.
GhostCode never modifies it: it only reads its history. The integration test
asserts that at least seven commits exist and that the seven known commit
messages are detected — commit hashes are never hard-coded, because they can
differ between environments.

## Project structure

```
ghostcode/
│
├── README.md
├── requirements.txt
├── demo.py                     # small manual demo CLI
│
├── src/
│   └── ghostcode/
│       ├── __init__.py
│       └── git_analyzer.py     # GitHistoryAnalyzer + data models
│
└── tests/
    ├── conftest.py
    └── test_git_analyzer.py
```

## Known limitations

* Merge commits report statistics relative to their first parent (GitPython
  `Commit.stats` semantics).
* Only local repositories are supported; remotes are never contacted.
* The analyzer caches the opened repository per instance and reads history
  at `analyze()` time; it does not watch for new commits.

## License

See [LICENSE](LICENSE).
