"""Windows-safe process identity and durable event-claim contracts."""
from __future__ import annotations

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import start_student_agent

from copilot.student_core import process_liveness as process_liveness_module
from copilot.student_core.models import HookEvent
from copilot.student_core.process_liveness import (
    FileClaimStore,
    ProcessIdentity,
    ProcessLiveness,
)
from copilot.student_core.spool import EventSpool


pytestmark = [pytest.mark.contract, pytest.mark.windows, pytest.mark.critical]


class _FixedLiveness:
    def __init__(
        self,
        states: dict[str, str] | None = None,
        *,
        default: str = "alive",
        legacy_state: str = "alive",
    ) -> None:
        self.states = states or {}
        self.default = default
        self.legacy_state = legacy_state
        self.probes: list[ProcessIdentity] = []

    def probe(self, identity: ProcessIdentity) -> str:
        self.probes.append(identity)
        return self.states.get(identity.owner_token, self.default)

    def probe_pid(self, _pid: int) -> str:
        return self.legacy_state


def _identity(owner: str, *, pid: int = 1234, started_at: int = 9876) -> ProcessIdentity:
    return ProcessIdentity(pid=pid, started_at=started_at, owner_token=owner)


def _event() -> HookEvent:
    return HookEvent(event="Stop", student_id="student-1", session_id="session-1")


def _spool(
    root: Path,
    identity: ProcessIdentity,
    liveness: _FixedLiveness,
) -> EventSpool:
    return EventSpool(root, process_identity=identity, process_liveness=liveness)


def test_process_identity_round_trips_as_strict_json_contract() -> None:
    identity = _identity("agent-a")

    assert ProcessIdentity.from_dict(identity.to_dict()) == identity
    with pytest.raises(ValueError):
        ProcessIdentity(pid=0, started_at=1, owner_token="agent-a")
    with pytest.raises(ValueError):
        ProcessIdentity(pid=1, started_at=0, owner_token="agent-a")
    with pytest.raises(ValueError):
        ProcessIdentity(pid=1, started_at=1, owner_token="")


def test_process_liveness_classifies_alive_and_pid_reuse() -> None:
    observed_started_at = 321
    liveness = ProcessLiveness(started_at_reader=lambda _pid: observed_started_at)

    assert liveness.probe(_identity("same", started_at=observed_started_at)) == "alive"
    assert liveness.probe(_identity("reused", started_at=observed_started_at - 1)) == "reused"


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (ProcessLookupError(), "dead"),
        (FileNotFoundError(), "dead"),
        (PermissionError(), "unknown"),
        (OSError("probe failed"), "unknown"),
        (ValueError("malformed process metadata"), "unknown"),
    ],
)
def test_process_liveness_failures_are_fail_closed(
    error: BaseException,
    expected: str,
) -> None:
    def fail(_pid: int) -> int:
        raise error

    assert ProcessLiveness(started_at_reader=fail).probe(_identity("agent")) == expected


def test_current_process_uses_stable_local_fallback_when_native_probe_is_blocked() -> None:
    def blocked(_pid: int) -> int:
        raise PermissionError("sandbox blocked process inspection")

    liveness = ProcessLiveness(started_at_reader=blocked)

    first = liveness.current_identity(owner_token="first-instance")
    second = liveness.current_identity(owner_token="second-instance")

    assert first.pid == os.getpid()
    assert second.pid == first.pid
    assert second.started_at == first.started_at
    assert liveness.probe(first) == "alive"


def test_foreign_probe_cannot_misclassify_local_fallback_as_pid_reuse() -> None:
    def blocked(_pid: int) -> int:
        raise PermissionError("sandbox blocked process inspection")

    fallback_identity = ProcessLiveness(started_at_reader=blocked).current_identity(
        owner_token="fallback-owner"
    )
    foreign_record = ProcessIdentity(
        pid=fallback_identity.pid + 1,
        started_at=fallback_identity.started_at,
        owner_token=fallback_identity.owner_token,
    )
    recovered_probe = ProcessLiveness(started_at_reader=lambda _pid: 123)

    assert recovered_probe.probe(foreign_record) == "unknown"


def test_windows_process_probe_never_calls_posix_os_kill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Windows liveness must not call os.kill")

    monkeypatch.setattr(os, "kill", forbidden)
    liveness = ProcessLiveness(
        platform_name="win32",
        started_at_reader=lambda _pid: 9876,
    )

    assert liveness.probe(_identity("windows-agent")) == "alive"


