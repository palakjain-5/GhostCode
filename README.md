# GhostCode

**Developer Intent Drift Detection System** — *under active development.*

GhostCode analyses a local Git repository and tries to understand the
*intent* behind the code — as recorded in commit history, commit messages and
code structure — and then compares that intent against the current Python
implementation. The goal is to flag places where the code may have drifted
away from what the developers originally intended.

> ⚠️ **Project status:** GhostCode is a work in progress. **Six pipeline
> stages currently exist** — the Git History Analyzer, the Python Code
> Analyzer, Intent Extraction, Intent Drift Detection, Risk Scoring and
> the Streamlit Dashboard. Database storage, LLM integration and
> deployment are future work.
>
> GhostCode extracts structural evidence from Python source code and
> structured intent from Git history, then compares them, scores the
> resulting drift and presents everything in a dashboard: this phase
> implements the read-only Streamlit presentation layer.

## Intended pipeline

```
Git Repository
      ↓
Git History Analyzer        ← implemented
      ↓
Python Code Analyzer        ← implemented
      ↓
Intent Extraction           ← implemented
      ↓
Intent vs Implementation    ← implemented
Comparison + Drift Detection
      ↓
Risk Scoring                ← implemented
      ↓
Streamlit Dashboard         ← implemented (this phase)
```

## Current development phase

| Stage | Status |
| --- | --- |
| Git History Analyzer | ✅ implemented |
| Python Code Analyzer (AST) | ✅ implemented |
| Intent Extraction | ✅ implemented |
| Intent vs Implementation Comparison + Drift Detection | ✅ implemented |
| Risk Scoring | ✅ implemented |
| Streamlit Dashboard | ✅ implemented (this phase) |

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
| Conditionals | `if`, `elif`, ternary expressions (line, kind and the unparsed condition text) |
| Loops | `for` (incl. `async for`), `while` |
| Returns | line and the unparsed return expression |
| Exceptions | `raise` (type + literal message) and `except` handlers (type) |
| Size | lines of code (non-blank, non-comment) |

Repository-level summaries (`RepositoryCodeAnalysis`): total Python files,
total functions (including methods), total classes, total imports, total
function calls — plus a structured `errors` list so files with invalid
Python source are reported instead of silently hidden.

> The analyzer only answers *"what is structurally present?"*. It does not
> judge correctness and does not infer intent — the intent-vs-implementation
> comparison and drift detection are performed by the drift-detection stage.

## Purpose of `IntentExtractor`

`IntentExtractor` turns the commit messages produced by `GitHistoryAnalyzer`
into structured **intent** records — a reliable, explainable baseline of *what
the developers said they were doing*, to be compared later against *what the
code currently does*.

> **Intent extraction in this phase is deterministic and heuristic. It does not use an LLM.**

It uses only controlled vocabularies and plain Python string matching — no
APIs, no network, no embeddings, no models. The same message always yields
the same `Intent`.

> **Intent extraction does not determine whether the current implementation satisfies the intent. That comparison is performed by `IntentDriftDetector` (see below).**

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

## Purpose of `IntentDriftDetector`

`IntentDriftDetector` is the drift-detection stage: it compares the
*historical intent* (from `GitHistoryAnalyzer` commits +
`IntentExtractor`) against the *current code structure* (from
`PythonCodeAnalyzer`) across chronological commits and reports places where
a previously established behavior is no longer represented in later code.

> **Drift detection is deterministic and heuristic. It does not use an
> LLM, and it does not score risk itself — the risk scoring stage that
> follows scores each drift finding (see “Risk Scoring” below).**

### How detection works

1. **Chronology** — commits are sorted by `committed_at` (input order is
   irrelevant), and an `Intent` is extracted for each one.
2. **Confidence gate** — original intents with confidence below
   `0.5` (`DEFAULT_MIN_INTENT_CONFIDENCE`) never anchor a drift claim; they
   are skipped with a documented threshold instead of guessed at.
3. **Pairing** — for every confident original, the *nearest later related
   commit* is found: related means a **shared changed file** or a **shared
   non-`unknown` category**. Unrelated commits are never paired.
