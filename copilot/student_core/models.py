"""Platform-neutral contracts shared by the student hook and agent."""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Mapping

_EVENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


def _text(value: Any, field: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    return value


@dataclass(frozen=True)
class HookEvent:
    """The small, JSON-safe envelope written by the WorkBuddy hook."""

    event: str
    student_id: str = ""
    session_id: str = ""
    cwd: str = ""
    transcript_tail: str = ""
    transcript_path: str = ""
    prompt: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.event, str) or not self.event.strip():
            raise ValueError("event is required")
        for field in (
            "student_id",
            "session_id",
            "cwd",
            "transcript_tail",
            "transcript_path",
            "prompt",
        ):
            _text(getattr(self, field), field)

    def to_dict(self) -> dict[str, str]:
        return {
            "event": self.event,
            "student_id": self.student_id,
            "session_id": self.session_id,
            "cwd": self.cwd,
            "transcript_tail": self.transcript_tail,
            "transcript_path": self.transcript_path,
            "prompt": self.prompt,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "HookEvent":
        if not isinstance(data, Mapping):
            raise ValueError("hook event must be an object")
        fields = {
            name: _text(data.get(name, ""), name)
            for name in (
                "event",
                "student_id",
                "session_id",
                "cwd",
                "transcript_tail",
                "transcript_path",
                "prompt",
            )
        }
        return cls(**fields)


@dataclass(frozen=True)
class SpoolEntry:
    """A durable event and its safe, path-independent identifier."""

    event_id: str
    payload: HookEvent
    enqueued_at_ns: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.event_id, str) or not _EVENT_ID.fullmatch(self.event_id):
            raise ValueError("invalid event_id")
        if not isinstance(self.payload, HookEvent):
            raise TypeError("payload must be a HookEvent")
        if (
            isinstance(self.enqueued_at_ns, bool)
            or not isinstance(self.enqueued_at_ns, int)
            or self.enqueued_at_ns < 0
        ):
            raise ValueError("enqueued_at_ns must be a non-negative integer")

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "enqueued_at_ns": self.enqueued_at_ns,
            "payload": self.payload.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SpoolEntry":
        if not isinstance(data, Mapping):
            raise ValueError("spool entry must be an object")
        event_id = data.get("event_id")
        if not isinstance(event_id, str):
            raise ValueError("event_id must be a string")
        payload = data.get("payload")
        if not isinstance(payload, Mapping):
            raise ValueError("payload must be an object")
        enqueued_at_ns = data.get("enqueued_at_ns", 0)
        return cls(
            event_id=event_id,
            payload=HookEvent.from_dict(payload),
            enqueued_at_ns=enqueued_at_ns,
        )
