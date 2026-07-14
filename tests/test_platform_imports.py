from __future__ import annotations

import ast
import configparser
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from tempfile import TemporaryDirectory

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROBE_FILENAME = "_pytest_gate_probe.py"
REQUIREMENT_FILES = {
    "core": PROJECT_ROOT / "requirements-core.txt",
    "server": PROJECT_ROOT / "requirements-server.txt",
    "macos": PROJECT_ROOT / "requirements-macos.txt",
    "windows": PROJECT_ROOT / "requirements-windows.txt",
    "default": PROJECT_ROOT / "requirements.txt",
}
FORBIDDEN_CORE_IMPORTS = {"AppKit", "Foundation", "objc", "fcntl"}
MAC_ONLY_REQUIREMENTS = {"pyobjc-core", "pyobjc-framework-cocoa", "rumps"}
SERVER_ONLY_REQUIREMENTS = {"fastapi", "uvicorn", "httpx"}

pytestmark = [
    pytest.mark.contract,
    pytest.mark.core,
    pytest.mark.critical,
]


def _meaningful_lines(path: Path) -> list[str]:
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _include_target(line: str) -> str | None:
    match = re.fullmatch(r"-r\s+([^\s#]+)", line)
    return match.group(1) if match else None


def _project_name(line: str) -> str | None:
    if _include_target(line) is not None:
        return None
    match = re.match(r"([A-Za-z0-9_.-]+)", line)
    assert match, f"unrecognized requirement line: {line!r}"
    return match.group(1).lower().replace("_", "-")


def _direct_layer(path: Path) -> tuple[set[str], set[str]]:
    packages: set[str] = set()
    includes: set[str] = set()
    for line in _meaningful_lines(path):
        target = _include_target(line)
        if target is not None:
            includes.add(target)
        else:
            name = _project_name(line)
            assert name is not None
            packages.add(name)
    return packages, includes


def _resolved_packages(path: Path, seen: frozenset[Path] = frozenset()) -> set[str]:
    resolved = path.resolve()
    assert resolved not in seen, f"cyclic requirements include at {path.name}"
    assert path.is_file(), f"missing requirements layer: {path.name}"
    packages, includes = _direct_layer(path)
    for target in includes:
        included_path = path.parent / target
        assert included_path.is_file(), f"{path.name} includes missing file {target}"
        packages |= _resolved_packages(included_path, seen | {resolved})
    return packages


