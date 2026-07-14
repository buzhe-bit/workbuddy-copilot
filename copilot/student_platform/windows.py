"""Windows-owned WorkBuddy discovery with an explicit W0 evidence boundary.

Nothing in this module infers WorkBuddy's private project-directory encoding or
an active session.  The shared ``WorkBuddyDataAdapter`` can read an explicit
config directory; this module only selects from the documented Windows roots
and refuses rollout claims until a real-machine W0 manifest is present.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
import ntpath
import os
from pathlib import Path, PureWindowsPath
import stat
from typing import Literal, Protocol

from .workbuddy import (
    AdapterFailure,
    MAX_TRANSCRIPT_BYTES,
    MAX_TRANSCRIPT_CANDIDATES,
    ProbeResult,
    TranscriptCandidate,
    TranscriptIndexResult,
    TranscriptReadResult,
    TranscriptSnapshot,
    WorkBuddyDataAdapter,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MANIFEST_PATH = (
    PROJECT_ROOT / "tests" / "fixtures" / "workbuddy" / "windows" / "manifest.json"
)
@dataclass(frozen=True)
class WindowsConfigDirCandidate:
    """One known config root selected without expanding an unknown path."""

    path: Path | None
    source: str | None
    exists: bool = False


@dataclass(frozen=True)
class WindowsProbeResult:
    """Typed W0 outcome; ``ready`` still is not a Windows rollout approval."""

    status: Literal["ready", "blocked"]
    message: str
    config: WindowsConfigDirCandidate
    manifest_path: Path
    rollout_ready: bool = False

    @property
    def verdict(self) -> str:
        """Human-readable W0 gate suitable for logs and operator output."""
        return f"{self.status.upper()}: {self.message}"


@dataclass(frozen=True)
class WindowsWorkBuddyProfile:
    """Versioned, explicit mapping observed at the W0 evidence boundary."""

    schema_version: int
    evidence_level: str
    synthetic: bool
    workbuddy_version: str
    config_dir: Path
    hook_command: str
    database_relative_path: str
    projects_relative_path: str
    session_metadata_keys: tuple[str, ...]
    manifest_path: Path

    def mapped_path(self, relative_path: str) -> Path:
        """Join a previously validated Windows-relative mapping on this host."""
        return self.config_dir.joinpath(*PureWindowsPath(relative_path).parts)


@dataclass(frozen=True)
class WindowsProfileLoadResult:
    """Typed profile outcome; blocked inputs never become a partial profile."""

    profile: WindowsWorkBuddyProfile | None = None
    failure: AdapterFailure | None = None

    @property
    def ready(self) -> bool:
        return self.profile is not None and self.failure is None


def _safe_mapping_path(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    candidate = PureWindowsPath(value.strip())
    if candidate.is_absolute() or candidate.drive or candidate.root:
        return None
    parts = candidate.parts
    if not parts or any(
        part in {"", ".", ".."}
        or part.rstrip(" .") != part
        or ":" in part
        for part in parts
    ):
        return None
    return "/".join(parts)


def _looks_like_windows_absolute_path(value: str) -> bool:
    candidate = PureWindowsPath(value)
    return bool(candidate.drive or candidate.root or value.startswith("\\\\"))


def _normalise_profile_config(value: str | os.PathLike[str]) -> str:
    text = os.fspath(value)
    if _looks_like_windows_absolute_path(text):
        return ntpath.normcase(ntpath.normpath(ntpath.expandvars(text)))
    return os.path.normcase(str(Path(text).expanduser().resolve(strict=False)))


def _profile_matches_config(profile_path: Path, config_dir: Path) -> bool:
    return _normalise_profile_config(profile_path) == _normalise_profile_config(config_dir)


def load_windows_workbuddy_profile(
    manifest_path: str | os.PathLike[str],
    *,
    config_dir: str | os.PathLike[str],
    allow_synthetic_fixture: bool = False,
) -> WindowsProfileLoadResult:
    """Load a strict W0 profile, or an explicitly requested synthetic fixture.

    ``allow_synthetic_fixture`` is intentionally opt-in and is never used by
    ``WindowsWorkBuddyData``.  It exists so parser/scanner tests can use a
    visibly synthetic fixture without relabelling it as real-machine evidence.
    """

    path = Path(manifest_path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return WindowsProfileLoadResult(
            failure=AdapterFailure(
                "windows_profile_required", "an explicit Windows W0 profile is required"
            )
        )
    except (OSError, json.JSONDecodeError):
        return WindowsProfileLoadResult(
            failure=AdapterFailure(
                "windows_profile_invalid", "the Windows WorkBuddy profile is invalid"
            )
        )
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        return WindowsProfileLoadResult(
            failure=AdapterFailure(
                "windows_profile_invalid", "the Windows WorkBuddy profile schema is unsupported"
            )
        )

    synthetic = raw.get("synthetic")
    evidence_level = raw.get("evidence_level")
    if not isinstance(synthetic, bool) or not isinstance(evidence_level, str):
        return WindowsProfileLoadResult(
            failure=AdapterFailure(
                "windows_profile_invalid", "profile evidence metadata is invalid"
            )
        )
    normalized_evidence = evidence_level.strip()
    if synthetic:
        if normalized_evidence.casefold() != "synthetic":
            return WindowsProfileLoadResult(
                failure=AdapterFailure(
                    "windows_profile_invalid", "synthetic input cannot claim W0 evidence"
                )
            )
        if not allow_synthetic_fixture:
            return WindowsProfileLoadResult(
                failure=AdapterFailure(
                    "windows_profile_synthetic",
                    "synthetic WorkBuddy data is blocked in production",
                )
            )
    elif normalized_evidence != "W0":
        return WindowsProfileLoadResult(
            failure=AdapterFailure(
                "windows_profile_not_w0", "a non-synthetic W0 profile is required"
            )
        )

    workbuddy_version = raw.get("workbuddy_version")
    manifest_config = raw.get("config_dir")
    hook_command = raw.get("hook_command")
    mapping = raw.get("transcript_mapping")
    if not all(
        isinstance(value, str) and value.strip()
        for value in (workbuddy_version, manifest_config, hook_command)
    ) or not isinstance(mapping, dict):
        return WindowsProfileLoadResult(
            failure=AdapterFailure(
                "windows_profile_invalid", "required Windows profile fields are missing"
            )
        )

    database_relative_path = _safe_mapping_path(mapping.get("database_relative_path"))
    projects_relative_path = _safe_mapping_path(mapping.get("projects_relative_path"))
    session_keys = mapping.get("session_metadata_keys")
    if (
        database_relative_path is None
        or projects_relative_path is None
        or not isinstance(session_keys, list)
        or tuple(session_keys) != ("session_id", "sessionId")
    ):
        return WindowsProfileLoadResult(
            failure=AdapterFailure(
                "windows_profile_invalid", "transcript mapping is invalid or escapes config root"
            )
        )

    explicit_config = Path(config_dir).expanduser()
    if not synthetic and not _profile_matches_config(Path(manifest_config), explicit_config):
        return WindowsProfileLoadResult(
            failure=AdapterFailure(
                "windows_profile_mismatch", "profile and configured WorkBuddy roots differ"
            )
        )
    return WindowsProfileLoadResult(
        profile=WindowsWorkBuddyProfile(
            schema_version=1,
            evidence_level=normalized_evidence,
            synthetic=synthetic,
            workbuddy_version=workbuddy_version.strip(),
            config_dir=explicit_config,
            hook_command=hook_command.strip(),
            database_relative_path=database_relative_path,
            projects_relative_path=projects_relative_path,
            session_metadata_keys=tuple(session_keys),
            manifest_path=path,
        )
    )


def windows_extended_path(path: str | os.PathLike[str]) -> str:
    """Return the Win32 extended spelling without changing UNC semantics."""
    value = os.fspath(path)
    if value.startswith("\\\\?\\"):
        return value
    if value.startswith("\\\\"):
        return "\\\\?\\UNC\\" + value[2:]
    candidate = PureWindowsPath(value)
    if candidate.drive and candidate.root:
        return "\\\\?\\" + value
    return value


@dataclass(frozen=True)
class WindowsTranscriptEntry:
    """Filesystem-neutral entry used by the injectable Windows backend."""

    name: str
    path: Path
    is_directory: bool
    is_file: bool
    is_reparse_point: bool


class WindowsTranscriptBackend(Protocol):
    def is_directory(self, path: Path) -> bool: ...

    def is_reparse_point(self, path: Path) -> bool: ...

    def iter_entries(self, directory: Path) -> tuple[WindowsTranscriptEntry, ...]: ...

    def read_bytes(self, path: Path, limit: int) -> bytes: ...


class _WindowsUnsafePathError(OSError):
    """A handle resolved outside the pinned projects root."""


class _WindowsReparsePointError(OSError):
    """A path became a reparse point before its handle was opened."""


class LocalWindowsTranscriptBackend:
    """Local backend using Win32 extended paths and reparse attributes on NT."""

    FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    _FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    _FILE_FLAG_SEQUENTIAL_SCAN = 0x08000000
    _FILE_ATTRIBUTE_DIRECTORY = 0x10
    _FILE_ATTRIBUTE_REPARSE_POINT = 0x400
    _FILE_LIST_DIRECTORY = 0x0001
    _FILE_READ_ATTRIBUTES = 0x0080
    _GENERIC_READ = 0x80000000
    _FILE_SHARE_READ = 0x00000001
    _FILE_SHARE_WRITE = 0x00000002
    _OPEN_EXISTING = 3

    @staticmethod
    def _os_path(path: Path) -> str:
        return windows_extended_path(path) if os.name == "nt" else os.fspath(path)

    def is_directory(self, path: Path) -> bool:
        opened = os.stat(self._os_path(path), follow_symlinks=False)
        return stat.S_ISDIR(opened.st_mode)

    def is_reparse_point(self, path: Path) -> bool:
        opened = os.lstat(self._os_path(path))
        attributes = int(getattr(opened, "st_file_attributes", 0))
        reparse_attribute = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
        return bool(attributes & reparse_attribute)

    def iter_entries(self, directory: Path) -> tuple[WindowsTranscriptEntry, ...]:
        entries: list[WindowsTranscriptEntry] = []
        with os.scandir(self._os_path(directory)) as handle:
            for entry in handle:
                opened = entry.stat(follow_symlinks=False)
                attributes = int(getattr(opened, "st_file_attributes", 0))
                reparse_attribute = int(
                    getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
                )
                entries.append(
                    WindowsTranscriptEntry(
                        name=entry.name,
                        path=directory / entry.name,
                        is_directory=entry.is_dir(follow_symlinks=False),
                        is_file=entry.is_file(follow_symlinks=False),
                        is_reparse_point=bool(attributes & reparse_attribute),
                    )
                )
        return tuple(sorted(entries, key=lambda item: item.name.casefold()))

    def read_bytes(self, path: Path, limit: int) -> bytes:
        with open(self._os_path(path), "rb") as handle:
            return handle.read(limit + 1)

    def iter_entries_secure(
        self,
        root: Path,
        directory: Path,
    ) -> tuple[WindowsTranscriptEntry, ...]:
        """Enumerate while a non-replaceable, verified directory handle is held."""
        if os.name != "nt" or type(self) is not LocalWindowsTranscriptBackend:
            if self.is_reparse_point(directory):
                return ()
            if not _path_is_within(root, directory):
                raise _WindowsUnsafePathError("directory escaped projects root")
            return self.iter_entries(directory)

        kernel32, root_handle, directory_handle = self._open_verified_handles(
            root,
            directory,
            directory=True,
        )
        try:
            # FILE_SHARE_DELETE is deliberately absent from both handles, so
            # the directory cannot be renamed/replaced between validation and
            # the path-based Win32 enumeration performed by os.scandir.
            return self.iter_entries(directory)
        finally:
            kernel32.CloseHandle(directory_handle)
            kernel32.CloseHandle(root_handle)

    def read_bytes_secure(self, root: Path, path: Path, limit: int) -> bytes:
        """Read from the exact verified Win32 handle, never by reopening a path."""
        if os.name != "nt" or type(self) is not LocalWindowsTranscriptBackend:
            if self.is_reparse_point(path):
                raise _WindowsReparsePointError("file became a reparse point")
            if not _path_is_within(root, path):
                raise _WindowsUnsafePathError("file escaped projects root")
            return self.read_bytes(path, limit)

        import ctypes
        from ctypes import wintypes

        kernel32, root_handle, file_handle = self._open_verified_handles(
            root,
            path,
            directory=False,
        )
        try:
            remaining = max(0, int(limit)) + 1
            chunks: list[bytes] = []
            while remaining > 0:
                chunk_size = min(remaining, 64 * 1024)
                buffer = ctypes.create_string_buffer(chunk_size)
                observed = wintypes.DWORD(0)
                if not kernel32.ReadFile(
                    file_handle,
                    buffer,
                    chunk_size,
                    ctypes.byref(observed),
                    None,
                ):
                    raise ctypes.WinError(ctypes.get_last_error())
                if observed.value == 0:
                    break
                chunks.append(buffer.raw[: observed.value])
                remaining -= observed.value
            return b"".join(chunks)
        finally:
            kernel32.CloseHandle(file_handle)
            kernel32.CloseHandle(root_handle)

    @staticmethod
    def _normalized_final_windows_path(value: str) -> str:
        if value.startswith("\\\\?\\UNC\\"):
            value = "\\\\" + value[8:]
        elif value.startswith("\\\\?\\"):
            value = value[4:]
        return ntpath.normcase(ntpath.normpath(value))

    @classmethod
    def _final_path_is_within(cls, root: str, candidate: str) -> bool:
        root_path = cls._normalized_final_windows_path(root)
        candidate_path = cls._normalized_final_windows_path(candidate)
        try:
            return ntpath.commonpath((root_path, candidate_path)) == root_path
        except ValueError:
            return False

    @classmethod
    def _open_verified_handles(
        cls,
        root: Path,
        target: Path,
        *,
        directory: bool,
    ):
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.argtypes = (
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        )
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.GetFinalPathNameByHandleW.argtypes = (
            wintypes.HANDLE,
            wintypes.LPWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
        )
        kernel32.GetFinalPathNameByHandleW.restype = wintypes.DWORD
        kernel32.ReadFile.argtypes = (
            wintypes.HANDLE,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.c_void_p,
        )
        kernel32.ReadFile.restype = wintypes.BOOL

        class ByHandleFileInformation(ctypes.Structure):
            _fields_ = (
                ("dwFileAttributes", wintypes.DWORD),
                ("ftCreationTime", wintypes.FILETIME),
                ("ftLastAccessTime", wintypes.FILETIME),
                ("ftLastWriteTime", wintypes.FILETIME),
                ("dwVolumeSerialNumber", wintypes.DWORD),
                ("nFileSizeHigh", wintypes.DWORD),
                ("nFileSizeLow", wintypes.DWORD),
                ("nNumberOfLinks", wintypes.DWORD),
                ("nFileIndexHigh", wintypes.DWORD),
                ("nFileIndexLow", wintypes.DWORD),
            )

        kernel32.GetFileInformationByHandle.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(ByHandleFileInformation),
        )
        kernel32.GetFileInformationByHandle.restype = wintypes.BOOL
        invalid_handle = ctypes.c_void_p(-1).value

        def open_handle(path: Path, *, is_directory: bool):
            desired_access = (
                cls._FILE_LIST_DIRECTORY | cls._FILE_READ_ATTRIBUTES
                if is_directory
                else cls._GENERIC_READ
            )
            flags = cls.FILE_FLAG_OPEN_REPARSE_POINT
            flags |= (
                cls._FILE_FLAG_BACKUP_SEMANTICS
                if is_directory
                else cls._FILE_FLAG_SEQUENTIAL_SCAN
            )
            handle = kernel32.CreateFileW(
                windows_extended_path(path),
                desired_access,
                cls._FILE_SHARE_READ | cls._FILE_SHARE_WRITE,
                None,
                cls._OPEN_EXISTING,
                flags,
                None,
            )
            if handle in (None, 0, invalid_handle):
                raise ctypes.WinError(ctypes.get_last_error())
            return handle

        def handle_info(handle):
            info = ByHandleFileInformation()
            if not kernel32.GetFileInformationByHandle(handle, ctypes.byref(info)):
                raise ctypes.WinError(ctypes.get_last_error())
            size = 32768
            buffer = ctypes.create_unicode_buffer(size)
            length = kernel32.GetFinalPathNameByHandleW(handle, buffer, size, 0)
            if length == 0:
                raise ctypes.WinError(ctypes.get_last_error())
            if length >= size:
                size = int(length) + 1
                buffer = ctypes.create_unicode_buffer(size)
                length = kernel32.GetFinalPathNameByHandleW(handle, buffer, size, 0)
                if length == 0 or length >= size:
                    raise ctypes.WinError(ctypes.get_last_error())
            return int(info.dwFileAttributes), buffer.value

        root_handle = open_handle(root, is_directory=True)
        target_handle = None
        try:
            root_attributes, root_final = handle_info(root_handle)
            if root_attributes & cls._FILE_ATTRIBUTE_REPARSE_POINT:
                raise _WindowsUnsafePathError("projects root is a reparse point")
            if not root_attributes & cls._FILE_ATTRIBUTE_DIRECTORY:
                raise _WindowsUnsafePathError("projects root is not a directory")

            target_handle = open_handle(target, is_directory=directory)
            target_attributes, target_final = handle_info(target_handle)
            if target_attributes & cls._FILE_ATTRIBUTE_REPARSE_POINT:
                raise _WindowsReparsePointError("target is a reparse point")
            observed_directory = bool(
                target_attributes & cls._FILE_ATTRIBUTE_DIRECTORY
            )
            if observed_directory != directory:
                raise _WindowsUnsafePathError("target type changed while opening")
            if not cls._final_path_is_within(root_final, target_final):
                raise _WindowsUnsafePathError("target escaped projects root")
            return kernel32, root_handle, target_handle
        except BaseException:
            if target_handle not in (None, 0, invalid_handle):
                kernel32.CloseHandle(target_handle)
            kernel32.CloseHandle(root_handle)
            raise

def _path_is_within(root: Path, candidate: Path) -> bool:
    try:
        root_text = os.path.normcase(os.path.abspath(os.fspath(root)))
        candidate_text = os.path.normcase(os.path.abspath(os.fspath(candidate)))
        return os.path.commonpath((root_text, candidate_text)) == root_text
    except (OSError, ValueError):
        return False


class WindowsTranscriptScanner:
    """Bounded Windows traversal that never follows reparse points."""

    BUSY_WINERRORS = frozenset({32, 33})

    def __init__(self, *, backend: WindowsTranscriptBackend | None = None) -> None:
        self.backend = backend or LocalWindowsTranscriptBackend()

    def index(self, adapter: WorkBuddyDataAdapter) -> TranscriptIndexResult:
        root = adapter.projects_dir
        try:
            if self.backend.is_reparse_point(root):
                return TranscriptIndexResult(
                    root=root,
                    failure=AdapterFailure(
                        "transcript_index_incomplete",
                        "Windows projects root must not be a reparse point",
                    ),
                )
            if not self.backend.is_directory(root):
                return TranscriptIndexResult(cache_success=False)
        except FileNotFoundError:
            return TranscriptIndexResult(cache_success=False)
        except BaseException as exc:
            return self._exception_result(root, exc)

        index: dict[str, list[TranscriptCandidate]] = {}
        candidates = 0
        bytes_read = 0
        pending: list[tuple[Path, Path]] = [(root, Path())]
        while pending:
            directory, relative_directory = pending.pop()
            try:
                if relative_directory.parts and self.backend.is_reparse_point(directory):
                    continue
                if not self.backend.is_directory(directory):
                    return TranscriptIndexResult(
                        root=root,
                        failure=AdapterFailure(
                            "transcript_index_incomplete",
                            "Windows transcript directory changed while indexing",
                        ),
                        cache_failure=False,
                    )
                secure_iter = getattr(self.backend, "iter_entries_secure", None)
                entries = (
                    secure_iter(root, directory)
                    if callable(secure_iter)
                    else self.backend.iter_entries(directory)
                )
            except _WindowsReparsePointError:
                continue
            except BaseException as exc:
                return self._exception_result(root, exc)
            for entry in entries:
                if not _path_is_within(root, entry.path):
                    return TranscriptIndexResult(
                        root=root,
                        failure=AdapterFailure(
                            "transcript_index_incomplete",
                            "Windows transcript entry escaped the configured projects root",
                        ),
                    )
                if entry.is_reparse_point:
                    continue
                if entry.is_directory:
                    pending.append(
                        (entry.path, relative_directory / entry.name)
                    )
                    continue
                if not entry.is_file or not entry.name.lower().endswith(".jsonl"):
                    continue
                candidates += 1
                if candidates > adapter.max_transcript_candidates:
                    return TranscriptIndexResult(
                        root=root,
                        failure=AdapterFailure(
                            "transcript_index_incomplete",
                            "transcript candidate limit exceeded",
                        ),
                    )
                remaining = adapter.max_transcript_bytes - bytes_read
                if remaining < 0:
                    return TranscriptIndexResult(
                        root=root,
                        failure=AdapterFailure(
                            "transcript_index_incomplete", "transcript byte limit exceeded"
                        ),
                    )
                try:
                    if self.backend.is_reparse_point(entry.path):
                        continue
                    secure_read = getattr(self.backend, "read_bytes_secure", None)
                    data = (
                        secure_read(root, entry.path, remaining)
                        if callable(secure_read)
                        else self.backend.read_bytes(entry.path, remaining)
                    )
                except _WindowsReparsePointError:
                    continue
                except BaseException as exc:
                    return self._exception_result(root, exc)
                if len(data) > remaining:
                    return TranscriptIndexResult(
                        root=root,
                        failure=AdapterFailure(
                            "transcript_index_incomplete", "transcript byte limit exceeded"
                        ),
                    )
                bytes_read += len(data)
                session_ids, failure = adapter._session_ids_from_jsonl(data)
                if failure is not None:
                    # WorkBuddy can expose the final, not-yet-complete JSONL
                    # line while it is appending.  A resident client must
                    # retry the next Stop instead of poisoning the adapter
                    # until process restart.
                    return TranscriptIndexResult(
                        root=root,
                        failure=failure,
                        cache_failure=False,
                    )
                candidate = TranscriptCandidate(
                    relative_path=relative_directory / entry.name,
                    device=0,
                    inode=0,
                    content=data,
                )
                for session_id in session_ids:
                    index.setdefault(session_id, []).append(candidate)

        return TranscriptIndexResult(
            index={
                session_id: tuple(
                    sorted(items, key=lambda item: item.relative_path.as_posix())
                )
                for session_id, items in index.items()
            },
            root=root,
            # A resident Windows client must observe transcript growth and
            # sessions created after startup. Each scanner pass remains an
            # immutable, verified snapshot, but it is never reused by a later
            # explicit read.
            cache_success=False,
        )

    @staticmethod
    def read(candidate: TranscriptCandidate) -> TranscriptReadResult:
        return TranscriptReadResult(
            content=candidate.content.decode("utf-8", errors="replace")
        )

    def _exception_result(self, root: Path, exc: BaseException) -> TranscriptIndexResult:
        if isinstance(exc, _WindowsUnsafePathError):
            return TranscriptIndexResult(
                root=root,
                failure=AdapterFailure(
                    "transcript_index_incomplete",
                    "Windows transcript handle escaped the configured projects root",
                ),
                cache_failure=False,
            )
        if getattr(exc, "winerror", None) in self.BUSY_WINERRORS:
            return TranscriptIndexResult(
                root=root,
                failure=AdapterFailure(
                    "busy", "WorkBuddy transcript is temporarily locked"
                ),
                cache_failure=False,
            )
        if isinstance(exc, PermissionError):
            return TranscriptIndexResult(
                root=root,
                failure=AdapterFailure(
                    "permission_denied", "WorkBuddy transcripts cannot be read"
                ),
            )
        if isinstance(exc, FileNotFoundError):
            return TranscriptIndexResult(
                root=root,
                failure=AdapterFailure(
                    "transcript_index_incomplete",
                    "Windows transcript changed while indexing",
                ),
                cache_failure=False,
            )
        return TranscriptIndexResult(
            root=root,
            failure=AdapterFailure(
                "temporarily_unavailable", "Windows transcripts are temporarily unavailable"
            ),
            cache_failure=False,
        )


def _is_existing_directory(path: Path) -> bool:
    try:
        return path.is_dir()
    except OSError:
        return False


def _existing_programdata_roots(programdata: str | None) -> list[Path]:
    if not programdata:
        return []
    users_root = Path(programdata) / "WorkBuddy" / "users"
    if not _is_existing_directory(users_root):
        return []
    try:
        user_roots = sorted(users_root.iterdir(), key=lambda item: item.name.casefold())
    except OSError:
        return []
    return [
        candidate / ".workbuddy"
        for candidate in user_roots
        if _is_existing_directory(candidate / ".workbuddy")
    ]


def _existing_workbuddy_env_roots(system_drive: str | None) -> list[Path]:
    """Enumerate, but never fabricate, WorkBuddy's documented fallback root."""

    if not system_drive:
        return []
    users_root = Path(system_drive) / "WorkBuddy-env"
    if not _is_existing_directory(users_root):
        return []
    try:
        user_roots = sorted(users_root.iterdir(), key=lambda item: item.name.casefold())
    except OSError:
        return []
    return [
        candidate / ".workbuddy"
        for candidate in user_roots
        if _is_existing_directory(candidate / ".workbuddy")
    ]


