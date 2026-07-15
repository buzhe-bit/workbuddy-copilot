#!/usr/bin/env python3
"""Private, manifest-scoped state transitions for the macOS student installer."""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from typing import Any, Mapping
from urllib.parse import urlsplit
import uuid


OWNER_ID = "workbuddy-copilot-macos-v1"
SCHEMA_VERSION = 1


class InstallStateError(RuntimeError):
    """Fail-closed installer state error safe to show without secret values."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _absolute(path: str | os.PathLike[str]) -> Path:
    return Path(os.path.abspath(os.path.expanduser(os.fspath(path))))


def _require_absolute(path: str | os.PathLike[str], name: str) -> Path:
    raw = Path(os.path.expanduser(os.fspath(path)))
    if not raw.is_absolute():
        raise InstallStateError(f"{name} must be an absolute path")
    return _absolute(raw)


def _require_directory(path: Path, name: str) -> None:
    if path.is_symlink() or not path.is_dir():
        raise InstallStateError(f"{name} must be a real directory")


def _require_file(path: Path, name: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise InstallStateError(f"{name} must be a regular file")


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def _assert_real_child(child: Path, parent: Path, name: str) -> None:
    try:
        resolved_child = child.resolve(strict=True)
        resolved_parent = parent.resolve(strict=True)
    except OSError as exc:
        raise InstallStateError(f"{name} cannot be resolved safely") from exc
    if not _inside(resolved_child, resolved_parent):
        raise InstallStateError(f"{name} escapes its trusted root")


def _assert_private(path: Path, name: str) -> None:
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise InstallStateError(f"{name} must not be readable by group or other users")


def _assert_no_symlink_chain(path: Path, name: str) -> None:
    current = _absolute(path)
    while True:
        if current.is_symlink():
            raise InstallStateError(f"{name} must not contain a symlink")
        if current.parent == current:
            return
        current = current.parent


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        try:
            os.fsync(descriptor)
        except OSError:
            pass
    finally:
        os.close(descriptor)


def _write_bytes_atomically(path: Path, payload: bytes, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_temporary = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(raw_temporary)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, mode)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _json_bytes(payload: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    ).encode("utf-8")


def _write_json_atomically(path: Path, payload: Mapping[str, Any]) -> None:
    _write_bytes_atomically(path, _json_bytes(payload), mode=0o600)


def _read_mapping_snapshot(path: Path, name: str) -> tuple[dict[str, Any], bytes]:
    _require_file(path, name)
    try:
        raw = path.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InstallStateError(f"{name} is unreadable or invalid JSON") from exc
    if not isinstance(payload, dict):
        raise InstallStateError(f"{name} must contain a JSON object")
    return payload, raw


def _read_mapping(path: Path, name: str) -> dict[str, Any]:
    return _read_mapping_snapshot(path, name)[0]


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _validate_student_config(path: Path, project_root: Path) -> None:
    _require_file(path, "config.json")
    if not _inside(path, project_root):
        raise InstallStateError("config.json must stay inside the release directory")
    config = _read_mapping(path, "config.json")
    student_id = config.get("student_id")
    if not isinstance(student_id, str) or not student_id.strip() or len(student_id) > 256:
        raise InstallStateError("config.json requires a valid student_id")

    service = config.get("service")
    if not isinstance(service, dict):
        raise InstallStateError("config.json requires service settings")
    base_url = str(service.get("public_base_url") or "").strip().rstrip("/")
    parsed = urlsplit(base_url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise InstallStateError(
            "student service.public_base_url must be a credential-free HTTPS URL"
        )
    if str(service.get("token") or "").strip():
        raise InstallStateError("legacy service.token must not be sent to a student")

    auth = config.get("auth")
    if not isinstance(auth, dict) or str(auth.get("mode") or "").lower() != "pilot":
        raise InstallStateError("student auth.mode must be pilot")
    token = auth.get("student_token")
    if not isinstance(token, str) or not token.strip() or len(token) > 4096:
        raise InstallStateError("student auth.student_token is missing or invalid")
    if auth.get("allow_shared_student_token") is not False:
        raise InstallStateError("student config must disable shared student tokens")
    if str(auth.get("mentor_token") or "").strip() or str(
        auth.get("token") or ""
    ).strip():
        raise InstallStateError("mentor or legacy shared tokens must not be sent to a student")
    if str(config.get("token") or "").strip():
        raise InstallStateError("legacy root token must not be sent to a student")
    mapped_tokens = auth.get("student_tokens", {})
    if not isinstance(mapped_tokens, dict) or mapped_tokens:
        raise InstallStateError("server-side student token mappings must not be sent to a student")
    llm = config.get("llm", {})
    if not isinstance(llm, dict) or str(llm.get("api_key") or "").strip():
        raise InstallStateError("LLM API keys must not be sent to a student")


def _remove_owned_hooks(
    settings: Mapping[str, Any],
    *,
    include_legacy: bool,
) -> dict[str, Any]:
    cleaned = deepcopy(dict(settings))
    hooks = cleaned.get("hooks")
    if hooks is None:
        return cleaned
    if not isinstance(hooks, dict):
        raise InstallStateError("WorkBuddy settings hooks must be a JSON object")
    for event, raw_blocks in tuple(hooks.items()):
        if not isinstance(raw_blocks, list):
            raise InstallStateError("WorkBuddy hook event must contain a list")
        retained_blocks: list[Any] = []
        for raw_block in raw_blocks:
            if not isinstance(raw_block, dict):
                raise InstallStateError("WorkBuddy hook block must be a JSON object")
            raw_entries = raw_block.get("hooks", [])
            if not isinstance(raw_entries, list):
                raise InstallStateError("WorkBuddy hook entries must be a list")
            retained_entries: list[Any] = []
            for raw_entry in raw_entries:
                if not isinstance(raw_entry, dict):
                    raise InstallStateError("WorkBuddy hook entry must be a JSON object")
                command = str(raw_entry.get("command") or "")
                owned = f"COPILOT_ENTRY_OWNER={OWNER_ID}" in command
                legacy = include_legacy and "copilot/hook.py" in command
                if not owned and not legacy:
                    retained_entries.append(raw_entry)
            if retained_entries:
                retained_block = deepcopy(raw_block)
                retained_block["hooks"] = retained_entries
                retained_blocks.append(retained_block)
        if retained_blocks:
            hooks[event] = retained_blocks
        else:
            hooks.pop(event, None)
    if not hooks:
        cleaned.pop("hooks", None)
    return cleaned


def _require_owned_hooks(settings: Mapping[str, Any]) -> None:
    _remove_owned_hooks(settings, include_legacy=False)
    hooks = settings.get("hooks")
    marker = f"COPILOT_ENTRY_OWNER={OWNER_ID}"
    for event in ("UserPromptSubmit", "Stop"):
        blocks = hooks.get(event, []) if isinstance(hooks, dict) else []
        found = any(
            marker in str(entry.get("command") or "")
            for block in blocks
            for entry in block.get("hooks", [])
        )
        if not found:
            raise InstallStateError(f"required owned {event} hook is missing")


def _validate_layout(
    payload: Mapping[str, Any],
    *,
    trusted_state_dir: Path,
    trusted_workbuddy_root: Path,
) -> dict[str, Path]:
    try:
        paths = {
            name: _require_absolute(str(payload[name]), name)
            for name in (
                "project_root",
                "config_path",
                "workbuddy_root",
                "settings_path",
                "hook_link_path",
                "hook_target_path",
                "spool_dir",
                "state_dir",
                "manifest_path",
                "transaction_path",
            )
        }
    except KeyError as exc:
        raise InstallStateError("installer state is missing a required path") from exc
    _require_directory(paths["project_root"], "project_root")
    _assert_no_symlink_chain(paths["project_root"], "project_root")
    if paths["state_dir"] != trusted_state_dir:
        raise InstallStateError("installer state directory does not match the CLI path")
    if paths["workbuddy_root"] != trusted_workbuddy_root:
        raise InstallStateError("installer state does not match the trusted WorkBuddy root")
    _assert_no_symlink_chain(paths["state_dir"], "state_dir")
    _assert_no_symlink_chain(paths["workbuddy_root"], "trusted WorkBuddy root")
    _assert_no_symlink_chain(paths["hook_link_path"].parent, "WorkBuddy hook directory")
    _assert_no_symlink_chain(paths["spool_dir"], "WorkBuddy spool directory")
    _assert_no_symlink_chain(paths["hook_target_path"], "hook_target_path")
    _require_file(paths["config_path"], "config_path")
    _require_file(paths["hook_target_path"], "hook_target_path")
    if not _inside(paths["config_path"], paths["project_root"]):
        raise InstallStateError("config_path escapes project_root")
    if paths["hook_target_path"] != paths["project_root"] / "copilot" / "hook.py":
        raise InstallStateError("hook_target_path is not owned by this release")
    _assert_real_child(
        paths["hook_target_path"], paths["project_root"], "hook_target_path"
    )
    expected_workbuddy = paths["workbuddy_root"]
    if paths["settings_path"] != expected_workbuddy / "settings.json":
        raise InstallStateError("settings_path escapes the trusted WorkBuddy root")
    if paths["hook_link_path"] != expected_workbuddy / "copilot" / "hook.py":
        raise InstallStateError("hook_link_path is not the owned WorkBuddy link")
    if paths["spool_dir"] != expected_workbuddy / "copilot" / "spool":
        raise InstallStateError("spool_dir is not the owned WorkBuddy spool")
    install_id = str(payload.get("install_id") or "")
    if len(install_id) != 32 or any(
        character not in "0123456789abcdef" for character in install_id
    ):
        raise InstallStateError("installer state has an invalid install_id")
    if paths["manifest_path"] != paths["state_dir"] / "installer-manifest.json":
        raise InstallStateError("manifest_path is not owned by state_dir")
    if paths["transaction_path"] != paths["state_dir"] / ".install-transaction.json":
        raise InstallStateError("transaction_path is not owned by state_dir")
    return paths


def _prepare(args: argparse.Namespace) -> int:
    project_root = _require_absolute(args.project_root, "project_root")
    config_path = _require_absolute(args.config, "config")
    workbuddy_root = _require_absolute(args.workbuddy_root, "workbuddy_root")
    settings_path = _require_absolute(args.settings, "settings")
    hook_link_path = _require_absolute(args.hook_link, "hook_link")
    spool_dir = _require_absolute(args.spool_dir, "spool_dir")
    state_dir = _require_absolute(args.state_dir, "state_dir")
    _require_directory(project_root, "project_root")
    _assert_no_symlink_chain(project_root, "project_root")
    venv_path = project_root / "venv"
    if venv_path.exists() or venv_path.is_symlink():
        raise InstallStateError(
            "fresh-install-only: project venv already exists and will not be reused"
        )
    hook_target_path = project_root / "copilot" / "hook.py"
    _assert_no_symlink_chain(hook_target_path, "copilot/hook.py")
    _require_file(hook_target_path, "copilot/hook.py")
    _assert_real_child(hook_target_path, project_root, "copilot/hook.py")
    _validate_student_config(config_path, project_root)
    _assert_no_symlink_chain(workbuddy_root, "trusted WorkBuddy root")
    _assert_no_symlink_chain(hook_link_path.parent, "WorkBuddy hook directory")
    _assert_no_symlink_chain(spool_dir, "WorkBuddy spool directory")
    _assert_no_symlink_chain(state_dir, "private state directory")
    if settings_path != workbuddy_root / "settings.json":
        raise InstallStateError("settings must stay under the trusted WorkBuddy root")
    if settings_path.is_symlink():
        raise InstallStateError("WorkBuddy settings must not be a symlink")
    if settings_path.exists() and not settings_path.is_file():
        raise InstallStateError("WorkBuddy settings must be a regular file")
    if hook_link_path != workbuddy_root / "copilot" / "hook.py":
        raise InstallStateError("hook link must stay under the WorkBuddy config directory")
    if spool_dir != workbuddy_root / "copilot" / "spool":
        raise InstallStateError("spool directory must stay under the WorkBuddy config directory")
    if state_dir.exists() and not state_dir.is_dir():
        raise InstallStateError("private state path must be a directory")
    manifest_path = state_dir / "installer-manifest.json"
    if manifest_path.exists() or manifest_path.is_symlink():
        raise InstallStateError(
            "fresh-install-only: installer manifest already exists; run the old "
            "release's uninstall_macos.sh if installed, then archive its state"
        )
    transaction_path = state_dir / ".install-transaction.json"
    if transaction_path.exists() or transaction_path.is_symlink():
        raise InstallStateError("an unfinished macOS install transaction already exists")
    if hook_link_path.exists() or hook_link_path.is_symlink():
        raise InstallStateError("fresh-install-only: WorkBuddy hook link already exists")

    settings_existed = settings_path.exists()
    settings_payload = (
        _read_mapping(settings_path, "WorkBuddy settings") if settings_existed else {}
    )
    if _remove_owned_hooks(settings_payload, include_legacy=True) != settings_payload:
        raise InstallStateError(
            "fresh-install-only: preexisting Copilot hook must be removed first"
        )

    os.chmod(config_path, 0o600)
    _assert_private(config_path, "config.json")
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(state_dir, 0o700)
    _assert_private(state_dir, "private state directory")

    install_id = uuid.uuid4().hex
    transaction = {
        "schema_version": SCHEMA_VERSION,
        "owner_id": OWNER_ID,
        "install_id": install_id,
        "prepared_at": _utc_now(),
        "project_root": str(project_root),
        "config_path": str(config_path),
        "workbuddy_root": str(workbuddy_root),
        "settings_path": str(settings_path),
        "settings_existed": settings_existed,
        "hook_link_path": str(hook_link_path),
        "hook_target_path": str(hook_target_path),
        "spool_dir": str(spool_dir),
        "state_dir": str(state_dir),
        "manifest_path": str(manifest_path),
        "transaction_path": str(transaction_path),
    }
    _write_json_atomically(transaction_path, transaction)
    print(transaction_path)
    return 0


def _read_owned_state(
    path: Path,
    name: str,
    *,
    path_field: str,
) -> dict[str, Any]:
    payload = _read_mapping(path, name)
    _assert_private(path, name)
    if payload.get("schema_version") != SCHEMA_VERSION or payload.get("owner_id") != OWNER_ID:
        raise InstallStateError(f"{name} owner or schema mismatch")
    if _require_absolute(str(payload.get(path_field) or ""), path_field) != path:
        raise InstallStateError(f"{name} path does not match its protected location")
    return payload


def _owned_link_matches(link: Path, target: Path) -> bool:
    if not link.is_symlink():
        return False
    try:
        raw_target = Path(os.readlink(link))
    except OSError:
        return False
    if not raw_target.is_absolute():
        raw_target = link.parent / raw_target
    return _absolute(raw_target) == target


def _finalize(args: argparse.Namespace) -> int:
    state_dir = _require_absolute(args.state_dir, "state_dir")
    workbuddy_root = _require_absolute(args.workbuddy_root, "workbuddy_root")
    transaction_path = state_dir / ".install-transaction.json"
    transaction = _read_owned_state(
        transaction_path,
        "install transaction",
        path_field="transaction_path",
    )
    paths = _validate_layout(
        transaction,
        trusted_state_dir=state_dir,
        trusted_workbuddy_root=workbuddy_root,
    )
    settings, settings_bytes = _read_mapping_snapshot(
        paths["settings_path"], "installed WorkBuddy settings"
    )
    _require_owned_hooks(settings)
    if not _owned_link_matches(paths["hook_link_path"], paths["hook_target_path"]):
        raise InstallStateError("owned WorkBuddy hook link is missing or changed")
    manifest = {key: value for key, value in transaction.items() if key != "prepared_at"}
    manifest.update(
        {
            "status": "installed",
            "installed_at": _utc_now(),
            "settings_after_sha256": _sha256_bytes(settings_bytes),
        }
    )
    # Transform the one transaction file into the installed manifest with a
    # single rename; transaction and manifest are never visible together.
    _write_json_atomically(transaction_path, manifest)
    os.replace(transaction_path, paths["manifest_path"])
    os.chmod(paths["manifest_path"], 0o600)
    _fsync_directory(paths["state_dir"])
    print(paths["manifest_path"])
    return 0


def _remove_owned_link(link: Path, target: Path) -> bool:
    if _owned_link_matches(link, target):
        link.unlink()
        _fsync_directory(link.parent)
        return True
    return False


def _remove_owned_settings(path: Path, *, settings_existed: bool) -> str:
    if not path.exists() and not path.is_symlink():
        return "settings_missing"
    current = _read_mapping(path, "WorkBuddy settings")
    cleaned = _remove_owned_hooks(current, include_legacy=False)
    if not settings_existed and not cleaned:
        path.unlink()
        _fsync_directory(path.parent)
        return "created_empty_settings_removed"
    if cleaned == current:
        return "owned_hooks_absent"
    mode = stat.S_IMODE(path.stat().st_mode)
    _write_bytes_atomically(path, _json_bytes(cleaned), mode=mode)
    return "owned_hooks_removed"


def _rollback(args: argparse.Namespace) -> int:
    state_dir = _require_absolute(args.state_dir, "state_dir")
    workbuddy_root = _require_absolute(args.workbuddy_root, "workbuddy_root")
    transaction_path = state_dir / ".install-transaction.json"
    transaction = _read_owned_state(
        transaction_path,
        "install transaction",
        path_field="transaction_path",
    )
    paths = _validate_layout(
        transaction,
        trusted_state_dir=state_dir,
        trusted_workbuddy_root=workbuddy_root,
    )
    _remove_owned_settings(
        paths["settings_path"],
        settings_existed=bool(transaction.get("settings_existed")),
    )
    _remove_owned_link(paths["hook_link_path"], paths["hook_target_path"])
    transaction_path.unlink(missing_ok=True)
    _fsync_directory(paths["state_dir"])
    print("macOS install transaction rolled back")
    return 0


def _uninstall(args: argparse.Namespace) -> int:
    state_dir = _require_absolute(args.state_dir, "state_dir")
    workbuddy_root = _require_absolute(args.workbuddy_root, "workbuddy_root")
    manifest_path = state_dir / "installer-manifest.json"
    manifest = _read_owned_state(
        manifest_path,
        "installer manifest",
        path_field="manifest_path",
    )
    paths = _validate_layout(
        manifest,
        trusted_state_dir=state_dir,
        trusted_workbuddy_root=workbuddy_root,
    )
    requested_project = _require_absolute(args.project_root, "project_root")
    if requested_project != paths["project_root"]:
        raise InstallStateError("installer manifest belongs to another release directory")
    if manifest.get("status") == "uninstalled":
        print(manifest_path)
        return 0
    if manifest.get("status") != "installed":
        raise InstallStateError("installer manifest status is unsupported")
    rollback_result = _remove_owned_settings(
        paths["settings_path"],
        settings_existed=bool(manifest.get("settings_existed")),
    )

    hook_link_removed = _remove_owned_link(
        paths["hook_link_path"], paths["hook_target_path"]
    )
    updated = dict(manifest)
    updated.update(
        {
            "status": "uninstalled",
            "uninstalled_at": _utc_now(),
            "settings_rollback": rollback_result,
            "hook_link_removed": hook_link_removed,
        }
    )
    _write_json_atomically(manifest_path, updated)
    print(manifest_path)
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    prepare = commands.add_parser("prepare")
    prepare.add_argument("--project-root", required=True)
    prepare.add_argument("--config", required=True)
    prepare.add_argument("--workbuddy-root", required=True)
    prepare.add_argument("--settings", required=True)
    prepare.add_argument("--hook-link", required=True)
    prepare.add_argument("--spool-dir", required=True)
    prepare.add_argument("--state-dir", required=True)

    finalize = commands.add_parser("finalize")
    finalize.add_argument("--state-dir", required=True)
    finalize.add_argument("--workbuddy-root", required=True)

    rollback = commands.add_parser("rollback")
    rollback.add_argument("--state-dir", required=True)
    rollback.add_argument("--workbuddy-root", required=True)

    uninstall = commands.add_parser("uninstall")
    uninstall.add_argument("--state-dir", required=True)
    uninstall.add_argument("--workbuddy-root", required=True)
    uninstall.add_argument("--project-root", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return {
            "prepare": _prepare,
            "finalize": _finalize,
            "rollback": _rollback,
            "uninstall": _uninstall,
        }[args.command](args)
    except (InstallStateError, OSError, ValueError, TypeError) as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