@pytest.mark.windows
def test_student_core_import_tree_is_platform_neutral():
    package_dir = PROJECT_ROOT / "copilot" / "student_core"
    source_files = sorted(package_dir.rglob("*.py"))
    assert source_files, "copilot.student_core package is missing"

    declared_imports: set[str] = set()
    for source_file in source_files:
        tree = ast.parse(source_file.read_text(encoding="utf-8"), filename=str(source_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                declared_imports.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                declared_imports.add(node.module.split(".", 1)[0])

    assert FORBIDDEN_CORE_IMPORTS.isdisjoint(declared_imports), (
        "Student Core declares platform-only imports: "
        f"{sorted(FORBIDDEN_CORE_IMPORTS & declared_imports)}"
    )

    probe = """
import importlib
import json
import pkgutil
import sys

package = importlib.import_module("copilot.student_core")
for module in pkgutil.walk_packages(package.__path__, package.__name__ + "."):
    importlib.import_module(module.name)
print(json.dumps(sorted({name.split(".", 1)[0] for name in sys.modules})))
"""
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=PROJECT_ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    imported_roots = set(json.loads(completed.stdout.splitlines()[-1]))
    # Python 3.14's POSIX ``pathlib`` implementation imports ``fcntl``
    # internally.  That transitive standard-library choice says nothing about
    # Windows compatibility; direct declarations are rejected above, while a
    # real Windows lane additionally proves that the package imports without
    # the unavailable module.
    runtime_forbidden = {"AppKit", "Foundation", "objc"}
    if sys.platform.startswith("win"):
        runtime_forbidden.add("fcntl")
    assert runtime_forbidden.isdisjoint(imported_roots), (
        "Student Core loaded platform-only modules: "
        f"{sorted(runtime_forbidden & imported_roots)}"
    )


@pytest.mark.windows
def test_core_requirements_are_platform_neutral_client_runtime_only():
    core = REQUIREMENT_FILES["core"]
    assert core.is_file(), "requirements-core.txt is missing"
    core_text = core.read_text(encoding="utf-8").lower()
    packages, includes = _direct_layer(core)

    assert "pyobjc" not in core_text
    assert not includes, "core must not depend on a platform or server layer"
    assert packages == {"websockets"}


@pytest.mark.windows
def test_requirement_include_graph_keeps_platform_layers_isolated():
    missing = [path.name for path in REQUIREMENT_FILES.values() if not path.is_file()]
    assert not missing, f"missing requirement files: {missing}"

    graph = {
        layer: _direct_layer(path)[1]
        for layer, path in REQUIREMENT_FILES.items()
    }
    assert graph == {
        "core": set(),
        "server": set(),
        "macos": {"requirements-core.txt"},
        "windows": {"requirements-core.txt"},
        "default": {"requirements-server.txt", "requirements-macos.txt"},
    }


@pytest.mark.windows
def test_resolved_requirement_lanes_retain_default_macos_compatibility():
    server_direct, _ = _direct_layer(REQUIREMENT_FILES["server"])
    mac_direct, _ = _direct_layer(REQUIREMENT_FILES["macos"])
    windows_direct, _ = _direct_layer(REQUIREMENT_FILES["windows"])
    default_direct, _ = _direct_layer(REQUIREMENT_FILES["default"])

    assert server_direct == SERVER_ONLY_REQUIREMENTS
    assert mac_direct == MAC_ONLY_REQUIREMENTS
    assert windows_direct == set()
    assert default_direct == set(), "requirements.txt must be a compatibility include file"

    core = _resolved_packages(REQUIREMENT_FILES["core"])
    server = _resolved_packages(REQUIREMENT_FILES["server"])
    macos = _resolved_packages(REQUIREMENT_FILES["macos"])
    windows = _resolved_packages(REQUIREMENT_FILES["windows"])
    default = _resolved_packages(REQUIREMENT_FILES["default"])

    assert server == SERVER_ONLY_REQUIREMENTS
    assert windows == core
    assert MAC_ONLY_REQUIREMENTS.isdisjoint(windows)
    assert SERVER_ONLY_REQUIREMENTS.isdisjoint(windows)
    assert macos == core | MAC_ONLY_REQUIREMENTS
    assert SERVER_ONLY_REQUIREMENTS.isdisjoint(macos)
    assert default == server | macos
    assert {
        "fastapi",
        "uvicorn",
        "httpx",
        "rumps",
        "pyobjc-core",
        "pyobjc-framework-cocoa",
    }.issubset(default)


def _run_pytest_probe(
    source: str, *, timeout_seconds: float = 15.0
) -> subprocess.CompletedProcess[str]:
    tests_dir = PROJECT_ROOT / "tests"
    with TemporaryDirectory(prefix="_pytest_probe_", dir=tests_dir) as probe_dir:
        probe_path = Path(probe_dir) / PROBE_FILENAME
        probe_path.write_text(source, encoding="utf-8")
        try:
            return subprocess.run(
                [sys.executable, "-m", "pytest", str(probe_path), "-q"],
                cwd=PROJECT_ROOT,
                text=True,
                capture_output=True,
                check=False,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as error:
            raise AssertionError(
                f"pytest probe timed out after {timeout_seconds:g}s: {probe_path.name}"
            ) from error


@pytest.mark.windows
def test_pytest_lanes_are_strict_and_critical_skips_fail():
    config = configparser.ConfigParser()
    config.read(PROJECT_ROOT / "pytest.ini", encoding="utf-8")
    pytest_config = config["pytest"]
    marker_lines = {
        line.strip().split(":", 1)[0]
        for line in pytest_config.get("markers", "").splitlines()
        if line.strip()
    }

    assert "strict_markers" not in pytest_config
    assert "--strict-markers" in shlex.split(pytest_config.get("addopts", ""))
    assert {
        "critical",
        "contract",
        "unit",
        "integration",
        "component",
        "real_machine",
        "server",
        "core",
        "macos",
        "windows",
    }.issubset(marker_lines)

    completed = _run_pytest_probe(
        "import pytest\n\n"
        "@pytest.mark.critical\n"
        "def test_critical_skip_probe():\n"
        "    pytest.skip('critical dependency unavailable')\n"
    )
    output = completed.stdout + completed.stderr
    assert completed.returncode != 0, output
    assert "critical" in output.lower()


def test_critical_skip_policy_covers_collection_but_not_optional_tests():
    optional = _run_pytest_probe(
        "import pytest\n\n"
        "def test_optional_dependency():\n"
        "    pytest.skip('optional dependency unavailable')\n"
    )
    assert optional.returncode == 0, optional.stdout + optional.stderr
    assert "1 skipped" in optional.stdout

    critical_collection = _run_pytest_probe(
        "import pytest\n\n"
        "pytestmark = pytest.mark.critical\n"
        "pytest.importorskip('_workbuddy_missing_critical_dependency')\n\n"
        "def test_critical_dependency():\n"
        "    pass\n"
    )
    output = critical_collection.stdout + critical_collection.stderr
    assert critical_collection.returncode != 0, output
    assert "critical" in output.lower()



@pytest.mark.parametrize(
    "critical_source",
    [
        (
            "import pytest as pt\n\n"
            "pytestmark = pt.mark.critical\n"
            "pt.importorskip('_workbuddy_missing_critical_dependency')\n"
        ),
        (
            "import pytest as pt\n"
            "from pytest import importorskip as require_dependency\n\n"
            "pytestmark = pt.mark.critical\n"
            "require_dependency('_workbuddy_missing_critical_dependency')\n"
        ),
    ],
)
def test_critical_collection_skip_detects_supported_pytest_aliases(critical_source):
    completed = _run_pytest_probe(
        critical_source + "\ndef test_critical_dependency():\n    pass\n"
    )
    output = completed.stdout + completed.stderr
    assert completed.returncode != 0, output
    assert "critical" in output.lower()


def test_optional_importorskip_inside_test_does_not_block_critical_peer():
    completed = _run_pytest_probe(
        "import pytest\n\n"
        "@pytest.mark.critical\n"
        "def test_release_gate():\n"
        "    assert True\n\n"
        "def test_optional_dependency():\n"
        "    pytest.importorskip('_workbuddy_missing_optional_dependency')\n"
    )
    output = completed.stdout + completed.stderr
    assert completed.returncode == 0, output
    assert "1 passed" in output
    assert "1 skipped" in output
    assert "usageerror" not in output.lower()


def test_pytest_probes_are_isolated_when_run_concurrently():
    sources = [
        (
            "import pytest\nimport time\n\n"
            f"def test_optional_probe_{index}():\n"
            "    time.sleep(0.2)\n"
            f"    pytest.skip('optional dependency {index}')\n"
        )
        for index in range(2)
    ]
    with ThreadPoolExecutor(max_workers=2) as executor:
        completed = list(executor.map(_run_pytest_probe, sources))

    for result in completed:
        assert result.returncode == 0, result.stdout + result.stderr
        assert "1 skipped" in result.stdout


def test_recursive_collection_ignores_live_probe_but_explicit_probe_uses_policy():
    critical_source = (
        "import pytest\n\n"
        "pytestmark = pytest.mark.critical\n"
        "pytest.importorskip('_workbuddy_missing_critical_dependency')\n"
    )
    tests_dir = PROJECT_ROOT / "tests"
    with TemporaryDirectory(prefix="_pytest_probe_", dir=tests_dir) as probe_dir:
        probe_path = Path(probe_dir) / PROBE_FILENAME
        probe_path.write_text(critical_source, encoding="utf-8")
        sentinel_path = Path(probe_dir) / "test_collection_sentinel.py"
        sentinel_path.write_text(
            "def test_collection_sentinel():\n    pass\n",
            encoding="utf-8",
        )

        recursive = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", probe_dir, "-q"],
            cwd=PROJECT_ROOT,
            text=True,
            capture_output=True,
            check=False,
            timeout=15,
        )
        recursive_output = recursive.stdout + recursive.stderr
        assert recursive.returncode == 0, recursive_output
        assert "test_collection_sentinel.py::test_collection_sentinel" in recursive_output
        assert PROBE_FILENAME not in recursive_output

        explicit = subprocess.run(
            [sys.executable, "-m", "pytest", str(probe_path), "-q"],
            cwd=PROJECT_ROOT,
            text=True,
            capture_output=True,
            check=False,
            timeout=15,
        )
        explicit_output = explicit.stdout + explicit.stderr
        assert explicit.returncode != 0, explicit_output
        assert "critical" in explicit_output.lower()


def test_pytest_probe_timeout_fails_with_clear_message():
    with pytest.raises(AssertionError, match=r"pytest probe timed out after 0\.1s"):
        _run_pytest_probe(
            "import time\ntime.sleep(2)\n\ndef test_too_slow():\n    pass\n",
            timeout_seconds=0.1,
        )


def test_test_plan_uses_runnable_current_gates_and_windows_native_command():
    plan = (PROJECT_ROOT / "docs" / "test-plan-v3.md").read_text(encoding="utf-8")

    empty_current_selectors = {
        "$PY -m pytest -m unit -q",
        "$PY -m pytest -m integration -q",
        "$PY -m pytest -m component -q",
        "$PY -m pytest -m server -q",
        "$PY -m pytest -m macos -q",
        "$PY -m pytest -m windows -q",
    }
    assert empty_current_selectors.isdisjoint(plan.splitlines())
    assert (
        ".\\.venv-win\\Scripts\\python.exe -m pytest "
        "tests/test_platform_imports.py -q"
    ) in plan
    assert "Windows 原生 PowerShell/cmd" not in plan
    assert "逐 Task" in plan
