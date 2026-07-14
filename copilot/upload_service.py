"""State-machine boundary for mentor-triggered transcript uploads."""
from __future__ import annotations

import logging
from typing import Any, Literal

from .store import (
    Store,
    UploadRetryClaimConflict,
    UploadSessionRegistrationConflict,
)

log = logging.getLogger("copilot.upload_service")

TransferStatus = Literal["pending", "running", "stored", "failed"]
AnalysisStatus = Literal["not_requested", "pending", "running", "done", "failed"]

TRANSFER: dict[str, frozenset[str]] = {
    "pending": frozenset({"running", "failed"}),
    "running": frozenset({"stored", "failed"}),
    "failed": frozenset({"running"}),
    "stored": frozenset(),
}

ANALYSIS: dict[str, frozenset[str]] = {
    "not_requested": frozenset({"pending"}),
    "pending": frozenset({"running", "failed"}),
    "running": frozenset({"done", "failed"}),
    "failed": frozenset({"pending"}),
    "done": frozenset(),
}


class InvalidStateTransition(ValueError):
    """Raised when an upload request would move outside its state machine."""


class UploadRequestNotFound(LookupError):
    """Raised when a request is missing or belongs to another student."""


class UploadTranscriptNotFound(LookupError):
    """Raised when a request's persisted transcript cannot be retried."""


