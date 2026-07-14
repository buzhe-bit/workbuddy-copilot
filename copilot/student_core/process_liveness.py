"""Stable process identity and crash-safe file-claim primitives.

The claim layer deliberately distinguishes a process that is definitely gone
from one that merely cannot be inspected.  That distinction is what prevents
Windows permission or probe failures from being misread as permission to steal
work from another student agent.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import sys
import time
from typing import Any, Callable, Iterator, Literal, Mapping, Protocol


LivenessState = Literal["alive", "dead", "unknown", "reused"]
_CLAIM_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,255}$")
_CLAIM_VERSION = 1
# Native process clocks are many orders of magnitude below this bit.  A set
# bit marks a local-only fallback that another process must classify as
# unknown rather than comparing it with a native clock and claiming PID reuse.
_LOCAL_FALLBACK_FLAG = 1 << 127
# Stable for this interpreter process.  It is used only when the host denies
# native inspection of *our own* PID; other PIDs remain ``unknown`` rather
# than being guessed dead or alive.
_SELF_FALLBACK_STARTED_AT = _LOCAL_FALLBACK_FLAG | time.time_ns()


@dataclass(frozen=True)
class ProcessIdentity:
    """A PID plus the stable attributes needed to detect PID reuse."""

    pid: int
    started_at: int
    owner_token: str

    def __post_init__(self) -> None:
        if isinstance(self.pid, bool) or not isinstance(self.pid, int) or self.pid <= 0:
            raise ValueError("pid must be a positive integer")
        if (
            isinstance(self.started_at, bool)
            or not isinstance(self.started_at, int)
            or self.started_at <= 0
        ):
            raise ValueError("started_at must be a positive integer")
        if (
            not isinstance(self.owner_token, str)
            or not self.owner_token
            or self.owner_token != self.owner_token.strip()
            or len(self.owner_token) > 512
            or any(ord(character) < 0x20 for character in self.owner_token)
        ):
            raise ValueError("owner_token must be a non-empty safe string")

    def to_dict(self) -> dict[str, object]:
        return {
            "pid": self.pid,
            "started_at": self.started_at,
            "owner_token": self.owner_token,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ProcessIdentity":
        if not isinstance(data, Mapping) or set(data) != {
            "pid",
            "started_at",
            "owner_token",
        }:
            raise ValueError("invalid process identity")
        return cls(
            pid=data["pid"],
            started_at=data["started_at"],
            owner_token=data["owner_token"],
        )


class ProcessLivenessLike(Protocol):
    def probe(self, identity: ProcessIdentity) -> LivenessState: ...

    def probe_pid(self, pid: int) -> Literal["alive", "dead", "unknown"]: ...


class ProcessLiveness:
    """Compare a recorded start time with the process currently using a PID."""

    def __init__(
        self,
        *,
        started_at_reader: Callable[[int], int] | None = None,
        platform_name: str | None = None,
    ) -> None:
        self.platform_name = platform_name or sys.platform
        self._started_at_reader = started_at_reader or self._default_reader(
            self.platform_name
        )

    @staticmethod
    def _default_reader(platform_name: str) -> Callable[[int], int]:
        if platform_name.lower().startswith("win"):
            return _windows_process_started_at
        if platform_name.lower().startswith("linux"):
            return _linux_process_started_at
        return _ps_process_started_at

    def current_identity(self, *, owner_token: str | None = None) -> ProcessIdentity:
        pid = os.getpid()
        try:
            started_at = self._started_at_reader(pid)
            if (
                isinstance(started_at, bool)
                or not isinstance(started_at, int)
                or started_at <= 0
            ):
                raise ValueError("invalid current process metadata")
        except (OSError, ValueError, TypeError, OverflowError):
            started_at = _SELF_FALLBACK_STARTED_AT
        return ProcessIdentity(
            pid=pid,
            started_at=started_at,
            owner_token=owner_token or secrets.token_urlsafe(24),
        )

    def probe(self, identity: ProcessIdentity) -> LivenessState:
        if not isinstance(identity, ProcessIdentity):
            return "unknown"
        if identity.started_at & _LOCAL_FALLBACK_FLAG:
            if (
                identity.pid == os.getpid()
                and identity.started_at == _SELF_FALLBACK_STARTED_AT
            ):
                return "alive"
            return "unknown"
        try:
            observed_started_at = self._started_at_reader(identity.pid)
        except (ProcessLookupError, FileNotFoundError):
            return "dead"
        except (PermissionError, OSError, ValueError, TypeError, OverflowError):
            return "unknown"
        if (
            isinstance(observed_started_at, bool)
            or not isinstance(observed_started_at, int)
            or observed_started_at <= 0
        ):
            return "unknown"
        if observed_started_at != identity.started_at:
            return "reused"
        return "alive"

    def probe_pid(self, pid: int) -> Literal["alive", "dead", "unknown"]:
        """Legacy existence probe; never guesses reuse without a start time."""

        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            return "unknown"
        try:
            observed_started_at = self._started_at_reader(pid)
        except (ProcessLookupError, FileNotFoundError):
            return "dead"
        except (PermissionError, OSError, ValueError, TypeError, OverflowError):
            return "unknown"
        if (
            isinstance(observed_started_at, bool)
            or not isinstance(observed_started_at, int)
            or observed_started_at <= 0
        ):
            return "unknown"
        return "alive"


def _linux_process_started_at(pid: int) -> int:
    # /proc/<pid>/stat field 22 is the process start time in clock ticks.  The
    # command name may contain spaces or parentheses, so split after its final
    # closing parenthesis rather than splitting the whole line.
    stat_path = Path("/proc") / str(pid) / "stat"
    raw = stat_path.read_text(encoding="ascii")
    try:
        fields_after_name = raw.rsplit(")", 1)[1].split()
        return int(fields_after_name[19])
    except (IndexError, ValueError) as exc:
        raise ValueError("malformed /proc process metadata") from exc


def _ps_process_started_at(pid: int) -> int:
    # Keep subprocess lazy: importing it eagerly loads POSIX-only ``fcntl`` on
    # macOS, which would contaminate the platform-neutral Student Core import
    # graph even on runtimes that only use the Windows backend.
    import subprocess

    try:
        completed = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            check=False,
            capture_output=True,
            text=True,
            timeout=1.0,
            env={**os.environ, "LC_ALL": "C"},
        )
    except subprocess.TimeoutExpired as exc:
        raise OSError("process metadata probe timed out") from exc
    value = completed.stdout.strip()
    if completed.returncode != 0 or not value:
        raise ProcessLookupError(pid)
    try:
        started = datetime.strptime(value, "%a %b %d %H:%M:%S %Y")
    except ValueError as exc:
        raise ValueError("malformed ps process metadata") from exc
    # Local wall-clock seconds are stable for comparison on the same host.
    return int(time.mktime(started.timetuple()) * 1_000_000_000)


def _windows_process_started_at(pid: int) -> int:
    """Read a Windows process creation FILETIME without touching ``os.kill``."""

    if not sys.platform.startswith("win"):
        raise OSError("Windows process API is unavailable")
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    error_access_denied = 5
    error_invalid_parameter = 87
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ]
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        error = ctypes.get_last_error()
        if error == error_invalid_parameter:
            raise ProcessLookupError(pid)
        if error == error_access_denied:
            raise PermissionError(pid)
        raise OSError(error, "OpenProcess failed")
    try:
        creation = wintypes.FILETIME()
        exit_time = wintypes.FILETIME()
        kernel_time = wintypes.FILETIME()
        user_time = wintypes.FILETIME()
        if not kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel_time),
            ctypes.byref(user_time),
        ):
            error = ctypes.get_last_error()
            if error == error_access_denied:
                raise PermissionError(pid)
            raise OSError(error, "GetProcessTimes failed")
        return (int(creation.dwHighDateTime) << 32) | int(creation.dwLowDateTime)
    finally:
        kernel32.CloseHandle(handle)


@dataclass(frozen=True)
class ClaimRecord:
    identity: ProcessIdentity
    created_at_ns: int
    version: int = _CLAIM_VERSION

    def __post_init__(self) -> None:
        if (
            isinstance(self.version, bool)
            or not isinstance(self.version, int)
            or self.version != _CLAIM_VERSION
        ):
            raise ValueError("unsupported claim version")
        if (
            isinstance(self.created_at_ns, bool)
            or not isinstance(self.created_at_ns, int)
            or self.created_at_ns <= 0
        ):
            raise ValueError("invalid claim creation time")

    def to_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "identity": self.identity.to_dict(),
            "created_at_ns": self.created_at_ns,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ClaimRecord":
        if not isinstance(data, Mapping) or set(data) != {
            "version",
            "identity",
            "created_at_ns",
        }:
            raise ValueError("invalid claim record")
        identity = data["identity"]
        if not isinstance(identity, Mapping):
            raise ValueError("invalid claim identity")
        return cls(
            version=data["version"],
            identity=ProcessIdentity.from_dict(identity),
            created_at_ns=data["created_at_ns"],
        )


@dataclass(frozen=True)
class LegacyClaimRecord:
    """The pre-v1 ``pid created_at_ns`` marker, read only for safe migration."""

    pid: int
    created_at_ns: int

    def __post_init__(self) -> None:
        if isinstance(self.pid, bool) or not isinstance(self.pid, int) or self.pid <= 0:
            raise ValueError("invalid legacy claim pid")
        if (
            isinstance(self.created_at_ns, bool)
            or not isinstance(self.created_at_ns, int)
            or self.created_at_ns <= 0
        ):
            raise ValueError("invalid legacy claim creation time")


@dataclass(frozen=True)
class _ObservedClaim:
    record: ClaimRecord | LegacyClaimRecord
    stat: os.stat_result


class FileClaimStore:
    """Versioned JSON claims protected by a crash-released SQLite mutex."""

    def __init__(
        self,
        directory: str | os.PathLike[str],
        *,
        process_identity: ProcessIdentity,
        process_liveness: ProcessLivenessLike,
        claim_path_factory: Callable[[str], Path] | None = None,
    ) -> None:
        self.directory = Path(directory).expanduser()
        if self.directory.is_symlink():
            raise ValueError("claim directory must not be a symlink")
        self.directory.mkdir(parents=True, exist_ok=True)
        if not self.directory.is_dir() or self.directory.is_symlink():
            raise ValueError("claim directory must be a directory")
        if not isinstance(process_identity, ProcessIdentity):
            raise TypeError("process_identity must be a ProcessIdentity")
        if not callable(getattr(process_liveness, "probe", None)):
            raise TypeError("process_liveness must provide probe()")
        self.process_identity = process_identity
        self.process_liveness = process_liveness
        self.audit_path = self.directory / ".copilot-claim-audit.jsonl"
        self._lock_path = self.directory / ".copilot-claim-locks.sqlite3"
        self._claim_path_factory = claim_path_factory or (
            lambda claim_id: self.directory / f".{claim_id}.claim"
        )
        self._health_issues: dict[str, str] = {}

    @staticmethod
    def _validate_claim_id(claim_id: str) -> str:
        if not isinstance(claim_id, str) or not _CLAIM_ID.fullmatch(claim_id):
            raise ValueError("invalid claim_id")
        return claim_id

    def path_for(self, claim_id: str) -> Path:
        identifier = self._validate_claim_id(claim_id)
        path = Path(self._claim_path_factory(identifier))
        try:
            if path.parent.resolve(strict=False) != self.directory.resolve(strict=False):
                raise ValueError("claim path must stay inside claim directory")
        except OSError as exc:
            raise ValueError("invalid claim path") from exc
        return path

    def acquire(self, claim_id: str) -> bool:
        identifier = self._validate_claim_id(claim_id)
        path = self.path_for(identifier)
        with self._exclusive():
            observed, issue = self._observe(path)
            if issue is None and observed is None:
                self._write_claim(path)
                self._clear_health(identifier)
                return True
            if issue is not None:
                self._record_health(identifier, issue)
                return False
            assert observed is not None
            if isinstance(observed.record, LegacyClaimRecord):
                legacy_state = self._safe_probe_pid(observed.record.pid)
                if legacy_state != "dead":
                    self._record_health(
                        identifier,
                        "liveness_unknown" if legacy_state == "unknown" else "legacy_claim",
                    )
                    return False
                if not self._unlink_observed(path, observed):
                    self._record_health(identifier, "claim_changed")
                    return False
                self._write_claim(path)
                self._clear_health(identifier)
                return True
            state = self._safe_probe(observed.record.identity)
            if state in {"alive", "unknown"}:
                if state == "unknown":
                    self._record_health(identifier, "liveness_unknown")
                else:
                    self._clear_health(identifier)
                return False
            if not self._unlink_observed(path, observed):
                self._record_health(identifier, "claim_changed")
                return False
            self._write_claim(path)
            self._clear_health(identifier)
            return True

    def is_active(self, claim_id: str) -> bool:
        identifier = self._validate_claim_id(claim_id)
        path = self.path_for(identifier)
        with self._exclusive():
            observed, issue = self._observe(path)
            if issue is None and observed is None:
                self._clear_health(identifier)
                return False
            if issue is not None:
                self._record_health(identifier, issue)
                return True
            assert observed is not None
            if isinstance(observed.record, LegacyClaimRecord):
                legacy_state = self._safe_probe_pid(observed.record.pid)
                if legacy_state == "dead" and self._unlink_observed(path, observed):
                    self._clear_health(identifier)
                    return False
                self._record_health(
                    identifier,
                    "liveness_unknown" if legacy_state == "unknown" else "legacy_claim",
                )
                return True
            state = self._safe_probe(observed.record.identity)
            if state in {"dead", "reused"}:
                if self._unlink_observed(path, observed):
                    self._clear_health(identifier)
                    return False
                self._record_health(identifier, "claim_changed")
                return True
            if state == "unknown":
                self._record_health(identifier, "liveness_unknown")
            else:
                self._clear_health(identifier)
            return True

    def release(
        self,
        claim_id: str,
        *,
        expected_identity: ProcessIdentity | None = None,
    ) -> bool:
        identifier = self._validate_claim_id(claim_id)
        path = self.path_for(identifier)
        expected = expected_identity or self.process_identity
        with self._exclusive():
            observed, issue = self._observe(path)
            if issue is None and observed is None:
                self._clear_health(identifier)
                return False
            if issue is not None:
                self._record_health(identifier, issue)
                return False
            assert observed is not None
            if (
                not isinstance(observed.record, ClaimRecord)
                or observed.record.identity != expected
            ):
                if isinstance(observed.record, LegacyClaimRecord):
                    self._record_health(identifier, "legacy_claim")
                return False
            removed = self._unlink_observed(path, observed)
            if removed:
                self._clear_health(identifier)
            return removed

    def complete(
        self,
        claim_id: str,
        action: Callable[[], None],
        *,
        expected_identity: ProcessIdentity | None = None,
        allow_unclaimed: bool = False,
    ) -> bool:
        """Run ``action`` and remove only an unmodified claim owned by caller."""

        identifier = self._validate_claim_id(claim_id)
        path = self.path_for(identifier)
        expected = expected_identity or self.process_identity
        with self._exclusive():
            observed, issue = self._observe(path)
            if issue is None and observed is None:
                if not allow_unclaimed:
                    return False
                action()
                self._clear_health(identifier)
                return True
            if issue is not None:
                self._record_health(identifier, issue)
                return False
            assert observed is not None
            if (
                not isinstance(observed.record, ClaimRecord)
                or observed.record.identity != expected
                or not self._still_matches(path, observed)
            ):
                if isinstance(observed.record, LegacyClaimRecord):
                    self._record_health(identifier, "legacy_claim")
                return False
            action()
            removed = self._unlink_observed(path, observed)
            if removed:
                self._clear_health(identifier)
            return removed

    def repair(
        self,
        claim_id: str,
        *,
        expected_identity: ProcessIdentity,
        expected_owner_token: str,
        reason: str,
    ) -> bool:
        identifier = self._validate_claim_id(claim_id)
        if not isinstance(expected_identity, ProcessIdentity):
            raise TypeError("expected_identity must be a ProcessIdentity")
        if not isinstance(expected_owner_token, str):
            raise TypeError("expected_owner_token must be a string")
        clean_reason = str(reason or "").strip()
        if not clean_reason:
            raise ValueError("repair reason is required")
        if len(clean_reason) > 512:
            raise ValueError("repair reason is too long")
        if not secrets.compare_digest(
            expected_identity.owner_token,
            expected_owner_token,
        ):
            return False
        path = self.path_for(identifier)
        with self._exclusive():
            observed, issue = self._observe(path)
            if issue is not None:
                self._record_health(identifier, issue)
                return False
            if (
                observed is None
                or not isinstance(observed.record, ClaimRecord)
                or observed.record.identity != expected_identity
            ):
                if observed is not None and isinstance(
                    observed.record, LegacyClaimRecord
                ):
                    self._record_health(identifier, "legacy_claim")
                return False
            self._append_audit(
                claim_id=identifier,
                identity=expected_identity,
                reason=clean_reason,
            )
            # Audit callbacks and external processes may race with repair.  A
            # second content+inode comparison ensures we never delete their
            # replacement after the durable audit write.
            if not self._still_matches(path, observed):
                self._record_health(identifier, "claim_changed")
                return False
            removed = self._unlink_observed(path, observed)
            if removed:
                self._clear_health(identifier)
            return removed

    def identity_for_repair(self, claim_id: str) -> ProcessIdentity | None:
        """Read a versioned claim identity for an explicit audited repair.

        The subsequent :meth:`repair` call still re-reads and compares the
        complete record under the exclusive gate, so a replacement between
        these two calls cannot be deleted.
        """

        identifier = self._validate_claim_id(claim_id)
        path = self.path_for(identifier)
        with self._exclusive():
            observed, issue = self._observe(path)
            if issue is not None:
                self._record_health(identifier, issue)
                return None
            if observed is None:
                self._clear_health(identifier)
                return None
            if not isinstance(observed.record, ClaimRecord):
                self._record_health(identifier, "legacy_claim")
                return None
            return observed.record.identity

    def health(self) -> dict[str, object]:
        issues = [
            {"claim_id": claim_id, "reason": self._health_issues[claim_id]}
            for claim_id in sorted(self._health_issues)
        ]
        return {
            "status": "degraded" if issues else "ok",
            "unknown_claims": issues,
        }

    @contextmanager
    def _exclusive(self) -> Iterator[None]:
        if self._lock_path.is_symlink():
            raise ValueError("claim lock must not be a symlink")
        connection = sqlite3.connect(self._lock_path, timeout=5.0, isolation_level=None)
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS claim_mutex (singleton INTEGER PRIMARY KEY)"
            )
            yield
            connection.commit()
        except BaseException:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
            raise
        finally:
            connection.close()

    def _observe(self, path: Path) -> tuple[_ObservedClaim | None, str | None]:
        if path.is_symlink():
            return None, "unsafe_claim_path"
        try:
            stat = path.lstat()
        except FileNotFoundError:
            return None, None
        except OSError:
            return None, "unreadable_claim"
        if not path.is_file():
            return None, "unsafe_claim_path"
        try:
            raw = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return None, "unreadable_claim"
        try:
            decoded = json.loads(raw)
            if not isinstance(decoded, Mapping):
                raise ValueError("claim must be an object")
            record: ClaimRecord | LegacyClaimRecord = ClaimRecord.from_dict(decoded)
        except (json.JSONDecodeError, ValueError, TypeError):
            try:
                parts = raw.split()
                if len(parts) != 2:
                    raise ValueError("not a legacy claim")
                record = LegacyClaimRecord(pid=int(parts[0]), created_at_ns=int(parts[1]))
            except (ValueError, TypeError, IndexError):
                return None, "malformed_claim"
        return _ObservedClaim(record=record, stat=stat), None

    def _safe_probe(self, identity: ProcessIdentity) -> LivenessState:
        try:
            state = self.process_liveness.probe(identity)
        except BaseException:
            return "unknown"
        if state not in {"alive", "dead", "unknown", "reused"}:
            return "unknown"
        return state

    def _safe_probe_pid(self, pid: int) -> Literal["alive", "dead", "unknown"]:
        probe_pid = getattr(self.process_liveness, "probe_pid", None)
        if not callable(probe_pid):
            return "unknown"
        try:
            state = probe_pid(pid)
        except BaseException:
            return "unknown"
        if state not in {"alive", "dead", "unknown"}:
            return "unknown"
        return state

    def _write_claim(self, path: Path) -> None:
        record = ClaimRecord(
            identity=self.process_identity,
            created_at_ns=time.time_ns(),
        )
        payload = json.dumps(
            record.to_dict(),
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("ascii")
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        created_stat = os.fstat(fd)
        try:
            _write_all(fd, payload)
            os.fsync(fd)
        except BaseException:
            try:
                if os.path.samestat(created_stat, path.lstat()):
                    path.unlink()
            except OSError:
                pass
            raise
        finally:
            os.close(fd)
        _fsync_directory(self.directory)

    def _still_matches(self, path: Path, observed: _ObservedClaim) -> bool:
        current, issue = self._observe(path)
        if issue is not None or current is None or current.record != observed.record:
            return False
        try:
            return os.path.samestat(observed.stat, current.stat)
        except OSError:
            return False

    def _unlink_observed(self, path: Path, observed: _ObservedClaim) -> bool:
        if not self._still_matches(path, observed):
            return False
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        _fsync_directory(self.directory)
        return True

    def _append_audit(
        self,
        *,
        claim_id: str,
        identity: ProcessIdentity,
        reason: str,
    ) -> None:
        if self.audit_path.is_symlink():
            raise ValueError("claim audit must not be a symlink")
        payload = json.dumps(
            {
                "version": 1,
                "action": "repair",
                "claim_id": claim_id,
                "pid": identity.pid,
                "started_at": identity.started_at,
                "reason": reason,
                "timestamp_ns": time.time_ns(),
            },
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("ascii") + b"\n"
        fd = os.open(
            self.audit_path,
            os.O_CREAT | os.O_APPEND | os.O_WRONLY,
            0o600,
        )
        try:
            _write_all(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        _fsync_directory(self.directory)

    def _record_health(self, claim_id: str, reason: str) -> None:
        self._health_issues[claim_id] = reason

    def _clear_health(self, claim_id: str) -> None:
        self._health_issues.pop(claim_id, None)


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write")
        view = view[written:]


def _fsync_directory(directory: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(directory, flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