4. **Behavior check** — the original intent is checked against the current
   code with a rule chosen from its action/category (table below). The
   check returns `present` / `weakened` / `partial` / `absent`.
5. **Verdict** — related pairs always produce a comparison row (including
   `severity = "none"` for preserved intents). Confident intents with *no*
   related later commit are still checked against the current state as a
   safety net — but they only produce a row when something was actually
   lost.

### Behavior rules

| Rule | Chosen when | What must be in the current code |
| --- | --- | --- |
| `logging-coverage` | `action == log` or category `logging` | Every sibling flow (name-stem families like `authenticate_*`, falling back to category-related branched functions) can still reach a logging sink (`record`, `audit`, ...) within 4 call hops |
| `validation-presence` | `action == validate` or category `validation` | The intent's marker function (e.g. `validate_email`) exists **and** is called somewhere |
| `prevention-guard` | `action == prevent` or `restrict` | A guard matching the *distinctive* object marker (`duplicate`) — as a function, call or condition — or tracked domain state that is written **and** consulted. State that is written but never read means the protection is unenforced |
| `generic-presence` | everything else | Functions matching the intent object (or, failing that, its category) still exist and are wired up |

Structural evidence comes exclusively from the AST analyzer's current
output — function names, call names, call-graph reachability and conditional
expressions. Established files come from the original commit's
`changed_files`.

### Drift types and severity

| Status | Drift type | Severity | Meaning |
| --- | --- | --- | --- |
| `present` | `none` | `none` | Behavior still represented — no significant drift |
| `weakened` | `behavior_weakened` | `low` | Present but no longer connected to the paths that use it |
| `partial` | `behavior_partial` | `medium` | Some flows still have it, others lost it |
| `absent` | `behavior_lost` | `high` | No longer represented in later code |

`DriftResult.confidence` is a **heuristic score, not a probability**: the
original intent's confidence scaled by an evidence-strength factor
(1.00 for a full loss, 0.90 for partial/preserved, 0.85 for weakened) and
capped at `0.95` — the detector never claims certainty.

### Drift result data model

Every comparison returns a `DriftResult` (all fields serializable via
`to_dict()`):

| Field | Type | Meaning |
| --- | --- | --- |
| `original_commit_hash` / `original_commit_message` | `str` | Commit where the original intent was established |
| `later_commit_hash` / `later_commit_message` | `str` | Later commit the drift is anchored to |
| `original_intent` / `later_intent` | `Intent` | The two intents being compared (action, category, object, ...) |
| `drift_type` | `str` | One of `DRIFT_TYPES` (`none`, `behavior_weakened`, `behavior_partial`, `behavior_lost`) |
| `severity` | `str` | One of `SEVERITIES` (`none`, `low`, `medium`, `high`) |
| `reason` | `str` | One-sentence human explanation |
| `evidence` | `list[str]` | Comparison evidence: pairing basis, both intents, category/action continuity, rule used |
| `implementation` | `list[str]` | Structural evidence about the current code (per-function coverage, guard presence) |
| `confidence` | `float` | Heuristic score in `[0.0, 1.0]` |

`DriftReport` wraps all rows for a repository and exposes `.drifted`
(any severity above `none`) and `.significant` (`medium`/`high`).

### Example output (real output from `python3 demo.py drift`)

```
DRIFT DETECTED
Original commit: Log every failed login attempt
Later commit: Improve authentication flow

Original intent:
action = log
category = authentication
object = failed login attempt

Later implementation:
- rule: logging coverage over 2 function(s) [sibling name-stem family]
- authenticate_password -> logging present (reaches 'record')
- authenticate_token -> logging absent

Severity:
MEDIUM

Reason:
A previously established authentication behavior was partially lost during refactoring.
```

The three intentional scenarios of the controlled PayGuard test repository
are all detected: email validation stays **preserved** (no drift), the
lost logging on the token authentication path is **MEDIUM**, and the
removed duplicate-payment guard (`processed_transaction_ids` written but
never consulted) is **HIGH**.

