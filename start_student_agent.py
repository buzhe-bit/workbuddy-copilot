#!/usr/bin/env python3
"""Start the platform-neutral headless student agent.

WorkBuddy-specific upload handling is supplied by a later platform adapter;
this entry point is still useful for hook delivery and smoke testing.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
from pathlib import Path
import sqlite3
import sys

from copilot.config import load_config, service_url
from copilot.student_core.agent import StudentAgent
from copilot.student_core.coordinator import StudentCoordinator
from copilot.student_core.process_liveness import FileClaimStore
from copilot.student_core.spool import EventSpool
from copilot.student_core.transport import StudentTransport
from copilot.student_platform.windows_runtime import (
    WindowsRuntimeBlocked,
    WindowsStudentRuntime,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the WorkBuddy student agent")
    parser.add_argument("--config", default=os.environ.get("COPILOT_CONFIG"))
    parser.add_argument("--base-url", default=os.environ.get("COPILOT_BASE_URL"))
    parser.add_argument("--student-id", default=os.environ.get("COPILOT_STUDENT_ID"))
    parser.add_argument(
        "--token",
        default=os.environ.get("COPILOT_STUDENT_TOKEN") or os.environ.get("COPILOT_TOKEN"),
    )
    parser.add_argument(
        "--spool-dir",
        default=os.environ.get("COPILOT_SPOOL_DIR"),
    )
    parser.add_argument("--interval", type=float, default=float(os.environ.get("COPILOT_AGENT_INTERVAL", "1")))
    parser.add_argument(
        "--platform",
        choices=("auto", "windows", "core"),
        default=os.environ.get("COPILOT_STUDENT_PLATFORM", "auto"),
    )
    parser.add_argument(
        "--state-dir",
        default=os.environ.get(
            "COPILOT_STATE_DIR",
            str(Path.home() / ".copilot" / "state"),
        ),
    )
    parser.add_argument(
        "--workbuddy-config-dir",
        default=os.environ.get("WORKBUDDY_CONFIG_DIR"),
    )
    parser.add_argument(
        "--workbuddy-profile",
        default=os.environ.get("COPILOT_WINDOWS_WORKBUDDY_PROFILE"),
    )
    parser.add_argument(
        "--repair-claim",
        help="repair one explicit event-spool claim and exit",
    )
    parser.add_argument(
        "--expected-owner-token",
        default=os.environ.get("COPILOT_REPAIR_OWNER_TOKEN"),
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--reason",
        help="operator reason recorded in the local repair audit",
    )
    parser.add_argument(
        "--spool-only",
        action="store_true",
        help="deliver Hook spool events without opening a second WebSocket",
    )
    return parser


def _core_settings(args: argparse.Namespace) -> tuple[str, str, str, str]:
    cfg = load_config(args.config) if args.config else {}
    auth = cfg.get("auth", {}) if isinstance(cfg.get("auth"), dict) else {}
    student = cfg.get("student", {}) if isinstance(cfg.get("student"), dict) else {}
    base_url = args.base_url or (service_url(cfg) if cfg else "http://127.0.0.1:8765")
    student_id = args.student_id or str(cfg.get("student_id") or "student-1")
    token = args.token
    if token is None:
        token = str(
            auth.get("student_token")
            or auth.get("token")
            or cfg.get("token")
            or ""
        )
    spool_dir = args.spool_dir or student.get("spool_dir") or str(
        Path.home() / ".workbuddy" / "copilot" / "spool"
    )
    return str(base_url), str(student_id), str(token), str(spool_dir)


def _repair_claim(args: argparse.Namespace) -> int:
    claim_id = str(args.repair_claim or "").strip()
    expected_owner_token = str(args.expected_owner_token or "")
    reason = str(args.reason or "").strip()
    if not claim_id or not expected_owner_token or not reason:
        print(
            "BLOCKED: repair requires claim id, expected owner token, and reason",
            file=sys.stderr,
        )
        return 2
    try:
        spool = EventSpool(_core_settings(args)[3])
        command_state_dir = spool.directory / ".copilot-upload-commands"
        command_store = FileClaimStore(
            command_state_dir,
            process_identity=spool.process_identity,
            process_liveness=spool.process_liveness,
            claim_path_factory=lambda identifier: command_state_dir / f"{identifier}.claim",
        )
        selected_store = None
        expected_identity = None
        for candidate in (spool.claim_store, command_store):
            identity = candidate.identity_for_repair(claim_id)
            if identity is not None:
                selected_store = candidate
                expected_identity = identity
                break
        if selected_store is None or expected_identity is None:
            print("BLOCKED: claim is missing, legacy, or unreadable", file=sys.stderr)
            return 3
        repaired = selected_store.repair(
            claim_id,
            expected_identity=expected_identity,
            expected_owner_token=expected_owner_token,
            reason=reason,
        )
    except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
        print(f"BLOCKED: claim repair failed ({type(exc).__name__})", file=sys.stderr)
        return 3
    if not repaired:
        print("BLOCKED: claim identity or owner token did not match", file=sys.stderr)
        return 3
    print(f"REPAIRED: {claim_id}")
    return 0


async def _run(args: argparse.Namespace) -> None:
    base_url, student_id, token, spool_dir = _core_settings(args)
    platform = args.platform
    if platform == "auto":
        platform = "windows" if sys.platform.startswith("win") else "core"
    if platform == "windows":
        runtime = WindowsStudentRuntime.build(
            base_url=base_url,
            student_id=student_id,
            token=token,
            spool_dir=spool_dir,
            state_dir=args.state_dir,
            workbuddy_config_dir=args.workbuddy_config_dir,
            profile_path=args.workbuddy_profile,
            interval=args.interval,
        )
        await runtime.run()
        return
    spool = EventSpool(spool_dir)
    transport = StudentTransport(
        base_url,
        student_id=student_id,
        token=token,
    )
    # WorkBuddyData upload orchestration is injected by a platform adapter in
    # a later phase.  Do not pretend this headless core can complete uploads.
    coordinator = StudentCoordinator(spool, transport, uploader=None)
    if args.spool_only:
        while True:
            try:
                await coordinator.flush_spool_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logging.getLogger("copilot.student_agent").warning(
                    "student spool cycle failed type=%s",
                    type(exc).__name__,
                )
            await asyncio.sleep(args.interval)
    await StudentAgent(coordinator, interval=args.interval).run()


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.repair_claim:
        return _repair_claim(args)
    if args.expected_owner_token or args.reason:
        print("BLOCKED: --expected-owner-token/--reason require --repair-claim", file=sys.stderr)
        return 2
    try:
        asyncio.run(_run(args))
    except WindowsRuntimeBlocked as exc:
        print(f"BLOCKED: {exc.code}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