def test_event_claim_persists_versioned_stable_process_identity(tmp_path: Path) -> None:
    identity = _identity("agent-a")
    spool = _spool(tmp_path, identity, _FixedLiveness())
    spool.enqueue(_event(), event_id="event-a")

    assert spool.claim("event-a") is True

    claim = json.loads((tmp_path / ".event-a.claim").read_text(encoding="utf-8"))
    assert claim == {
        "version": 1,
        "identity": identity.to_dict(),
        "created_at_ns": claim["created_at_ns"],
    }
    assert isinstance(claim["created_at_ns"], int)
    assert claim["created_at_ns"] > 0


def test_alive_claim_blocks_other_agent_and_hides_pending_event(tmp_path: Path) -> None:
    first = _spool(tmp_path, _identity("first"), _FixedLiveness())
    first.enqueue(_event(), event_id="owned")
    assert first.claim("owned") is True

    second_liveness = _FixedLiveness({"first": "alive"})
    second = _spool(tmp_path, _identity("second"), second_liveness)

    assert second.claim("owned") is False
    assert second.pending() == []
    assert second_liveness.probes[-1] == _identity("first")
    assert json.loads((tmp_path / ".owned.claim").read_text())["identity"]["owner_token"] == "first"


@pytest.mark.parametrize("state", ["dead", "reused"])
def test_only_dead_or_reused_claim_can_be_automatically_reclaimed(
    tmp_path: Path,
    state: str,
) -> None:
    first = _spool(tmp_path, _identity("first"), _FixedLiveness())
    first.enqueue(_event(), event_id="stale")
    assert first.claim("stale") is True

    second = _spool(
        tmp_path,
        _identity("second", pid=4321, started_at=6789),
        _FixedLiveness({"first": state}),
    )

    assert second.claim("stale") is True
    claim = json.loads((tmp_path / ".stale.claim").read_text())
    assert claim["identity"] == second.process_identity.to_dict()
    assert second.claim_health()["status"] == "ok"


def test_same_pid_with_different_started_at_is_reclaimed_as_reused(tmp_path: Path) -> None:
    first_identity = _identity("first", pid=777, started_at=100)
    first = _spool(tmp_path, first_identity, _FixedLiveness())
    first.enqueue(_event(), event_id="pid-reuse")
    assert first.claim("pid-reuse") is True

    second_identity = _identity("second", pid=777, started_at=200)
    second = EventSpool(
        tmp_path,
        process_identity=second_identity,
        process_liveness=ProcessLiveness(started_at_reader=lambda _pid: 200),
    )

    assert second.claim("pid-reuse") is True
    claim = json.loads((tmp_path / ".pid-reuse.claim").read_text())
    assert claim["identity"] == second_identity.to_dict()


def test_unknown_claim_never_expires_and_is_visible_in_health(tmp_path: Path) -> None:
    first = _spool(tmp_path, _identity("first"), _FixedLiveness())
    first.enqueue(_event(), event_id="ambiguous")
    assert first.claim("ambiguous") is True
    claim_path = tmp_path / ".ambiguous.claim"
    claim = json.loads(claim_path.read_text())
    claim["created_at_ns"] = 1
    claim_path.write_text(json.dumps(claim), encoding="utf-8")

    second = _spool(
        tmp_path,
        _identity("second"),
        _FixedLiveness({"first": "unknown"}),
    )

    assert second.claim("ambiguous") is False
    assert second.pending() == []
    assert claim_path.exists()
    assert second.claim_health() == {
        "status": "degraded",
        "unknown_claims": [
            {"claim_id": "ambiguous", "reason": "liveness_unknown"}
        ],
    }