### Intent drift detection limitations

* Pairing requires a shared changed file or a shared non-`unknown`
  category; an unrelated-looking refactor that silently breaks something
  is only caught by the safety-net check against the current state.
* Commits are ordered by `(committed_at, hash)`; two commits made within
  the same second are therefore ordered by hash, which is arbitrary in
  practice. Real histories are almost always seconds/minutes apart, but
  scripted repositories that create commits in a tight loop can exhibit
  order-sensitive results.
* Coverage/guard checks are name-based: renaming a behavior function
  beyond all intent markers can look like a loss, and an inline
  membership guard written without any marker keyword is invisible to the
  prevention rule.
* Call-graph reachability does not verify that a logging call sits on the
  *failure* branch — only that the sink is reachable.
* Only the current working tree is analyzed; historical file contents are
  never read, so drift is always measured against today's code.
* Severity and confidence are deterministic heuristics, not risk — the
  detector does not say *how bad* a drift is; that judgment belongs to the
  risk scoring stage below.

## Risk Scoring

`risk_scorer` is the fifth pipeline stage: it consumes the
`DriftResult` rows produced by `IntentDriftDetector` and assigns each one a
**risk score from 0 to 100** representing the *potential engineering impact*
of the detected drift, together with a severity, the exact factors that
produced the score, and a prose explanation.

> **The score is deterministic and explainable — and it is explicitly NOT a
> probability.** It is a hand-written weighted heuristic with published
> weights; “87/100” means “weighed factors added up to 87”, never “87%
> likely to fail”. Nothing in this stage uses an LLM, a model, randomness
> or a network.

### What the score means

* **0** — no drift (or a preserved/benign change): nothing to act on.
* **20–49 (MEDIUM)** — a real but contained behavior change.
* **50–74 (HIGH)** — meaningful loss in an important area, or a broad
  loss backed by strong evidence.
* **75–100 (CRITICAL)** — a significant loss in a sensitive area
  (payment / security), usually with high-confidence, direct AST evidence.

### How scoring works

**1. Baseline from the drift verdict** (highest applicable tier wins):

| Drift verdict | Baseline |
| --- | --- |
| no significant drift / preserved / benign (`drift_type = none`) | **0** |
| weakened or partial drift | **35** |
| lost drift (not classified `high`) | **60** |
| significant / high drift (`severity = high`) | **70** |

When the baseline is 0, **no contextual factor is applied at all** — the
score is exactly 0, so a preserved comparison can never look risky (no
false positives).

**2. Contextual factors** (only for actual drift), summed on top:

| Factor | Condition | Points |
| --- | --- | --- |
| Impact area | cleanup / refactor / general | +0 |
| | validation | +5 |
| | authentication | +10 |
| | logging / audit | +10 |
| | payment / transaction | +20 |
| | security / access control | +20 |
| Scope | single function/file | +0 |
| | multiple functions | +5 |
| | multiple files | +10 |
| Confidence | `≥ 0.90` / `≥ 0.75` / `< 0.75` | +10 / +5 / +0 |
| Evidence strength | direct AST rule / heuristic rule | +10 / +0 |

* The **impact area** comes from the original intent's category (the
  domain where the behavior was established), mapped through the explicit
  `AREA_IMPACT` table — every category has a listed weight, nothing is
  defaulted silently.
* **Scope** is read from the drift evidence: two or more shared changed
  files → `+10`; otherwise two or more cited functions → `+5`.
* **Direct AST evidence** means the behavior rule inspected concrete
  structure (`logging-coverage`, `validation-presence`,
  `prevention-guard`, `file-presence`); the name-matching
  `generic-presence` rule counts as indirect (`+0`).

**3. Cap** — the raw total is capped at `100` (the pre-cap total is kept
as `raw_score`).

**4. Severity mapping** — `0–19 → LOW`, `20–49 → MEDIUM`,
`50–74 → HIGH`, `75–100 → CRITICAL`, plus one explicit **floor rule**: a
drift whose own severity is `high` in a security/payment-sensitive area is
**never mapped below `HIGH`**, regardless of how few contextual factors
applied — a significant loss in those areas cannot become `LOW` just
because the context is thin. The floor only ever raises a severity, never
lowers one.

