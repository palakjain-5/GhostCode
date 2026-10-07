# GhostCode

**Developer Intent Drift Detection System** — *under active development.*

GhostCode analyses a local Git repository and tries to understand the
*intent* behind the code — as recorded in commit history, commit messages and
code structure — and then compares that intent against the current Python
implementation. The goal is to flag places where the code may have drifted
away from what the developers originally intended.

> ⚠️ **Project status:** GhostCode is a work in progress. **Intent drift
> detection is not implemented yet.** Three pipeline stages currently exist —
> the Git History Analyzer, the Python Code Analyzer and Intent Extraction.
> The intent-vs-implementation comparison, drift detection, risk scoring and
> the dashboard are all future work.
>
> GhostCode currently extracts structural evidence from Python source code.
> Intent extraction in this phase is deterministic and heuristic; drift detection is implemented in a later phase.

## Intended pipeline

```
Git Repository
      ↓
Git History Analyzer        ← implemented
      ↓
Python Code Analyzer        ← implemented
      ↓
Intent Extraction           ← implemented (this phase)
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
| Python Code Analyzer (AST) | ✅ implemented |
| Intent Extraction | ✅ implemented (this phase) |
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

## Purpose of `IntentExtractor`

`IntentExtractor` turns the commit messages produced by `GitHistoryAnalyzer`
into structured **intent** records — a reliable, explainable baseline of *what
the developers said they were doing*, to be compared later against *what the
code currently does*.

> **Intent extraction in this phase is deterministic and heuristic. It does not use an LLM.**

It uses only controlled vocabularies and plain Python string matching — no
APIs, no network, no embeddings, no models. The same message always yields
the same `Intent`.

> **Intent extraction does not determine whether the current implementation satisfies the intent. That comparison is performed by a later drift-detection phase.**

### Intent data model

Every extraction returns an `Intent` dataclass, serializable with
`to_dict()` (plain, JSON-friendly types):

| Field | Type | Meaning |
| --- | --- | --- |
| `commit_hash` | `str` or `None` | Source commit (`None` for a raw message) |
| `commit_message` | `str` | The message the intent was extracted from |
| `action` | `str` or `None` | Normalized verb (e.g. `"prevent"`); `None` when no known verb is found |
| `object` | `str` | Main thing affected (conservative; falls back to the message itself) |
| `scope` | `str` or `None` | Where the intent applies (`"for ..."` phrase, otherwise the object) |
| `category` | `str` | One of the supported categories below |
| `keywords` | `list[str]` | Domain keywords that justified the category |
| `confidence` | `float` | Heuristic score in `[0.0, 1.0]` |
| `evidence` | `list[str]` | Human-readable reasons for every extraction decision |

### Supported categories

A small controlled vocabulary (`ghostcode.intent_extractor.CATEGORIES`), in
tie-break order — the first listed wins when scores are equal:

`validation`, `authentication`, `authorization`, `logging`, `payment`,
`security`, `refactoring`, `bug_fix`, `feature`, `data_processing`,
`testing`, `configuration`, `cleanup`, `unknown`

Nothing outside this list is ever produced.

### How the heuristics work

* **Action** — the first word in the message that normalizes to a known
  development verb (`add`, `implement`, `create`, `introduce`, `prevent`,
  `fix`, `remove`, `delete`, `refactor`, `improve`, `update`, `change`,
  `log`, `validate`, `handle`, `support`, `allow`, `restrict`, `secure`,
  `clean`, `simplify`). Regular inflections are normalized
  (`Added → add`, `Adding → add`, `Improved → improve`,
  `Preventing → prevent`, `Simplified → simplify`), plus the explicit
  aliases `cleanup → clean` and `bugfix → fix`. If nothing matches, the
  action is `None` — a verb is never invented.
* **Category** — each category declares domain keywords or phrases (e.g.
  `payment → {payment, transaction, settlement, processing}`).
  Keywords are matched as **whole words** against the lowercased message, so
  the `log` signal can never fire inside the word `login`. The category with
  the most matched keywords wins; ties are broken by the fixed order of
  `CATEGORIES` (fully deterministic, and explained in the evidence). With no
  match at all the category is `unknown`.
* **Object** — the message after the action verb (the `up` particle of
  `clean up` is dropped), cut at an explicit `for <phrase>` and stripped of
  leading determiners (`the`, `every`, `a`, ...). If nothing can be
  identified, the commit message itself is used as the object — the
  extractor stays conservative and never fabricates content.
* **Scope** — an explicit `for <phrase>` (e.g. `... for user registration`)
  becomes the scope directly; otherwise a known action makes the object
  phrase its implicit scope; otherwise there is no scope (`None`).
* **Evidence** — every decision records *why*: the matched action keyword,
  each matched category keyword, competing category scores when several
  categories matched, the object phrase and the scope phrase — or explicit
  fallback notes such as `no known action verb found`.

### Confidence

`confidence` is a **heuristic score, not a statistical probability**. It is
a transparent checklist that adds up a fixed weight whenever a signal was
found:

| Signal found | Weight |
| --- | --- |
| a known action verb | 0.30 |
| a non-`unknown` category | 0.30 |
| an object distinct from the raw message | 0.25 |
| a scope phrase | 0.15 |

The total is rounded to two decimals and always lies in `[0.0, 1.0]`. A
message like `Initial PayGuard application` (category but no verb) scores
`0.30`; a fully decomposed message scores `1.00`.

### Example output (real output from `python3 demo.py intent`)

```
Commit:
Prevent duplicate payment processing

