from __future__ import annotations

import sqlite3

import pytest

from copilot.store import Store
from copilot.upload_service import (
    InvalidStateTransition,
    UploadRequestNotFound,
    UploadRequestService,
)


@pytest.fixture
def store(tmp_path) -> Store:
    return Store(tmp_path / "copilot.db")


@pytest.fixture
def service(store: Store) -> UploadRequestService:
    return UploadRequestService(store)


def test_transfer_and_analysis_states_and_errors_are_independent(
    store: Store,
    service: UploadRequestService,
):
    request_id = service.create("mentor-1", "student-a")

    service.mark_transfer(request_id, "student-a", "running")
    service.mark_transfer(request_id, "student-a", "stored", result={"stored": 2})
    service.mark_analysis(request_id, "student-a", "pending")
    service.mark_analysis(request_id, "student-a", "running")
    service.mark_analysis(request_id, "student-a", "failed", error="provider_timeout")

    row = store.get_upload_request(request_id)
    assert row is not None
    assert row["transfer_status"] == "stored"
    assert row["analysis_status"] == "failed"
    assert row["transfer_error"] == ""
    assert row["analysis_error"] == "provider_timeout"
    assert row["status"] == "done"
    assert row["result_json"] == '{"stored": 2}'


@pytest.mark.parametrize(
    ("axis", "terminal", "backward"),
    [("transfer", "stored", "running"), ("analysis", "done", "pending")],
)
def test_terminal_states_cannot_move_backwards(
    service: UploadRequestService,
    axis: str,
    terminal: str,
    backward: str,
):
    request_id = service.create("mentor-1", "student-a")
    if axis == "transfer":
        service.mark_transfer(request_id, "student-a", "running")
        service.mark_transfer(request_id, "student-a", terminal)
        transition = service.mark_transfer
    else:
        service.mark_analysis(request_id, "student-a", "pending")
        service.mark_analysis(request_id, "student-a", "running")
        service.mark_analysis(request_id, "student-a", terminal)
        transition = service.mark_analysis

    with pytest.raises(InvalidStateTransition, match=f"{terminal}.*{backward}"):
        transition(request_id, "student-a", backward)


def test_failed_states_can_retry_and_same_state_is_idempotent(
    store: Store,
    service: UploadRequestService,
):
    request_id = service.create("mentor-1", "student-a")

    service.mark_transfer(request_id, "student-a", "failed", error="offline")
    failed = store.get_upload_request(request_id)
    service.mark_transfer(request_id, "student-a", "failed", error="ignored duplicate")
    duplicate = store.get_upload_request(request_id)
    service.mark_transfer(request_id, "student-a", "running")

    service.mark_analysis(request_id, "student-a", "pending")
    service.mark_analysis(request_id, "student-a", "failed", error="timeout")
    service.mark_analysis(request_id, "student-a", "pending")

    row = store.get_upload_request(request_id)
    assert failed is not None and duplicate is not None and row is not None
    assert duplicate["updated_at"] == failed["updated_at"]
    assert duplicate["transfer_error"] == "offline"
    assert row["transfer_status"] == "running"
    assert row["transfer_error"] == ""
    assert row["analysis_status"] == "pending"
    assert row["analysis_error"] == ""


def test_transfer_retry_clears_stale_result_but_analysis_transition_preserves_result(
    store: Store,
    service: UploadRequestService,
):
    retried_id = service.create("mentor-1", "student-a")
    service.mark_transfer(
        retried_id,
        "student-a",
        "failed",
        error="offline",
        result={"failed": 1},
    )
    service.mark_transfer(retried_id, "student-a", "running")
    service.mark_transfer(retried_id, "student-a", "stored")

    retried = store.get_upload_request(retried_id)
    assert retried is not None
    assert retried["result_json"] is None

    analyzed_id = service.create("mentor-1", "student-a")
    service.mark_transfer(analyzed_id, "student-a", "running")
    service.mark_transfer(
        analyzed_id,
        "student-a",
        "stored",
        result={"synced": 2},
    )
    service.mark_analysis(analyzed_id, "student-a", "pending")
    service.mark_analysis(analyzed_id, "student-a", "running")
    service.mark_analysis(analyzed_id, "student-a", "done")

    analyzed = store.get_upload_request(analyzed_id)
    assert analyzed is not None
    assert analyzed["result_json"] == '{"synced": 2}'


def test_store_compare_and_set_rejects_stale_expected_without_overwriting(store: Store):
    request_id = store.add_upload_request("mentor-1", "student-a")

    first = store.compare_and_set_upload_request_axis(
        request_id,
        student_id="student-a",
        axis="transfer",
        expected="pending",
        new_status="running",
        error="",
    )
    stale = store.compare_and_set_upload_request_axis(
        request_id,
        student_id="student-a",
        axis="transfer",
        expected="pending",
        new_status="failed",
        error="stale writer",
    )

    assert first == 1
    assert stale == 0
    row = store.get_upload_request(request_id)
    assert row is not None
    assert row["transfer_status"] == "running"
    assert row["transfer_error"] == ""


