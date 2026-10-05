"""领域对象与状态枚举。

所有对象均可序列化为普通 ``dict``，便于内存存储快照回滚与 JSON 输出。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def utc_now_iso() -> str:
    """当前 UTC 时间的 ISO-8601 字符串。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class EventStatus(str, Enum):
    SCHEDULED = "scheduled"
    CANCELLED = "cancelled"
    COMPLETED = "completed"


class ChainRole(str, Enum):
    ORIGIN = "origin"
    MAKEUP = "makeup"


class BookingStatus(str, Enum):
    ACTIVE = "active"
    WITHDRAWN = "withdrawn"


class CancellationStatus(str, Enum):
    OPEN = "open"              # 已登记取消，尚未确认补办
    CONFIRMED = "confirmed"    # 已确认补办（含再次改期后仍然成立）
    RESTORED = "restored"      # 原场次恢复，登记作废


class CandidateStatus(str, Enum):
    PENDING = "pending"
    SELECTED = "selected"
    SUPERSEDED = "superseded"  # 被更新的改期版本取代
    VOID = "void"              # 原场次恢复后置废


class NotifyStatus(str, Enum):
    PENDING = "pending"
    SENT = "sent"
    DELIVERED = "delivered"
    FAILED = "failed"


@dataclass
class Resource:
    """有限资源（场地、设备、讲解配额等），按容量占用。"""

    id: str
    name: str
    capacity: int

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "capacity": self.capacity}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Resource":
        return cls(id=value["id"], name=value["name"], capacity=value["capacity"])


@dataclass
class Event:
    """活动场次。

    ``resource_plan`` 为 ``{资源ID: 占用数量}``；取消登记后原场次的占用被
    “冻结”而不是释放，直到确认补办时一次性转移（或恢复时保留）。
    """

    id: str
    title: str
    start_time: str
    capacity: int
    resource_plan: dict[str, int] = field(default_factory=dict)
    status: EventStatus = EventStatus.SCHEDULED
    chain_id: str | None = None
    chain_role: ChainRole | None = None
    # 占用与预约已转出到的目标场次：原场次确认补办后指向首个补办场次，
    # 旧补办场次改期后指向新版本，恢复时清空。
    replaced_by: str | None = None

    @property
    def occupies_resources(self) -> bool:
        """该场次当前是否仍占用资源池。

        取消后、补办确认前为“冻结占用”（仍计容量，避免别人抢占）；
        一旦占用在确认/改期事务中转出，或场次结束，即不再占用。
        """
        if self.status is EventStatus.COMPLETED:
            return False
        if self.status is EventStatus.CANCELLED and self.replaced_by:
            return False
        return True

    @property
    def accepts_bookings(self) -> bool:
        return self.status is EventStatus.SCHEDULED

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "start_time": self.start_time,
            "capacity": self.capacity,
            "resource_plan": dict(self.resource_plan),
            "status": self.status.value,
            "chain_id": self.chain_id,
            "chain_role": self.chain_role.value if self.chain_role else None,
            "replaced_by": self.replaced_by,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Event":
        return cls(
            id=value["id"],
            title=value["title"],
            start_time=value["start_time"],
            capacity=value["capacity"],
            resource_plan=dict(value.get("resource_plan") or {}),
            status=EventStatus(value["status"]),
            chain_id=value.get("chain_id"),
            chain_role=ChainRole(value["chain_role"]) if value.get("chain_role") else None,
            replaced_by=value.get("replaced_by"),
        )


@dataclass
class Booking:
    """预约/参与者记录。

    一条记录沿补办链移动：``event_id`` 始终指向当前归属场次，
    ``history`` 保留每一段归属，退出后状态为 ``withdrawn``。
    """

    id: str
    student_id: str
    student_name: str
    contact: str
    event_id: str
    status: BookingStatus = BookingStatus.ACTIVE
    checked_in: bool = False
    chain_id: str | None = None
    history: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "student_id": self.student_id,
            "student_name": self.student_name,
            "contact": self.contact,
            "event_id": self.event_id,
            "status": self.status.value,
            "checked_in": self.checked_in,
            "chain_id": self.chain_id,
            "history": list(self.history),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Booking":
        return cls(
            id=value["id"],
            student_id=value["student_id"],
            student_name=value["student_name"],
            contact=value["contact"],
            event_id=value["event_id"],
            status=BookingStatus(value["status"]),
            checked_in=value.get("checked_in", False),
            chain_id=value.get("chain_id"),
            history=list(value.get("history") or []),
        )


@dataclass
class MakeupCandidate:
    """补办候选；同一登记的多次改期通过递增 ``version`` 形成版本序列。"""

    id: str
    version: int
    scheduled_start: str
    capacity: int
    resource_plan: dict[str, int] = field(default_factory=dict)
    status: CandidateStatus = CandidateStatus.PENDING
    created_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "version": self.version,
            "scheduled_start": self.scheduled_start,
            "capacity": self.capacity,
            "resource_plan": dict(self.resource_plan),
            "status": self.status.value,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "MakeupCandidate":
        return cls(
            id=value["id"],
            version=value["version"],
            scheduled_start=value["scheduled_start"],
            capacity=value["capacity"],
            resource_plan=dict(value.get("resource_plan") or {}),
            status=CandidateStatus(value["status"]),
            created_at=value["created_at"],
        )


@dataclass
class CancellationRecord:
    """取消补办登记，即一条“补办链”。"""

    id: str
    origin_event_id: str
    reason_code: str
    reason_note: str
    status: CancellationStatus = CancellationStatus.OPEN
    created_at: str = field(default_factory=utc_now_iso)
    candidates: list[MakeupCandidate] = field(default_factory=list)
    # 通知状态：取消通知与补办通知分别维护，attempts 保留全部发送尝试。
    notifications: dict[str, list[dict[str, Any]]] = field(default_factory=lambda: {
        "cancel": [{"at": utc_now_iso(), "status": NotifyStatus.PENDING.value}],
        "makeup": [],
    })
    current_makeup_event_id: str | None = None
    reschedule_count: int = 0
    confirmations: list[dict[str, Any]] = field(default_factory=list)

    def notify_status(self, kind: str) -> str:
        attempts = self.notifications[kind]
        return attempts[-1]["status"] if attempts else NotifyStatus.PENDING.value

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "origin_event_id": self.origin_event_id,
            "reason_code": self.reason_code,
            "reason_note": self.reason_note,
            "status": self.status.value,
            "created_at": self.created_at,
            "candidates": [c.to_dict() for c in self.candidates],
            "cancel_notify_status": self.notify_status("cancel"),
            "makeup_notify_status": self.notify_status("makeup"),
            "notification_attempts": {k: list(v) for k, v in self.notifications.items()},
            "current_makeup_event_id": self.current_makeup_event_id,
            "reschedule_count": self.reschedule_count,
            "confirmations": list(self.confirmations),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "CancellationRecord":
        return cls(
            id=value["id"],
            origin_event_id=value["origin_event_id"],
            reason_code=value["reason_code"],
            reason_note=value["reason_note"],
            status=CancellationStatus(value["status"]),
            created_at=value["created_at"],
            candidates=[MakeupCandidate.from_dict(c) for c in value.get("candidates", [])],
            notifications={k: list(v) for k, v in value.get("notification_attempts", {}).items()}
            or {"cancel": [], "makeup": []},
            current_makeup_event_id=value.get("current_makeup_event_id"),
            reschedule_count=value.get("reschedule_count", 0),
            confirmations=list(value.get("confirmations") or []),
        )
