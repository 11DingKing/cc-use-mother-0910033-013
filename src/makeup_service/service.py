"""活动取消补办核心服务。

确定性规则概览（与 README 一致）：

- 取消登记：仅 筹备/待确认/已排定 可取消；同一场次重复登记幂等返回原记录；
  登记后有效预约转为待迁移，并按学校生成取消通知。
- 确认补办：参与者与有限资源在同一把锁内"预检 → 应用"，资源在目标时段
  不足则整体失败、不产生任何副作用；重复确认按幂等键或相同候选返回既有
  补办关系；确认其他候选需走改期接口。
- 多次改期：每次改期生成版本 +1 的新补办关系，参与者与资源整体迁往新场次，
  旧场次与旧关系被取代，仅最新版本生效；补办场次已开始则拒绝改期。
- 原场次恢复：未补办直接恢复；已补办且补办场次未开始则整体迁回原场次；
  补办场次已开始则恢复窗口关闭，拒绝恢复。
- 部分退出：退出即终态，迁移时自动排除，归属接口与报表口径一致。
- 报表去重：按补办关系把原场次与补办场次串成链，每条链只按最终生效场次
  统计一次，杜绝取消与补办重复计入完成量。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from .errors import (
    AlreadyConfirmedError,
    InvalidStateError,
    NotFoundError,
    ResourceConflictError,
    RestorationClosedError,
)
from .models import (
    CANCELLABLE_STATUSES,
    LOCKED_STATUSES,
    SESSION_TRANSITIONS,
    Allocation,
    CancellationRecord,
    CancellationStatus,
    CandidateStatus,
    LinkStatus,
    MakeupCandidate,
    MakeupLink,
    Notification,
    NotificationKind,
    NotificationStatus,
    Participant,
    Reservation,
    ReservationStatus,
    Resource,
    Session,
    SessionStatus,
    Withdrawal,
)

#: 报表中每条补办链路的确定结局。
OUTCOME_COMPLETED = "已完成"
OUTCOME_IN_PROGRESS = "执行中"
OUTCOME_NOT_STARTED = "未开始"
OUTCOME_CANCELLED = "已取消"


@dataclass
class _TransferPlan:
    """一次原子转移的预检结果：待迁预约、在册参与者与资源占用。"""

    reservations: list[Reservation]
    members: dict[str, list[str]]
    allocations: list[Allocation]


class MakeupService:
    """取消-补办领域服务（内存实现，线程安全，写操作原子）。"""

    def __init__(self, *, now: Callable[[], datetime] | None = None) -> None:
        self._now = now or datetime.now
        self._lock = threading.RLock()
        self._seq: dict[str, int] = {}
        self._sessions: dict[str, Session] = {}
        self._participants: dict[str, Participant] = {}
        self._reservations: dict[str, Reservation] = {}
        self._resources: dict[str, Resource] = {}
        self._allocations: dict[str, Allocation] = {}
        self._cancellations: dict[str, CancellationRecord] = {}
        self._candidates: dict[str, MakeupCandidate] = {}
        self._links: dict[str, MakeupLink] = {}
        self._notifications: dict[str, Notification] = {}
        self._withdrawals: dict[tuple[str, str], Withdrawal] = {}

    # ------------------------------------------------------------------
    # 基础建档
    # ------------------------------------------------------------------

    def add_session(
        self,
        title: str,
        school: str,
        venue_id: str,
        start_time: datetime,
        *,
        status: SessionStatus = SessionStatus.SCHEDULED,
        duration_minutes: int = 120,
    ) -> Session:
        if status in LOCKED_STATUSES:
            raise InvalidStateError("新场次不能直接创建为执行中或已结算")
        if duration_minutes <= 0:
            raise InvalidStateError("场次时长必须为正数")
        with self._lock:
            session = Session(
                session_id=self._next_id("SES"),
                title=title,
                school=school,
                venue_id=venue_id,
                start_time=start_time,
                status=status,
                duration_minutes=duration_minutes,
            )
            self._sessions[session.session_id] = session
            return session

    def register_participant(self, name: str, school: str) -> Participant:
        with self._lock:
            participant = Participant(self._next_id("PTP"), name, school)
            self._participants[participant.participant_id] = participant
            return participant

    def add_reservation(
        self, session_id: str, school: str, participant_ids: list[str]
    ) -> Reservation:
        with self._lock:
            session = self._require_session(session_id)
            if session.cancelled:
                raise InvalidStateError("场次已取消，不能新增预约")
            if session.status in LOCKED_STATUSES:
                raise InvalidStateError("场次已开始，不能新增预约")
            ids = list(dict.fromkeys(participant_ids))
            if not ids:
                raise InvalidStateError("预约至少包含一名参与者")
            for pid in ids:
                participant = self._require_participant(pid)
                if participant.school != school:
                    raise InvalidStateError(f"参与者 {pid} 不属于 {school}")
            booked = {
                pid
                for r in self._reservations.values()
                if r.session_id == session_id
                and r.status in (ReservationStatus.ACTIVE, ReservationStatus.MIGRATABLE)
                for pid in self._current_participants(r)
            }
            overlap = booked.intersection(ids)
            if overlap:
                raise InvalidStateError(
                    "参与者在场次已有有效预约：" + "、".join(sorted(overlap))
                )
            reservation = Reservation(
                reservation_id=self._next_id("RSV"),
                session_id=session_id,
                school=school,
                participant_ids=tuple(ids),
                status=ReservationStatus.ACTIVE,
                previous_reservation_id=None,
                created_at=self._now(),
            )
            self._reservations[reservation.reservation_id] = reservation
            return reservation

    def add_resource(self, kind: str, name: str, capacity: int = 1) -> Resource:
        if capacity < 1:
            raise InvalidStateError("资源容量至少为 1")
        with self._lock:
            resource = Resource(self._next_id("RES"), kind, name, capacity)
            self._resources[resource.resource_id] = resource
            return resource

    def allocate_resource(
        self, session_id: str, resource_id: str, quantity: int = 1
    ) -> Allocation:
        with self._lock:
            session = self._require_session(session_id)
            resource = self._require_resource(resource_id)
            if session.cancelled:
                raise InvalidStateError("场次已取消，不能占用资源")
            if session.status in LOCKED_STATUSES:
                raise InvalidStateError("场次已开始，不能调整资源")
            if quantity < 1:
                raise InvalidStateError("占用数量至少为 1")
            start, end = self._window(session)
            if not self._resource_available(resource, None, start, end, quantity):
                raise ResourceConflictError(f"资源 {resource.name} 在该时段余量不足")
            allocation = Allocation(
                allocation_id=self._next_id("ALC"),
                resource_id=resource_id,
                session_id=session_id,
                quantity=quantity,
            )
            self._allocations[allocation.allocation_id] = allocation
            return allocation

    def advance_session(self, session_id: str, target: SessionStatus | str) -> Session:
        with self._lock:
            session = self._require_session(session_id)
            target = SessionStatus(target)
            if session.cancelled:
                raise InvalidStateError("场次已取消，不能推进状态")
            if target not in SESSION_TRANSITIONS[session.status]:
                raise InvalidStateError(
                    f"场次不能从 {session.status.value} 变为 {target.value}"
                )
            session.status = target
            return session

    # ------------------------------------------------------------------
    # 取消登记
    # ------------------------------------------------------------------

    def register_cancellation(
        self, session_id: str, reason: str, notice_id: str, actor: str
    ) -> CancellationRecord:
        """登记取消原因与原场次；同一场次的重复登记幂等返回原记录。"""
        with self._lock:
            session = self._require_session(session_id)
            existing = self._active_cancellation(session_id)
            if existing is not None:
                return existing
            if session.status not in CANCELLABLE_STATUSES:
                raise InvalidStateError(
                    f"场次状态为 {session.status.value}，不能登记取消"
                )
            record = CancellationRecord(
                cancellation_id=self._next_id("CAN"),
                session_id=session_id,
                reason=reason,
                notice_id=notice_id,
                registered_by=actor,
                registered_at=self._now(),
            )
            self._cancellations[record.cancellation_id] = record
            session.cancelled = True
            session.close_reason = f"取消登记（{record.cancellation_id}）：{reason}"
            schools = set()
            for reservation in self._reservations_of(session_id, ReservationStatus.ACTIVE):
                reservation.status = ReservationStatus.MIGRATABLE
                if self._current_participants(reservation):
                    schools.add(reservation.school)
            self._notify(record.cancellation_id, NotificationKind.CANCELLATION, schools)
            return record

    # ------------------------------------------------------------------
    # 补办候选与确认
    # ------------------------------------------------------------------

    def propose_candidate(
        self, cancellation_id: str, venue_id: str, start_time: datetime
    ) -> MakeupCandidate:
        with self._lock:
            record = self._require_cancellation(cancellation_id)
            if record.status == CancellationStatus.RESTORED:
                raise InvalidStateError("取消单已恢复，不能新增补办候选")
            candidate = MakeupCandidate(
                candidate_id=self._next_id("CND"),
                cancellation_id=cancellation_id,
                venue_id=venue_id,
                start_time=start_time,
                status=CandidateStatus.PENDING,
            )
            self._candidates[candidate.candidate_id] = candidate
            return candidate

    def confirm_makeup(
        self, cancellation_id: str, candidate_id: str, *, idempotency_key: str = ""
    ) -> MakeupLink:
        """确认补办：原子转移参与者与有限资源。

        资源在目标时段不足时整体失败；相同幂等键或相同候选的重复确认
        直接返回既有补办关系；已补办后确认其他候选需走改期接口。
        """
        with self._lock:
            record = self._require_cancellation(cancellation_id)
            if record.status == CancellationStatus.RESTORED:
                raise InvalidStateError("取消单已恢复，不能确认补办")
            hit = self._link_by_key(idempotency_key)
            if hit is not None:
                return hit
            candidate = self._require_candidate(candidate_id)
            if candidate.cancellation_id != cancellation_id:
                raise InvalidStateError("候选不属于该取消单")
            prior = self._link_of_candidate(candidate_id)
            if prior is not None:
                return prior
            if record.status == CancellationStatus.FULFILLED:
                raise AlreadyConfirmedError("补办已确认；如需变更请使用改期接口")
            if candidate.status != CandidateStatus.PENDING:
                raise InvalidStateError(
                    f"候选状态为 {candidate.status.value}，不能确认"
                )
            original = self._require_session(record.session_id)
            start, end = self._makeup_window(original, candidate)
            plan = self._plan_transfer(
                original.session_id, ReservationStatus.MIGRATABLE, start, end
            )
            makeup = self._new_makeup_session(original, candidate, version=1)
            moved, count, schools = self._move(original, makeup, plan)
            candidate.status = CandidateStatus.CONFIRMED
            candidate.makeup_session_id = makeup.session_id
            for other in self._candidates_of(cancellation_id):
                if other.candidate_id != candidate_id and other.status == CandidateStatus.PENDING:
                    other.status = CandidateStatus.REJECTED
            link = self._record_link(
                record=record,
                candidate=candidate,
                previous_session_id=original.session_id,
                makeup_session_id=makeup.session_id,
                version=1,
                supersedes=None,
                idempotency_key=idempotency_key,
                moved=moved,
                allocations=plan.allocations,
                participant_count=count,
            )
            record.status = CancellationStatus.FULFILLED
            self._notify(cancellation_id, NotificationKind.MAKEUP_CONFIRMED, schools)
            return link

    def reschedule_makeup(
        self, cancellation_id: str, candidate_id: str, *, idempotency_key: str = ""
    ) -> MakeupLink:
        """多次改期：生成版本 +1 的补办关系，参与者与资源整体迁往新场次。"""
        with self._lock:
            record = self._require_cancellation(cancellation_id)
            if record.status != CancellationStatus.FULFILLED:
                raise InvalidStateError("仅已补办的取消单可以改期")
            hit = self._link_by_key(idempotency_key)
            if hit is not None:
                return hit
            candidate = self._require_candidate(candidate_id)
            if candidate.cancellation_id != cancellation_id:
                raise InvalidStateError("候选不属于该取消单")
            current = self._active_link(cancellation_id)
            if current is None:
                raise InvalidStateError("取消单没有生效中的补办关系")
            if current.candidate_id == candidate_id:
                return current
            if candidate.status not in (CandidateStatus.PENDING, CandidateStatus.REJECTED):
                raise InvalidStateError(
                    f"候选状态为 {candidate.status.value}，不能用于改期"
                )
            original = self._require_session(record.session_id)
            current_makeup = self._require_session(current.makeup_session_id)
            if current_makeup.status in LOCKED_STATUSES:
                raise InvalidStateError("补办场次已开始，不能改期")
            start, end = self._makeup_window(original, candidate)
            plan = self._plan_transfer(
                current_makeup.session_id, ReservationStatus.ACTIVE, start, end
            )
            version = current.version + 1
            makeup = self._new_makeup_session(original, candidate, version=version)
            moved, count, schools = self._move(current_makeup, makeup, plan)
            current_makeup.cancelled = True
            current_makeup.close_reason = f"被改期版本 v{version} 取代"
            self._candidates[current.candidate_id].status = CandidateStatus.SUPERSEDED
            candidate.status = CandidateStatus.CONFIRMED
            candidate.makeup_session_id = makeup.session_id
            current.status = LinkStatus.SUPERSEDED
            link = self._record_link(
                record=record,
                candidate=candidate,
                previous_session_id=current_makeup.session_id,
                makeup_session_id=makeup.session_id,
                version=version,
                supersedes=current.link_id,
                idempotency_key=idempotency_key,
                moved=moved,
                allocations=plan.allocations,
                participant_count=count,
            )
            self._notify(cancellation_id, NotificationKind.RESCHEDULED, schools)
            return link

    # ------------------------------------------------------------------
    # 原场次恢复
    # ------------------------------------------------------------------

    def restore_original_session(self, cancellation_id: str) -> Session:
        """恢复原场次：未补办直接恢复；已补办且未开始则整体迁回。"""
        with self._lock:
            record = self._require_cancellation(cancellation_id)
            original = self._require_session(record.session_id)
            if record.status == CancellationStatus.RESTORED:
                return original
            if record.status == CancellationStatus.OPEN:
                schools = set()
                for reservation in self._reservations_of(
                    original.session_id, ReservationStatus.MIGRATABLE
                ):
                    reservation.status = ReservationStatus.ACTIVE
                    if self._current_participants(reservation):
                        schools.add(reservation.school)
            else:
                link = self._active_link(cancellation_id)
                if link is None:
                    raise InvalidStateError("取消单没有生效中的补办关系")
                makeup = self._require_session(link.makeup_session_id)
                if makeup.status in LOCKED_STATUSES:
                    raise RestorationClosedError("补办场次已开始，原场次恢复窗口已关闭")
                start, end = self._window(original)
                plan = self._plan_transfer(
                    makeup.session_id, ReservationStatus.ACTIVE, start, end
                )
                _, _, schools = self._move(makeup, original, plan)
                makeup.cancelled = True
                makeup.close_reason = "原场次恢复，补办撤销"
                link.status = LinkStatus.REVOKED
            original.cancelled = False
            original.close_reason = ""
            for candidate in self._candidates_of(cancellation_id):
                if candidate.status != CandidateStatus.VOID:
                    candidate.status = CandidateStatus.VOID
            record.status = CancellationStatus.RESTORED
            self._notify(cancellation_id, NotificationKind.RESTORED, schools)
            return original

    # ------------------------------------------------------------------
    # 部分学生退出
    # ------------------------------------------------------------------

    def withdraw_participant(
        self, participant_id: str, reason: str = "", *, session_id: str | None = None
    ) -> Withdrawal:
        """退出即终态：从当前有效/待迁移预约中退出，迁移时自动排除。"""
        with self._lock:
            self._require_participant(participant_id)
            booked = [
                r
                for r in self._reservations.values()
                if participant_id in r.participant_ids
            ]
            if not booked:
                raise NotFoundError(f"参与者没有任何预约：{participant_id}")
            open_reservations = [
                r
                for r in booked
                if r.status in (ReservationStatus.ACTIVE, ReservationStatus.MIGRATABLE)
                and (r.reservation_id, participant_id) not in self._withdrawals
            ]
            if session_id is not None:
                open_reservations = [
                    r for r in open_reservations if r.session_id == session_id
                ]
            if not open_reservations:
                raise InvalidStateError("参与者已退出或无可退出的预约")
            if len(open_reservations) > 1:
                raise InvalidStateError("参与者在多个场次有待处理预约，请指定 session_id")
            reservation = sorted(open_reservations, key=lambda r: r.reservation_id)[0]
            session = self._require_session(reservation.session_id)
            if session.status in LOCKED_STATUSES:
                raise InvalidStateError("场次已开始，不能退出")
            withdrawal = Withdrawal(
                withdrawal_id=self._next_id("WDL"),
                participant_id=participant_id,
                reservation_id=reservation.reservation_id,
                session_id=session.session_id,
                reason=reason,
                created_at=self._now(),
            )
            self._withdrawals[(reservation.reservation_id, participant_id)] = withdrawal
            return withdrawal

    # ------------------------------------------------------------------
    # 通知
    # ------------------------------------------------------------------

    def dispatch_notifications(self) -> int:
        """发送全部待发送通知，返回发送数量（确定性：全部成功）。"""
        with self._lock:
            pending = sorted(
                (n for n in self._notifications.values() if n.status == NotificationStatus.PENDING),
                key=lambda n: n.notification_id,
            )
            for notification in pending:
                notification.status = NotificationStatus.SENT
                notification.sent_at = self._now()
            return len(pending)

    # ------------------------------------------------------------------
    # 查询与报表
    # ------------------------------------------------------------------

    def get_session(self, session_id: str) -> Session:
        with self._lock:
            return self._require_session(session_id)

    def list_sessions(self) -> list[Session]:
        with self._lock:
            return [self._sessions[sid] for sid in sorted(self._sessions)]

    def get_reservation(self, reservation_id: str) -> Reservation:
        with self._lock:
            try:
                return self._reservations[reservation_id]
            except KeyError:
                raise NotFoundError(f"预约不存在：{reservation_id}") from None

    def list_reservations(self, session_id: str) -> list[Reservation]:
        with self._lock:
            self._require_session(session_id)
            return sorted(
                (r for r in self._reservations.values() if r.session_id == session_id),
                key=lambda r: r.reservation_id,
            )

    def list_allocations(self, session_id: str | None = None) -> list[Allocation]:
        with self._lock:
            allocations = sorted(
                self._allocations.values(), key=lambda a: a.allocation_id
            )
            if session_id is not None:
                allocations = [a for a in allocations if a.session_id == session_id]
            return allocations

    def get_cancellation(self, cancellation_id: str) -> CancellationRecord:
        with self._lock:
            return self._require_cancellation(cancellation_id)

    def list_candidates(self, cancellation_id: str) -> list[MakeupCandidate]:
        with self._lock:
            self._require_cancellation(cancellation_id)
            return self._candidates_of(cancellation_id)

    def list_links(self, cancellation_id: str) -> list[MakeupLink]:
        with self._lock:
            self._require_cancellation(cancellation_id)
            return sorted(
                (l for l in self._links.values() if l.cancellation_id == cancellation_id),
                key=lambda l: l.version,
            )

    def list_migratable_reservations(self, cancellation_id: str) -> list[Reservation]:
        with self._lock:
            record = self._require_cancellation(cancellation_id)
            return self._reservations_of(record.session_id, ReservationStatus.MIGRATABLE)

    def list_notifications(self, cancellation_id: str | None = None) -> list[Notification]:
        with self._lock:
            notifications = sorted(
                self._notifications.values(), key=lambda n: n.notification_id
            )
            if cancellation_id is not None:
                notifications = [
                    n for n in notifications if n.cancellation_id == cancellation_id
                ]
            return notifications

    def cancellation_detail(self, cancellation_id: str) -> dict:
        """取消单全景：原因、原场次、可迁移预约、补办候选、补办关系与通知状态。"""
        with self._lock:
            record = self._require_cancellation(cancellation_id)
            original = self._require_session(record.session_id)
            migratable = [
                {
                    "reservation_id": r.reservation_id,
                    "school": r.school,
                    "status": r.status.value,
                    "participants": self._current_participants(r),
                }
                for r in self._reservations_of(
                    original.session_id, ReservationStatus.MIGRATABLE
                )
            ]
            return {
                "cancellation": record.to_dict(),
                "original_session": original.to_dict(),
                "migratable_reservations": migratable,
                "candidates": [c.to_dict() for c in self._candidates_of(cancellation_id)],
                "links": [l.to_dict() for l in self.list_links(cancellation_id)],
                "notifications": [
                    n.to_dict() for n in self.list_notifications(cancellation_id)
                ],
            }

    def participant_attribution(self, participant_id: str | None = None) -> dict:
        """每个参与者的最终归属：已归属 / 待安置 / 已退出，含迁移链。"""
        with self._lock:
            if participant_id is not None:
                targets = [self._require_participant(participant_id)]
            else:
                targets = [self._participants[pid] for pid in sorted(self._participants)]
            items: list[dict] = []
            for participant in targets:
                items.extend(self._attribution_rows(participant))
            return {
                "generated_at": self._now().isoformat(timespec="seconds"),
                "items": items,
            }

    def completion_report(self) -> dict:
        """完成量报表：按补办关系串链去重，每条链只统计最终生效场次。"""
        with self._lock:
            chains: list[dict] = []
            chained_sessions: set[str] = set()
            for cancellation_id in sorted(self._cancellations):
                record = self._cancellations[cancellation_id]
                links = sorted(
                    (l for l in self._links.values() if l.cancellation_id == cancellation_id),
                    key=lambda l: l.version,
                )
                session_ids = [record.session_id] + [l.makeup_session_id for l in links]
                chained_sessions.update(session_ids)
                if record.status == CancellationStatus.RESTORED:
                    final_id: str | None = record.session_id
                elif record.status == CancellationStatus.FULFILLED:
                    active = [l for l in links if l.status == LinkStatus.ACTIVE]
                    final_id = active[-1].makeup_session_id if active else None
                else:
                    final_id = None
                chains.append(
                    self._chain_row(
                        cancellation_id, record.session_id, final_id, session_ids, len(links)
                    )
                )
            for session_id in sorted(self._sessions):
                if session_id not in chained_sessions:
                    chains.append(
                        self._chain_row(session_id, session_id, session_id, [session_id], 0)
                    )
            chains.sort(key=lambda row: row["chain_id"])
            totals = {
                OUTCOME_COMPLETED: 0,
                OUTCOME_IN_PROGRESS: 0,
                OUTCOME_NOT_STARTED: 0,
                OUTCOME_CANCELLED: 0,
            }
            for row in chains:
                totals[row["outcome"]] += 1
            return {
                "generated_at": self._now().isoformat(timespec="seconds"),
                "totals": {"chains": len(chains), **totals},
                "chains": chains,
            }

    # ------------------------------------------------------------------
    # 内部：原子转移
    # ------------------------------------------------------------------

    def _plan_transfer(
        self,
        from_session_id: str,
        reservation_status: ReservationStatus,
        start: datetime,
        end: datetime,
    ) -> _TransferPlan:
        """转移预检：收集待迁预约与资源占用，并校验目标时段资源余量。"""
        reservations = self._reservations_of(from_session_id, reservation_status)
        members = {r.reservation_id: self._current_participants(r) for r in reservations}
        allocations = sorted(
            (
                a
                for a in self._allocations.values()
                if a.session_id == from_session_id and a.active
            ),
            key=lambda a: a.allocation_id,
        )
        for allocation in allocations:
            resource = self._resources[allocation.resource_id]
            if not self._resource_available(
                resource, from_session_id, start, end, allocation.quantity
            ):
                raise ResourceConflictError(
                    f"资源 {resource.name} 在目标时段余量不足：需要 {allocation.quantity}"
                )
        return _TransferPlan(reservations, members, allocations)

    def _move(
        self, from_session: Session, to_session: Session, plan: _TransferPlan
    ) -> tuple[list[tuple[str, str]], int, set[str]]:
        """应用转移：预约逐条迁移、资源占用改挂新场次。预检通过后不会失败。"""
        moved: list[tuple[str, str]] = []
        count = 0
        schools: set[str] = set()
        for reservation in plan.reservations:
            members = plan.members[reservation.reservation_id]
            reservation.status = ReservationStatus.MIGRATED
            if not members:
                continue
            successor = Reservation(
                reservation_id=self._next_id("RSV"),
                session_id=to_session.session_id,
                school=reservation.school,
                participant_ids=tuple(members),
                status=ReservationStatus.ACTIVE,
                previous_reservation_id=reservation.reservation_id,
                created_at=self._now(),
            )
            self._reservations[successor.reservation_id] = successor
            moved.append((reservation.reservation_id, successor.reservation_id))
            count += len(members)
            schools.add(reservation.school)
        for allocation in plan.allocations:
            allocation.session_id = to_session.session_id
        return moved, count, schools

    # ------------------------------------------------------------------
    # 内部：领域辅助
    # ------------------------------------------------------------------

    def _next_id(self, prefix: str) -> str:
        self._seq[prefix] = self._seq.get(prefix, 0) + 1
        return f"{prefix}-{self._seq[prefix]:04d}"

    def _window(self, session: Session) -> tuple[datetime, datetime]:
        return (
            session.start_time,
            session.start_time + timedelta(minutes=session.duration_minutes),
        )

    def _makeup_window(
        self, original: Session, candidate: MakeupCandidate
    ) -> tuple[datetime, datetime]:
        return (
            candidate.start_time,
            candidate.start_time + timedelta(minutes=original.duration_minutes),
        )

    def _resource_available(
        self,
        resource: Resource,
        exclude_session_id: str | None,
        start: datetime,
        end: datetime,
        quantity: int,
    ) -> bool:
        """目标时段内资源余量是否足够；已取消场次的占用视为待转移不计入。"""
        used = 0
        for allocation in self._allocations.values():
            if (
                not allocation.active
                or allocation.resource_id != resource.resource_id
                or allocation.session_id == exclude_session_id
            ):
                continue
            other = self._sessions[allocation.session_id]
            if other.cancelled:
                continue
            other_start, other_end = self._window(other)
            if other_start < end and start < other_end:
                used += allocation.quantity
        return resource.capacity - used >= quantity

    def _current_participants(self, reservation: Reservation) -> list[str]:
        """预约当前在册参与者：登记名单减去已退出。"""
        return [
            pid
            for pid in reservation.participant_ids
            if (reservation.reservation_id, pid) not in self._withdrawals
        ]

    def _reservations_of(
        self, session_id: str, status: ReservationStatus
    ) -> list[Reservation]:
        return sorted(
            (
                r
                for r in self._reservations.values()
                if r.session_id == session_id and r.status == status
            ),
            key=lambda r: r.reservation_id,
        )

    def _candidates_of(self, cancellation_id: str) -> list[MakeupCandidate]:
        return sorted(
            (c for c in self._candidates.values() if c.cancellation_id == cancellation_id),
            key=lambda c: c.candidate_id,
        )

    def _active_cancellation(self, session_id: str) -> CancellationRecord | None:
        active = [
            record
            for record in self._cancellations.values()
            if record.session_id == session_id
            and record.status != CancellationStatus.RESTORED
        ]
        return sorted(active, key=lambda r: r.cancellation_id)[-1] if active else None

    def _active_link(self, cancellation_id: str) -> MakeupLink | None:
        active = [
            link
            for link in self._links.values()
            if link.cancellation_id == cancellation_id and link.status == LinkStatus.ACTIVE
        ]
        return active[-1] if active else None

    def _link_of_candidate(self, candidate_id: str) -> MakeupLink | None:
        for link in self._links.values():
            if link.candidate_id == candidate_id:
                return link
        return None

    def _link_by_key(self, idempotency_key: str) -> MakeupLink | None:
        if not idempotency_key:
            return None
        for link in self._links.values():
            if link.idempotency_key == idempotency_key:
                return link
        return None

    def _new_makeup_session(
        self, original: Session, candidate: MakeupCandidate, *, version: int
    ) -> Session:
        makeup = Session(
            session_id=self._next_id("SES"),
            title=f"{original.title}（补办 v{version}）",
            school=original.school,
            venue_id=candidate.venue_id,
            start_time=candidate.start_time,
            status=SessionStatus.SCHEDULED,
            duration_minutes=original.duration_minutes,
        )
        self._sessions[makeup.session_id] = makeup
        return makeup

    def _record_link(
        self,
        *,
        record: CancellationRecord,
        candidate: MakeupCandidate,
        previous_session_id: str,
        makeup_session_id: str,
        version: int,
        supersedes: str | None,
        idempotency_key: str,
        moved: list[tuple[str, str]],
        allocations: list[Allocation],
        participant_count: int,
    ) -> MakeupLink:
        link = MakeupLink(
            link_id=self._next_id("LNK"),
            cancellation_id=record.cancellation_id,
            candidate_id=candidate.candidate_id,
            original_session_id=record.session_id,
            previous_session_id=previous_session_id,
            makeup_session_id=makeup_session_id,
            version=version,
            status=LinkStatus.ACTIVE,
            idempotency_key=idempotency_key,
            created_at=self._now(),
            supersedes=supersedes,
            moved_reservations=tuple(moved),
            moved_allocations=tuple(a.allocation_id for a in allocations),
            participant_count=participant_count,
        )
        self._links[link.link_id] = link
        return link

    def _notify(
        self, cancellation_id: str, kind: NotificationKind, schools: set[str]
    ) -> None:
        for school in sorted(schools):
            notification = Notification(
                notification_id=self._next_id("NTF"),
                cancellation_id=cancellation_id,
                school=school,
                kind=kind,
                status=NotificationStatus.PENDING,
                created_at=self._now(),
            )
            self._notifications[notification.notification_id] = notification

    def _chain_row(
        self,
        chain_id: str,
        original_session_id: str,
        final_session_id: str | None,
        session_ids: list[str],
        versions: int,
    ) -> dict:
        if final_session_id is None:
            outcome = OUTCOME_CANCELLED
            participant_count = 0
        else:
            session = self._sessions[final_session_id]
            if session.status == SessionStatus.SETTLED:
                outcome = OUTCOME_COMPLETED
            elif session.status == SessionStatus.IN_PROGRESS:
                outcome = OUTCOME_IN_PROGRESS
            else:
                outcome = OUTCOME_NOT_STARTED
            participant_count = sum(
                len(self._current_participants(r))
                for r in self._reservations_of(final_session_id, ReservationStatus.ACTIVE)
            )
        return {
            "chain_id": chain_id,
            "original_session_id": original_session_id,
            "final_session_id": final_session_id,
            "session_ids": list(session_ids),
            "versions": versions,
            "outcome": outcome,
            "participant_count": participant_count,
        }

    def _attribution_rows(self, participant: Participant) -> list[dict]:
        containing = [
            r
            for r in self._reservations.values()
            if participant.participant_id in r.participant_ids
        ]
        lineages: dict[str, list[Reservation]] = {}
        for reservation in containing:
            lineages.setdefault(self._lineage_root(reservation), []).append(reservation)
        rows = []
        for root_id in sorted(lineages):
            lineage = sorted(lineages[root_id], key=lambda r: r.reservation_id)
            latest = lineage[-1]
            row = {
                "participant_id": participant.participant_id,
                "name": participant.name,
                "school": participant.school,
                "chain": [
                    {"reservation_id": r.reservation_id, "session_id": r.session_id}
                    for r in lineage
                ],
            }
            if (latest.reservation_id, participant.participant_id) in self._withdrawals:
                row.update(state="已退出", session_id=None, session_status=None)
            elif latest.status == ReservationStatus.ACTIVE:
                session = self._sessions[latest.session_id]
                row.update(
                    state="已归属",
                    session_id=session.session_id,
                    session_status=session.status.value,
                )
            else:
                row.update(state="待安置", session_id=None, session_status=None)
            rows.append(row)
        return rows

    def _lineage_root(self, reservation: Reservation) -> str:
        current = reservation
        while current.previous_reservation_id is not None:
            current = self._reservations[current.previous_reservation_id]
        return current.reservation_id

    # ------------------------------------------------------------------
    # 内部：存在性校验
    # ------------------------------------------------------------------

    def _require_session(self, session_id: str) -> Session:
        try:
            return self._sessions[session_id]
        except KeyError:
            raise NotFoundError(f"场次不存在：{session_id}") from None

    def _require_participant(self, participant_id: str) -> Participant:
        try:
            return self._participants[participant_id]
        except KeyError:
            raise NotFoundError(f"参与者不存在：{participant_id}") from None

    def _require_resource(self, resource_id: str) -> Resource:
        try:
            return self._resources[resource_id]
        except KeyError:
            raise NotFoundError(f"资源不存在：{resource_id}") from None

    def _require_cancellation(self, cancellation_id: str) -> CancellationRecord:
        try:
            return self._cancellations[cancellation_id]
        except KeyError:
            raise NotFoundError(f"取消单不存在：{cancellation_id}") from None

    def _require_candidate(self, candidate_id: str) -> MakeupCandidate:
        try:
            return self._candidates[candidate_id]
        except KeyError:
            raise NotFoundError(f"补办候选不存在：{candidate_id}") from None
