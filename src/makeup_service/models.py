"""领域模型：场次、预约、资源、取消登记与补办关系。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat(timespec="seconds")


class SessionStatus(str, Enum):
    """场次生命周期，与 domain/contract.json 的 states 一一对应。"""

    PREPARING = "筹备"
    PENDING = "待确认"
    SCHEDULED = "已排定"
    IN_PROGRESS = "执行中"
    SETTLED = "已结算"


#: 允许登记取消的状态（执行中与已结算的场次不可取消）。
CANCELLABLE_STATUSES = {
    SessionStatus.PREPARING,
    SessionStatus.PENDING,
    SessionStatus.SCHEDULED,
}

#: 已锁定状态：不可取消、不可改期、不可恢复、不可退出、不可加约。
LOCKED_STATUSES = {SessionStatus.IN_PROGRESS, SessionStatus.SETTLED}

#: 场次状态机的合法前进方向。
SESSION_TRANSITIONS = {
    SessionStatus.PREPARING: {SessionStatus.PENDING},
    SessionStatus.PENDING: {SessionStatus.SCHEDULED},
    SessionStatus.SCHEDULED: {SessionStatus.IN_PROGRESS},
    SessionStatus.IN_PROGRESS: {SessionStatus.SETTLED},
    SessionStatus.SETTLED: set(),
}


class ReservationStatus(str, Enum):
    ACTIVE = "有效"
    MIGRATABLE = "待迁移"
    MIGRATED = "已迁移"


class CancellationStatus(str, Enum):
    OPEN = "待补办"
    FULFILLED = "已补办"
    RESTORED = "已恢复"


class CandidateStatus(str, Enum):
    PENDING = "候选"
    CONFIRMED = "已确认"
    REJECTED = "已落选"
    SUPERSEDED = "被取代"
    VOID = "已作废"


class LinkStatus(str, Enum):
    ACTIVE = "生效中"
    SUPERSEDED = "被取代"
    REVOKED = "已撤销"


class NotificationKind(str, Enum):
    CANCELLATION = "取消通知"
    MAKEUP_CONFIRMED = "补办确认通知"
    RESCHEDULED = "改期通知"
    RESTORED = "恢复通知"


class NotificationStatus(str, Enum):
    PENDING = "待发送"
    SENT = "已发送"


class AttributionState(str, Enum):
    """参与者最终归属的三种确定结局。"""

    ASSIGNED = "已归属"
    PENDING_PLACEMENT = "待安置"
    WITHDRAWN = "已退出"


@dataclass
class Session:
    session_id: str
    title: str
    school: str
    venue_id: str
    start_time: datetime
    status: SessionStatus = SessionStatus.SCHEDULED
    duration_minutes: int = 120
    cancelled: bool = False
    close_reason: str = ""

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "title": self.title,
            "school": self.school,
            "venue_id": self.venue_id,
            "start_time": _iso(self.start_time),
            "duration_minutes": self.duration_minutes,
            "status": self.status.value,
            "cancelled": self.cancelled,
            "close_reason": self.close_reason,
        }


@dataclass
class Participant:
    participant_id: str
    name: str
    school: str

    def to_dict(self) -> dict:
        return {
            "participant_id": self.participant_id,
            "name": self.name,
            "school": self.school,
        }


@dataclass
class Reservation:
    """学校在某场次的一批预约；participant_ids 创建后不变，退出由 Withdrawal 记录。"""

    reservation_id: str
    session_id: str
    school: str
    participant_ids: tuple[str, ...]
    status: ReservationStatus
    previous_reservation_id: str | None
    created_at: datetime

    def to_dict(self) -> dict:
        return {
            "reservation_id": self.reservation_id,
            "session_id": self.session_id,
            "school": self.school,
            "participant_ids": list(self.participant_ids),
            "status": self.status.value,
            "previous_reservation_id": self.previous_reservation_id,
            "created_at": _iso(self.created_at),
        }


@dataclass
class Resource:
    resource_id: str
    kind: str
    name: str
    capacity: int

    def to_dict(self) -> dict:
        return {
            "resource_id": self.resource_id,
            "kind": self.kind,
            "name": self.name,
            "capacity": self.capacity,
        }


@dataclass
class Allocation:
    """有限资源在某场次时段上的占用；确认补办/改期/恢复时随场次整体转移。"""

    allocation_id: str
    resource_id: str
    session_id: str
    quantity: int
    active: bool = True

    def to_dict(self) -> dict:
        return {
            "allocation_id": self.allocation_id,
            "resource_id": self.resource_id,
            "session_id": self.session_id,
            "quantity": self.quantity,
            "active": self.active,
        }


@dataclass
class CancellationRecord:
    """取消登记：原因、原场次、停用通知与登记人。"""

    cancellation_id: str
    session_id: str
    reason: str
    notice_id: str
    registered_by: str
    registered_at: datetime
    status: CancellationStatus = CancellationStatus.OPEN

    def to_dict(self) -> dict:
        return {
            "cancellation_id": self.cancellation_id,
            "session_id": self.session_id,
            "reason": self.reason,
            "notice_id": self.notice_id,
            "registered_by": self.registered_by,
            "registered_at": _iso(self.registered_at),
            "status": self.status.value,
        }


@dataclass
class MakeupCandidate:
    candidate_id: str
    cancellation_id: str
    venue_id: str
    start_time: datetime
    status: CandidateStatus
    makeup_session_id: str | None = None

    def to_dict(self) -> dict:
        return {
            "candidate_id": self.candidate_id,
            "cancellation_id": self.cancellation_id,
            "venue_id": self.venue_id,
            "start_time": _iso(self.start_time),
            "status": self.status.value,
            "makeup_session_id": self.makeup_session_id,
        }


@dataclass
class MakeupLink:
    """补办关系：一次确认或改期产生的版本化关联，含原子转移的审计信息。"""

    link_id: str
    cancellation_id: str
    candidate_id: str
    original_session_id: str
    previous_session_id: str
    makeup_session_id: str
    version: int
    status: LinkStatus
    idempotency_key: str
    created_at: datetime
    supersedes: str | None = None
    moved_reservations: tuple[tuple[str, str], ...] = ()
    moved_allocations: tuple[str, ...] = ()
    participant_count: int = 0

    def to_dict(self) -> dict:
        return {
            "link_id": self.link_id,
            "cancellation_id": self.cancellation_id,
            "candidate_id": self.candidate_id,
            "original_session_id": self.original_session_id,
            "previous_session_id": self.previous_session_id,
            "makeup_session_id": self.makeup_session_id,
            "version": self.version,
            "status": self.status.value,
            "idempotency_key": self.idempotency_key,
            "created_at": _iso(self.created_at),
            "supersedes": self.supersedes,
            "moved_reservations": [list(pair) for pair in self.moved_reservations],
            "moved_allocations": list(self.moved_allocations),
            "participant_count": self.participant_count,
        }


@dataclass
class Notification:
    notification_id: str
    cancellation_id: str
    school: str
    kind: NotificationKind
    status: NotificationStatus
    created_at: datetime
    sent_at: datetime | None = None

    def to_dict(self) -> dict:
        return {
            "notification_id": self.notification_id,
            "cancellation_id": self.cancellation_id,
            "school": self.school,
            "kind": self.kind.value,
            "status": self.status.value,
            "created_at": _iso(self.created_at),
            "sent_at": _iso(self.sent_at),
        }


@dataclass
class Withdrawal:
    """参与者退出记录：退出即终态，迁移与归属均以它为准。"""

    withdrawal_id: str
    participant_id: str
    reservation_id: str
    session_id: str
    reason: str
    created_at: datetime

    def to_dict(self) -> dict:
        return {
            "withdrawal_id": self.withdrawal_id,
            "participant_id": self.participant_id,
            "reservation_id": self.reservation_id,
            "session_id": self.session_id,
            "reason": self.reason,
            "created_at": _iso(self.created_at),
        }
