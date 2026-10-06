"""Python source code analyzer for GhostCode.

This module implements the second stage of the GhostCode pipeline: reading
the *current* Python source code of a repository and converting its
structure into plain, structured Python objects (dataclasses).

The analyzer uses Python's standard-library :mod:`ast` module. It only
answers the question "what is structurally present in the current code?".
It does not judge correctness, does not infer developer intent and does
not detect drift — those are later pipeline stages and are deliberately
not implemented here.

Design rules (shared with the Git history analyzer):

* Read-only: source files are never modified.
* Offline: no network calls, no external services.
* Deterministic: the same source always yields the same structured data.
* UI-independent and LLM-independent: pure ``ast`` analysis, no models.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Union

__all__ = [
    # Errors
    "CodeAnalyzerError",
    "PathNotFoundError",
    "InvalidPathError",
    "InvalidSourceError",
    "UnreadableFileError",
    # Data models
    "CallInfo",
    "ReturnInfo",
    "ConditionalInfo",
    "LoopInfo",
    "ExceptionInfo",
    "ImportInfo",
    "FunctionInfo",
    "ClassInfo",
    "PythonFileAnalysis",
    "FileAnalysisError",
    "RepositoryCodeAnalysis",
    # Analyzer
    "DEFAULT_EXCLUDED_DIRS",
    "PythonCodeAnalyzer",
]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class CodeAnalyzerError(Exception):
    """Base class for every error raised by the Python code analyzer."""


class PathNotFoundError(CodeAnalyzerError):
    """Raised when the supplied path does not exist."""


class InvalidPathError(CodeAnalyzerError):
    """Raised when the supplied path exists but has the wrong kind
    (a file where a directory was expected, or vice versa)."""


class InvalidSourceError(CodeAnalyzerError):
    """Raised when a file cannot be parsed as Python source code."""


class UnreadableFileError(CodeAnalyzerError):
    """Raised when a file cannot be read (permissions, encoding, I/O)."""


# ---------------------------------------------------------------------------
# Serialization helper
# ---------------------------------------------------------------------------


def _serialize(value: Any) -> Any:
    """Recursively convert a model (or nested model) into plain Python
    types: dataclasses become dicts, ``Path`` becomes ``str``, containers
    are converted element-wise. The result is deterministic and can be
    passed straight to ``json.dumps`` if a later phase ever needs it."""

    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (list, tuple)):
        return [_serialize(item) for item in value]
    if isinstance(value, dict):
        return {key: _serialize(item) for key, item in value.items()}
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: _serialize(getattr(value, f.name)) for f in fields(value)}
    return value


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class CallInfo:
    """A single function/method call found in the source."""

    #: Bare name that is called, e.g. ``_settle`` for ``self._settle(x)``.
    function_name: str
    #: Source line of the call.
    line_number: int
    #: Call expression as written, e.g. ``self._settle`` or ``len``.
    qualified_name: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Plain-dict representation of this model."""
        return _serialize(self)


@dataclass
class ReturnInfo:
    """A ``return`` statement."""

    #: Source line of the statement.
    line_number: int
    #: Unparsed return expression (e.g. ``"user"``, ``"None"``), or
    #: ``None`` for a bare ``return`` without a value.
    value: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Plain-dict representation of this model."""
        return _serialize(self)


@dataclass
class ConditionalInfo:
    """A conditional construct: ``if``, ``elif`` or a ternary expression."""

    #: Source line.
    line_number: int
    #: One of ``"if"``, ``"elif"``, ``"ternary"``.
    kind: str

    def to_dict(self) -> Dict[str, Any]:
        """Plain-dict representation of this model."""
        return _serialize(self)


@dataclass
class LoopInfo:
    """A loop construct."""

    #: Source line.
    line_number: int
    #: One of ``"for"``, ``"while"`` (``async for`` is reported as ``"for"``).
    kind: str

    def to_dict(self) -> Dict[str, Any]:
        """Plain-dict representation of this model."""
        return _serialize(self)


@dataclass
class ExceptionInfo:
    """An exception-related construct: a ``raise`` or an ``except`` handler."""

    #: Source line.
    line_number: int
    #: One of ``"raise"``, ``"except"``.
    kind: str
    #: Exception type as written (e.g. ``ValueError``), when available.
    exception_type: Optional[str] = None
    #: Literal exception message when the raise passes a string constant.
    message: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Plain-dict representation of this model."""
        return _serialize(self)


