"""Fail-closed Windows W1 evidence validation.

Rollout status is derived here from a strict schema, canonical evidence hash,
referenced artifact hashes, the expected commit/build, a trusted runner ID and
the complete real-machine gate set.  A config boolean is never consulted.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Mapping, Sequence


REQUIRED_ARTIFACT_IDS = frozenset(
    {"w0_probe", "w1_pytest", "installer_manifest", "lifecycle_log"}
)
REQUIRED_W1_TEST_IDS = frozenset(
    {
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
    }
)


@dataclass(frozen=True)
class WindowsEvidenceResult:
    status: str
    verdict: str
    rollout_ready: bool
    errors: tuple[str, ...] = ()
    evidence_sha256: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "verdict": self.verdict,
            "rollout_ready": self.rollout_ready,
            "errors": list(self.errors),
            "evidence_sha256": self.evidence_sha256,
        }


def canonical_evidence_sha256(payload: Mapping[str, Any]) -> str:
    canonical = dict(payload)
    canonical.pop("evidence_sha256", None)
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    temporary = Path(raw)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(dict(payload), handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _type_matches(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, Mapping)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return False


def _json_const_equal(value: Any, expected: Any) -> bool:
    """Compare JSON constants without Python's bool/int aliasing."""

    return type(value) is type(expected) and value == expected


def _schema_errors(value: Any, schema: Mapping[str, Any], path: str = "$") -> list[str]:
    """Validate the intentionally small JSON-Schema subset used by W1."""

    errors: list[str] = []
    expected_type = schema.get("type")
    if isinstance(expected_type, str) and not _type_matches(value, expected_type):
        return [f"{path}:type:{expected_type}"]
    if "const" in schema and not _json_const_equal(value, schema["const"]):
        errors.append(f"{path}:const")
    enum = schema.get("enum")
    if isinstance(enum, list) and not any(
        _json_const_equal(value, candidate) for candidate in enum
    ):
        errors.append(f"{path}:enum")
    if isinstance(value, str):
        if len(value) < int(schema.get("minLength", 0)):
            errors.append(f"{path}:minLength")
        if "maxLength" in schema and len(value) > int(schema["maxLength"]):
            errors.append(f"{path}:maxLength")
        pattern = schema.get("pattern")
        if isinstance(pattern, str) and re.fullmatch(pattern, value) is None:
            errors.append(f"{path}:pattern")
    if isinstance(value, list):
        if len(value) < int(schema.get("minItems", 0)):
            errors.append(f"{path}:minItems")
        item_schema = schema.get("items")
        if isinstance(item_schema, Mapping):
            for index, item in enumerate(value):
                errors.extend(_schema_errors(item, item_schema, f"{path}[{index}]"))
    if isinstance(value, Mapping):
        required = schema.get("required", [])
        if isinstance(required, list):
            for key in required:
                if key not in value:
                    errors.append(f"{path}:required:{key}")
        properties = schema.get("properties", {})
        if isinstance(properties, Mapping):
            for key, child in value.items():
                child_schema = properties.get(key)
                if isinstance(child_schema, Mapping):
                    errors.extend(_schema_errors(child, child_schema, f"{path}.{key}"))
                elif schema.get("additionalProperties") is False:
                    errors.append(f"{path}:additionalProperties:{key}")
    return errors


