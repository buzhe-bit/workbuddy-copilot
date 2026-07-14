from __future__ import annotations

import ast
from dataclasses import dataclass
import importlib.util
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
COPILOT_ROOT = PROJECT_ROOT / "copilot"
SERVER_RUNTIME_ROOTS = (
    "copilot.service",
    "copilot.app_context",
    "copilot.mentor.routes",
)
STUDENT_CLIENT_ALLOWLIST = {
    "copilot.floating_native",
    "copilot.hook",
    "copilot.student_platform.windows_runtime",
    "copilot.wb_sync",
    "copilot.wb_upload",
}

pytestmark = [pytest.mark.server, pytest.mark.contract, pytest.mark.critical]


@dataclass(frozen=True)
class _StaticExpression:
    text: str
    uses_home: bool = False


def _path_to_module(path: Path) -> str:
    relative = path.relative_to(PROJECT_ROOT).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _module_path(module: str) -> Path | None:
    if module == "copilot":
        return COPILOT_ROOT / "__init__.py"
    if not module.startswith("copilot."):
        return None
    relative = Path(*module.split(".")[1:])
    module_file = COPILOT_ROOT / relative.with_suffix(".py")
    if module_file.is_file():
        return module_file
    package_file = COPILOT_ROOT / relative / "__init__.py"
    return package_file if package_file.is_file() else None


def _ancestor_modules(module: str) -> tuple[str, ...]:
    parts = module.split(".")
    return tuple(".".join(parts[:index]) for index in range(1, len(parts)))


def _imported_modules(tree: ast.AST, current_module: str) -> set[str]:
    current_path = _module_path(current_module)
    is_package = current_path is not None and current_path.name == "__init__.py"
    package = current_module if is_package else current_module.rpartition(".")[0]
    imported: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names if alias.name.startswith("copilot"))
            continue
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.level:
            package_parts = package.split(".") if package else []
            keep = max(0, len(package_parts) - (node.level - 1))
            base_parts = package_parts[:keep]
            if node.module:
                base_parts.extend(node.module.split("."))
            base = ".".join(base_parts)
        else:
            base = node.module or ""
        if not base.startswith("copilot"):
            continue
        imported.add(base)
        for alias in node.names:
            if alias.name != "*":
                imported.add(f"{base}.{alias.name}")
    return imported


def _call_name(node: ast.Call) -> str:
    current: ast.AST = node.func
    names: list[str] = []
    while isinstance(current, ast.Attribute):
        names.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        names.append(current.id)
    return ".".join(reversed(names))


def _static_expression(
    node: ast.AST,
    assignments: dict[str, ast.AST],
    resolving: frozenset[str] = frozenset(),
) -> _StaticExpression | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return _StaticExpression(node.value)
    if isinstance(node, ast.Name) and node.id in assignments and node.id not in resolving:
        return _static_expression(
            assignments[node.id], assignments, resolving | {node.id}
        )
    if isinstance(node, ast.JoinedStr):
        parts: list[_StaticExpression] = []
        for value in node.values:
            if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
                return None
            parts.append(_StaticExpression(value.value))
        return _StaticExpression("".join(part.text for part in parts))
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Div)):
        left = _static_expression(node.left, assignments, resolving)
        right = _static_expression(node.right, assignments, resolving)
        if left is None or right is None:
            return None
        separator = "/" if isinstance(node.op, ast.Div) else ""
        text = (
            f"{left.text.rstrip('/')}/{right.text.lstrip('/')}"
            if separator
            else left.text + right.text
        )
        return _StaticExpression(text, left.uses_home or right.uses_home)
    if not isinstance(node, ast.Call):
        return None

    name = _call_name(node)
    if name in {"Path.home", "pathlib.Path.home"}:
        return _StaticExpression("~", uses_home=True)
    if name in {"Path", "pathlib.Path"} and node.args:
        return _static_expression(node.args[0], assignments, resolving)
    if name.endswith(".joinpath") and isinstance(node.func, ast.Attribute):
        values = [_static_expression(node.func.value, assignments, resolving)]
        values.extend(_static_expression(arg, assignments, resolving) for arg in node.args)
    elif name in {"os.path.join", "posixpath.join", "ntpath.join"}:
        values = [_static_expression(arg, assignments, resolving) for arg in node.args]
    else:
        return None
    if not values or any(value is None for value in values):
        return None
    known_values = [value for value in values if value is not None]
    return _StaticExpression(
        "/".join(value.text.strip("/\\") for value in known_values),
        any(value.uses_home for value in known_values),
    )


def _path_violations(value: _StaticExpression) -> set[str]:
    normalized = value.text.replace("\\", "/")
    components = [component for component in normalized.split("/") if component]
    if ".workbuddy" not in components:
        return set()
    workbuddy_index = components.index(".workbuddy")
    descendants = components[workbuddy_index + 1 :]
    violations = {"path-home-workbuddy"} if value.uses_home else set()
    if "workbuddy.db" in descendants:
        violations.add("workbuddy-db-path")
    if "projects" in descendants or any(part.endswith(".jsonl") for part in descendants):
        violations.add("workbuddy-projects-path")
    return violations