def test_malformed_or_symlinked_claim_is_unknown_and_never_deleted(tmp_path: Path) -> None:
    spool = _spool(tmp_path, _identity("agent"), _FixedLiveness())
    spool.enqueue(_event(), event_id="malformed")
    malformed = tmp_path / ".malformed.claim"
    malformed.write_text("not-json", encoding="utf-8")

    outside = tmp_path / "outside-claim"
    outside.write_text("do-not-touch", encoding="utf-8")
    spool.enqueue(_event(), event_id="symlinked")
    symlinked = tmp_path / ".symlinked.claim"
    symlinked.symlink_to(outside)

    assert spool.claim("malformed") is False
    assert spool.claim("symlinked") is False
    assert malformed.read_text() == "not-json"
    assert symlinked.is_symlink()
    assert outside.read_text() == "do-not-touch"
    assert spool.claim_health() == {
        "status": "degraded",
        "unknown_claims": [
            {"claim_id": "malformed", "reason": "malformed_claim"},
            {"claim_id": "symlinked", "reason": "unsafe_claim_path"},
        ],
    }


def test_boolean_claim_version_is_malformed_not_version_one(tmp_path: Path) -> None:
    spool = _spool(tmp_path, _identity("agent"), _FixedLiveness())
    spool.enqueue(_event(), event_id="bad-version")
    claim_path = tmp_path / ".bad-version.claim"
    claim_path.write_text(
        json.dumps(
            {
                "version": True,
                "identity": _identity("other").to_dict(),
                "created_at_ns": 1,
            }
        ),
        encoding="utf-8",
    )

    assert spool.claim("bad-version") is False
    assert claim_path.exists()
    assert spool.claim_health()["unknown_claims"] == [
        {"claim_id": "bad-version", "reason": "malformed_claim"}
    ]


@pytest.mark.parametrize("state", ["alive", "unknown"])
def test_legacy_claim_is_fail_closed_until_owner_is_definitely_dead(
    tmp_path: Path,
    state: str,
) -> None:
    spool = _spool(
        tmp_path,
        _identity("new-agent"),
        _FixedLiveness(legacy_state=state),
    )
    spool.enqueue(_event(), event_id="legacy")
    legacy_claim = tmp_path / ".legacy.claim"
    legacy_claim.write_text("1234 1000000000\n", encoding="ascii")

    assert spool.claim("legacy") is False
    assert legacy_claim.read_text(encoding="ascii") == "1234 1000000000\n"
    assert spool.claim_health() == {
        "status": "degraded",
        "unknown_claims": [
            {
                "claim_id": "legacy",
                "reason": "liveness_unknown" if state == "unknown" else "legacy_claim",
            }
        ],
    }


def test_legacy_claim_migrates_to_versioned_identity_only_when_pid_is_dead(
    tmp_path: Path,
) -> None:
    identity = _identity("new-agent")
    spool = _spool(
        tmp_path,
        identity,
        _FixedLiveness(legacy_state="dead"),
    )
    spool.enqueue(_event(), event_id="legacy-dead")
    claim_path = tmp_path / ".legacy-dead.claim"
    claim_path.write_text("1234 1000000000\n", encoding="ascii")

    assert spool.claim("legacy-dead") is True
    claim = json.loads(claim_path.read_text(encoding="utf-8"))
    assert claim["version"] == 1
    assert claim["identity"] == identity.to_dict()
    assert spool.claim_health()["status"] == "ok"


def test_release_and_ack_require_matching_identity_and_owner(tmp_path: Path) -> None:
    first = _spool(tmp_path, _identity("first"), _FixedLiveness())
    first.enqueue(_event(), event_id="release")
    assert first.claim("release") is True

    second = _spool(tmp_path, _identity("second"), _FixedLiveness())

    assert second.release_claim("release") is False
    assert second.ack("release") is False
    assert (tmp_path / "release.json").exists()
    assert (tmp_path / ".release.claim").exists()
    assert first.release_claim("release") is True
    assert not (tmp_path / ".release.claim").exists()
    assert first.ack("release") is True
    assert not (tmp_path / "release.json").exists()


def test_concurrent_agents_have_exactly_one_claim_winner(tmp_path: Path) -> None:
    seed = _spool(tmp_path, _identity("seed"), _FixedLiveness())
    seed.enqueue(_event(), event_id="race")
    barrier = threading.Barrier(2)

    def compete(owner: str) -> bool:
        contender = _spool(tmp_path, _identity(owner), _FixedLiveness())
        barrier.wait(timeout=2)
        return contender.claim("race")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(compete, ["agent-a", "agent-b"]))

    assert sorted(results) == [False, True]
    claim = json.loads((tmp_path / ".race.claim").read_text())
    assert claim["identity"]["owner_token"] in {"agent-a", "agent-b"}


