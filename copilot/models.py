"""领域模型定义。

用 dataclass 定义核心业务概念，替代全程 dict 传递。
这些模型不含持久化逻辑，不含 I/O，只定义字段和类型。
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math
import re
from collections.abc import Mapping
from typing import Any, Iterator, Literal, Optional, TYPE_CHECKING

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


def normalize_confidence(value: object) -> float:
    """Return a finite confidence in ``[0, 1]`` with legacy-safe defaults."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.5
    normalized = float(value)
    if not math.isfinite(normalized):
        return 0.5
    return min(max(normalized, 0.0), 1.0)


def normalize_evidence(value: object) -> list[str]:
    """Keep at most three non-empty textual evidence snippets."""
    if not isinstance(value, list):
        return []
    evidence: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        snippet = item.strip()
        if not snippet:
            continue
        evidence.append(snippet[:160])
        if len(evidence) == 3:
            break
    return evidence


def normalize_latency_ms(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    numeric = float(value)
    if not math.isfinite(numeric):
        return 0
    return max(0, int(numeric))


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
    open_attention_count: int = 0
    highest_attention_priority: str = ""
    last_attention_at: float = 0.0


AttentionSourceType = Literal["analysis", "student_ask", "system"]
AttentionCategory = Literal["learning", "system"]
AttentionPriority = Literal["high", "medium"]


@dataclass(frozen=True)
class AttentionDecision:
    """A bounded, persistence-ready mentor-attention projection."""

    source_type: AttentionSourceType
    source_id: str
    category: AttentionCategory
    student_id: str
    session_id: str
    priority: AttentionPriority
    reason_code: str
    reason: str
    evidence: tuple[str, ...] = ()
    suggested_action: str = ""
    confidence: float = 0.5
    created_at: float = 0.0


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


@dataclass(frozen=True)
class QuestionAnswerOutcome:
    """Result of one student question without hiding provider degradation."""

    status: Literal["answered", "degraded", "failed"]
    answer: str
    error_code: str = ""


@dataclass(frozen=True, eq=False)
class UploadOutcome(Mapping[str, int]):
    """Confirmed transcript-upload result.

    The mapping view intentionally preserves the former
    ``total/synced/skipped/failed`` return contract while new callers use the
    explicit fields and the fail-closed :attr:`complete` gate.
    """

    matched: int
    attempted: int
    accepted: int
    skipped: int
    failed: int
    error_code: str = ""

    def __post_init__(self) -> None:
        for field_name in ("matched", "attempted", "accepted", "skipped", "failed"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or int(value) != value or value < 0:
                raise ValueError(f"{field_name} must be a non-negative integer")

    @property
    def complete(self) -> bool:
        """Only a non-empty, fully confirmed match can complete a command."""
        return (
            self.matched > 0
            and self.attempted == self.matched
            and self.failed == 0
            and self.accepted + self.skipped == self.attempted
        )

    def __getitem__(self, key: str) -> int:
        values = {
            "total": self.matched,
            "synced": self.accepted,
            "skipped": self.skipped,
            "failed": self.failed,
        }
        return values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(("total", "synced", "skipped", "failed"))

    def __len__(self) -> int:
        return 4


@dataclass(frozen=True)
class AnalysisEnvelope:
    """One stable analysis wire shape shared by WebSocket and catch-up."""

    student_id: str
    session_id: str
    report_id: int
    event: str
    result: Mapping[str, Any]
    timestamp: float
    # ``report_id`` describes the source, but reports can finish analysis out
    # of order. ``analysis_id`` is assigned at durable commit and is therefore
    # the only safe delivery cursor. Zero preserves old in-process adapters.
    analysis_id: int = 0
    type: str = "analysis"

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "analysis_id": self.analysis_id,
            "student_id": self.student_id,
            "session_id": self.session_id,
            "report_id": self.report_id,
            "event": self.event,
            "result": dict(self.result),
            "timestamp": self.timestamp,
        }


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
    confidence: float = 0.5
    evidence: list[str] = field(default_factory=list)
    model: str = ""
    prompt_hash: str = ""
    latency_ms: int = 0

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
            confidence=normalize_confidence(d.get("confidence", 0.5)),
            evidence=normalize_evidence(d.get("evidence", [])),
            model=(str(d.get("model") or "")[:200] if isinstance(d.get("model"), str) else ""),
            prompt_hash=(
                str(d.get("prompt_hash") or "")[:128]
                if isinstance(d.get("prompt_hash"), str)
                else ""
            ),
            latency_ms=normalize_latency_ms(d.get("latency_ms", 0)),
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
            "confidence": self.confidence,
            "evidence": list(self.evidence),
            "model": self.model,
            "prompt_hash": self.prompt_hash,
            "latency_ms": self.latency_ms,
        }
