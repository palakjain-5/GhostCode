"""Tests for the GhostCode Python code analyzer.

The suite is split into two groups:

* Unit tests that run against small temporary source files/directories
  created on the fly — fully deterministic, no external dependencies.
* An integration test against the controlled PayGuard test repository
  (skipped automatically when that repository is not present).

Line numbers are only asserted indirectly (via ordering); no absolute line
numbers of real-world code are hard-coded.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ghostcode.code_analyzer import (
    ClassInfo,
    FunctionInfo,
    InvalidPathError,
    InvalidSourceError,
    PathNotFoundError,
    PythonCodeAnalyzer,
    PythonFileAnalysis,
    RepositoryCodeAnalysis,
)

#: Controlled, read-only test repository (see README.md).
PAYGUARD_PATH = Path.home() / "Projects" / "PayGuard"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write(root: Path, relative: str, source: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return path


def _analyze(tmp_path: Path, source: str, name: str = "sample.py") -> PythonFileAnalysis:
    path = _write(tmp_path, name, source)
    return PythonCodeAnalyzer(tmp_path).analyze_file(path)


# ---------------------------------------------------------------------------
# 1-2. Function detection and arguments
# ---------------------------------------------------------------------------


def test_simple_function_is_detected(tmp_path: Path) -> None:
    analysis = _analyze(
        tmp_path,
        'def greet(name):\n    """Say hello."""\n    message = "Hello, " + name\n    return message\n',
    )
    assert [f.name for f in analysis.functions] == ["greet"]
    fn = analysis.functions[0]
    assert isinstance(fn, FunctionInfo)
    assert fn.line_number == 1
    assert fn.end_line_number >= fn.line_number
    assert fn.decorators == []
    assert analysis.classes == []


def test_function_arguments_are_extracted(tmp_path: Path) -> None:
    analysis = _analyze(
        tmp_path,
        "def configure(a, b=2, *args, verbose=True, **kwargs):\n"
        "    return a\n"
        "\n"
        "@dispatcher.register('task')\n"
        "def handle():\n"
        "    return None\n",
    )
    assert analysis.functions[0].arguments == [
        "a",
        "b",
        "args",
        "verbose",
        "kwargs",
    ]
    assert analysis.functions[1].arguments == []

    # Decorators are recorded without the leading "@".
    assert analysis.functions[1].decorators == ["dispatcher.register('task')"]


# ---------------------------------------------------------------------------
# 3. Function call detection
# ---------------------------------------------------------------------------


def test_function_calls_are_detected(tmp_path: Path) -> None:
    analysis = _analyze(
        tmp_path,
        "def check(transaction_id):\n"
        "    if is_duplicate(transaction_id):\n"
        "        audit_log('duplicate')\n"
        "        return False\n"
        "    processor.settle(transaction_id)\n"
        "    return True\n",
    )
    # File-level: every call in the file.
    names = [c.function_name for c in analysis.function_calls]
    assert names == ["is_duplicate", "audit_log", "settle"]
    assert all(c.line_number > 0 for c in analysis.function_calls)

    # Qualified names keep the expression as written.
    qualified = [c.qualified_name for c in analysis.function_calls]
    assert qualified == ["is_duplicate", "audit_log", "processor.settle"]

    # Function-level: scoped to the owning function.
    fn = analysis.functions[0]
    assert [c.function_name for c in fn.calls] == [
        "is_duplicate",
        "audit_log",
        "settle",
    ]


# ---------------------------------------------------------------------------
# 4. Return detection
# ---------------------------------------------------------------------------


def test_return_statements_are_detected(tmp_path: Path) -> None:
    analysis = _analyze(
        tmp_path,
        "def lookup(key):\n"
        "    if not key:\n"
        "        return\n"
        "    return {'key': key}\n"
        "def fallback():\n"
        "    return None\n",
    )
    returns = analysis.return_statements
    assert len(returns) == 3
    assert all(r.line_number > 0 for r in returns)

    # Bare return has no value; others carry the unparsed expression.
    assert returns[0].value is None
    assert returns[1].value == "{'key': key}"
    assert returns[2].value == "None"

    # Scoped per function.
    assert [r.value for r in analysis.functions[0].returns] == [
        None,
        "{'key': key}",
    ]


# ---------------------------------------------------------------------------
# 5. Conditional detection
# ---------------------------------------------------------------------------


def test_conditionals_are_detected(tmp_path: Path) -> None:
    analysis = _analyze(
        tmp_path,
        "def classify(value):\n"
        "    if value > 10:\n"
        "        kind = 'large'\n"
        "    elif value > 5:\n"
        "        kind = 'medium'\n"
        "    else:\n"
        "        if value > 0:\n"
        "            kind = 'small'\n"
        "    label = 'yes' if value else 'no'\n"
        "    return kind, label\n"
        "def identity(value):\n"
        "    return value\n",
    )
    kinds = [c.kind for c in analysis.conditionals]
    # if / elif / (nested if, not elif) / ternary
    assert kinds == ["if", "elif", "if", "ternary"]

    fn = analysis.functions[0]
    assert fn.has_conditions is True
    assert analysis.functions[1].has_conditions is False


def test_conditionals_include_expression(tmp_path: Path) -> None:
    """Conditionals carry the unparsed condition text (added for drift
    evidence). Existing line/kind consumers are unaffected."""
    analysis = _analyze(
        tmp_path,
        "def check(user, txn_id, seen):\n"
        "    if user is None or txn_id in seen:\n"
        "        return False\n"
        "    return True\n",
    )
    expressions = [c.expression for c in analysis.conditionals]
    assert expressions == ["user is None or txn_id in seen"]
    # Round-trips through serialization alongside the original fields.
    assert analysis.to_dict()["conditionals"][0]["expression"] == (
        "user is None or txn_id in seen"
    )


# ---------------------------------------------------------------------------
# 6. Loop detection
# ---------------------------------------------------------------------------


def test_loops_are_detected(tmp_path: Path) -> None:
    analysis = _analyze(
        tmp_path,
        "def total(numbers):\n"
        "    result = 0\n"
        "    for n in numbers:\n"
        "        result += n\n"
        "    while result < 0:\n"
        "        result = 0\n"
        "    return result\n"
        "def identity(value):\n"
        "    return value\n",
    )
    assert [loop.kind for loop in analysis.loops] == ["for", "while"]

    fn = analysis.functions[0]
    assert fn.has_loops is True
    assert analysis.functions[1].has_loops is False


# ---------------------------------------------------------------------------
# 7. Exception/raise detection
# ---------------------------------------------------------------------------


def test_exceptions_are_detected(tmp_path: Path) -> None:
    analysis = _analyze(
        tmp_path,
        "def withdraw(amount, ledger):\n"
        "    if amount <= 0:\n"
        "        raise ValueError('amount must be positive')\n"
        "    try:\n"
        "        ledger.append(amount)\n"
        "    except KeyError as exc:\n"
        "        raise RuntimeError('missing ledger') from exc\n"
        "    return amount\n",
    )
    kinds = [e.kind for e in analysis.exceptions]
    assert kinds == ["raise", "except", "raise"]

    value_error = analysis.exceptions[0]
    assert value_error.exception_type == "ValueError"
    assert value_error.message == "amount must be positive"

    handler = analysis.exceptions[1]
    assert handler.exception_type == "KeyError"
    assert handler.message is None

    runtime_error = analysis.exceptions[2]
    assert runtime_error.exception_type == "RuntimeError"
    assert runtime_error.message == "missing ledger"

    # Function-level: the raise statements of this function.
    fn = analysis.functions[0]
    assert [e.exception_type for e in fn.raises] == [
        "ValueError",
        "RuntimeError",
    ]


# ---------------------------------------------------------------------------
# 8-9. Class and method detection
# ---------------------------------------------------------------------------


def test_class_is_detected(tmp_path: Path) -> None:
    analysis = _analyze(
        tmp_path,
        "class Base:\n"
        "    pass\n"
        "\n"
        "@object\n"
        "class Derived(Base):\n"
        "    def describe(self):\n"
        "        return 'derived'\n",
    )
    assert [c.name for c in analysis.classes] == ["Base", "Derived"]
    base, derived = analysis.classes
    assert isinstance(base, ClassInfo)
    assert base.base_classes == []
    assert base.methods == []
    assert derived.base_classes == ["Base"]
    assert derived.decorators == ["object"]
    assert derived.end_line_number >= derived.line_number


def test_methods_are_detected(tmp_path: Path) -> None:
    analysis = _analyze(
        tmp_path,
        "class Processor:\n"
        "    def __init__(self, ledger=None):\n"
        "        self.ledger = ledger\n"
        "    def settle(self, payment):\n"
        "        self.ledger.append(payment)\n"
        "        return True\n",
    )
    processor = analysis.classes[0]
    assert [m.name for m in processor.methods] == ["__init__", "settle"]

    settle = processor.methods[1]
    assert settle.arguments == ["self", "payment"]
    assert [c.function_name for c in settle.calls] == ["append"]
    assert [r.value for r in settle.returns] == ["True"]

    # Methods live under their class, not in the module-level function list.
    assert analysis.functions == []
    # ...but they are counted in the repository totals (checked in the
    # recursive-repository test).


# ---------------------------------------------------------------------------
# 10. Import detection
# ---------------------------------------------------------------------------


def test_imports_are_detected(tmp_path: Path) -> None:
    analysis = _analyze(
        tmp_path,
        "import os\n"
        "import json as js\n"
        "from models import Payment, User\n"
        "from .helpers import util\n"
        "from audit import record\n",
    )
    assert len(analysis.imports) == 5

    plain = analysis.imports[0]
    assert (plain.kind, plain.module, plain.names, plain.alias) == (
        "import",
        "os",
        [],
        None,
    )

    aliased = analysis.imports[1]
    assert (aliased.kind, aliased.module, aliased.alias) == (
        "import",
        "json",
        "js",
    )

    from_models = analysis.imports[2]
    assert from_models.kind == "from"
    assert from_models.module == "models"
    assert from_models.names == ["Payment", "User"]

    relative = analysis.imports[3]
    assert relative.module == ".helpers"
    assert relative.names == ["util"]

    from_audit = analysis.imports[4]
    assert (from_audit.module, from_audit.names) == ("audit", ["record"])


# ---------------------------------------------------------------------------
# 11. Recursive repository analysis
# ---------------------------------------------------------------------------


def test_recursive_repository_analysis(tmp_path: Path) -> None:
    _write(tmp_path, "main.py", "import sys\n\nclass App:\n    def run(self):\n        return start()\n\ndef start():\n    return 1\n")
    _write(tmp_path, "pkg/__init__.py", "def setup():\n    return None\n")
    _write(tmp_path, "pkg/core.py", "import os\n\nclass Core:\n    def execute(self, job):\n        return job\n")

    analyzer = PythonCodeAnalyzer(tmp_path)
    found = analyzer.discover_python_files()
    root = tmp_path.resolve()
    assert [p.relative_to(root).as_posix() for p in found] == [
        "main.py",
        "pkg/__init__.py",
        "pkg/core.py",
    ]

    analysis = analyzer.analyze()
    assert isinstance(analysis, RepositoryCodeAnalysis)
    assert analysis.repository_path == root
    assert analysis.errors == []

    # Summary totals.
    assert analysis.total_python_files == 3
    # start + setup + App.run + Core.execute
    assert analysis.total_functions == 4
    assert analysis.total_classes == 2
    assert analysis.total_imports == 2
    # Only App.run makes a call: start()
    assert analysis.total_function_calls == 1

    # Module names are derived from the layout; __init__ maps to its package.
    modules = {f.module_name for f in analysis.files}
    assert modules == {"main", "pkg", "pkg.core"}

    # analyze_repository(path) is equivalent for the same root.
    again = analyzer.analyze_repository(tmp_path)
    assert again.to_dict() == analysis.to_dict()


# ---------------------------------------------------------------------------
# 12. Virtual-environment / cache directories are ignored
# ---------------------------------------------------------------------------


def test_cache_and_virtualenv_directories_are_ignored(tmp_path: Path) -> None:
    _write(tmp_path, "app.py", "def real():\n    return 1\n")
    for hidden in (
        ".venv/lib/site.py",
        "venv/lib/venv_file.py",
        "env/lib/env_file.py",
        "build/generated.py",
        "dist/bundle.py",
        ".git/hooks/pre_commit.py",
        "__pycache__/cached.py",
        ".pytest_cache/pytest_file.py",
        "node_modules/pkg/index.py",
    ):
        _write(tmp_path, hidden, "def should_not_appear():\n    return 1\n")

    analyzer = PythonCodeAnalyzer(tmp_path)
    found = analyzer.discover_python_files()
    assert [p.name for p in found] == ["app.py"]

    analysis = analyzer.analyze()
    assert analysis.total_python_files == 1
    assert [f.name for f in analysis.files[0].functions] == ["real"]

    # Additional user-supplied exclusions are honoured too.
    _write(tmp_path, "vendor/tool.py", "def vendored():\n    return 1\n")
    strict = PythonCodeAnalyzer(tmp_path, excluded_dirs={"vendor"})
    names = [p.name for p in strict.discover_python_files()]
    assert names == ["app.py"]
    # Without the extra exclusion the vendor file is included.
    assert [p.name for p in analyzer.discover_python_files()] == [
        "app.py",
        "tool.py",
    ]


# ---------------------------------------------------------------------------
# 13. Invalid Python source and path error handling
# ---------------------------------------------------------------------------


def test_invalid_python_source_is_reported_not_hidden(tmp_path: Path) -> None:
    _write(tmp_path, "good.py", "def ok():\n    return 1\n")
    _write(tmp_path, "broken.py", "def broken(:\n    pass\n")

    # Direct analysis raises a clear error.
    with pytest.raises(InvalidSourceError, match="Invalid Python source"):
        PythonCodeAnalyzer(tmp_path).analyze_file(tmp_path / "broken.py")

    # Repository analysis records the problem and keeps going.
    analysis = PythonCodeAnalyzer(tmp_path).analyze()
    assert [f.module_name for f in analysis.files] == ["good"]
    assert analysis.total_python_files == 1
    assert analysis.total_errors == 1

    error = analysis.errors[0]
    assert error.file_path == tmp_path.resolve() / "broken.py"
    assert error.error_type == "invalid-source"
    assert "Invalid Python source" in error.message

    # The structured report is serializable.
    assert error.to_dict()["error_type"] == "invalid-source"


def test_path_errors_are_clear(tmp_path: Path) -> None:
    # Missing path (repository and file level).
    missing = PythonCodeAnalyzer(tmp_path / "no-such-dir")
    with pytest.raises(PathNotFoundError, match="does not exist"):
        missing.analyze_repository()
    with pytest.raises(PathNotFoundError, match="does not exist"):
        missing.analyze()
    with pytest.raises(PathNotFoundError, match="does not exist"):
        PythonCodeAnalyzer(tmp_path).analyze_file(tmp_path / "missing.py")

    # A file where a directory is expected.
    single = _write(tmp_path, "one.py", "x = 1\n")
    with pytest.raises(InvalidPathError, match="not a directory"):
        PythonCodeAnalyzer(tmp_path).analyze_repository(single)

    # A directory where a file is expected.
    (tmp_path / "pkg").mkdir()
    with pytest.raises(InvalidPathError, match="not a file"):
        PythonCodeAnalyzer(tmp_path).analyze_file(tmp_path / "pkg")


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def test_analysis_serializes_to_plain_dicts(tmp_path: Path) -> None:
    _write(tmp_path, "app.py", "import sys\n\ndef main():\n    return run()\n")
    analysis = PythonCodeAnalyzer(tmp_path).analyze()

    data = analysis.to_dict()
    assert isinstance(data, dict)
    assert data["repository_path"] == str(tmp_path.resolve())
    assert data["total_python_files"] == 1
    assert data["total_functions"] == 1

    file_data = data["files"][0]
    assert isinstance(file_data, dict)
    assert isinstance(file_data["file_path"], str)
    assert file_data["module_name"] == "app"
    assert file_data["functions"][0]["name"] == "main"
    assert file_data["functions"][0]["calls"][0]["function_name"] == "run"
    # Everything is plain built-in types (JSON-ready).
    import json

    json.dumps(data)


# ---------------------------------------------------------------------------
# 14. PayGuard integration test
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not PAYGUARD_PATH.exists(),
    reason="PayGuard test repository not found at ~/Projects/PayGuard",
)
def test_payguard_repository_analysis() -> None:
    analysis = PythonCodeAnalyzer(PAYGUARD_PATH).analyze()

    # Python files and functions are discovered (no hard-coded file list).
    assert analysis.total_python_files >= 5
    assert analysis.total_functions >= 5
    assert analysis.total_classes >= 1
    assert analysis.errors == []

    # Important structural calls are found through AST structure alone.
    call_names = {
        call.function_name
        for file in analysis.files
        for call in file.function_calls
    }
    assert "validate_email" in call_names  # email validation
    assert "_failed_login" in call_names  # login failure handling
    assert "record" in call_names  # audit logging
    assert "_settle" in call_names  # payment settlement

    # Calls are also attributed to the functions that contain them.
    owners = {
        fn.name
        for file in analysis.files
        for fn in file.functions
        for call in fn.calls
        if call.function_name == "_failed_login"
    }
    assert "authenticate_password" in owners