def _read_json(path: Path) -> Mapping[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError("JSON root must be an object")
    return value


def seal_windows_evidence(
    path: str | os.PathLike[str],
    *,
    schema_path: str | os.PathLike[str],
) -> str:
    evidence_path = Path(path)
    payload = dict(_read_json(evidence_path))
    payload["evidence_sha256"] = canonical_evidence_sha256(payload)
    schema = _read_json(Path(schema_path))
    errors = _schema_errors(payload, schema)
    if errors:
        raise ValueError(f"evidence schema rejected: {errors[0]}")
    _atomic_json(evidence_path, payload)
    return str(payload["evidence_sha256"])


def _safe_artifact_path(root: Path, raw_path: Any) -> Path | None:
    if not isinstance(raw_path, str) or not raw_path.strip():
        return None
    relative = Path(raw_path)
    if relative.is_absolute() or ".." in relative.parts:
        return None
    candidate = root / relative
    try:
        candidate.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return None
    if candidate.is_symlink():
        return None
    return candidate


def _hosted_ci_default() -> bool:
    github = str(os.environ.get("GITHUB_ACTIONS") or "").lower() == "true"
    environment = str(os.environ.get("RUNNER_ENVIRONMENT") or "").lower()
    return github and environment != "self-hosted"


def validate_windows_evidence(
    path: str | os.PathLike[str],
    expected_commit: str,
    expected_build: str,
    *,
    expected_runner_id: str | None = None,
    schema_path: str | os.PathLike[str] | None = None,
    hosted_ci: bool | None = None,
) -> WindowsEvidenceResult:
    evidence_path = Path(path)
    if not evidence_path.exists():
        return WindowsEvidenceResult(
            status="blocked",
            verdict="BLOCKED: real-machine evidence missing",
            rollout_ready=False,
            errors=("real_machine_evidence_missing",),
        )
    errors: list[str] = []
    if evidence_path.is_symlink() or not evidence_path.is_file():
        errors.append("unsafe_evidence_path")
    try:
        payload = _read_json(evidence_path)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        return WindowsEvidenceResult(
            status="implementation_candidate",
            verdict="implementation_candidate",
            rollout_ready=False,
            errors=("evidence_unreadable",),
        )
    resolved_schema = (
        Path(schema_path)
        if schema_path is not None
        else Path(__file__).with_name("windows_evidence.schema.json")
    )
    try:
        schema = _read_json(resolved_schema)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        errors.append("schema_unavailable")
    else:
        errors.extend(f"schema:{item}" for item in _schema_errors(payload, schema))

    actual_evidence_hash = canonical_evidence_sha256(payload)
    claimed_hash = str(payload.get("evidence_sha256") or "")
    if claimed_hash != actual_evidence_hash:
        errors.append("evidence_hash_mismatch")
    if str(payload.get("commit_sha") or "") != str(expected_commit or ""):
        errors.append("commit_mismatch")
    if str(payload.get("build_id") or "") != str(expected_build or ""):
        errors.append("build_mismatch")

    execution = payload.get("execution_environment")
    runner_id = (
        str(execution.get("runner_id") or "")
        if isinstance(execution, Mapping)
        else ""
    )
    if not expected_runner_id:
        errors.append("trusted_runner_required")
    elif runner_id != str(expected_runner_id):
        errors.append("runner_mismatch")
    if _hosted_ci_default() if hosted_ci is None else bool(hosted_ci):
        errors.append("hosted_ci_not_real_machine")

    artifact_ids: set[str] = set()
    artifact_paths: dict[Path, str] = {}
    artifacts = payload.get("artifacts")
    if isinstance(artifacts, Sequence) and not isinstance(artifacts, (str, bytes)):
        for raw in artifacts:
            if not isinstance(raw, Mapping):
                continue
            artifact_id = str(raw.get("id") or "")
            if artifact_id in artifact_ids:
                errors.append(f"duplicate_artifact:{artifact_id}")
            artifact_ids.add(artifact_id)
            artifact_path = _safe_artifact_path(evidence_path.parent, raw.get("path"))
            if artifact_path is None:
                errors.append(f"unsafe_artifact_path:{artifact_id}")
                continue
            if not artifact_path.is_file():
                errors.append(f"artifact_missing:{artifact_id}")
                continue
            try:
                resolved_artifact = artifact_path.resolve(strict=True)
            except OSError:
                errors.append(f"artifact_unreadable:{artifact_id}")
                continue
            previous_id = artifact_paths.get(resolved_artifact)
            if previous_id is not None:
                errors.append(f"duplicate_artifact_path:{artifact_id}")
            else:
                artifact_paths[resolved_artifact] = artifact_id
            try:
                digest = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
            except OSError:
                errors.append(f"artifact_unreadable:{artifact_id}")
                continue
            if digest != str(raw.get("sha256") or ""):
                errors.append(f"artifact_hash_mismatch:{artifact_id}")
    for artifact_id in sorted(REQUIRED_ARTIFACT_IDS - artifact_ids):
        errors.append(f"missing_artifact:{artifact_id}")

    passed_tests: set[str] = set()
    seen_tests: set[str] = set()
    tests = payload.get("tests")
    if isinstance(tests, Sequence) and not isinstance(tests, (str, bytes)):
        for raw in tests:
            if not isinstance(raw, Mapping):
                continue
            test_id = str(raw.get("id") or "")
            if test_id in seen_tests:
                errors.append(f"duplicate_w1_test:{test_id}")
            seen_tests.add(test_id)
            if raw.get("status") == "passed":
                passed_tests.add(test_id)
    for test_id in sorted(REQUIRED_W1_TEST_IDS - passed_tests):
        errors.append(f"missing_w1_test:{test_id}")

    unique_errors = tuple(dict.fromkeys(errors))
    if unique_errors:
        return WindowsEvidenceResult(
            status="implementation_candidate",
            verdict="implementation_candidate",
            rollout_ready=False,
            errors=unique_errors,
            evidence_sha256=actual_evidence_hash,
        )
    return WindowsEvidenceResult(
        status="rollout_ready",
        verdict="rollout_ready",
        rollout_ready=True,
        evidence_sha256=actual_evidence_hash,
    )


__all__ = [
    "REQUIRED_ARTIFACT_IDS",
    "REQUIRED_W1_TEST_IDS",
    "WindowsEvidenceResult",
    "canonical_evidence_sha256",
    "seal_windows_evidence",
    "validate_windows_evidence",
]
