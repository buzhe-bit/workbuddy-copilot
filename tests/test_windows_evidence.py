from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

import pytest

from copilot.student_platform.windows_evidence import (
    WindowsEvidenceResult,
    canonical_evidence_sha256,
    seal_windows_evidence,
    validate_windows_evidence,
)


pytestmark = [pytest.mark.windows, pytest.mark.critical]
PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCHEMA = (
    PROJECT_ROOT
    / "tests"
    / "fixtures"
    / "workbuddy"
    / "windows"
    / "evidence.schema.json"
)
PRODUCTION_SCHEMA = (
    PROJECT_ROOT / "copilot" / "student_platform" / "windows_evidence.schema.json"
)
COMMIT = "a" * 40
BUILD = "build-2026.07.15"
RUNNER = "win-pilot-01"
REQUIRED_ARTIFACT_IDS = (
    "w0_probe",
    "w1_pytest",
    "installer_manifest",
    "lifecycle_log",
)
REQUIRED_W1_TEST_IDS = (
    "install_upgrade_uninstall",
    "workbuddy_git_bash_hook",
    "native_floating_ui",
    "dpi_multimonitor",
    "focus_drag_topmost",
    "chinese_user_path",
    "sleep_wake",
    "login_autostart",
    "disconnect_recovery",
    "identity_isolation",
    "antivirus_compatibility",
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _evidence(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    artifacts: list[dict[str, str]] = []
    for artifact_id in REQUIRED_ARTIFACT_IDS:
        artifact = tmp_path / f"{artifact_id}.json"
        artifact.write_text(
            json.dumps({"id": artifact_id, "redacted": True}),
            encoding="utf-8",
        )
        artifacts.append(
            {"id": artifact_id, "path": artifact.name, "sha256": _sha256(artifact)}
        )
    payload: dict[str, Any] = {
        "schema_version": 1,
        "evidence_level": "W1",
        "commit_sha": COMMIT,
        "build_id": BUILD,
        "generated_at": "2026-07-15T12:00:00Z",
        "execution_environment": {
            "kind": "self_hosted_windows",
            "runner_id": RUNNER,
            "machine_id_sha256": "b" * 64,
        },
        "platform": {
            "os": "Windows",
            "version": "10.0.26100",
            "architecture": "AMD64",
        },
        "python_version": "3.13.7",
        "workbuddy": {"version": "1.2.3", "build": "wb-456"},
        "critical_skip_count": 0,
        "tests": [
            {"id": test_id, "status": "passed"}
            for test_id in REQUIRED_W1_TEST_IDS
        ],
        "artifacts": artifacts,
        "evidence_sha256": "0" * 64,
    }
    path = tmp_path / "windows-w1-evidence.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    seal_windows_evidence(path, schema_path=SCHEMA)
    return path


def test_valid_w1_requires_external_commit_build_and_runner_match(tmp_path: Path) -> None:
    path = _evidence(tmp_path)

    result = validate_windows_evidence(
        path,
        expected_commit=COMMIT,
        expected_build=BUILD,
        expected_runner_id=RUNNER,
        schema_path=SCHEMA,
        hosted_ci=False,
    )

    assert isinstance(result, WindowsEvidenceResult)
    assert result.status == "rollout_ready"
    assert result.rollout_ready is True
    assert result.verdict == "rollout_ready"
    assert result.errors == ()


def test_production_schema_is_authoritative_and_fixture_matches() -> None:
    assert json.loads(PRODUCTION_SCHEMA.read_text(encoding="utf-8")) == json.loads(
        SCHEMA.read_text(encoding="utf-8")
    )


def test_missing_evidence_is_explicitly_blocked(tmp_path: Path) -> None:
    result = validate_windows_evidence(
        tmp_path / "missing.json",
        expected_commit=COMMIT,
        expected_build=BUILD,
        expected_runner_id=RUNNER,
        schema_path=SCHEMA,
        hosted_ci=False,
    )

    assert result.status == "blocked"
    assert result.rollout_ready is False
    assert result.verdict == "BLOCKED: real-machine evidence missing"


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("commit_sha", "c" * 40, "commit_mismatch"),
        ("build_id", "other-build", "build_mismatch"),
        ("python_version", "3.14.0", "schema"),
        ("critical_skip_count", 1, "schema"),
    ],
)
def test_mismatch_or_schema_failure_never_becomes_rollout_ready(
    tmp_path: Path,
    field: str,
    value: Any,
    error: str,
) -> None:
    path = _evidence(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload[field] = value
    payload["evidence_sha256"] = canonical_evidence_sha256(payload)
    path.write_text(json.dumps(payload), encoding="utf-8")

    result = validate_windows_evidence(
        path,
        expected_commit=COMMIT,
        expected_build=BUILD,
        expected_runner_id=RUNNER,
        schema_path=SCHEMA,
        hosted_ci=False,
    )

    assert result.status == "implementation_candidate"
    assert result.rollout_ready is False
    assert any(error in item for item in result.errors)


@pytest.mark.parametrize(
    ("field", "value"),
    [("schema_version", True), ("critical_skip_count", False)],
)
def test_json_schema_const_rejects_boolean_number_aliases(
    tmp_path: Path, field: str, value: Any
) -> None:
    path = _evidence(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload[field] = value
    payload["evidence_sha256"] = canonical_evidence_sha256(payload)
    path.write_text(json.dumps(payload), encoding="utf-8")

    result = validate_windows_evidence(
        path,
        expected_commit=COMMIT,
        expected_build=BUILD,
        expected_runner_id=RUNNER,
        schema_path=SCHEMA,
        hosted_ci=False,
    )

    assert result.status == "implementation_candidate"
    assert any("schema" in error and "const" in error for error in result.errors)


def test_validator_rejects_tampered_evidence_and_referenced_artifact(tmp_path: Path) -> None:
    path = _evidence(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["workbuddy"]["build"] = "tampered"
    path.write_text(json.dumps(payload), encoding="utf-8")

    evidence_tamper = validate_windows_evidence(
        path,
        expected_commit=COMMIT,
        expected_build=BUILD,
        expected_runner_id=RUNNER,
        schema_path=SCHEMA,
        hosted_ci=False,
    )
    assert "evidence_hash_mismatch" in evidence_tamper.errors

    path = _evidence(tmp_path / "artifact")
    (path.parent / "w0_probe.json").write_text("tampered", encoding="utf-8")
    artifact_tamper = validate_windows_evidence(
        path,
        expected_commit=COMMIT,
        expected_build=BUILD,
        expected_runner_id=RUNNER,
        schema_path=SCHEMA,
        hosted_ci=False,
    )
    assert "artifact_hash_mismatch:w0_probe" in artifact_tamper.errors
    assert artifact_tamper.rollout_ready is False


def test_unreadable_artifact_is_candidate_not_validator_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _evidence(tmp_path)
    real_read_bytes = Path.read_bytes

    def unreadable(candidate: Path) -> bytes:
        if candidate.name == "w0_probe.json":
            raise PermissionError("denied")
        return real_read_bytes(candidate)

    monkeypatch.setattr(Path, "read_bytes", unreadable)

    result = validate_windows_evidence(
        path,
        expected_commit=COMMIT,
        expected_build=BUILD,
        expected_runner_id=RUNNER,
        schema_path=SCHEMA,
        hosted_ci=False,
    )

    assert result.status == "implementation_candidate"
    assert "artifact_unreadable:w0_probe" in result.errors


def test_artifact_cannot_self_report_rollout_or_escape_evidence_directory(
    tmp_path: Path,
) -> None:
    path = _evidence(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["rollout_ready"] = True
    payload["artifacts"][0]["path"] = "../outside.json"
    payload["evidence_sha256"] = canonical_evidence_sha256(payload)
    path.write_text(json.dumps(payload), encoding="utf-8")

    result = validate_windows_evidence(
        path,
        expected_commit=COMMIT,
        expected_build=BUILD,
        expected_runner_id=RUNNER,
        schema_path=SCHEMA,
        hosted_ci=False,
    )

    assert result.status == "implementation_candidate"
    assert result.rollout_ready is False
    assert any("schema" in item for item in result.errors)
    assert any("unsafe_artifact_path" in item for item in result.errors)


def test_required_artifact_ids_cannot_reuse_one_physical_file(tmp_path: Path) -> None:
    path = _evidence(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["artifacts"][1]["path"] = payload["artifacts"][0]["path"]
    payload["artifacts"][1]["sha256"] = payload["artifacts"][0]["sha256"]
    payload["evidence_sha256"] = canonical_evidence_sha256(payload)
    path.write_text(json.dumps(payload), encoding="utf-8")

    result = validate_windows_evidence(
        path,
        expected_commit=COMMIT,
        expected_build=BUILD,
        expected_runner_id=RUNNER,
        schema_path=SCHEMA,
        hosted_ci=False,
    )

    assert result.status == "implementation_candidate"
    assert "duplicate_artifact_path:w1_pytest" in result.errors


def test_hosted_or_untrusted_runner_is_only_an_implementation_candidate(
    tmp_path: Path,
) -> None:
    path = _evidence(tmp_path)

    hosted = validate_windows_evidence(
        path,
        expected_commit=COMMIT,
        expected_build=BUILD,
        expected_runner_id=RUNNER,
        schema_path=SCHEMA,
        hosted_ci=True,
    )
    untrusted = validate_windows_evidence(
        path,
        expected_commit=COMMIT,
        expected_build=BUILD,
        expected_runner_id=None,
        schema_path=SCHEMA,
        hosted_ci=False,
    )

    assert hosted.status == "implementation_candidate"
    assert "hosted_ci_not_real_machine" in hosted.errors
    assert untrusted.status == "implementation_candidate"
    assert "trusted_runner_required" in untrusted.errors


def test_missing_any_required_w1_gate_is_only_an_implementation_candidate(
    tmp_path: Path,
) -> None:
    path = _evidence(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["tests"] = [
        item for item in payload["tests"] if item["id"] != "chinese_user_path"
    ]
    payload["evidence_sha256"] = canonical_evidence_sha256(payload)
    path.write_text(json.dumps(payload), encoding="utf-8")

    result = validate_windows_evidence(
        path,
        expected_commit=COMMIT,
        expected_build=BUILD,
        expected_runner_id=RUNNER,
        schema_path=SCHEMA,
        hosted_ci=False,
    )

    assert result.status == "implementation_candidate"
    assert "missing_w1_test:chinese_user_path" in result.errors


def test_cli_is_a_thin_fail_closed_wrapper(tmp_path: Path) -> None:
    path = _evidence(tmp_path)
    script = PROJECT_ROOT / "scripts" / "validate_windows_evidence.py"

    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--evidence",
            str(path),
            "--schema",
            str(SCHEMA),
            "--expected-commit",
            COMMIT,
            "--expected-build",
            BUILD,
            "--expected-runner-id",
            RUNNER,
            "--hosted-ci",
        ],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 1
    output = json.loads(completed.stdout)
    assert output["status"] == "implementation_candidate"
    assert output["rollout_ready"] is False


def test_cli_auto_detects_github_hosted_runner_without_optional_flag(tmp_path: Path) -> None:
    path = _evidence(tmp_path)
    script = PROJECT_ROOT / "scripts" / "validate_windows_evidence.py"
    env = os.environ.copy()
    env.update({"GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "github-hosted"})

    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--evidence",
            str(path),
            "--schema",
            str(SCHEMA),
            "--expected-commit",
            COMMIT,
            "--expected-build",
            BUILD,
            "--expected-runner-id",
            RUNNER,
        ],
        cwd=PROJECT_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 1
    output = json.loads(completed.stdout)
    assert output["status"] == "implementation_candidate"
    assert "hosted_ci_not_real_machine" in output["errors"]


def test_cli_can_seal_runner_output_before_validating(tmp_path: Path) -> None:
    path = _evidence(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["evidence_sha256"] = "0" * 64
    path.write_text(json.dumps(payload), encoding="utf-8")
    script = PROJECT_ROOT / "scripts" / "validate_windows_evidence.py"
    env = os.environ.copy()
    env.pop("GITHUB_ACTIONS", None)
    env.pop("RUNNER_ENVIRONMENT", None)

    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--evidence",
            str(path),
            "--schema",
            str(SCHEMA),
            "--expected-commit",
            COMMIT,
            "--expected-build",
            BUILD,
            "--expected-runner-id",
            RUNNER,
            "--seal",
        ],
        cwd=PROJECT_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["status"] == "rollout_ready"
    assert json.loads(path.read_text())["evidence_sha256"] != "0" * 64


def test_w1_runner_and_probe_capture_verifiable_current_build_metadata() -> None:
    runner = (PROJECT_ROOT / "run_windows_w1.ps1").read_text(encoding="utf-8")
    probe = (PROJECT_ROOT / "probe_windows_workbuddy.ps1").read_text(encoding="utf-8")

    for required in (
        "ExpectedCommit",
        "BuildId",
        "RunnerId",
        "git rev-parse HEAD",
        "status --porcelain",
        "--untracked-files=normal",
        "-3.13",
        'windows and real_machine',
        "critical_skip_count",
        "Get-FileHash",
        "validate_windows_evidence.py",
        "--expected-commit",
        "--expected-build",
        "--expected-runner-id",
    ):
        assert required in runner
    assert "implementation_candidate" in runner
    assert "rollout_ready" not in runner
    for required in (
        "BuildId",
        "CommitSha",
        "PROCESSOR_ARCHITECTURE",
        "build_id",
        "commit_sha",
        "architecture",
    ):
        assert required in probe
    lowered = probe.lower()
    assert "token_value" not in lowered
    assert "cookie_value" not in lowered


def test_w0_probe_fails_closed_when_workbuddy_profile_or_config_is_missing() -> None:
    runner = (PROJECT_ROOT / "run_windows_w1.ps1").read_text(encoding="utf-8")
    probe = (PROJECT_ROOT / "probe_windows_workbuddy.ps1").read_text(encoding="utf-8")

    for required in (
        "ProfilePath",
        "workbuddy_not_detected",
        "config_not_detected",
        "profile_missing",
        "blocked_reasons",
        "status = 'blocked'",
    ):
        assert required in probe
    assert "-ProfilePath $verifiedProfilePath" in runner
    assert "$probePayload.gate.status -ne 'passed'" in runner


def test_windows_quality_lane_packages_schema_and_derives_hosted_blocked_status() -> None:
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "quality.yml").read_text(
        encoding="utf-8"
    )
    hosted = workflow.split("  windows-hosted:", 1)[1].split(
        "  windows-w1-evidence:", 1
    )[0]
    blocked = workflow.split("  windows-w1-evidence:", 1)[1]

    for script in (
        "install_windows.ps1",
        "uninstall_windows.ps1",
        "probe_windows_workbuddy.ps1",
        "run_windows_w1.ps1",
    ):
        assert script in hosted
    for test_path in (
        "tests/test_windows_runtime_config.py",
        "tests/test_windows_installer_lifecycle.py",
        "tests/test_windows_evidence.py",
        "tests/test_floating_windows.py",
    ):
        assert test_path in hosted
    assert "python -m pip wheel . --no-deps" in hosted
    assert "windows_evidence.schema.json" in hosted
    assert "validate_windows_evidence.py" in blocked
    assert "--hosted-ci" in blocked
    assert "$validationStatus -ne 2" in blocked
    assert "exit 0" in blocked
    assert 'status = "BLOCKED"' not in blocked
    assert blocked.count("[System.IO.File]::WriteAllText(") == 1


def test_real_machine_pytest_gate_is_stateful_and_never_an_empty_placeholder() -> None:
    gate = (PROJECT_ROOT / "tests" / "test_windows_w1_real_machine.py").read_text(
        encoding="utf-8"
    )
    runner = (PROJECT_ROOT / "run_windows_w1.ps1").read_text(encoding="utf-8")

    for marker in ("pytest.mark.windows", "pytest.mark.real_machine", "pytest.mark.critical"):
        assert marker in gate
    environment_inputs = (
        "WORKBUDDY_W1_INSTALLER_MANIFEST",
        "WORKBUDDY_W1_LIFECYCLE_LOG",
        "WORKBUDDY_W1_GIT_BASH",
        "WORKBUDDY_W1_CHINESE_TEST_ROOT",
        "WORKBUDDY_W1_IDENTITY_CHECK_URL",
        "WORKBUDDY_W1_STUDENT_A_TOKEN_FILE",
        "WORKBUDDY_W1_RECOVERY_RESULT",
    )
    for required in environment_inputs:
        assert required in gate
        assert required in runner
    for required in (
        "Get-ScheduledTask",
        "--health-check",
        "urllib.request",
    ):
        assert required in gate
    assert "pytest.skip" not in gate
    assert "@pytest.mark.skip" not in gate
    assert "test_windows_w1_real_machine.py" in runner
