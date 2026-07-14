#!/usr/bin/env python3
"""Fail-closed filesystem probe for the Windows Hook spool directory."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import tempfile


def _try_lock(fd: int) -> bool:
    try:
        if os.name == "nt":
            msvcrt = __import__("msvcrt")
            if os.fstat(fd).st_size == 0:
                os.lseek(fd, 0, os.SEEK_SET)
                os.write(fd, b"\0")
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            fcntl = __import__("fcntl")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        return False
    return True


def _unlock(fd: int) -> None:
    try:
        if os.name == "nt":
            msvcrt = __import__("msvcrt")
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            fcntl = __import__("fcntl")
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


def probe_spool(directory: Path) -> None:
    if directory.is_symlink() or not directory.is_dir():
        raise OSError("spool directory must be a real directory")
    owner_fd, owner_name = tempfile.mkstemp(
        dir=directory,
        prefix=".copilot-spool-capability-",
        suffix=".owner",
    )
    owner = Path(owner_name)
    linked = owner.with_suffix(".link")
    probe_fd: int | None = None
    owner_locked = False
    probe_locked = False
    try:
        owner_locked = _try_lock(owner_fd)
        if not owner_locked:
            raise OSError("spool directory does not provide the required byte lock")
        os.fsync(owner_fd)
        os.link(owner, linked)
        probe_fd = os.open(linked, os.O_RDWR)
        probe_locked = _try_lock(probe_fd)
        if probe_locked:
            raise OSError("hard-link paths do not share the required byte lock")
        if not os.path.samestat(os.fstat(owner_fd), os.fstat(probe_fd)):
            raise OSError("spool hard link did not preserve file identity")
    finally:
        if probe_fd is not None:
            if probe_locked:
                _unlock(probe_fd)
            os.close(probe_fd)
        if owner_locked:
            _unlock(owner_fd)
        os.close(owner_fd)
        linked.unlink(missing_ok=True)
        owner.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spool-dir", required=True)
    args = parser.parse_args()
    try:
        probe_spool(Path(args.spool_dir).expanduser())
    except OSError as exc:
        print(f"spool capability probe failed: {exc}", file=__import__("sys").stderr)
        return 1
    print("spool capability probe passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