def probe_windows_config_dir(
    environ: Mapping[str, str] | None = None,
) -> WindowsConfigDirCandidate:
    """Return only an explicit or existing official WorkBuddy config root.

    ``WORKBUDDY_CONFIG_DIR`` is authoritative even before the target directory
    exists, because a managed installer can deliberately create that explicit
    location.  All fallback candidates must already exist; the probe never
    manufactures a path from a guessed username, installation location, or
    project-directory encoding.
    """

    env = os.environ if environ is None else environ
    explicit = env.get("WORKBUDDY_CONFIG_DIR")
    if explicit:
        path = Path(explicit)
        return WindowsConfigDirCandidate(
            path=path,
            source="WORKBUDDY_CONFIG_DIR",
            exists=_is_existing_directory(path),
        )

    userprofile = env.get("USERPROFILE")
    if userprofile:
        default_root = Path(userprofile) / ".workbuddy"
        if _is_existing_directory(default_root):
            return WindowsConfigDirCandidate(
                path=default_root,
                source="USERPROFILE/.workbuddy",
                exists=True,
            )

    roots = _existing_programdata_roots(env.get("PROGRAMDATA") or env.get("ProgramData"))
    if roots:
        return WindowsConfigDirCandidate(
            path=roots[0], source="ProgramData/WorkBuddy/users", exists=True
        )
    roots = _existing_workbuddy_env_roots(env.get("SystemDrive") or env.get("SYSTEMDRIVE"))
    if roots:
        return WindowsConfigDirCandidate(
            path=roots[0], source="SystemDrive/WorkBuddy-env", exists=True
        )
    return WindowsConfigDirCandidate(path=None, source=None, exists=False)


