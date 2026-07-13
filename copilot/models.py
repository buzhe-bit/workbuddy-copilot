"""领域模型定义。

用 dataclass 定义核心业务概念，替代全程 dict 传递。
这些模型不含持久化逻辑，不含 I/O，只定义字段和类型。
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Iterator, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .transcript import TranscriptSnapshot


_EVENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


def normalize_event_id(value: str | None) -> str | None:
    """Validate a durable hook id while preserving legacy empty ids."""
    if value is None or value == "":
        return None
    if not isinstance(value, str) or _EVENT_ID_PATTERN.fullmatch(value) is None:
        raise ValueError("invalid event_id")
    return value


@dataclass
class Student:
    """学员。"""
    student_id: str
    display_name: str = ""
    analysis_count: int = 0
    session_count: int = 0
    last_ts: float = 0.0
    last_topic: str = ""
    last_severity: str = "info"
    alert_count: int = 0
    last_diagnosis: str = ""


@dataclass
class Conversation:
    """对话（WorkBuddy 会话）。"""
    session_id: str
    work_dir: str = ""
    title: str = ""
    group_type: str = ""
    space_name: str = ""
    status: str = ""
    mode: str = ""
    created_at: float = 0.0
    last_activity_at: float = 0.0
    deleted: bool = False
    # Copilot 侧统计（由 CopilotRepo 填充）
    analysis_count: int = 0
    message_count: int = 0
    alert_count: int = 0
    last_diagnosis: str = ""
    last_topic: str = ""
    last_severity: str = "info"
    last_is_technical: int = 0


@dataclass
class Session:
    """Copilot 侧会话。"""
    session_id: str = ""
    student_id: str = ""
    work_dir: str = ""
    title: str = ""
    group_type: str = ""
    space_name: str = ""
    created_at: float = 0.0
    last_activity_at: float = 0.0


@dataclass
class MentorMessage:
    """导师下发给学员浮标的消息。"""
    id: int = 0
    student_id: str = ""
    mentor_id: str = ""
    session_id: str = ""
    text: str = ""
    message_id: str = ""
    created_at: float = 0.0
    delivered_at: Optional[float] = None
    read_at: Optional[float] = None


@dataclass
class TimelineEntry:
    """时间线条目（三表 UNION 的统一格式）。"""
    type: str  # prompt / ai_summary / analysis
    content: str = ""
    created_at: float = 0.0
    session_id: str = ""
    seq_in_session: Optional[int] = None
    prompt_id: Optional[int] = None
    reply_ref: Optional[str] = None
    has_summary: bool = False
    has_full_reply: bool = False
    # analysis 额外字段
    suggestion: str = ""
    severity: str = ""
    understanding: str = ""
    topic: str = ""
    is_technical: bool = False


@dataclass(frozen=True)
class AcceptedReport:
    """Durable result of accepting one hook report.

    Iteration preserves the legacy ``report_id, session_id, snapshot`` unpacking
    contract while callers migrate to the explicit delivery fields.
    """

    report_id: int
    session_id: str
    snapshot: TranscriptSnapshot
    duplicate: bool = False
    analysis_status: str = "not_requested"

    def __iter__(self) -> Iterator[object]:
        yield self.report_id
        yield self.session_id
        yield self.snapshot


@dataclass
class AnalysisResult:
    """LLM 分析返回结果（原始 dict 的类型安全版本）。"""
    topic: str = ""
    understanding: str = "medium"
    off_topic: bool = False
    stuck_at: str = ""
    progress: str = ""
    guidance: str = ""
    alert: str = ""
    is_technical: bool = False
    severity: str = "info"
    diagnosis: str = ""
    suggestion: str = ""
    ai_reply_summary: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> AnalysisResult:
        """从 LLM 返回的 dict 构造。"""
        return cls(
            topic=d.get("topic", ""),
            understanding=d.get("understanding", "medium"),
            off_topic=d.get("off_topic", False),
            stuck_at=d.get("stuck_at", ""),
            progress=d.get("progress", ""),
            guidance=d.get("guidance", ""),
            alert=d.get("alert", ""),
            is_technical=d.get("is_technical", False),
            severity=d.get("severity", "info"),
            diagnosis=d.get("diagnosis", ""),
            suggestion=d.get("suggestion", ""),
            ai_reply_summary=d.get("ai_reply_summary", ""),
        )

    def to_dict(self) -> dict:
        """转回 dict（兼容 store.add_analysis 的接口）。"""
        return {
            "topic": self.topic,
            "understanding": self.understanding,
            "off_topic": self.off_topic,
            "stuck_at": self.stuck_at,
            "progress": self.progress,
            "guidance": self.guidance,
            "alert": self.alert,
            "is_technical": self.is_technical,
            "severity": self.severity,
            "diagnosis": self.diagnosis,
            "suggestion": self.suggestion,
            "ai_reply_summary": self.ai_reply_summary,
        }