### Risk data model

Every scored finding is a `RiskScore` dataclass, JSON-serializable via
`to_dict()`:

| Field | Type | Meaning |
| --- | --- | --- |
| `score` | `int` | Final score in `[0, 100]` (raw total capped) |
| `severity` | `str` | `LOW` / `MEDIUM` / `HIGH` / `CRITICAL` |
| `factors` | `list[RiskFactor]` | Every applied factor (`code`, `label`, `points`); their points always sum to `raw_score` |
| `explanation` | `str` | Prose explanation of why the score was produced |
| `affected_area` | `str` | Domain the drift affects (e.g. `payment`, `authentication`) |
| `confidence` | `float` | Confidence of the underlying drift detection |
| `drift_type` | `str` | `none` / `behavior_weakened` / `behavior_partial` / `behavior_lost` |
| `source_commit` | `str` | Commit where the original intent was established |
| `detected_commit` | `str` | Later commit where the drift was detected |
| `raw_score` | `int` | Factor sum before the cap at 100 |

### API

```python
from ghostcode.risk_scorer import calculate_risk, calculate_risk_report

score = calculate_risk(drift_result)          # one finding -> RiskScore
report = calculate_risk_report(drift_report)  # repository -> RiskReport
```

`calculate_risk_report` returns a `RiskReport` with every drift finding
scored (preserved comparisons are not findings) and a repository summary:

```python
report.to_dict()
# {
#   "total_findings": 2,
#   "high_risk_findings": 2,   # HIGH or above
#   "critical_findings": 1,    # subset of the above
#   "overall_risk": 100,       # worst finding drives repository risk
#   "findings": [ ...RiskScore dicts... ],
# }
```

`resolve_severity(score, drift_severity, affected_area)` exposes the
range mapping and the floor rule on their own.

### Example (real output from `python3 demo.py risk ~/Projects/PayGuard`)

```
RISK SCORE: 100/100
SEVERITY: CRITICAL
Original commit: Prevent duplicate payment processing
Detected commit: Refactor payment processing
Affected area: payment
Confidence: 0.95

Factors:
+70 significant/high drift
+20 payment/transaction impact
+10 high confidence
+10 direct AST evidence

Raw total: 110
Final score: 100

Explanation:
"A previously established payment behavior is no longer represented in
later code. The affected area is payment/transaction processing. Drift was
detected at the later commit 'Refactor payment processing'. The
prevention-guard rule inspects concrete AST structure (functions, calls
and state), so the evidence is direct. Weighted factors total 110, capped
to 100."
```

On the controlled PayGuard repository the three scenarios separate
exactly as intended: the preserved email validation scores **0 / LOW** (no
finding at all), the partially lost failed-login logging scores in the
**HIGH** band with authentication factors applied, and the lost
duplicate-payment guard scores the **highest** (capped **100 /
CRITICAL**) with the payment impact factor — strictly above the
authentication scenario.

### Risk scoring limitations

* The weights are an explainable prototype heuristic, **not** a
  calibrated probability, not machine learning, and not empirically
  validated — they encode a documented engineering judgment.
* Impact area is derived from the intent category of the original commit,
  so a misclassified category (or `unknown`) falls back to `general` (+0)
  and scores lower than a human might expect.
* Scope is read from the drift evidence (shared files and cited
  functions); behaviors spanning files beyond the pairing file list are
  under-counted.
* Evidence strength is per *rule*, not per line: a direct rule with thin
  evidence still receives the `+10`, and the heuristic `generic-presence`
  rule never does.
* The severity floor fires only for `severity = high` findings in
  payment/security/access-control areas; it does not intervene anywhere
  else.
* `overall_risk` is the worst finding's score — it does not accumulate
  across many small findings (the finding counts are reported alongside
  for that context).

## Dashboard