def test_failed_claim_write_closes_handle_before_windows_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FileClaimStore(
        tmp_path,
        process_identity=_identity("writer"),
        process_liveness=_FixedLiveness(),
    )
    claim_path = store.path_for("failed-write")
    open_claim_fds: set[int] = set()
    real_open = os.open
    real_close = os.close
    real_fsync = os.fsync
    real_unlink = Path.unlink

    def tracked_open(path, flags, mode=0o777):
        fd = real_open(path, flags, mode)
        if Path(path) == claim_path:
            open_claim_fds.add(fd)
        return fd

    def tracked_close(fd: int) -> None:
        try:
            real_close(fd)
        finally:
            open_claim_fds.discard(fd)

    def fail_claim_fsync(fd: int) -> None:
        if fd in open_claim_fds:
            raise OSError("simulated claim fsync failure")
        real_fsync(fd)

    def windows_unlink(self: Path, *args, **kwargs):
        if self == claim_path and open_claim_fds:
            raise PermissionError("Windows denies deletion while the claim handle is open")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(process_liveness_module.os, "open", tracked_open)
    monkeypatch.setattr(process_liveness_module.os, "close", tracked_close)
    monkeypatch.setattr(process_liveness_module.os, "fsync", fail_claim_fsync)
    monkeypatch.setattr(Path, "unlink", windows_unlink)

    with pytest.raises(OSError, match="claim fsync"):
        store._write_claim(claim_path)

    assert claim_path.exists() is False


def test_repair_requires_exact_identity_owner_and_nonempty_reason(tmp_path: Path) -> None:
    owner = _identity("secret-owner", pid=55, started_at=66)
    spool = _spool(tmp_path, owner, _FixedLiveness())
    spool.enqueue(_event(), event_id="repair")
    assert spool.claim("repair") is True
    operator = _spool(tmp_path, _identity("operator"), _FixedLiveness())

    with pytest.raises(ValueError):
        operator.repair_claim(
            "repair",
            expected_identity=owner,
            expected_owner_token="secret-owner",
            reason="   ",
        )
    assert operator.repair_claim(
        "repair",
        expected_identity=_identity("secret-owner", pid=55, started_at=99),
        expected_owner_token="secret-owner",
        reason="verified stale process",
    ) is False
    assert operator.repair_claim(
        "repair",
        expected_identity=owner,
        expected_owner_token="wrong-owner",
        reason="verified stale process",
    ) is False
    assert (tmp_path / ".repair.claim").exists()


def test_repair_durably_audits_before_deleting_without_leaking_token(tmp_path: Path) -> None:
    owner = _identity("never-log-this-token", pid=55, started_at=66)
    spool = _spool(tmp_path, owner, _FixedLiveness())
    spool.enqueue(_event(), event_id="repair")
    assert spool.claim("repair") is True
    operator = _spool(tmp_path, _identity("operator"), _FixedLiveness())

    assert operator.repair_claim(
        "repair",
        expected_identity=owner,
        expected_owner_token=owner.owner_token,
        reason="operator verified process exit",
    ) is True

    assert not (tmp_path / ".repair.claim").exists()
    audit_text = operator.claim_store.audit_path.read_text(encoding="utf-8")
    audit = json.loads(audit_text)
    assert audit == {
        "version": 1,
        "action": "repair",
        "claim_id": "repair",
        "pid": owner.pid,
        "started_at": owner.started_at,
        "reason": "operator verified process exit",
        "timestamp_ns": audit["timestamp_ns"],
    }
    assert "never-log-this-token" not in audit_text