@dataclass
class ImportInfo:
    """An import statement (one entry per imported module)."""

    #: Source line.
    line_number: int
    #: One of ``"import"``, ``"from"``.
    kind: str
    #: Imported module as written (relative imports keep their dots,
    #: e.g. ``".helpers"``).
    module: str
    #: Names imported by a ``from``-import (empty for plain imports).
    names: List[str] = field(default_factory=list)
    #: Alias for plain imports (``import json as js`` -> ``"js"``).
    alias: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Plain-dict representation of this model."""
        return _serialize(self)


@dataclass
class FunctionInfo:
    """A function or method definition."""

    #: Function name (without ``def``).
    name: str
    #: Line of the ``def`` (or ``async def``) statement.
    line_number: int
    #: Line where the function body ends.
    end_line_number: int
    #: Parameter names in declaration order (positional-only markers and
    #: annotations are not included).
    arguments: List[str] = field(default_factory=list)
    #: Decorator expressions without the leading ``@`` (e.g. ``dataclass``).
    decorators: List[str] = field(default_factory=list)
    #: Calls made inside this function (its own body only; nested
    #: definitions own their calls).
    calls: List[CallInfo] = field(default_factory=list)
    #: Return statements inside this function.
    returns: List[ReturnInfo] = field(default_factory=list)
    #: Whether the function contains ``if``/``elif``/ternary conditions.
    has_conditions: bool = False
    #: Whether the function contains ``for``/``while`` loops.
    has_loops: bool = False
    #: ``raise`` statements inside this function.
    raises: List[ExceptionInfo] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Plain-dict representation of this model."""
        return _serialize(self)