The **Streamlit dashboard** is the presentation stage: a read-only web UI
over the five analysis stages. It *consumes* the existing public APIs —
`GitHistoryAnalyzer.analyze()`, `PythonCodeAnalyzer.analyze()`,
`IntentExtractor.extract_all()`, `IntentDriftDetector.detect()` and
`calculate_risk_report()` — and contains **no analysis logic of its own**,
so the UI can never disagree with the CLI, the library or the tests.

> **The dashboard is presentation only.** It is deterministic (the same
> repository always renders the same numbers), offline (no network), and
> strictly read-only — the analyzed repository is never modified. There is
> no LLM, no database and no deployment machinery behind it: state lives
> in the browser session for the duration of the run.

### Running it

```bash
pip install -r requirements.txt          # now includes streamlit
streamlit run dashboard.py
```

Optionally preselect a repository path:

```bash
GHOSTCODE_REPOSITORY=~/Projects/PayGuard streamlit run dashboard.py
```

(`python3 dashboard.py` outside Streamlit just prints this usage hint.)

### What's on screen

Enter a local repository path in the sidebar and click **Analyze
repository** — the full pipeline runs once and six tabs render:

| Tab | Content |
| --- | --- |
| **Overview** | Repository header, headline metrics (commits, files, intents, comparisons, findings, overall risk) and the pipeline status with per-stage counts |
| **Git History** | Chronological commit table (hash, summary, author, date, files, +/−) with a per-commit detail view (full message, changed files) |
| **Python Code** | Code totals (files, functions, classes, imports, calls, parse errors) and a per-module table; parse errors are listed, never hidden |
| **Intents** | One row per commit — action, object, category, confidence — plus per-intent evidence |
| **Drift** | Comparison summary (comparisons / drifted / significant), one row per pair, and per-pair detail: severity, reason, structural implementation evidence and the full comparison evidence |
| **Risk** | Risk summary (findings, high-risk, critical, overall), one row per finding, and per-finding detail: every factor with its exact points, raw vs final score and the prose explanation |

### API

The view model is built by a pure function that needs no UI runtime:

```python
from ghostcode.dashboard import build_dashboard_model

model = build_dashboard_model("~/Projects/PayGuard")
if model.error:
    print("failed:", model.error)
else:
    print(model.git["total_commits"], "commits")
    print(model.risk["total_findings"], "findings,",
          "overall", model.risk["overall_risk"])
    payload = model.to_dict()          # plain dict, JSON-serializable
```

`DashboardModel` carries one section per stage (`git`, `code`, `intents`,
`drift`, `risk`) plus `error` — failures never raise, they are captured
as a readable message for the UI. `ghostcode.dashboard` imports
Streamlit *lazily inside the view functions*, so importing the builder
(and the package root) does not require Streamlit.

### Dashboard limitations

* The UI is a viewer, not an editor: no filtering, search or pagination —
  very large histories render as one long table.
* Analysis results are held in the session (and optionally cached by
  Streamlit); nothing is persisted, so a refresh re-runs the pipeline.
* One repository is analyzed per session (changing the path re-analyzes).
* All upstream limitations apply unchanged: heuristics stay heuristics,
  and the dashboard displays them as such (confidence and risk captions
  say so on screen).
* The dashboard is a development/demo surface — authentication, multi-user
  access and deployment are explicitly out of scope for this phase.

## Installation

Requires Python 3.

