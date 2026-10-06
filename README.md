# GhostCode

**Developer Intent Drift Detection System** — *under active development.*

GhostCode analyses a local Git repository and tries to understand the
*intent* behind the code — as recorded in commit history, commit messages and
code structure — and then compares that intent against the current Python
implementation. The goal is to flag places where the code may have drifted
away from what the developers originally intended.

> ⚠️ **Project status:** GhostCode is a work in progress. **Intent drift
> detection is not implemented yet.** Two pipeline stages currently exist —
> the Git History Analyzer and the Python Code Analyzer. Intent extraction,
> the intent-vs-implementation comparison, drift detection, risk scoring and
> the dashboard are all future work.
>
> GhostCode currently extracts structural evidence from Python source code.
> Intent inference and drift detection are implemented in later phases.

## Intended pipeline

```
Git Repository
      ↓
Git History Analyzer        ← implemented
      ↓
Python Code Analyzer        ← implemented (this phase)
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
| Git History Analyzer | ✅ implemented |
| Python Code Analyzer (AST) | ✅ implemented (this phase) |
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

## Purpose of `PythonCodeAnalyzer`

`PythonCodeAnalyzer` inspects the **current** Python source code of a
repository and produces structured, deterministic evidence about what is
structurally present.

### Why AST?

The analyzer uses Python's standard-library [`ast`](https://docs.python.org/3/library/ast.html)
module rather than text/regex matching or an LLM:

* **Precise** — it understands Python's real grammar (nesting, scopes,
  decorators), so `self._settle(x)` is recognized as a call to `_settle`
  without confusing it with prose in comments or docstrings.
* **Deterministic** — the same source always produces the exact same
  structured output; no model, no randomness, no network.
* **Free** — no extra dependencies, no API keys, no tokens.
* **Explainable** — every fact carries a source line, so later stages (and
  humans) can verify where it came from.

### What it extracts

Per Python file (`PythonFileAnalysis`):

| Area | Details |
| --- | --- |
| Functions | name, line range, arguments, decorators, calls, returns, conditionals, loops, raises |
| Classes | name, line range, base classes, decorators, methods |
| Imports | `import` / `from ... import ...` (including relative imports and aliases) |
| Function calls | bare name (`_settle`), qualified name (`self._settle`), source line |
| Conditionals | `if`, `elif`, ternary expressions |
| Loops | `for` (incl. `async for`), `while` |
| Returns | line and the unparsed return expression |
| Exceptions | `raise` (type + literal message) and `except` handlers (type) |
| Size | lines of code (non-blank, non-comment) |

Repository-level summaries (`RepositoryCodeAnalysis`): total Python files,
total functions (including methods), total classes, total imports, total
function calls — plus a structured `errors` list so files with invalid
Python source are reported instead of silently hidden.

> The analyzer only answers *"what is structurally present?"*. It does not
> judge correctness, does not infer intent and does not detect drift — those
> are later pipeline stages.

## Installation

Requires Python 3.

```bash
cd GhostCode
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Dependencies for this phase: `GitPython` and `pytest` (nothing else). The
code analyzer uses only the Python standard library (`ast`). Python 3.9+ is
required (`ast.unparse`).

## Running the tests

```bash
pytest
```

The suite covers both analyzer stages:

* **Git History Analyzer** — repository validation, error handling, branch
  detection (including detached HEAD), commit messages/hashes, changed files
  and insertion/deletion statistics.
* **Python Code Analyzer** — function/class/method detection, arguments,
  calls, returns, conditionals, loops, exceptions, imports, recursive
  repository scanning, ignored directories (venvs/caches), invalid-source
  reporting and serialization.

Both suites include an integration test against the PayGuard test
repository (automatically skipped if PayGuard is not present).

## Usage examples

### Git history

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

### Python code structure

```python
from ghostcode.code_analyzer import PythonCodeAnalyzer

analysis = PythonCodeAnalyzer("/Users/you/Projects/PayGuard").analyze()

print(analysis.total_python_files)   # 7
print(analysis.total_functions)      # 28
print(analysis.total_classes)        # 3
print(analysis.total_function_calls)  # 77

for file in analysis.files:
    print(file.module_name, [f.name for f in file.functions])

# Find every call to a given function, with the line it appears on:
for file in analysis.files:
    for call in file.function_calls:
        if call.function_name == "_settle":
            print(file.file_path, call.line_number, call.qualified_name)

# A single file can also be analyzed on its own:
one = PythonCodeAnalyzer("/Users/you/Projects/PayGuard").analyze_file("payments.py")

# Every model exposes a deterministic plain-dict form:
data = analysis.to_dict()
```

### Demo script

```bash
python3 demo.py git ~/Projects/PayGuard       # git history summary
python3 demo.py code ~/Projects/PayGuard      # python code summary
python3 demo.py code ~/Projects/PayGuard -v   # per-file breakdown
```

The git command prints:

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

The code command prints (real output from the current PayGuard repository):

```
Repository: PayGuard

Python files: 7
Functions: 28
Classes: 3
Imports: 18
Function calls: 77

Important discovered calls (defined in this repository):
- _active_user
- _failed_login
- _settle
- add_user
- ...
- record
- register_user
- validate_email
- validate_username
Unique call names: 48
```

"Important" means: a discovered call whose target is itself a function or
method defined in the repository — selected purely from AST structure, with
no hard-coded names. (The bare path form `python3 demo.py ~/Projects/PayGuard`
still runs the git summary.)

## PayGuard — the controlled test repository

`~/Projects/PayGuard` is a small local Git repository with seven known
commits, used as the **controlled, read-only test repository** for GhostCode.
GhostCode never modifies it: it only reads its history and parses its source.
The integration tests assert that at least seven commits and their known
messages are detected, and that Python files, functions and important
structural calls (e.g. `validate_email`, `_failed_login`, `record`,
`_settle`) are discovered through AST structure — no hashes, line numbers or
repository-specific rules are hard-coded.

## Project structure

```
ghostcode/
│
├── README.md
├── requirements.txt
├── demo.py                     # small manual demo CLI (git + code)
│
├── src/
│   └── ghostcode/
│       ├── __init__.py
│       ├── git_analyzer.py     # GitHistoryAnalyzer + data models
│       └── code_analyzer.py    # PythonCodeAnalyzer + data models
│
└── tests/
    ├── conftest.py
    ├── test_git_analyzer.py
    └── test_code_analyzer.py
```

## Known limitations

* Merge commits report statistics relative to their first parent (GitPython
  `Commit.stats` semantics).
* Only local repositories are supported; remotes are never contacted.
* The Git analyzer caches the opened repository per instance and reads
  history at `analyze()` time; it does not watch for new commits.
* The code analyzer reports static structure only: dynamically constructed
  calls (e.g. via `getattr`) and calls resolved at runtime are not visible
  to it. Lambdas are not reported as functions.
* Ternaries are detected everywhere; comprehension conditions (`[x for x in
  y if x]`) are not classified as conditionals.
* `.py` files with syntax errors are recorded in
  `RepositoryCodeAnalysis.errors` instead of failing the whole analysis.

## License

See [LICENSE](LICENSE).