def test_failed_audit_leaves_claim_in_place(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = _identity("owner")
    spool = _spool(tmp_path, owner, _FixedLiveness())
    spool.enqueue(_event(), event_id="audit-first")
    assert spool.claim("audit-first") is True
    operator = _spool(tmp_path, _identity("operator"), _FixedLiveness())

    def fail_audit(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(operator.claim_store, "_append_audit", fail_audit)

    with pytest.raises(OSError, match="disk full"):
        operator.repair_claim(
            "audit-first",
            expected_identity=owner,
            expected_owner_token=owner.owner_token,
            reason="verified stale",
        )
    assert (tmp_path / ".audit-first.claim").exists()


def test_repair_detects_claim_replacement_after_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = _identity("owner")
    spool = _spool(tmp_path, owner, _FixedLiveness())
    spool.enqueue(_event(), event_id="cas")
    assert spool.claim("cas") is True
    operator = _spool(tmp_path, _identity("operator"), _FixedLiveness())
    claim_path = tmp_path / ".cas.claim"
    replacement = _identity("replacement", pid=999, started_at=888)
    original_append = operator.claim_store._append_audit

    def replace_after_audit(*args: object, **kwargs: object) -> None:
        original_append(*args, **kwargs)
        claim_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "identity": replacement.to_dict(),
                    "created_at_ns": 123,
                }
            ),
            encoding="utf-8",
        )

    monkeypatch.setattr(operator.claim_store, "_append_audit", replace_after_audit)

    assert operator.repair_claim(
        "cas",
        expected_identity=owner,
        expected_owner_token=owner.owner_token,
        reason="verified stale",
    ) is False
    assert json.loads(claim_path.read_text())["identity"] == replacement.to_dict()


def test_file_claim_store_can_be_injected_for_command_claim_reuse(tmp_path: Path) -> None:
    identity = _identity("shared-primitive")
    liveness = _FixedLiveness()
    store = FileClaimStore(tmp_path, process_identity=identity, process_liveness=liveness)
    spool = EventSpool(tmp_path, claim_store=store)
    spool.enqueue(_event(), event_id="injected")

    assert spool.claim("injected") is True
    assert spool.process_identity == identity
    assert spool.claim_store is store


def test_windows_installer_has_no_silent_claim_cleanup_path() -> None:
    source = (Path(__file__).resolve().parents[1] / "install_windows.ps1").read_text(
        encoding="utf-8"
    ).lower()

    assert "*.claim" not in source
    assert "remove-item" not in "\n".join(
        line for line in source.splitlines() if "claim" in line
    )


def test_repair_claim_cli_requires_matching_owner_and_writes_audit(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    spool_dir = tmp_path / "spool"
    owner = _identity("repair-owner", pid=4401, started_at=901)
    spool = _spool(spool_dir, owner, _FixedLiveness(default="unknown"))
    spool.enqueue(_event(), event_id="repair-event")
    assert spool.claim("repair-event") is True

    wrong = start_student_agent.main([
        "--spool-dir",
        str(spool_dir),
        "--repair-claim",
        "repair-event",
        "--expected-owner-token",
        "wrong-owner",
        "--reason",
        "operator verified abandoned process",
    ])
    assert wrong != 0
    assert (spool_dir / ".repair-event.claim").exists()

    repaired = start_student_agent.main([
        "--spool-dir",
        str(spool_dir),
        "--repair-claim",
        "repair-event",
        "--expected-owner-token",
        owner.owner_token,
        "--reason",
        "operator verified abandoned process",
    ])
    captured = capsys.readouterr()

    assert repaired == 0
    assert not (spool_dir / ".repair-event.claim").exists()
    assert owner.owner_token not in captured.out
    assert owner.owner_token not in captured.err
    audit = (spool_dir / ".copilot-claim-audit.jsonl").read_text(encoding="utf-8")
    assert "repair-event" in audit
    assert "operator verified abandoned process" in audit
    assert owner.owner_token not in audit


def test_repair_claim_cli_can_audit_a_command_claim(tmp_path: Path) -> None:
    spool_dir = tmp_path / "spool"
    owner = _identity("command-owner", pid=4402, started_at=902)
    spool = _spool(spool_dir, owner, _FixedLiveness(default="unknown"))
    command_dir = spool_dir / ".copilot-upload-commands"
    claim_id = "a" * 64
    command_store = FileClaimStore(
        command_dir,
        process_identity=owner,
        process_liveness=spool.process_liveness,
        claim_path_factory=lambda identifier: command_dir / f"{identifier}.claim",
    )
    assert command_store.acquire(claim_id) is True

    assert start_student_agent.main([
        "--spool-dir",
        str(spool_dir),
        "--repair-claim",
        claim_id,
        "--expected-owner-token",
        owner.owner_token,
        "--reason",
        "operator verified abandoned upload command",
    ]) == 0

    assert not (command_dir / f"{claim_id}.claim").exists()
    audit = (command_dir / ".copilot-claim-audit.jsonl").read_text(encoding="utf-8")
    assert claim_id in audit
    assert owner.owner_token not in audit
