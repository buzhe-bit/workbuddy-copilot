from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tomllib


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PREFLIGHT = PROJECT_ROOT / "scripts" / "python_preflight.py"


def test_project_metadata_declares_the_release_python_range():
    metadata = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert metadata["project"]["requires-python"] == ">=3.13,<3.14"
    assert metadata["tool"]["setuptools"]["packages"]["find"]["include"] == ["copilot*"]


def test_shared_production_coverage_gate_is_at_least_80_percent():
    metadata = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dev_requirements = (PROJECT_ROOT / "requirements-dev.txt").read_text(encoding="utf-8")
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "quality.yml").read_text(
        encoding="utf-8"
    )

    assert metadata["tool"]["coverage"]["report"]["fail_under"] >= 80
    assert "pytest-cov" in dev_requirements
    assert "--cov-fail-under=80" in workflow


def test_python_preflight_enforces_both_version_bounds():
    probe = """
from scripts.python_preflight import supports_version

assert not supports_version((3, 12, 9))
assert supports_version((3, 13, 0))
assert supports_version((3, 13, 99))
assert not supports_version((3, 14, 0))
assert not supports_version((4, 0, 0))
"""
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr


def test_python_preflight_matches_current_interpreter_support():
    completed = subprocess.run(
        [sys.executable, str(PREFLIGHT)],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    expected = 0 if sys.version_info[:2] == (3, 13) else 1

    assert completed.returncode == expected
    assert ">=3.13,<3.14" in (completed.stdout + completed.stderr)