def scan_server_file(path: Path) -> set[str]:
    """Return local-student-filesystem violation categories for one module."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    assignments: dict[str, ast.AST] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if node.value is not None:
                for target in targets:
                    if isinstance(target, ast.Name):
                        assignments[target.id] = node.value

    violations: set[str] = set()
    current_module = _path_to_module(path) if path.is_relative_to(PROJECT_ROOT) else ""
    imported = _imported_modules(tree, current_module)
    if any(module in STUDENT_CLIENT_ALLOWLIST for module in imported):
        violations.add("student-client-import")

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            leaf_name = _call_name(node).rsplit(".", 1)[-1]
            if current_module != "copilot.transcript" and leaf_name in {
                "iter_recent_transcripts",
                "parse_transcript",
                "recent_messages",
            }:
                violations.add("local-transcript-read")
            expressions = list(node.args)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            expressions = [node.value]
        elif isinstance(node, ast.BinOp):
            expressions = [node]
        else:
            expressions = []
        for expression in expressions:
            static = _static_expression(expression, assignments)
            if static is not None:
                violations.update(_path_violations(static))

        if (
            isinstance(node, ast.Attribute)
            and node.attr == "parent"
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "parent"
            and not any(
                isinstance(child, ast.Name) and child.id == "__file__"
                for child in ast.walk(node)
            )
        ):
            violations.add("parent-parent-project-root")
    return violations


def resolve_server_graph(roots: tuple[str, ...] = SERVER_RUNTIME_ROOTS) -> dict[str, Path]:
    """Resolve project-local copilot modules reachable from server roots."""
    graph: dict[str, Path] = {}
    pending = list(roots)
    while pending:
        module = pending.pop()
        if module in graph:
            continue
        path = _module_path(module)
        if path is None:
            continue
        graph[module] = path
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for ancestor in _ancestor_modules(module):
            if ancestor not in graph and _module_path(ancestor) is not None:
                pending.append(ancestor)
        for imported in _imported_modules(tree, module):
            if imported not in graph and _module_path(imported) is not None:
                pending.append(imported)
    return graph


def scan_server_graph(
    roots: tuple[str, ...] = SERVER_RUNTIME_ROOTS,
) -> dict[str, set[str]]:
    """Return violations keyed by reachable server module."""
    return {
        module: violations
        for module, path in resolve_server_graph(roots).items()
        if (violations := scan_server_file(path))
    }


def scan_server_tree() -> dict[str, set[str]]:
    """Return violations in every non-client module under copilot/."""
    violations: dict[str, set[str]] = {}
    for path in sorted(COPILOT_ROOT.rglob("*.py")):
        module = _path_to_module(path)
        if module in STUDENT_CLIENT_ALLOWLIST:
            continue
        findings = scan_server_file(path)
        if findings:
            violations[module] = findings
    return violations


def test_breaker_fixture_detects_every_server_local_filesystem_bypass():
    breaker = PROJECT_ROOT / "tests" / "fixtures" / "bad_server_local_fs.py"

    assert scan_server_file(breaker) == {
        "local-transcript-read",
        "parent-parent-project-root",
        "path-home-workbuddy",
        "student-client-import",
        "workbuddy-db-path",
        "workbuddy-projects-path",
    }


def test_server_graph_covers_all_reachable_runtime_modules_and_is_clean():
    graph = resolve_server_graph()

    assert {
        "copilot.service",
        "copilot.app_context",
        "copilot.mentor.routes",
        "copilot.services",
        "copilot.store",
        "copilot.transcript",
        "copilot.upload_service",
    } <= set(graph)
    assert STUDENT_CLIENT_ALLOWLIST.isdisjoint(graph)
    assert scan_server_graph() == {}


def test_windows_runtime_is_a_student_client_but_never_server_reachable():
    graph = resolve_server_graph()

    assert "copilot.student_platform.windows_runtime" in STUDENT_CLIENT_ALLOWLIST
    assert "copilot.student_platform.windows_runtime" not in graph


def test_server_graph_includes_implicit_parent_packages():
    graph = resolve_server_graph()

    assert {"copilot", "copilot.mentor"} <= set(graph)


def test_full_server_tree_scan_catches_unreachable_non_allowlist_breaker():
    breaker_path = COPILOT_ROOT / "_server_redline_breaker.py"
    breaker_path.write_text(
        "from pathlib import Path\n"
        "def read_student_files():\n"
        "    return Path.home() / '.workbuddy' / 'workbuddy.db'\n",
        encoding="utf-8",
    )
    try:
        violations = scan_server_tree()
    finally:
        breaker_path.unlink(missing_ok=True)

    assert violations["copilot._server_redline_breaker"] == {
        "path-home-workbuddy",
        "workbuddy-db-path",
    }


def test_full_server_tree_scan_allows_only_student_client_workbuddy_access():
    assert scan_server_tree() == {}


def test_old_mentor_ws_is_archived_outside_runtime_import_graph():
    runtime_path = COPILOT_ROOT / "mentor" / "ws.py"
    archive_path = PROJECT_ROOT / "legacy" / "copilot" / "mentor_ws.py"

    importlib.invalidate_caches()
    assert not runtime_path.exists()
    assert importlib.util.find_spec("copilot.mentor.ws") is None
    assert archive_path.exists()
    archive = archive_path.read_text(encoding="utf-8")
    compile(archive, str(archive_path), "exec")
    assert "mentor_ws_endpoint" in archive
    assert "mentor_ws_clients" in archive
    assert archive_path not in resolve_server_graph().values()