```bash
cd GhostCode
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Dependencies: `GitPython` and `streamlit` (runtime) plus `pytest` (tests).
The analysis stages — code analyzer, intent extractor, drift detector and
risk scorer — use only the Python standard library; Streamlit is required
solely by the dashboard and is imported lazily, so everything else works
without it. Python 3.9+ is required (`ast.unparse`).

## Running the tests

```bash
pytest
```

The suite covers all six stages:

* **Git History Analyzer** — repository validation, error handling, branch
  detection (including detached HEAD), commit messages/hashes, changed files
  and insertion/deletion statistics.
* **Python Code Analyzer** — function/class/method detection, arguments,
  calls, returns, conditionals (including their expressions), loops,
  exceptions, imports, recursive repository scanning, ignored directories
  (venvs/caches), invalid-source reporting and serialization.

* **Intent Extraction** — action normalization (`Added → add`), category
  detection across the controlled vocabulary, object and scope extraction,
  deterministic confidence behaviour, evidence generation, serialization,
  and semantic checks of all seven PayGuard commit messages.

* **Intent Drift Detection** — no drift for preserved behavior, partial
  (medium) drift when a sibling flow loses logging, significant (high) drift
  when a prevention guard disappears, unrelated commits staying unpaired,
  multiple independent intents, low-confidence/unknown intents being
  skipped, chronological pairing regardless of input order, serialization —
  plus the three intentional PayGuard scenarios (preserved / medium / high).

* **Risk Scoring** — zero/minimal risk for no drift and preserved intents,
  the documented baseline tiers (0/35/60/70), every impact-area weight
  (validation +5 … payment/security +20), single vs multi-function vs
  multi-file scope, confidence and evidence-strength adjustments, the cap
  at 100 with the preserved raw total, the severity range boundaries and
  the sensitive-area floor rule, factor-sum invariants, JSON
  serialization, report rollups — plus the three PayGuard scenarios
  (preserved scoring 0/LOW, authentication ≥ MEDIUM, payment highest at
  HIGH/CRITICAL, C strictly above B).

* **Dashboard** — the pure pipeline builder across all five analysis
  stages (deterministic output, JSON serialization, commit/file/intent
  shapes, cross-stage drift↔risk consistency), captured error models for
  missing/non-git/empty repositories, severity icons, tab order, the
  non-Streamlit usage hint — plus Streamlit `AppTest` UI tests that run
  the real `dashboard.py` script in-process (preselected repository,
  intro state, error state, Analyze-button interaction and the risk
  findings view), skipped automatically when Streamlit is unavailable.

* **End-to-End Validation** (`tests/test_pipeline_e2e.py`) — the complete
  chain (Git history → code → intent → drift → risk → dashboard model) on
  purpose-built repositories with cross-stage linkage checks, dashboard ↔
  independent-pipeline equivalence, three-run determinism at every stage,
  and error isolation: invalid paths, non-Git directories, repositories
  without commits or without Python files, malformed and unreadable
  sources, and low-confidence intents — none may crash an unrelated stage.
  Covers no-drift, significant-drift and multiple-findings scenarios, the
  dashboard's success and error states, and PayGuard's documented
  end-to-end numbers (see the validation workflow below).

All seven suites include an integration test against the PayGuard test
repository (automatically skipped if PayGuard is not present).

### Validation workflow

To validate the complete system as an integrated application:

```bash
# 1. Full regression suite (all seven suites)
pytest -q

# 2. End-to-end validation only (pipeline, PayGuard, dashboard states)
pytest tests/test_pipeline_e2e.py -q

# 3. Demo workflow, stage by stage, against the controlled PayGuard repo
python3 demo.py git ~/Projects/PayGuard      # Git history analyzer
python3 demo.py code ~/Projects/PayGuard     # Python code analyzer
python3 demo.py intent ~/Projects/PayGuard   # intent extraction
python3 demo.py drift ~/Projects/PayGuard    # drift detection
python3 demo.py risk ~/Projects/PayGuard     # risk scoring
GHOSTCODE_REPOSITORY=~/Projects/PayGuard streamlit run dashboard.py
```

Expected PayGuard end-to-end results (asserted by the E2E suite):

| Check | Expected |
| --- | --- |
| Commits | 7 |
| Extracted intents | 7 (one per commit) |
| Drift comparisons | 3 (2 with drift, both significant) |
| Risk findings | 2 (both HIGH or above, 1 CRITICAL) |
| Overall risk | 100 |
| Scenario A — preserved email validation | 0 / LOW, no finding in the report |
| Scenario B — lost failed-login logging | 70 / HIGH |
| Scenario C — lost duplicate-payment guard | 100 / CRITICAL (capped from a raw total ≥ 100) |

PayGuard checks are skipped automatically when that repository is absent;
dashboard UI checks are skipped when Streamlit is not installed.

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

### Intent drift detection

```python
from ghostcode.git_analyzer import GitHistoryAnalyzer
from ghostcode.code_analyzer import PythonCodeAnalyzer
from ghostcode.drift_detector import IntentDriftDetector