class WindowsWorkBuddyProbe:
    """Gate the Windows adapter on real-machine W0 evidence, not assumptions."""

    def __init__(
        self,
        *,
        manifest_path: str | os.PathLike[str] = DEFAULT_MANIFEST_PATH,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.manifest_path = Path(manifest_path)
        self.environ = environ

    def probe(self) -> WindowsProbeResult:
        config = probe_windows_config_dir(self.environ)
        if not self.manifest_path.exists():
            return WindowsProbeResult(
                status="blocked",
                message="real-machine evidence missing",
                config=config,
                manifest_path=self.manifest_path,
            )
        if config.path is None:
            return WindowsProbeResult(
                status="blocked",
                message="WorkBuddy config directory not discovered",
                config=config,
                manifest_path=self.manifest_path,
            )
        profile_result = load_windows_workbuddy_profile(
            self.manifest_path,
            config_dir=config.path,
        )
        if profile_result.failure is not None or profile_result.profile is None:
            return WindowsProbeResult(
                status="blocked",
                message="real-machine evidence incomplete",
                config=config,
                manifest_path=self.manifest_path,
            )
        if not config.exists:
            return WindowsProbeResult(
                status="blocked",
                message="WorkBuddy config directory is missing",
                config=config,
                manifest_path=self.manifest_path,
            )
        return WindowsProbeResult(
            status="ready",
            message="W0 evidence recorded; W1 and real-machine rollout remain required",
            config=config,
            manifest_path=self.manifest_path,
        )


class WindowsWorkBuddyData(WorkBuddyDataAdapter):
    """Windows data adapter whose transcript capability is W0-profile gated.

    Session/database access keeps the existing explicit-config contract.
    Transcript lookup additionally requires a non-synthetic ``W0`` profile;
    bare config directories and synthetic fixtures fail closed with a typed
    result instead of falling back to the POSIX descriptor scanner.
    """

    def __init__(
        self,
        config_dir: str | os.PathLike[str],
        *,
        profile: WindowsWorkBuddyProfile | None = None,
        profile_path: str | os.PathLike[str] | None = None,
        transcript_backend: WindowsTranscriptBackend | None = None,
        max_transcript_candidates: int = MAX_TRANSCRIPT_CANDIDATES,
        max_transcript_bytes: int = MAX_TRANSCRIPT_BYTES,
    ) -> None:
        if profile is not None and profile_path is not None:
            raise ValueError("provide either profile or profile_path, not both")
        explicit_config = Path(config_dir).expanduser()
        profile_failure: AdapterFailure | None = None
        loaded_profile = profile
        if profile_path is not None:
            loaded = load_windows_workbuddy_profile(
                profile_path,
                config_dir=explicit_config,
                allow_synthetic_fixture=False,
            )
            loaded_profile = loaded.profile
            profile_failure = loaded.failure
        elif loaded_profile is None:
            profile_failure = AdapterFailure(
                "windows_profile_required", "an explicit Windows W0 profile is required"
            )
        elif loaded_profile.synthetic:
            profile_failure = AdapterFailure(
                "windows_profile_synthetic",
                "synthetic WorkBuddy data is blocked in production",
            )
        elif loaded_profile.evidence_level != "W0":
            profile_failure = AdapterFailure(
                "windows_profile_not_w0", "a non-synthetic W0 profile is required"
            )
        elif (
            loaded_profile.schema_version != 1
            or _safe_mapping_path(loaded_profile.database_relative_path)
            != loaded_profile.database_relative_path
            or _safe_mapping_path(loaded_profile.projects_relative_path)
            != loaded_profile.projects_relative_path
            or loaded_profile.session_metadata_keys != ("session_id", "sessionId")
            or not loaded_profile.workbuddy_version.strip()
            or not loaded_profile.hook_command.strip()
        ):
            profile_failure = AdapterFailure(
                "windows_profile_invalid", "the Windows WorkBuddy profile is invalid"
            )
        elif not _profile_matches_config(loaded_profile.config_dir, explicit_config):
            profile_failure = AdapterFailure(
                "windows_profile_mismatch", "profile and configured WorkBuddy roots differ"
            )

        database_path = explicit_config / "workbuddy.db"
        projects_dir = explicit_config / "projects"
        if loaded_profile is not None and profile_failure is None:
            database_path = loaded_profile.mapped_path(
                loaded_profile.database_relative_path
            )
            projects_dir = loaded_profile.mapped_path(
                loaded_profile.projects_relative_path
            )
        self.windows_profile = loaded_profile
        self.transcript_profile_failure = profile_failure
        super().__init__(
            explicit_config,
            database_path=database_path,
            projects_dir=projects_dir,
            max_transcript_candidates=max_transcript_candidates,
            max_transcript_bytes=max_transcript_bytes,
            transcript_scanner=WindowsTranscriptScanner(backend=transcript_backend),
        )

    def probe(self) -> ProbeResult:
        result = super().probe()
        if result.failure is not None or self.transcript_profile_failure is None:
            return result
        return ProbeResult(
            config_dir=result.config_dir,
            database_path=result.database_path,
            capabilities=frozenset(
                capability
                for capability in result.capabilities
                if capability != "transcripts"
            ),
        )

    def read_transcript(self, session_id: str) -> TranscriptReadResult:
        if self.transcript_profile_failure is not None:
            return TranscriptReadResult(failure=self.transcript_profile_failure)
        return super().read_transcript(session_id)

    def transcript_snapshot(self) -> TranscriptSnapshot:
        if self.transcript_profile_failure is not None:
            return TranscriptSnapshot(
                scanner=self.transcript_scanner,
                failure=self.transcript_profile_failure,
            )
        return super().transcript_snapshot()
