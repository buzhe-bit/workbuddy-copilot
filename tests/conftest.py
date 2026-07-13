from __future__ import annotations

import atexit
import ast
from fnmatch import fnmatchcase
import os
from pathlib import Path
import shutil
import tempfile

import pytest


# Test modules import ``copilot.service`` during collection, so HOME isolation
# must exist before those modules (and their global app) are imported.
_COLLECTION_SANDBOX = Path(tempfile.mkdtemp(prefix="workbuddy-pytest-"))
_COLLECTION_HOME = _COLLECTION_SANDBOX / "home"
_COLLECTION_HOME.mkdir()
(_COLLECTION_HOME / "AppData").mkdir()
os.environ["HOME"] = str(_COLLECTION_HOME)
os.environ["USERPROFILE"] = str(_COLLECTION_HOME)
os.environ["APPDATA"] = str(_COLLECTION_HOME / "AppData")
atexit.register(shutil.rmtree, _COLLECTION_SANDBOX, ignore_errors=True)


def _collection_tree(nodeid: str) -> ast.AST | None:
    source_path = Path(nodeid.split("::", 1)[0])
    if not source_path.is_absolute():
        source_path = Path.cwd() / source_path
    if source_path.suffix != ".py" or not source_path.is_file():
        return None
    try:
        return ast.parse(source_path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeError):
        return None


def _pytest_bindings(tree: ast.AST) -> tuple[set[str], set[str], set[str]]:
    pytest_names: set[str] = set()
    importorskip_names: set[str] = set()
    mark_names: set[str] = set()
    for statement in getattr(tree, "body", []):
        if isinstance(statement, ast.Import):
            for alias in statement.names:
                if alias.name == "pytest":
                    pytest_names.add(alias.asname or alias.name)
        elif isinstance(statement, ast.ImportFrom) and statement.module == "pytest":
            for alias in statement.names:
                local_name = alias.asname or alias.name
                if alias.name == "importorskip":
                    importorskip_names.add(local_name)
                elif alias.name == "mark":
                    mark_names.add(local_name)
    return pytest_names, importorskip_names, mark_names


def _expression_declares_critical(expression, pytest_names, mark_names) -> bool:
    for node in ast.walk(expression):
        if not isinstance(node, ast.Attribute) or node.attr != "critical":
            continue
        marker = node.value
        if isinstance(marker, ast.Name) and marker.id in mark_names:
            return True
        if (
            isinstance(marker, ast.Attribute)
            and marker.attr == "mark"
            and isinstance(marker.value, ast.Name)
            and marker.value.id in pytest_names
        ):
            return True
    return False


def _pytestmark_value(statement):
    if isinstance(statement, (ast.Assign, ast.AnnAssign)):
        targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
        if any(isinstance(target, ast.Name) and target.id == "pytestmark" for target in targets):
            return statement.value
    return None


def _definition_declares_critical(statement, pytest_names, mark_names) -> bool:
    if not isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return False
    if any(
        _expression_declares_critical(decorator, pytest_names, mark_names)
        for decorator in statement.decorator_list
    ):
        return True
    if isinstance(statement, ast.ClassDef):
        for child in statement.body:
            marker_value = _pytestmark_value(child)
            if marker_value is not None and _expression_declares_critical(
                marker_value, pytest_names, mark_names
            ):
                return True
            if _definition_declares_critical(child, pytest_names, mark_names):
                return True
    return False


def _collection_declares_critical(tree: ast.AST, pytest_names, mark_names) -> bool:
    for statement in getattr(tree, "body", []):
        marker_value = _pytestmark_value(statement)
        if marker_value is not None and _expression_declares_critical(
            marker_value, pytest_names, mark_names
        ):
            return True
        if _definition_declares_critical(statement, pytest_names, mark_names):
            return True
    return False


