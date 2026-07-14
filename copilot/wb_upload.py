"""Student-side bulk WorkBuddy transcript uploader.

Runs on the student machine. It reads the local WorkBuddy DB only to enumerate
sessions, reads local JSONL transcripts, strips non-message rows, and uploads
one filtered session transcript at a time.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from . import wb_sync
from .models import UploadOutcome, normalize_event_id
from .student_platform.workbuddy import (
    WorkBuddyDataAdapter,
    filter_message_jsonl as _filter_message_jsonl,
    filter_message_jsonl_text,
)

log = logging.getLogger("copilot.wb_upload")

DEFAULT_DB_PATH = wb_sync.DEFAULT_DB_PATH
DEFAULT_PROJECTS_DIR = Path.home() / ".workbuddy" / "projects"

_connect_readonly = wb_sync._connect_readonly
_config_token = wb_sync._config_token
_server_url = wb_sync._server_url
_student_id = wb_sync._student_id
_load_runtime_config = wb_sync._load_runtime_config


def read_sessions(db_path: str | os.PathLike[str] = DEFAULT_DB_PATH) -> list[dict[str, Any]]:
    """Enumerate non-deleted local WorkBuddy sessions."""
    return wb_sync.read_sessions(db_path, include_deleted=False)


def encode_cwd(cwd: str | os.PathLike[str] | None) -> str:
    """Encode WorkBuddy cwd into ~/.workbuddy/projects/<encoded> directory name."""
    return str(cwd or "").replace("/", "-").lstrip("-")


def transcript_path_for_session(
    session: dict[str, Any],
    *,
    projects_dir: str | os.PathLike[str] = DEFAULT_PROJECTS_DIR,
    db_path: str | os.PathLike[str] | None = None,
) -> Path | None:
    """Return a legacy unverified path only when no DB context was supplied.

    Older command-line callers with only ``projects_dir`` retain the historical
    path computation.  A call with a database path intentionally returns no
    path capability: normal upload uses adapter-owned transcript content and
    must never reopen a mutable local path after verification.
    """
    session_id = str(session.get("session_id") or session.get("id") or "")
    projects_path = Path(projects_dir).expanduser()
    if db_path is not None:
        return None
    cwd = str(session.get("work_dir") or session.get("cwd") or "")
    return projects_path / encode_cwd(cwd) / f"{session_id}.jsonl"


def filter_message_jsonl(path: str | os.PathLike[str]) -> str:
    """Compatibility wrapper over the platform adapter's JSONL filter."""
    return _filter_message_jsonl(path)


def content_sha256(content: str | bytes | bytearray) -> str:
    if isinstance(content, str):
        data = content.encode("utf-8")
    else:
        data = bytes(content)
    return hashlib.sha256(data).hexdigest()


def _auth_headers(token: str) -> dict[str, str]:
    if not token:
        return {}
    return {
        "Authorization": f"Bearer {token}",
        "X-Copilot-Token": token,
    }


def _read_json_response(resp) -> dict[str, Any]:
    body = resp.read().decode("utf-8")
    if not body:
        return {}
    parsed = json.loads(body)
    return parsed if isinstance(parsed, dict) else {}


def get_known_shas(
    server_url: str,
    student_id: str,
    token: str = "",
    timeout: float = 10.0,
) -> dict[str, dict[str, str]]:
    query = urllib.parse.urlencode({"student_id": student_id, "manifest_version": 2})
    url = server_url.rstrip("/") + f"/api/transcripts/known?{query}"
    req = urllib.request.Request(url, headers=_auth_headers(token), method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = _read_json_response(resp)
    manifest: dict[str, dict[str, str]] = {}
    for session_id, entry in body.items():
        if isinstance(entry, dict):
            sha = str(entry.get("sha") or "")
            status = str(entry.get("analysis_status") or "").strip().lower()
        else:
            sha = str(entry or "")
            status = "unknown"
        if sha:
            manifest[str(session_id)] = {"sha": sha, "analysis_status": status}
    return manifest


def should_upload(local_sha: str, remote_entry: Any) -> bool:
    """Return whether local content needs upload or failed analysis retry."""
    if isinstance(remote_entry, dict):
        remote_sha = str(remote_entry.get("sha") or "")
        analysis_status = str(remote_entry.get("analysis_status") or "").strip().lower()
    elif remote_entry:
        remote_sha = str(remote_entry)
        analysis_status = "unknown"
    else:
        return True
    return remote_sha != local_sha or analysis_status in {"failed", "unknown"}


def _legacy_probe_without_content(local_sha: str, remote_entry: Any) -> bool:
    """Legacy manifests cannot expose failure state; probe same SHA with an empty body."""
    if isinstance(remote_entry, dict):
        remote_sha = str(remote_entry.get("sha") or "")
        analysis_status = str(remote_entry.get("analysis_status") or "").strip().lower()
    elif remote_entry:
        remote_sha = str(remote_entry)
        analysis_status = "unknown"
    else:
        return False
    return remote_sha == local_sha and analysis_status == "unknown"


def post_transcript(
    server_url: str,
    session_id: str,
    payload: dict[str, Any],
    *,
    token: str = "",
    timeout: float = 60.0,
) -> dict[str, Any]:
    encoded_sid = urllib.parse.quote(str(session_id), safe="")
    url = server_url.rstrip("/") + f"/api/student/sessions/{encoded_sid}/transcript"
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        **_auth_headers(token),
    }
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return _read_json_response(resp)