@dataclass
class ClassInfo:
    """A class definition."""

    #: Class name.
    name: str
    #: Line of the ``class`` statement.
    line_number: int
    #: Line where the class body ends.
    end_line_number: int
    #: Base classes as written (e.g. ``["Base"]``).
    base_classes: List[str] = field(default_factory=list)
    #: Decorator expressions without the leading ``@``.
    decorators: List[str] = field(default_factory=list)
    #: Methods defined directly in this class.
    methods: List[FunctionInfo] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Plain-dict representation of this model."""
        return _serialize(self)


@dataclass
class PythonFileAnalysis:
    """Structural analysis of a single Python source file."""

    #: Absolute path of the analyzed file.
    file_path: Path
    #: Dotted module name relative to the analyzed root
    #: (e.g. ``"pkg.core"`` for ``pkg/core.py``).
    module_name: str
    #: Functions defined at module level (or nested inside functions).
    #: Methods are listed under their class instead.
    functions: List[FunctionInfo] = field(default_factory=list)
    #: Classes defined in the file.
    classes: List[ClassInfo] = field(default_factory=list)
    #: All import statements, in source order.
    imports: List[ImportInfo] = field(default_factory=list)
    #: Every call in the file (including calls inside functions).
    function_calls: List[CallInfo] = field(default_factory=list)
    #: Every ``if``/``elif``/ternary in the file.
    conditionals: List[ConditionalInfo] = field(default_factory=list)
    #: Every ``for``/``while`` loop in the file.
    loops: List[LoopInfo] = field(default_factory=list)
    #: Every ``return`` statement in the file.
    return_statements: List[ReturnInfo] = field(default_factory=list)
    #: Every ``raise`` and ``except`` in the file.
    exceptions: List[ExceptionInfo] = field(default_factory=list)
    #: Lines containing code (non-blank, non-comment-only).
    lines_of_code: int = 0

    def to_dict(self) -> Dict[str, Any]:
        """Plain-dict representation of this file analysis."""
        return _serialize(self)


@dataclass
class FileAnalysisError:
    """Structured report of a file that could not be analyzed.

    Files that fail parsing are recorded here instead of aborting the
    repository analysis, so problems are visible rather than hidden.
    """

    #: Absolute path of the file that failed.
    file_path: Path
    #: ``"invalid-source"`` (syntax/parse problem) or ``"unreadable"``
    #: (I/O or encoding problem).
    error_type: str
    #: Human-readable description of the problem.
    message: str

    def to_dict(self) -> Dict[str, Any]:
        """Plain-dict representation of this error report."""
        return _serialize(self)


@dataclass
class RepositoryCodeAnalysis:
    """Structural analysis of every Python file in a repository."""

    #: Absolute path of the analyzed repository/root.
    repository_path: Path
    #: Successfully analyzed files, in deterministic (sorted) order.
    files: List[PythonFileAnalysis] = field(default_factory=list)
    #: Files that could not be analyzed (see :class:`FileAnalysisError`).
    errors: List[FileAnalysisError] = field(default_factory=list)

    @property
    def total_python_files(self) -> int:
        """Number of successfully analyzed Python files."""
        return len(self.files)

    @property
    def total_functions(self) -> int:
        """Total number of function definitions, including class methods."""
        return sum(
            len(f.functions) + sum(len(c.methods) for c in f.classes)
            for f in self.files
        )

    @property
    def total_classes(self) -> int:
        """Total number of class definitions."""
        return sum(len(f.classes) for f in self.files)

    @property
    def total_imports(self) -> int:
        """Total number of import statements."""
        return sum(len(f.imports) for f in self.files)

    @property
    def total_function_calls(self) -> int:
        """Total number of function calls across all files."""
        return sum(len(f.function_calls) for f in self.files)

    @property
    def total_errors(self) -> int:
        """Number of files that could not be analyzed."""
        return len(self.errors)

    def to_dict(self) -> Dict[str, Any]:
        """Plain-dict representation, including the summary totals."""
        data = _serialize(self)
        data.update(
            {
                "total_python_files": self.total_python_files,
                "total_functions": self.total_functions,
                "total_classes": self.total_classes,
                "total_imports": self.total_imports,
                "total_function_calls": self.total_function_calls,
                "total_errors": self.total_errors,
            }
        )
        return data


# ---------------------------------------------------------------------------
# Analyzer
# ---------------------------------------------------------------------------

#: Directories never descended into during repository discovery.
DEFAULT_EXCLUDED_DIRS = frozenset(
    {
        ".git",
        "__pycache__",
        ".venv",
        "venv",
        "env",
        "build",
        "dist",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        ".eggs",
        "site-packages",
        "node_modules",
        "htmlcov",
        ".idea",
        ".vscode",
    }
)


class PythonCodeAnalyzer:
    """Inspects Python source code and returns structured, deterministic
    evidence about what is structurally present.

    Usage::

        analyzer = PythonCodeAnalyzer("/Users/me/Projects/PayGuard")
        analysis = analyzer.analyze()

        analysis.total_python_files
        analysis.total_functions
        analysis.files[0].functions   # list[FunctionInfo]
    """

    def __init__(
        self,
        repository_path: Union[str, Path],
        excluded_dirs: Optional[Iterable[str]] = None,
    ) -> None:
        self._repository_path = Path(repository_path).expanduser().resolve()
        self._excluded_dirs = frozenset(DEFAULT_EXCLUDED_DIRS) | frozenset(
            excluded_dirs or ()
        )

    @property
    def repository_path(self) -> Path:
        """The (resolved) root path the analyzer was created with."""
        return self._repository_path

    @property
    def excluded_dirs(self) -> frozenset:
        """Directory names that are skipped during discovery."""
        return self._excluded_dirs

    def __repr__(self) -> str:
        return f"PythonCodeAnalyzer({str(self._repository_path)!r})"

    # -- discovery ---------------------------------------------------------

    def discover_python_files(
        self, path: Optional[Union[str, Path]] = None
    ) -> List[Path]:
        """Recursively list Python files under ``path`` (default: the root
        the analyzer was created with), skipping excluded directories.

        :raises PathNotFoundError: the path does not exist.
        :raises InvalidPathError: the path is not a directory.
        """
        root = self._resolve_root(path)
        discovered = (
            p
            for p in root.rglob("*.py")
            if p.is_file() and not self._is_excluded(p, root)
        )
        return sorted(discovered)

    def _resolve_root(self, path: Optional[Union[str, Path]]) -> Path:
        target = (
            Path(path).expanduser().resolve()
            if path is not None
            else self._repository_path
        )
        if not target.exists():
            raise PathNotFoundError(f"Path does not exist: {target}")
        if not target.is_dir():
            raise InvalidPathError(f"Path is not a directory: {target}")
        return target

    def _is_excluded(self, path: Path, root: Path) -> bool:
        try:
            relative = path.relative_to(root)
        except ValueError:
            relative = path
        # Every part except the file name itself must avoid the exclusion list.
        return any(
            part in self._excluded_dirs for part in relative.parts[:-1]
        )

    # -- analysis ----------------------------------------------------------

    def analyze_file(
        self,
        path: Union[str, Path],
        root: Optional[Union[str, Path]] = None,
    ) -> PythonFileAnalysis:
        """Parse and analyze a single Python source file.

        ``root`` is only used to derive :attr:`PythonFileAnalysis.module_name`;
        when omitted, the analyzer's own root is used if the file lives
        under it, otherwise just the file stem.

        :raises PathNotFoundError: the path does not exist.
        :raises InvalidPathError: the path is not a file.
        :raises InvalidSourceError: the file is not valid Python.
        :raises UnreadableFileError: the file cannot be read.
        """
        target = Path(path).expanduser().resolve()
        if not target.exists():
            raise PathNotFoundError(f"Path does not exist: {target}")
        if target.is_dir():
            raise InvalidPathError(f"Path is not a file: {target}")

        try:
            source = target.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise UnreadableFileError(
                f"File is not valid UTF-8 text: {target} ({exc})"
            ) from exc
        except OSError as exc:
            raise UnreadableFileError(f"File could not be read: {target} ({exc})") from exc

        try:
            tree = ast.parse(source, filename=str(target))
        except SyntaxError as exc:
            line = f" (line {exc.lineno})" if exc.lineno else ""
            raise InvalidSourceError(
                f"Invalid Python source in {target}: {exc.msg}{line}"
            ) from exc
        except ValueError as exc:  # e.g. source containing null bytes
            raise InvalidSourceError(
                f"Invalid Python source in {target}: {exc}"
            ) from exc

        base: Optional[Path] = (
            Path(root).expanduser().resolve() if root is not None else None
        )
        module_name = self._module_name(target, base)

        return self._build_file_analysis(tree, target, module_name, source)

    def analyze_repository(
        self, path: Optional[Union[str, Path]] = None
    ) -> RepositoryCodeAnalysis:
        """Analyze every Python file under ``path`` (default: the analyzer's
        root).

        Files that fail to parse or read are recorded in
        :attr:`RepositoryCodeAnalysis.errors`; the remaining files are still
        analyzed.

        :raises PathNotFoundError: the path does not exist.
        :raises InvalidPathError: the path is not a directory.
        """
        root = self._resolve_root(path)
        result = RepositoryCodeAnalysis(repository_path=root)
        for file_path in self.discover_python_files(root):
            try:
                result.files.append(self.analyze_file(file_path, root=root))
            except InvalidSourceError as exc:
                result.errors.append(
                    FileAnalysisError(
                        file_path=file_path,
                        error_type="invalid-source",
                        message=str(exc),
                    )
                )
            except UnreadableFileError as exc:
                result.errors.append(
                    FileAnalysisError(
                        file_path=file_path,
                        error_type="unreadable",
                        message=str(exc),
                    )
                )
        return result

    def analyze(self) -> RepositoryCodeAnalysis:
        """Analyze the repository this analyzer was created with.

        This is the main entry point of the component::

            analysis = PythonCodeAnalyzer("/path/to/repo").analyze()
        """
        return self.analyze_repository(self._repository_path)

    # -- helpers -----------------------------------------------------------

    def _module_name(self, target: Path, root: Optional[Path]) -> str:
        base = root
        if base is None:
            try:
                target.relative_to(self._repository_path)
            except ValueError:
                return target.stem
            base = self._repository_path
        try:
            relative = target.relative_to(base)
        except ValueError:
            return target.stem
        parts = list(relative.with_suffix("").parts)
        if parts and parts[-1] == "__init__":
            parts = parts[:-1]
        return ".".join(parts)

    def _build_file_analysis(
        self,
        tree: ast.Module,
        target: Path,
        module_name: str,
        source: str,
    ) -> PythonFileAnalysis:
        analysis = PythonFileAnalysis(
            file_path=target,
            module_name=module_name,
            lines_of_code=self._count_lines_of_code(source),
        )

        # Map every node to its parent so scopes can be resolved.
        parents: Dict[ast.AST, ast.AST] = {}
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                parents[child] = parent

        def nearest_definition(node: ast.AST) -> Optional[ast.AST]:
            """Innermost enclosing function or class definition (or None
            for module-level code)."""
            current = parents.get(node)
            while current is not None:
                if isinstance(
                    current,
                    (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
                ):
                    return current
                current = parents.get(current)
            return None

        function_nodes: Dict[ast.AST, FunctionInfo] = {}
        class_nodes: Dict[ast.AST, ClassInfo] = {}
        all_functions: List[FunctionInfo] = []

        def function_of(node: ast.AST) -> Optional[FunctionInfo]:
            """Function owning *node*, if the node sits inside a function
            body. Definitions are always visited before the nodes inside
            them (breadth-first walk), so the lookup is complete by the
            time an event needs it."""
            owner = nearest_definition(node)
            if owner is None:
                return None
            return function_nodes.get(owner)

        # Pass 1: definitions and flat event lists (one deterministic walk).
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                info = self._function_info(node)
                function_nodes[node] = info
                all_functions.append(info)
                owner = nearest_definition(node)
                if isinstance(owner, ast.ClassDef):
                    # The class node is always visited before its methods
                    # (breadth-first walk), so it is already registered.
                    class_nodes.setdefault(
                        owner, self._class_info(owner)
                    ).methods.append(info)
                else:
                    analysis.functions.append(info)
                continue

            if isinstance(node, ast.ClassDef):
                analysis.classes.append(
                    class_nodes.setdefault(node, self._class_info(node))
                )
                continue

            line = getattr(node, "lineno", 0)

            if isinstance(node, ast.Call):
                call = self._call_info(node, line)
                analysis.function_calls.append(call)
                info = function_of(node)
                if info is not None:
                    info.calls.append(call)

            elif isinstance(node, ast.Import):
                for alias in node.names:
                    analysis.imports.append(
                        ImportInfo(
                            line_number=line,
                            kind="import",
                            module=alias.name,
                            alias=alias.asname,
                        )
                    )

            elif isinstance(node, ast.ImportFrom):
                level = "." * (node.level or 0)
                analysis.imports.append(
                    ImportInfo(
                        line_number=line,
                        kind="from",
                        module=f"{level}{node.module or ''}",
                        names=[alias.name for alias in node.names],
                    )
                )

            elif isinstance(node, ast.Return):
                ret = ReturnInfo(
                    line_number=line,
                    value=ast.unparse(node.value)
                    if node.value is not None
                    else None,
                )
                analysis.return_statements.append(ret)
                info = function_of(node)
                if info is not None:
                    info.returns.append(ret)

            elif isinstance(node, (ast.If, ast.IfExp)):
                if isinstance(node, ast.IfExp):
                    kind = "ternary"
                elif (
                    isinstance(parents.get(node), ast.If)
                    and node in parents[node].orelse
                    and node.col_offset == parents[node].col_offset
                ):
                    # An ``elif`` starts at the same column as its parent
                    # ``if``; a nested ``if`` inside an ``else`` block is
                    # always indented deeper.
                    kind = "elif"
                else:
                    kind = "if"
                cond = ConditionalInfo(line_number=line, kind=kind)
                analysis.conditionals.append(cond)
                info = function_of(node)
                if info is not None:
                    info.has_conditions = True

            elif isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
                kind = "while" if isinstance(node, ast.While) else "for"
                loop = LoopInfo(line_number=line, kind=kind)
                analysis.loops.append(loop)
                info = function_of(node)
                if info is not None:
                    info.has_loops = True

            elif isinstance(node, ast.Raise):
                exc_info = self._raise_info(node, line)
                analysis.exceptions.append(exc_info)
                info = function_of(node)
                if info is not None:
                    info.raises.append(exc_info)

            elif isinstance(node, ast.ExceptHandler):
                analysis.exceptions.append(
                    ExceptionInfo(
                        line_number=line,
                        kind="except",
                        exception_type=ast.unparse(node.type)
                        if node.type is not None
                        else None,
                    )
                )

        # Sort everything into deterministic source order (stable sort).
        analysis.functions.sort(key=lambda f: f.line_number)
        analysis.classes.sort(key=lambda c: c.line_number)
        for cls in analysis.classes:
            cls.methods.sort(key=lambda f: f.line_number)
        for info in all_functions:
            info.calls.sort(key=lambda c: c.line_number)
            info.returns.sort(key=lambda r: r.line_number)
            info.raises.sort(key=lambda e: e.line_number)
        analysis.function_calls.sort(key=lambda c: c.line_number)
        analysis.conditionals.sort(key=lambda c: c.line_number)
        analysis.loops.sort(key=lambda l: l.line_number)
        analysis.return_statements.sort(key=lambda r: r.line_number)
        analysis.exceptions.sort(key=lambda e: e.line_number)
        analysis.imports.sort(key=lambda i: i.line_number)
        return analysis

    @staticmethod
    def _count_lines_of_code(source: str) -> int:
        count = 0
        for line in source.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                count += 1
        return count

    @staticmethod
    def _function_info(node: ast.AST) -> FunctionInfo:
        func = node  # FunctionDef | AsyncFunctionDef
        return FunctionInfo(
            name=func.name,
            line_number=getattr(func, "lineno", 0),
            end_line_number=getattr(func, "end_lineno", None)
            or getattr(func, "lineno", 0),
            arguments=PythonCodeAnalyzer._argument_names(func),
            decorators=[ast.unparse(d) for d in func.decorator_list],
        )

    @staticmethod
    def _argument_names(func: Any) -> List[str]:
        args = func.args
        names = [a.arg for a in getattr(args, "posonlyargs", [])]
        names += [a.arg for a in args.args]
        if args.vararg is not None:
            names.append(args.vararg.arg)
        names += [a.arg for a in args.kwonlyargs]
        if args.kwarg is not None:
            names.append(args.kwarg.arg)
        return names

    @staticmethod
    def _class_info(node: ast.ClassDef) -> ClassInfo:
        return ClassInfo(
            name=node.name,
            line_number=node.lineno,
            end_line_number=node.end_lineno or node.lineno,
            base_classes=[ast.unparse(base) for base in node.bases],
            decorators=[ast.unparse(d) for d in node.decorator_list],
        )

    @staticmethod
    def _call_info(node: ast.Call, line: int) -> CallInfo:
        func = node.func
        qualified = ast.unparse(func)
        if isinstance(func, ast.Attribute):
            name = func.attr
        elif isinstance(func, ast.Name):
            name = func.id
        else:  # f()(), subscripted callables, etc.
            name = qualified
        return CallInfo(
            function_name=name, line_number=line, qualified_name=qualified
        )

    @staticmethod
    def _raise_info(node: ast.Raise, line: int) -> ExceptionInfo:
        expression = node.exc
        if expression is None:  # bare ``raise``
            return ExceptionInfo(line_number=line, kind="raise")
        if isinstance(expression, ast.Call):
            exception_type = ast.unparse(expression.func)
            message: Optional[str] = None
            if expression.args:
                first = expression.args[0]
                if isinstance(first, ast.Constant) and isinstance(
                    first.value, str
                ):
                    message = first.value
                else:
                    message = ast.unparse(first)
            return ExceptionInfo(
                line_number=line,
                kind="raise",
                exception_type=exception_type,
                message=message,
            )
        return ExceptionInfo(
            line_number=line,
            kind="raise",
            exception_type=ast.unparse(expression),
        )