class _ImportTimeImportOrSkipFinder(ast.NodeVisitor):
    """Find calls executed while importing a module, excluding test bodies."""

    def __init__(self, pytest_names: set[str], importorskip_names: set[str]):
        self.pytest_names = pytest_names
        self.importorskip_names = importorskip_names
        self.found = False

    def visit_Call(self, node: ast.Call) -> None:
        function = node.func
        if (
            isinstance(function, ast.Name)
            and function.id in self.importorskip_names
        ) or (
            isinstance(function, ast.Attribute)
            and function.attr == "importorskip"
            and isinstance(function.value, ast.Name)
            and function.value.id in self.pytest_names
        ):
            self.found = True
        self.generic_visit(node)

    def _visit_function_header(self, node) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        for default in (*node.args.defaults, *node.args.kw_defaults):
            if default is not None:
                self.visit(default)
        if node.returns is not None:
            self.visit(node.returns)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function_header(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function_header(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        for default in (*node.args.defaults, *node.args.kw_defaults):
            if default is not None:
                self.visit(default)


def _collection_uses_importorskip(
    tree: ast.AST, pytest_names: set[str], importorskip_names: set[str]
) -> bool:
    finder = _ImportTimeImportOrSkipFinder(pytest_names, importorskip_names)
    finder.visit(tree)
    return finder.found


def _is_explicit_collection_path(file_path: Path, config) -> bool:
    resolved_file = file_path.resolve()
    for argument in config.args:
        argument_path = Path(str(argument).split("::", 1)[0])
        if not argument_path.is_absolute():
            argument_path = Path.cwd() / argument_path
        try:
            if argument_path.resolve() == resolved_file:
                return True
        except OSError:
            continue
    return False


def _is_configured_test_file(file_path: Path, config) -> bool:
    return any(
        fnmatchcase(file_path.name, pattern)
        for pattern in config.getini("python_files")
    )


def pytest_collect_file(file_path, parent):
    """Reject importorskip before it can hide a critical module at collection."""
    source_path = Path(str(file_path))
    if not (
        _is_explicit_collection_path(source_path, parent.config)
        or _is_configured_test_file(source_path, parent.config)
    ):
        return None

    tree = _collection_tree(str(source_path))
    pytest_names, importorskip_names, mark_names = (
        _pytest_bindings(tree) if tree is not None else (set(), set(), set())
    )
    if (
        tree is not None
        and _collection_declares_critical(tree, pytest_names, mark_names)
        and _collection_uses_importorskip(tree, pytest_names, importorskip_names)
    ):
        raise pytest.UsageError(
            f"Critical test module {file_path} may not use importorskip"
        )
    return None


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """A release-blocking test must fail loudly when its prerequisites are absent."""
    outcome = yield
    report = outcome.get_result()
    if report.skipped and item.get_closest_marker("critical") is not None:
        report.outcome = "failed"
        report.longrepr = (
            f"Critical test skipped during {report.when}: {report.longrepr}. "
            "Install the lane dependency or mark the lane blocked before collection."
        )


@pytest.fixture(autouse=True)
def isolate_global_app_worker_lock(tmp_path):
    """Keep tests that use copilot.service:app away from a live local service lock."""
    try:
        from copilot.service import app
    except Exception:
        yield
        return

    context = getattr(app.state, "context", None)
    if context is None:
        yield
        return

    store_config = context.config.setdefault("store", {})
    original_db_path = store_config.get("db_path")
    if context.worker_lock_file is None:
        store_config["db_path"] = str(tmp_path / "copilot-test.db")
    try:
        yield
    finally:
        if context.worker_lock_file is None:
            if original_db_path is None:
                store_config.pop("db_path", None)
            else:
                store_config["db_path"] = original_db_path


def pytest_collection_modifyitems(config, items):
    def sort_key(item):
        path = str(item.path).replace("\\", "/")
        is_frontend_e2e = "/tests/e2e/" in path
        return (is_frontend_e2e, path, item.name)

    items.sort(key=sort_key)