history = GitHistoryAnalyzer("/Users/you/Projects/PayGuard").analyze()
code = PythonCodeAnalyzer("/Users/you/Projects/PayGuard").analyze()

report = IntentDriftDetector().detect(history.commits, code)

print(len(report.results), "comparisons")
print(len(report.significant), "significant (medium/high)")

for result in report.significant:
    print(result.severity.upper(), result.original_intent.category)
    print("  from:", result.original_commit_message.splitlines()[0])
    print("  to:  ", result.later_commit_message.splitlines()[0])
    print("  why: ", result.reason)
    for line in result.implementation:
        print("   ", line)

# A single intent can be checked against the current code directly:
from ghostcode.intent_extractor import extract_intent

commit = history.commits[1]
intent = extract_intent(commit.message, commit_hash=commit.hash)
check = IntentDriftDetector().check_behavior(intent, commit, code)
print(check.status, check.rule)
for line in check.evidence:
    print(" -", line)
```

### Risk scoring

```python
from ghostcode.git_analyzer import GitHistoryAnalyzer
from ghostcode.code_analyzer import PythonCodeAnalyzer
from ghostcode.drift_detector import IntentDriftDetector
from ghostcode.risk_scorer import calculate_risk, calculate_risk_report

history = GitHistoryAnalyzer("/Users/you/Projects/PayGuard").analyze()
code = PythonCodeAnalyzer("/Users/you/Projects/PayGuard").analyze()
drift = IntentDriftDetector().detect(history.commits, code)

# One finding at a time:
for finding in calculate_risk_report(drift).findings:
    print(f"{finding.score:>3}/100 {finding.severity:<8} {finding.affected_area}")
    for risk_factor in finding.factors:
        print("   ", risk_factor)          # e.g. "+20 payment/transaction impact"
    print("   ", finding.explanation)

# Or score a single drift result directly:
first = drift.results[0]
print(calculate_risk(first).score, calculate_risk(first).severity)

# Repository-level summary (all findings + overall risk):
report = calculate_risk_report(drift)
summary = report.to_dict()
print(summary["total_findings"], summary["overall_risk"])

# Every model exposes a deterministic plain-dict form:
data = report.findings[0].to_dict()
```

### Dashboard

```bash
streamlit run dashboard.py                          # opens the web UI
GHOSTCODE_REPOSITORY=~/Projects/PayGuard streamlit run dashboard.py
```

```python
# The same view model, without any UI runtime:
from ghostcode.dashboard import build_dashboard_model

model = build_dashboard_model("/Users/you/Projects/PayGuard")
assert model.ok, model.error
print(model.git["branch"], model.git["total_commits"])
print(len(model.drift["results"]), "comparisons")
print(model.risk["overall_risk"], "overall risk")
```

### Demo script

```bash
python3 demo.py git ~/Projects/PayGuard       # git history summary
python3 demo.py code ~/Projects/PayGuard      # python code summary
python3 demo.py code ~/Projects/PayGuard -v   # per-file breakdown
python3 demo.py intent ~/Projects/PayGuard    # extracted developer intents
python3 demo.py drift ~/Projects/PayGuard     # intent drift report
python3 demo.py drift ~/Projects/PayGuard -v  # + full evidence & confidence
python3 demo.py risk ~/Projects/PayGuard      # risk scores for each finding
python3 demo.py risk ~/Projects/PayGuard -v   # + hashes & full JSON summary
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

The drift command prints one comparison block per pair: a `DRIFT DETECTED`
header (or `NO SIGNIFICANT DRIFT` for preserved intents), the original and
later commit, the original intent, the later implementation's structural
evidence, the severity and a one-sentence reason (see the *Example output*
section of `IntentDriftDetector` above), followed by a summary:

```
Repository: PayGuard
Comparisons: 3
Drift detected: 2
Significant (medium/high): 2
```