def test_parent_done_cas_rechecks_children_before_terminal_transition(
    store: Store,
    service: UploadRequestService,
):
    request_id = service.create("mentor-1", "student-a")
    service.mark_transfer(request_id, "student-a", "running")
    service.mark_transfer(request_id, "student-a", "stored")
    service.register_session(
        request_id, "student-a", "sess-finished", "sha-finished"
    )
    store.compare_and_set_upload_request_session(
        request_id,
        "student-a",
        "sess-finished",
        expected="pending",
        new_status="running",
        sha="sha-finished",
    )
    store.compare_and_set_upload_request_session(
        request_id,
        "student-a",
        "sess-finished",
        expected="running",
        new_status="done",
        sha="sha-finished",
    )
    service.mark_analysis(request_id, "student-a", "running")

    service.register_session(
        request_id, "student-a", "sess-late", "sha-late"
    )
    with pytest.raises(InvalidStateTransition, match="concurrent"):
        service.mark_analysis(request_id, "student-a", "done")

    parent = store.get_upload_request(request_id)
    assert parent is not None
    assert parent["analysis_status"] == "running"
    assert {
        child["session_id"]: child["analysis_status"]
        for child in store.list_upload_request_sessions(request_id)
    } == {"sess-finished": "done", "sess-late": "pending"}


def test_store_compare_and_set_rejects_unapproved_axis_name(store: Store):
    request_id = store.add_upload_request("mentor-1", "student-a")
    with pytest.raises(ValueError, match="axis"):
        store.compare_and_set_upload_request_axis(
            request_id,
            student_id="student-a",
            axis="status = 'failed' --",
            expected="pending",
            new_status="failed",
            error="",
        )


def test_service_reports_not_found_for_missing_or_other_student(
    store: Store,
    service: UploadRequestService,
):
    request_id = service.create("mentor-1", "student-a")

    with pytest.raises(UploadRequestNotFound):
        service.mark_transfer("missing", "student-a", "running")
    with pytest.raises(UploadRequestNotFound):
        service.mark_transfer(request_id, "student-b", "running")

    row = store.get_upload_request(request_id)
    assert row is not None
    assert row["transfer_status"] == "pending"


def test_service_detects_compare_and_set_conflict(store: Store, service: UploadRequestService, monkeypatch):
    request_id = service.create("mentor-1", "student-a")
    real_cas = store.compare_and_set_upload_request_axis

    def concurrent_writer(*args, **kwargs):
        real_cas(
            request_id,
            student_id="student-a",
            axis="transfer",
            expected="pending",
            new_status="running",
            error="",
        )
        return 0

    monkeypatch.setattr(store, "compare_and_set_upload_request_axis", concurrent_writer)

    with pytest.raises(InvalidStateTransition, match="concurrent.*running"):
        service.mark_transfer(request_id, "student-a", "failed", error="offline")


def test_legacy_schema_is_migrated_once_and_old_rows_are_mapped(tmp_path):
    db_path = tmp_path / "legacy.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """CREATE TABLE upload_requests (
                request_id TEXT PRIMARY KEY,
                mentor_id TEXT NOT NULL,
                student_id TEXT NOT NULL,
                session_id TEXT,
                status TEXT NOT NULL,
                error_message TEXT DEFAULT '',
                result_json TEXT,
                updated_at REAL,
                created_at REAL NOT NULL
            )"""
        )
        conn.executemany(
            """INSERT INTO upload_requests
               (request_id, mentor_id, student_id, status, error_message, created_at)
               VALUES (?, 'mentor-1', 'student-a', ?, ?, 1)""",
            [
                ("pending", "pending", ""),
                ("running", "running", ""),
                ("done", "done", ""),
                ("failed", "failed", "network timeout"),
            ],
        )

    first = Store(db_path)
    second = Store(db_path)

    rows = {row["request_id"]: row for row in second.list_upload_requests("student-a")}
    assert rows["pending"]["transfer_status"] == "pending"
    assert rows["running"]["transfer_status"] == "running"
    assert rows["done"]["transfer_status"] == "stored"
    assert rows["failed"]["transfer_status"] == "failed"
    assert rows["failed"]["transfer_error"] == "network timeout"
    assert {row["analysis_status"] for row in rows.values()} == {"not_requested"}
    assert first.get_upload_request("done")["transfer_status"] == "stored"


def test_partial_axis_migration_derives_legacy_status_from_transfer(tmp_path):
    db_path = tmp_path / "partial-migration.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """CREATE TABLE upload_requests (
                request_id TEXT PRIMARY KEY,
                mentor_id TEXT NOT NULL,
                student_id TEXT NOT NULL,
                session_id TEXT,
                status TEXT NOT NULL,
                transfer_status TEXT NOT NULL DEFAULT 'pending',
                error_message TEXT DEFAULT '',
                result_json TEXT,
                updated_at REAL,
                created_at REAL NOT NULL
            )"""
        )
        conn.execute(
            """INSERT INTO upload_requests
               (request_id, mentor_id, student_id, status, transfer_status, created_at)
               VALUES ('partial', 'mentor-1', 'student-a', 'pending', 'stored', 1)"""
        )

    store = Store(db_path)

    row = store.get_upload_request("partial")
    assert row is not None
    assert row["transfer_status"] == "stored"
    assert row["status"] == "done"
    assert store.list_pending_upload_requests("student-a") == []