class UploadRequestService:
    def __init__(self, store: Store):
        self.store = store

    def create(
        self,
        mentor_id: str,
        student_id: str,
        session_id: str | None = None,
        *,
        request_id: str | None = None,
    ) -> str:
        return self.store.add_upload_request(
            mentor_id=mentor_id,
            student_id=student_id,
            session_id=session_id,
            request_id=request_id,
        )

    def list(
        self,
        student_id: str,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        return self.store.list_upload_requests(student_id=student_id, status=status)

    def get(self, request_id: str) -> dict[str, Any]:
        """Return a request for mentor status reads without student ownership input."""
        row = self.store.get_upload_request(request_id)
        if row is None:
            raise UploadRequestNotFound(f"upload request not found: {request_id}")
        return row

    def register_session(
        self,
        request_id: str,
        student_id: str,
        session_id: str,
        sha: str,
        *,
        analysis_status: AnalysisStatus = "pending",
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        row = self._get_owned(request_id, student_id)
        requested_session = str(row.get("session_id") or "")
        if requested_session and requested_session != session_id:
            raise InvalidStateTransition(
                f"upload request is scoped to session_id={requested_session}"
            )
        try:
            child = self.store.upsert_upload_request_session(
                request_id,
                student_id,
                session_id,
                sha,
                analysis_status=analysis_status,
            )
        except UploadSessionRegistrationConflict as exc:
            if "not found" in str(exc):
                raise UploadRequestNotFound(str(exc)) from exc
            raise InvalidStateTransition(str(exc)) from exc
        return child, self.refresh_parent_analysis(request_id, student_id)

    def mark_session_analysis(
        self,
        request_id: str,
        student_id: str,
        session_id: str,
        status: AnalysisStatus,
        *,
        error: str = "",
        sha: str | None = None,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        self._get_owned(request_id, student_id)
        children = self.store.list_upload_request_sessions(request_id)
        child = next(
            (item for item in children if item.get("session_id") == session_id),
            None,
        )
        if child is None or child.get("student_id") != student_id:
            raise UploadRequestNotFound(
                f"upload request session not found: {request_id}/{session_id}"
            )
        if sha is not None and str(child.get("sha") or "") != sha:
            return child, []
        exact_sha = sha or str(child.get("sha") or "")
        old = str(child.get("analysis_status") or "pending")
        if old != status:
            if status not in ANALYSIS.get(old, frozenset()):
                raise InvalidStateTransition(
                    f"invalid child analysis transition {old} -> {status}"
                )
            updated = self.store.compare_and_set_upload_request_session(
                request_id,
                student_id,
                session_id,
                expected=old,
                new_status=status,
                error=error,
                sha=exact_sha,
            )
            if not updated:
                latest = next(
                    item for item in self.store.list_upload_request_sessions(request_id)
                    if item.get("session_id") == session_id
                )
                if str(latest.get("sha") or "") != exact_sha:
                    return latest, []
                raise InvalidStateTransition("concurrent child analysis transition conflict")
            child = next(
                item for item in self.store.list_upload_request_sessions(request_id)
                if item.get("session_id") == session_id
            )
        return child, self.refresh_parent_analysis(request_id, student_id)

    def refresh_parent_analysis(
        self,
        request_id: str,
        student_id: str,
    ) -> list[dict[str, Any]]:
        parent = self._get_owned(request_id, student_id)
        children = self.store.list_upload_request_sessions(request_id)
        if not children:
            return []
        statuses = [str(child.get("analysis_status") or "pending") for child in children]
        actionable = [status for status in statuses if status != "not_requested"]
        if not actionable:
            return []
        if parent.get("transfer_status") != "stored":
            desired = "running" if any(
                status in {"running", "done", "failed"} for status in actionable
            ) or parent.get("analysis_status") == "running" else "pending"
        elif any(status == "failed" for status in actionable):
            desired = "failed"
        elif any(status == "running" for status in actionable):
            desired = "running"
        elif any(status == "pending" for status in actionable):
            desired = "running" if parent.get("analysis_status") == "running" else "pending"
        else:
            desired = "done"
        return self._advance_parent_analysis(request_id, student_id, desired)

    def _advance_parent_analysis(
        self,
        request_id: str,
        student_id: str,
        desired: AnalysisStatus,
    ) -> list[dict[str, Any]]:
        changed: list[dict[str, Any]] = []
        for _ in range(4):
            row = self._get_owned(request_id, student_id)
            current = str(row.get("analysis_status") or "not_requested")
            if current == desired:
                return changed
            if desired == "pending":
                next_status = "pending"
            elif desired == "running":
                next_status = "pending" if current in {"not_requested", "failed"} else "running"
            elif desired == "done":
                if current in {"not_requested", "failed"}:
                    next_status = "pending"
                elif current == "pending":
                    next_status = "running"
                else:
                    next_status = "done"
            else:  # failed
                next_status = "pending" if current == "not_requested" else "failed"
            changed.append(
                self.mark_analysis(
                    request_id,
                    student_id,
                    next_status,
                    error=("" if next_status != "failed" else self._child_failure_error(request_id)),
                )
            )
        raise InvalidStateTransition(f"cannot aggregate analysis state to {desired}")

    def _child_failure_error(self, request_id: str) -> str:
        for child in self.store.list_upload_request_sessions(request_id):
            if child.get("analysis_status") == "failed":
                return str(child.get("analysis_error") or "analysis failed")
        return "analysis failed"

    def recover_interrupted_analysis(
        self,
        error: str = "analysis interrupted; retry",
    ) -> list[dict[str, Any]]:
        self.store.recover_interrupted_raw_transcript_analyses(error)
        for child in self.store.list_active_upload_request_sessions():
            self.store.compare_and_set_upload_request_session(
                str(child["request_id"]),
                str(child["student_id"]),
                str(child["session_id"]),
                expected=str(child["analysis_status"]),
                new_status="failed",
                error=error,
                sha=str(child["sha"]),
            )
        recovered: list[dict[str, Any]] = []
        for row in self.store.list_active_upload_request_analyses():
            request_id = str(row["request_id"])
            student_id = str(row["student_id"])
            children = self.store.list_upload_request_sessions(request_id)
            try:
                if any(
                    str(child.get("analysis_status") or "not_requested")
                    != "not_requested"
                    for child in children
                ):
                    recovered.extend(
                        self.refresh_parent_analysis(request_id, student_id)
                    )
                else:
                    recovered.append(self.mark_analysis(
                        request_id,
                        student_id,
                        "failed",
                        error=error,
                    ))
            except InvalidStateTransition:
                continue
        return recovered

    def prepare_analysis_retry(
        self,
        request_id: str,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Atomically claim one specific or all failed child analyses."""
        try:
            return self.store.claim_upload_analysis_retry(request_id)
        except UploadRetryClaimConflict as exc:
            if str(exc) == "upload request not found":
                raise UploadRequestNotFound(str(exc)) from exc
            raise InvalidStateTransition(str(exc)) from exc

    def mark_transfer(
        self,
        request_id: str,
        student_id: str,
        status: TransferStatus,
        *,
        error: str | None = None,
        result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._transition(
            request_id=request_id,
            student_id=student_id,
            axis="transfer",
            new_status=status,
            transitions=TRANSFER,
            error=error,
            result=result,
        )

    def mark_analysis(
        self,
        request_id: str,
        student_id: str,
        status: AnalysisStatus,
        *,
        error: str | None = None,
    ) -> dict[str, Any]:
        return self._transition(
            request_id=request_id,
            student_id=student_id,
            axis="analysis",
            new_status=status,
            transitions=ANALYSIS,
            error=error,
        )

    def _transition(
        self,
        *,
        request_id: str,
        student_id: str,
        axis: Literal["transfer", "analysis"],
        new_status: str,
        transitions: dict[str, frozenset[str]],
        error: str | None,
        result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        row = self._get_owned(request_id, student_id)
        state_column = f"{axis}_status"
        old_status = str(row[state_column])
        if new_status == old_status:
            log.info(
                "upload transition idempotent request_id=%s axis=%s state=%s error_type=%s",
                request_id,
                axis,
                old_status,
                "reported" if error else "none",
            )
            return row
        if new_status not in transitions.get(old_status, frozenset()):
            raise InvalidStateTransition(
                f"invalid {axis} transition {old_status} -> {new_status}"
            )

        updated = self.store.compare_and_set_upload_request_axis(
            request_id,
            student_id=student_id,
            axis=axis,
            expected=old_status,
            new_status=new_status,
            error=error or "",
            result=result,
        )
        if updated:
            latest = self._get_owned(request_id, student_id)
            log.info(
                "upload transition request_id=%s axis=%s %s->%s error_type=%s",
                request_id,
                axis,
                old_status,
                new_status,
                "reported" if error else "none",
            )
            return latest

        latest = self._get_owned(request_id, student_id)
        concurrent_status = str(latest[state_column])
        if concurrent_status == new_status:
            return latest
        raise InvalidStateTransition(
            f"concurrent {axis} transition conflict: expected {old_status}, "
            f"found {concurrent_status}, requested {new_status}"
        )

    def _get_owned(self, request_id: str, student_id: str) -> dict[str, Any]:
        row = self.store.get_upload_request(request_id)
        if row is None or row.get("student_id") != student_id:
            raise UploadRequestNotFound(f"upload request not found: {request_id}")
        return row