The risk command runs the full pipeline (history → code → intent → drift →
risk) and prints one block per finding — `RISK SCORE: n/100`, the
severity, the original/detected commit, the affected area, the factor list
with their exact point contributions, the raw total vs the final score,
and the prose explanation (a full block is shown in the *Risk Scoring*
section above) — followed by the repository summary:

```json
{
  "total_findings": 2,
  "high_risk_findings": 2,
  "critical_findings": 1,
  "overall_risk": 100
}
```

Preserved comparisons produce no risk finding, so they never appear here.

## PayGuard — the controlled test repository

`~/Projects/PayGuard` is a small local Git repository with seven known
commits, used as the **controlled, read-only test repository** for GhostCode.
GhostCode never modifies it: it only reads its history, parses its source and
extracts intents from its messages. The integration tests assert that at
least seven commits and their known messages are detected, that Python files,
functions and important structural calls (e.g. `validate_email`,
`_failed_login`, `record`, `_settle`) are discovered through AST structure,
that every known commit yields the expected intent action/category, and that
the three intentional drift scenarios hold — email validation **preserved**
(no drift), failed-login logging **partially lost** on the token
authentication path after the authentication refactor (**MEDIUM**), and the
duplicate-payment guard no longer consulted after the payment refactor
(**HIGH**) — and that the three risk scenarios hold — the preserved
validation intent produces **no risk finding (0 / LOW, no false positive)**,
the lost failed-login logging scores **at least MEDIUM** with the
authentication impact factor applied, and the lost duplicate-payment guard
scores the **highest** of the three (**HIGH or CRITICAL**, payment impact
factor applied, strictly above the authentication scenario) — all through
semantic checks, with no hard-coded hashes, line numbers, complete output
strings, expected scores or repository-specific rules.

## Project structure

```
ghostcode/
│
├── README.md
├── requirements.txt
├── demo.py                     # small manual demo CLI (git + code + intent + drift + risk)
├── dashboard.py                # Streamlit entry point: `streamlit run dashboard.py`
│
├── src/
│   └── ghostcode/
│       ├── __init__.py
│       ├── git_analyzer.py     # GitHistoryAnalyzer + data models
│       ├── code_analyzer.py    # PythonCodeAnalyzer + data models
│       ├── intent_extractor.py # IntentExtractor + Intent model
│       ├── drift_detector.py   # IntentDriftDetector + DriftResult model
│       ├── risk_scorer.py      # risk scoring: RiskScore/RiskReport + calculate_risk
│       └── dashboard.py        # view model (build_dashboard_model) + Streamlit view
│
└── tests/
    ├── conftest.py
    ├── test_git_analyzer.py
    ├── test_code_analyzer.py
    ├── test_intent_extractor.py
    ├── test_drift_detector.py
    ├── test_risk_scorer.py
    └── test_dashboard.py
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
* Drift detection pairs commits only when they share a changed file or a
  non-`unknown` category; otherwise it falls back to a safety-net check
  against the current state (which reports only actual losses).
* Drift behavior checks are name-based (intent markers matched against
  function/call names and conditional text): renaming a behavior beyond all
  markers can resemble a loss, and call-graph reachability does not verify
  that a logging call sits on the failure branch.
* Drift is always measured against the current working tree; historical file
  contents are never read.
* Drift severity/confidence are deterministic heuristics — the *magnitude*
  judgment is made by the risk scoring stage, whose weights are a published
  heuristic rather than a calibrated probability (see the *Risk Scoring*
  limitations above).
* Risk scores inherit every upstream limitation (intent classification,
  name-based drift checks) and add their own: the impact area comes from
  the original commit's category, scope is read from the drift evidence
  text, evidence strength is judged per rule, and `overall_risk` is the
  worst finding rather than a cumulative measure.
* The dashboard is a read-only viewer: results live in the session (a
  refresh re-runs the pipeline), there is no filtering/pagination for very
  large repositories, and authentication or multi-user access are out of
  scope for this phase.

## License

See [LICENSE](LICENSE).