def _progress(
    idx: int,
    total: int,
    *,
    synced: int,
    skipped: int,
    failed: int,
    session_id: str,
    status: str,
) -> None:
    print(
        f"synced {idx}/{total} session={session_id} status={status} "
        f"synced={synced} skipped={skipped} failed={failed}",
        flush=True,
    )


def upload_conversations(
    cfg: dict[str, Any],
    student_id: str,
    mode: str = "missing",
    *,
    db_path: str | os.PathLike[str] = DEFAULT_DB_PATH,
    projects_dir: str | os.PathLike[str] = DEFAULT_PROJECTS_DIR,
    server_url: str | None = None,
    token: str | None = None,
    timeout: float = 60.0,
    request_id: str | None = None,
    session_id: str | None = None,
    data_adapter: WorkBuddyDataAdapter | None = None,
    analysis_mode: str = "analyze",
    source_event_id: str | None = None,
    source_report_id: int | None = None,
) -> UploadOutcome:
    """Upload filtered transcripts one session at a time.

    mode="missing" skips sessions whose filtered-content sha matches the server
    manifest. mode="full" ignores the manifest and posts every local session.
    """
    if mode not in {"missing", "full"}:
        raise ValueError("mode must be 'missing' or 'full'")
    if analysis_mode not in {"analyze", "store_only"}:
        raise ValueError("analysis_mode must be 'analyze' or 'store_only'")
    if analysis_mode == "store_only":
        if request_id:
            raise ValueError("store_only cannot use a mentor request_id")
        if not session_id:
            raise ValueError("store_only requires an exact session_id")
        if normalize_event_id(source_event_id) is None or int(source_report_id or 0) <= 0:
            raise ValueError("store_only requires a source Stop event and report")

    resolved_student_id = str(student_id or "").strip()
    if not resolved_student_id:
        raise ValueError("student_id is required")
    requested_session_id = str(session_id or "").strip()

    resolved_server_url = _server_url(cfg, server_url)
    resolved_token = token if token is not None else _config_token(cfg)
    if data_adapter is None:
        data_adapter = WorkBuddyDataAdapter(
            Path(db_path).expanduser().parent,
            database_path=db_path,
            projects_dir=projects_dir,
        )
        sessions = read_sessions(db_path)
    else:
        sessions = [session.to_dict() for session in data_adapter.list_sessions()]
    if requested_session_id:
        sessions = [
            session for session in sessions
            if str(session.get("session_id") or session.get("id") or "")
            == requested_session_id
        ]
    total = len(sessions)
    if requested_session_id and total != 1:
        return UploadOutcome(
            matched=total,
            attempted=0,
            accepted=0,
            skipped=0,
            failed=0,
            error_code="session_not_found" if total == 0 else "session_ambiguous",
        )
    known: dict[str, dict[str, str]] = {}
    if mode == "missing":
        try:
            known = get_known_shas(
                resolved_server_url,
                resolved_student_id,
                token=resolved_token,
                timeout=min(timeout, 10.0),
            )
        except Exception as exc:
            log.warning("known transcript manifest unavailable; uploading candidates: %s", exc)

    synced = 0
    skipped = 0
    failed = 0
    attempted = 0
    error_code = ""
    snapshot_factory = getattr(data_adapter, "transcript_snapshot", None)
    transcript_reader = (
        snapshot_factory() if callable(snapshot_factory) else data_adapter
    )

    for idx, session in enumerate(sessions, start=1):
        session_id = str(session.get("session_id") or session.get("id") or "")
        if not session_id:
            skipped += 1
            _progress(idx, total, synced=synced, skipped=skipped, failed=failed, session_id="", status="skipped")
            continue

        try:
            transcript = transcript_reader.read_transcript(session_id)
            if transcript.failure is not None:
                raise RuntimeError(transcript.failure.code)
            content = filter_message_jsonl_text(transcript.content)
            sha = content_sha256(content)
            remote_entry = known.get(session_id)
            same_sha_skip = mode == "missing" and not should_upload(sha, remote_entry)
            if same_sha_skip and not request_id:
                skipped += 1
                _progress(
                    idx,
                    total,
                    synced=synced,
                    skipped=skipped,
                    failed=failed,
                    session_id=session_id,
                    status="skipped",
                )
                continue

            payload = {
                "student_id": resolved_student_id,
                "filtered_content": (
                    "" if same_sha_skip or _legacy_probe_without_content(sha, remote_entry)
                    else content
                ),
                "sha": sha,
            }
            if request_id:
                payload["request_id"] = request_id
            if analysis_mode == "store_only":
                payload.update({
                    "analysis_mode": "store_only",
                    "source_event_id": str(source_event_id),
                    "source_report_id": int(source_report_id or 0),
                })
            attempted += 1
            response = post_transcript(
                resolved_server_url,
                session_id,
                payload,
                token=resolved_token,
                timeout=timeout,
            )
            confirmed = (
                response.get("ok") is True
                and str(response.get("session_id") or "") == session_id
                and str(response.get("sha") or "") == sha
            )
            if not confirmed:
                failed += 1
                error_code = error_code or "response_unconfirmed"
                _progress(
                    idx,
                    total,
                    synced=synced,
                    skipped=skipped,
                    failed=failed,
                    session_id=session_id,
                    status="failed",
                )
                continue
            if same_sha_skip or bool(response.get("skipped")):
                skipped += 1
                progress_status = "skipped"
            else:
                synced += 1
                progress_status = "synced"
            _progress(
                idx,
                total,
                synced=synced,
                skipped=skipped,
                failed=failed,
                session_id=session_id,
                status=progress_status,
            )
        except Exception as exc:
            failed += 1
            error_code = error_code or "upload_failed"
            log.warning("transcript upload failed session=%s: %s", session_id, exc)
            _progress(
                idx,
                total,
                synced=synced,
                skipped=skipped,
                failed=failed,
                session_id=session_id,
                status="failed",
            )

    if total == 0:
        error_code = "session_not_found" if requested_session_id else "no_sessions"
    elif failed and not error_code:
        error_code = "partial_failure"
    return UploadOutcome(
        matched=total,
        attempted=attempted,
        accepted=synced,
        skipped=skipped,
        failed=failed,
        error_code=error_code,
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Upload filtered WorkBuddy transcripts to Copilot")
    parser.add_argument("--config", default=os.environ.get("COPILOT_CONFIG"))
    parser.add_argument("--db-path", default=os.environ.get("COPILOT_WB_DB") or str(DEFAULT_DB_PATH))
    parser.add_argument("--projects-dir", default=os.environ.get("COPILOT_WB_PROJECTS") or str(DEFAULT_PROJECTS_DIR))
    parser.add_argument("--student-id", default=None)
    parser.add_argument("--server-url", default=None)
    parser.add_argument("--token", default=None)
    parser.add_argument("--mode", choices=("missing", "full"), default="missing")
    parser.add_argument("--timeout", type=float, default=60.0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s")
    args = _parse_args(argv)
    cfg = _load_runtime_config(args.config)
    student_id = _student_id(cfg, args.student_id)
    if not student_id:
        print("COPILOT_STUDENT_ID or config student_id is required", file=sys.stderr)
        return 2
    token = args.token if args.token is not None else _config_token(cfg)

    try:
        result = upload_conversations(
            cfg,
            student_id,
            mode=args.mode,
            db_path=args.db_path,
            projects_dir=args.projects_dir,
            server_url=args.server_url,
            token=token,
            timeout=args.timeout,
        )
        print(json.dumps(dict(result), ensure_ascii=False))
        return 0 if result["failed"] == 0 else 1
    except (OSError, sqlite3.Error, urllib.error.URLError, TimeoutError, ValueError) as exc:
        log.error("upload failed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