Intent:
Action: prevent
Object: duplicate payment processing
Category: payment
Scope: duplicate payment processing
Confidence: 1.00

Evidence:
- action keyword: prevent
- category keyword: payment
- category keyword: processing
- category scores: payment=2, data_processing=1
- object phrase: duplicate payment processing
- scope phrase: duplicate payment processing
```

### Intent extraction limitations

* It is keyword/verb heuristics, not language understanding: unusual
  phrasing, typos, unconventional commit styles or non-English messages can
  yield `unknown` / `None` fields — by design, since nothing is fabricated.
* Only the subject (first line) of a multi-line commit message is analysed;
  the body is stored but ignored.
* The object/scope split is textual, so leading adjectives stay in the
  object (`duplicate payment processing`) even where a human might omit them.
* The confidence score measures *how many signals were found*, not whether
  the intent is correct — and never whether it is satisfied.
* The category vocabulary is deliberately small; specialised domains may
  fall into `unknown`.

## Installation

Requires Python 3.

```bash
cd GhostCode
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Dependencies: `GitPython` and `pytest` (nothing else). The code analyzer
uses only the Python standard library (`ast`), and the intent extractor uses
only the standard library as well. Python 3.9+ is required
(`ast.unparse`).

## Running the tests

```bash
pytest
```

The suite covers all three stages:

* **Git History Analyzer** — repository validation, error handling, branch
  detection (including detached HEAD), commit messages/hashes, changed files
  and insertion/deletion statistics.
* **Python Code Analyzer** — function/class/method detection, arguments,
  calls, returns, conditionals, loops, exceptions, imports, recursive
  repository scanning, ignored directories (venvs/caches), invalid-source
  reporting and serialization.

* **Intent Extraction** — action normalization (`Added → add`), category
  detection across the controlled vocabulary, object and scope extraction,
  deterministic confidence behaviour, evidence generation, serialization,
  and semantic checks of all seven PayGuard commit messages.

All three suites include an integration test against the PayGuard test
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

### Intent extraction

```python
from ghostcode.git_analyzer import GitHistoryAnalyzer
from ghostcode.intent_extractor import IntentExtractor, extract_intent

history = GitHistoryAnalyzer("/Users/you/Projects/PayGuard").analyze()
intents = IntentExtractor().extract_all(history.commits)

for intent in intents:
    print(intent.action, intent.category, intent.confidence)

# Raw commit messages work too (handy for unit tests):
intent = extract_intent("Prevent duplicate payment processing")
print(intent.category)    # payment
print(intent.confidence)  # 1.0
print(intent.evidence)    # ['action keyword: prevent', 'category keyword: payment', ...]

# Every model exposes a deterministic plain-dict form:
data = intent.to_dict()
```

### Demo script

```bash
python3 demo.py git ~/Projects/PayGuard       # git history summary
python3 demo.py code ~/Projects/PayGuard      # python code summary
python3 demo.py code ~/Projects/PayGuard -v   # per-file breakdown
python3 demo.py intent ~/Projects/PayGuard    # extracted developer intents
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

The intent command prints the structured intent — action, object, category,
scope, confidence and evidence — for every commit (a full output block is
shown in the *Example output* section above).

## PayGuard — the controlled test repository

`~/Projects/PayGuard` is a small local Git repository with seven known
commits, used as the **controlled, read-only test repository** for GhostCode.
GhostCode never modifies it: it only reads its history, parses its source and
extracts intents from its messages. The integration tests assert that at
least seven commits and their known messages are detected, that Python files,
functions and important structural calls (e.g. `validate_email`,
`_failed_login`, `record`, `_settle`) are discovered through AST structure,
and that every known commit yields the expected intent action/category — all
through semantic checks, with no hard-coded hashes, line numbers, complete
output strings or repository-specific rules.

## Project structure

```
ghostcode/
│
├── README.md
├── requirements.txt
├── demo.py                     # small manual demo CLI (git + code + intent)
│
├── src/
│   └── ghostcode/
│       ├── __init__.py
│       ├── git_analyzer.py     # GitHistoryAnalyzer + data models
│       ├── code_analyzer.py    # PythonCodeAnalyzer + data models
│       └── intent_extractor.py # IntentExtractor + Intent model
│
└── tests/
    ├── conftest.py
    ├── test_git_analyzer.py
    ├── test_code_analyzer.py
    └── test_intent_extractor.py
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
* Intent extraction recognises a controlled vocabulary of verbs and domain
  keywords; it cannot understand arbitrary natural language, and messages it
  does not recognise fall back to category `unknown` / `None` fields rather
  than guessed answers.
* Only the subject line of a commit message is analysed.
* Intent extraction records what commits *say*, not what the code *does*; no
  comparison between the two happens yet (later phase).

## License

See [LICENSE](LICENSE).
