"""领域服务：取消登记、补办确认、退出/改期/恢复、通知与去重报表。

所有写操作都在单个 ``store.transaction()`` 中完成，校验失败整体回滚。

确定性规则汇总：

1. 登记取消：原场次进入 ``cancelled``，资源占用冻结不释放，预约挂上补办链；
   同一活动重复登记直接冲突报错。
2. 补办候选可登记多个；确认时二选一，其余候选作废。
3. 确认补办：原子地建立补办场次、校验容量与有限资源、转移全部在链有效参与者；
   原场次占用同时迁出。参与者不足容量或资源不足时整笔回滚。
4. 重复确认：同一候选重复确认幂等返回；确认另一候选按冲突处理（改期走专用流程）。
5. 学生退出不可逆：退出后任何确认/改期都不再迁移，恢复原场次也不自动回归。
6. 多次改期：每次生成单调递增的候选版本，旧版本与旧补办场次置为被取代，
   参与者与资源整体迁移到新版本。
7. 原场次恢复：仅允许在尚无补办确认（``open``）时执行；已确认后只能改期。
8. 报表：每条补办链至多计一次完成量，口径为“最终归属场次”。
"""
from __future__ import annotations

import uuid
from collections import defaultdict
from typing import Any

from .errors import ConflictError, NotFoundError, ValidationError
from .models import (
    Booking,
    BookingStatus,
    CandidateStatus,
    CancellationRecord,
    CancellationStatus,
    ChainRole,
    Event,
    EventStatus,
    MakeupCandidate,
    NotifyStatus,
    Resource,
    utc_now_iso,
)
from .store import InMemoryStore


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class Services:
    def __init__(self, store: InMemoryStore | None = None) -> None:
        self.store = store or InMemoryStore()

    # =====================================================================
    # 基础数据：资源、场次、预约
    # =====================================================================
    def create_resource(self, resource_id: str | None, name: str, capacity: int) -> dict[str, Any]:
        if not name:
            raise ValidationError("资源名称不能为空")
        if not isinstance(capacity, int) or capacity <= 0:
            raise ValidationError("资源容量必须为正整数")
        resource_id = resource_id or _new_id("res")
        if resource_id in self.store.resources:
            raise ConflictError(f"资源已存在：{resource_id}")
        resource = Resource(id=resource_id, name=name, capacity=capacity)
        self.store.add_resource(resource)
        return resource.to_dict()

    def create_event(
        self,
        event_id: str | None,
        title: str,
        start_time: str,
        capacity: int,
        resource_plan: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        if not title or not start_time:
            raise ValidationError("场次标题与开始时间不能为空")
        if not isinstance(capacity, int) or capacity <= 0:
            raise ValidationError("场次容量必须为正整数")
        resource_plan = self._validate_resource_plan(resource_plan or {})
        event_id = event_id or _new_id("evt")
        with self.store.transaction():
            if event_id in self.store.events:
                raise ConflictError(f"场次已存在：{event_id}")
            # 同一时间槽内有限资源容量校验。
            self._assert_resources_fit(start_time, resource_plan, exclude=set())
            event = Event(
                id=event_id,
                title=title,
                start_time=start_time,
                capacity=capacity,
                resource_plan=resource_plan,
            )
            self.store.add_event(event)
            return event.to_dict()

    def create_booking(
        self,
        event_id: str,
        student_id: str,
        student_name: str,
        contact: str,
        booking_id: str | None = None,
        checked_in: bool = False,
    ) -> dict[str, Any]:
        if not student_id:
            raise ValidationError("学生标识不能为空")
        with self.store.transaction():
            event = self._require_event(event_id)
            if not event.accepts_bookings:
                raise ConflictError(f"场次当前不可预约：{event_id}（{event.status.value}）")
            # 已恢复的旧链视为终结：恢复后的新报名不再挂入旧链。
            chain_id = event.chain_id
            if chain_id is not None:
                record = self.store.cancellations.get(chain_id)
                if record is not None and record.status is CancellationStatus.RESTORED:
                    chain_id = None
            if chain_id is not None:
                self._assert_student_not_in_chain(student_id, chain_id)
            else:
                self._assert_student_not_in_event(student_id, event_id)
            active = self.store.active_bookings_of_event(event_id)
            if len(active) >= event.capacity:
                raise ConflictError(f"场次容量已满：{event_id}")
            booking = Booking(
                id=booking_id or _new_id("bk"),
                student_id=student_id,
                student_name=student_name,
                contact=contact,
                event_id=event_id,
                checked_in=checked_in,
                chain_id=chain_id,
            )
            booking.history.append(
                {"at": utc_now_iso(), "action": "create", "event_id": event_id}
            )
            self.store.add_booking(booking)
            return booking.to_dict()

    def check_in(self, booking_id: str) -> dict[str, Any]:
        with self.store.transaction():
            booking = self._require_booking(booking_id)
            if booking.status is not BookingStatus.ACTIVE:
                raise ConflictError("仅有效预约可以签到")
            booking.checked_in = True
            booking.history.append({"at": utc_now_iso(), "action": "check_in"})
            return booking.to_dict()

    def complete_event(self, event_id: str) -> dict[str, Any]:
        with self.store.transaction():
            event = self._require_event(event_id)
            if event.status is not EventStatus.SCHEDULED:
                raise ConflictError(f"仅已排定场次可以结算：当前 {event.status.value}")
            event.status = EventStatus.COMPLETED
            return event.to_dict()

    # =====================================================================
    # 规则一：登记取消
    # =====================================================================
    def register_cancellation(
        self, event_id: str, reason_code: str, reason_note: str = ""
    ) -> dict[str, Any]:
        if not reason_code:
            raise ValidationError("取消原因编码不能为空")
        with self.store.transaction():
            origin = self._require_event(event_id)
            if origin.chain_role is ChainRole.MAKEUP:
                raise ConflictError("补办场次不能作为新的原场次登记取消，请使用改期流程")
            if origin.chain_id is not None:
                prior = self.store.cancellations.get(origin.chain_id)
                if prior is not None and prior.status is CancellationStatus.RESTORED:
                    raise ConflictError("该场次的取消登记已随恢复终结，不能再次登记")
            if origin.status is EventStatus.CANCELLED:
                raise ConflictError(f"场次已登记取消，不能重复登记：{event_id}")
            if origin.status is EventStatus.COMPLETED:
                raise ConflictError("已结算场次不能登记取消")
            record = CancellationRecord(
                id=_new_id("cn"),
                origin_event_id=event_id,
                reason_code=reason_code,
                reason_note=reason_note,
            )
            origin.status = EventStatus.CANCELLED
            origin.chain_id = record.id
            origin.chain_role = ChainRole.ORIGIN
            # 预约挂到补办链上；占用“冻结”在原场次，不释放。
            for booking in self.store.bookings_of_event(event_id):
                booking.chain_id = record.id
                booking.history.append(
                    {"at": utc_now_iso(), "action": "cancel_registered", "event_id": event_id}
                )
            self.store.add_cancellation(record)
            return self._cancellation_view(record)

    # =====================================================================
    # 规则二：补办候选
    # =====================================================================
    def add_candidate(
        self,
        cancellation_id: str,
        scheduled_start: str,
        capacity: int,
        resource_plan: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        if not scheduled_start:
            raise ValidationError("补办开始时间不能为空")
        if not isinstance(capacity, int) or capacity <= 0:
            raise ValidationError("补办容量必须为正整数")
        resource_plan = self._validate_resource_plan(resource_plan or {})
        with self.store.transaction():
            record = self._require_cancellation(cancellation_id)
            if record.status is CancellationStatus.RESTORED:
                raise ConflictError("登记已随原场次恢复作废，不能再添加候选")
            if record.status is CancellationStatus.CONFIRMED:
                raise ConflictError("补办已确认，调整时间请使用改期接口")
            version = len(record.candidates) + 1
            candidate = MakeupCandidate(
                id=_new_id("cd"),
                version=version,
                scheduled_start=scheduled_start,
                capacity=capacity,
                resource_plan=resource_plan,
            )
            record.candidates.append(candidate)
            return candidate.to_dict()

    # =====================================================================
    # 规则三 + 规则四：确认补办（原子转移）与重复确认
    # =====================================================================
    def confirm_makeup(
        self, cancellation_id: str, candidate_id: str, makeup_event_id: str | None = None
    ) -> dict[str, Any]:
        with self.store.transaction():
            record = self._require_cancellation(cancellation_id)
            candidate = self._require_candidate(record, candidate_id)

            if record.status is CancellationStatus.CONFIRMED:
                # 规则四：同一候选重复确认幂等；换候选属冲突。
                selected = self._selected_candidate(record)
                if selected is not None and selected.id == candidate_id:
                    view = self._cancellation_view(record)
                    view["idempotent_replay"] = True
                    return view
                raise ConflictError("补办已确认到其他候选，变更请使用改期接口")
            if record.status is CancellationStatus.RESTORED:
                raise ConflictError("原场次已恢复，不能确认补办")
            if candidate.status is not CandidateStatus.PENDING:
                raise ConflictError(f"候选状态不可确认：{candidate.status.value}")

            origin = self._require_event(record.origin_event_id)
            eligible = [
                b for b in self.store.bookings_of_chain(record.id)
                if b.status is BookingStatus.ACTIVE and b.event_id == origin.id
            ]
            if len(eligible) > candidate.capacity:
                raise ConflictError(
                    f"补办容量不足：需接纳 {len(eligible)} 人，候选容量 {candidate.capacity}"
                )

            makeup = Event(
                id=makeup_event_id or _new_id("evt"),
                title=f"{origin.title}（补办）",
                start_time=candidate.scheduled_start,
                capacity=candidate.capacity,
                resource_plan=candidate.resource_plan,
                chain_id=record.id,
                chain_role=ChainRole.MAKEUP,
            )
            if makeup.id in self.store.events:
                raise ConflictError(f"补办场次编号已存在：{makeup.id}")
            # 原场次冻结占用即将迁出，因此在新时间槽不计入原场次。
            self._assert_resources_fit(
                candidate.scheduled_start,
                candidate.resource_plan,
                exclude={origin.id},
            )

            # ---- 全部校验通过，以下为提交阶段 ----
            self.store.add_event(makeup)
            origin.replaced_by = makeup.id
            for other in record.candidates:
                if other.id == candidate.id:
                    other.status = CandidateStatus.SELECTED
                elif other.status is CandidateStatus.PENDING:
                    other.status = CandidateStatus.SUPERSEDED
            now = utc_now_iso()
            for booking in eligible:
                booking.event_id = makeup.id
                booking.history.append(
                    {
                        "at": now,
                        "action": "migrate",
                        "reason": "makeup_confirm",
                        "from_event_id": origin.id,
                        "to_event_id": makeup.id,
                        "candidate_version": candidate.version,
                    }
                )
            record.status = CancellationStatus.CONFIRMED
            record.current_makeup_event_id = makeup.id
            record.confirmations.append(
                {
                    "at": now,
                    "type": "initial",
                    "candidate_id": candidate.id,
                    "candidate_version": candidate.version,
                    "makeup_event_id": makeup.id,
                    "transferred": len(eligible),
                }
            )
            record.notifications["makeup"].append(
                {"at": now, "status": NotifyStatus.PENDING.value, "event_version": candidate.version}
            )
            return self._cancellation_view(record)

    # =====================================================================
    # 规则五：学生退出（不可逆）
    # =====================================================================
    def withdraw_participant(self, booking_id: str, reason: str = "") -> dict[str, Any]:
        with self.store.transaction():
            booking = self._require_booking(booking_id)
            if booking.status is BookingStatus.WITHDRAWN:
                # 重复退出幂等。
                return booking.to_dict()
            booking.status = BookingStatus.WITHDRAWN
            booking.checked_in = False
            booking.history.append(
                {"at": utc_now_iso(), "action": "withdraw", "reason": reason}
            )
            return booking.to_dict()

    # =====================================================================
    # 规则六：多次改期（新版本 + 原子转移）
    # =====================================================================
    def reschedule_makeup(
        self,
        cancellation_id: str,
        scheduled_start: str,
        capacity: int,
        resource_plan: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        if not scheduled_start:
            raise ValidationError("改期开始时间不能为空")
        if not isinstance(capacity, int) or capacity <= 0:
            raise ValidationError("改期容量必须为正整数")
        resource_plan = self._validate_resource_plan(resource_plan or {})
        with self.store.transaction():
            record = self._require_cancellation(cancellation_id)
            if record.status is not CancellationStatus.CONFIRMED:
                raise ConflictError("仅已确认补办可以改期")
            old_makeup = self._require_event(record.current_makeup_event_id)
            if old_makeup.replaced_by is not None:
                raise ConflictError("当前补办场次已被改期取代")

            version = len(record.candidates) + 1
            candidate = MakeupCandidate(
                id=_new_id("cd"),
                version=version,
                scheduled_start=scheduled_start,
                capacity=capacity,
                resource_plan=resource_plan,
                status=CandidateStatus.SELECTED,
            )
            eligible = [
                b
                for b in self.store.bookings_of_chain(record.id)
                if b.status is BookingStatus.ACTIVE and b.event_id == old_makeup.id
            ]
            if len(eligible) > capacity:
                raise ConflictError(
                    f"改期后容量不足：需接纳 {len(eligible)} 人，新容量 {capacity}"
                )
            new_makeup = Event(
                id=_new_id("evt"),
                title=old_makeup.title,
                start_time=scheduled_start,
                capacity=capacity,
                resource_plan=resource_plan,
                chain_id=record.id,
                chain_role=ChainRole.MAKEUP,
            )
            # 旧补办占用将迁出，不计入新时间槽。
            self._assert_resources_fit(scheduled_start, resource_plan, exclude={old_makeup.id})

            # ---- 提交 ----
            self.store.add_event(new_makeup)
            old_makeup.status = EventStatus.CANCELLED
            old_makeup.replaced_by = new_makeup.id
            selected = self._selected_candidate(record)
            if selected is not None:
                selected.status = CandidateStatus.SUPERSEDED
            record.candidates.append(candidate)
            now = utc_now_iso()
            for booking in eligible:
                booking.event_id = new_makeup.id
                booking.history.append(
                    {
                        "at": now,
                        "action": "migrate",
                        "reason": "reschedule",
                        "from_event_id": old_makeup.id,
                        "to_event_id": new_makeup.id,
                        "candidate_version": version,
                    }
                )
            record.current_makeup_event_id = new_makeup.id
            record.reschedule_count += 1
            record.confirmations.append(
                {
                    "at": now,
                    "type": "reschedule",
                    "candidate_id": candidate.id,
                    "candidate_version": version,
                    "makeup_event_id": new_makeup.id,
                    "transferred": len(eligible),
                }
            )
            record.notifications["makeup"].append(
                {"at": now, "status": NotifyStatus.PENDING.value, "event_version": version}
            )
            return self._cancellation_view(record)

    # =====================================================================
    # 规则七：原场次恢复
    # =====================================================================
    def restore_origin(self, cancellation_id: str) -> dict[str, Any]:
        with self.store.transaction():
            record = self._require_cancellation(cancellation_id)
            if record.status is CancellationStatus.RESTORED:
                raise ConflictError("原场次已恢复，不能重复恢复")
            if record.status is CancellationStatus.CONFIRMED:
                raise ConflictError("补办已确认，原场次不可恢复；请使用改期或另行取消")
            origin = self._require_event(record.origin_event_id)
            origin.status = EventStatus.SCHEDULED
            for candidate in record.candidates:
                if candidate.status is CandidateStatus.PENDING:
                    candidate.status = CandidateStatus.VOID
            record.status = CancellationStatus.RESTORED
            now = utc_now_iso()
            record.notifications.setdefault("restore", []).append(
                {"at": now, "status": NotifyStatus.PENDING.value}
            )
            # 有效预约留在原场次（本就未迁出）；已退出学生不自动回归。
            for booking in self.store.bookings_of_chain(record.id):
                if booking.status is BookingStatus.ACTIVE:
                    booking.history.append(
                        {"at": now, "action": "origin_restored", "event_id": origin.id}
                    )
            return self._cancellation_view(record)

    # =====================================================================
    # 通知状态
    # =====================================================================
    def update_notification(
        self, cancellation_id: str, kind: str, status: str
    ) -> dict[str, Any]:
        if kind not in ("cancel", "makeup", "restore"):
            raise ValidationError("通知类型必须是 cancel、makeup 或 restore")
        try:
            new_status = NotifyStatus(status)
        except ValueError as exc:
            raise ValidationError(f"未知通知状态：{status}") from exc
        with self.store.transaction():
            record = self._require_cancellation(cancellation_id)
            attempts = record.notifications.setdefault(kind, [])
            if not attempts:
                raise ConflictError(f"该登记尚无 {kind} 通知可更新")
            attempts.append({"at": utc_now_iso(), "status": new_status.value})
            return self._cancellation_view(record)

    # =====================================================================
    # 查询：登记详情（含可迁移预约）
    # =====================================================================
    def get_cancellation(self, cancellation_id: str) -> dict[str, Any]:
        record = self._require_cancellation(cancellation_id)
        return self._cancellation_view(record)

    def participant_destinations(self, cancellation_id: str) -> dict[str, Any]:
        """接口展示：每个参与者的最终归属与迁移路径。"""
        record = self._require_cancellation(cancellation_id)
        rows: list[dict[str, Any]] = []
        for booking in self.store.bookings_of_chain(record.id):
            final_event = self.store.events.get(booking.event_id)
            path = self._booking_path(record, booking)
            if booking.status is BookingStatus.WITHDRAWN:
                final_attribution = "withdrawn"
            elif record.status is CancellationStatus.RESTORED:
                final_attribution = "origin_restored"
            elif record.status is CancellationStatus.CONFIRMED and booking.event_id == record.current_makeup_event_id:
                final_attribution = f"makeup_v{self._selected_candidate(record).version}"
            elif record.status is CancellationStatus.OPEN and booking.event_id == record.origin_event_id:
                final_attribution = "origin_pending_makeup"
            else:
                final_attribution = "stale_event"
            rows.append(
                {
                    "booking_id": booking.id,
                    "student_id": booking.student_id,
                    "student_name": booking.student_name,
                    "status": booking.status.value,
                    "checked_in": booking.checked_in,
                    "final_event_id": booking.event_id,
                    "final_event_title": final_event.title if final_event else None,
                    "final_attribution": final_attribution,
                    "path": path,
                }
            )
        rows.sort(key=lambda row: row["student_id"])
        return {
            "cancellation_id": record.id,
            "chain_status": record.status.value,
            "origin_event_id": record.origin_event_id,
            "current_makeup_event_id": record.current_makeup_event_id,
            "participants": rows,
        }

    def _booking_path(self, record: CancellationRecord, booking: Booking) -> list[dict[str, str]]:
        """从历史中还原 原场次 → 补办v1 → 补办v2 的归属路径。"""
        path = [{"event_id": record.origin_event_id, "label": "origin"}]
        for confirmation in record.confirmations:
            if any(
                entry.get("action") == "migrate"
                and entry.get("to_event_id") == confirmation["makeup_event_id"]
                for entry in booking.history
            ):
                path.append(
                    {
                        "event_id": confirmation["makeup_event_id"],
                        "label": f"makeup_v{confirmation['candidate_version']}",
                    }
                )
        if booking.status is BookingStatus.WITHDRAWN:
            path.append({"event_id": None, "label": "withdrawn"})
        return path

    # =====================================================================
    # 规则八：按补办关系去重的报表
    # =====================================================================
    def completion_report(self) -> dict[str, Any]:
        """完成量报表。

        口径：每条补办链只认“最终归属场次”——已确认链认当前补办场次，
        已恢复/未确认链认原场次；被取代的旧场次与原场次永不重复计数。
        无链独立场次按自身状态计入。
        """
        items: list[dict[str, Any]] = []
        counted_students: set[str] = set()
        counted_events = 0

        # 先处理所有补办链。
        for record in self.store.cancellations.values():
            if record.status is CancellationStatus.CONFIRMED:
                rep_id = record.current_makeup_event_id
                rep_reason = "confirmed_current_makeup"
            else:
                rep_id = record.origin_event_id
                rep_reason = (
                    "restored_origin"
                    if record.status is CancellationStatus.RESTORED
                    else "open_origin_not_counted"
                )
            chain_event_ids = {e.id for e in self.store.events_of_chain(record.id)}
            for event in self.store.events_of_chain(record.id):
                is_rep = event.id == rep_id
                counted = is_rep and event.status is EventStatus.COMPLETED
                if record.status is CancellationStatus.OPEN:
                    counted = False  # 原场次已取消，链尚无完成
                active_ids = {
                    b.student_id
                    for b in self.store.bookings_of_event(event.id)
                    if b.status is BookingStatus.ACTIVE
                }
                items.append(
                    {
                        "event_id": event.id,
                        "title": event.title,
                        "chain_id": record.id,
                        "chain_role": event.chain_role.value if event.chain_role else None,
                        "status": event.status.value,
                        "counted": counted,
                        "dedup_reason": rep_reason if is_rep else "superseded_or_origin_in_chain",
                        "active_participants": len(active_ids),
                        "checked_in": sum(
                            1
                            for b in self.store.bookings_of_event(event.id)
                            if b.status is BookingStatus.ACTIVE and b.checked_in
                        ),
                    }
                )
                if counted:
                    counted_events += 1
                    counted_students.update(active_ids)

        # 与任何补办链无关的独立场次。
        chained = {e.id for e in self.store.events.values() if e.chain_id is not None}
        for event in self.store.events.values():
            if event.id in chained:
                continue
            counted = event.status is EventStatus.COMPLETED
            active_ids = {
                b.student_id
                for b in self.store.bookings_of_event(event.id)
                if b.status is BookingStatus.ACTIVE
            }
            items.append(
                {
                    "event_id": event.id,
                    "title": event.title,
                    "chain_id": None,
                    "chain_role": None,
                    "status": event.status.value,
                    "counted": counted,
                    "dedup_reason": "standalone",
                    "active_participants": len(active_ids),
                    "checked_in": sum(
                        1
                        for b in self.store.bookings_of_event(event.id)
                        if b.status is BookingStatus.ACTIVE and b.checked_in
                    ),
                }
            )
            if counted:
                counted_events += 1
                counted_students.update(active_ids)

        items.sort(key=lambda item: item["event_id"])
        chains_summary = []
        for record in self.store.cancellations.values():
            chains_summary.append(
                {
                    "cancellation_id": record.id,
                    "origin_event_id": record.origin_event_id,
                    "current_makeup_event_id": record.current_makeup_event_id,
                    "status": record.status.value,
                    "reschedule_count": record.reschedule_count,
                    "versions": len(record.candidates),
                }
            )
        chains_summary.sort(key=lambda item: item["cancellation_id"])
        return {
            "total_completed_events": counted_events,
            "total_unique_participants_completed": len(counted_students),
            "chains": chains_summary,
            "items": items,
        }

    # =====================================================================
    # 内部辅助
    # =====================================================================
    def _cancellation_view(self, record: CancellationRecord) -> dict[str, Any]:
        view = record.to_dict()
        if record.status is CancellationStatus.OPEN:
            migrate_source = record.origin_event_id
        elif record.current_makeup_event_id:
            migrate_source = record.current_makeup_event_id
        else:
            migrate_source = None
        migratable = []
        if migrate_source is not None and record.status is not CancellationStatus.RESTORED:
            migratable = [
                b.to_dict()
                for b in self.store.active_bookings_of_event(migrate_source)
                if b.chain_id == record.id
            ]
        view["migratable_bookings"] = migratable
        return view

    def _require_event(self, event_id: str) -> Event:
        try:
            return self.store.get_event(event_id)
        except KeyError:
            raise NotFoundError(f"场次不存在：{event_id}") from None

    def _require_booking(self, booking_id: str) -> Booking:
        try:
            return self.store.get_booking(booking_id)
        except KeyError:
            raise NotFoundError(f"预约不存在：{booking_id}") from None

    def _require_cancellation(self, cancellation_id: str) -> CancellationRecord:
        try:
            return self.store.get_cancellation(cancellation_id)
        except KeyError:
            raise NotFoundError(f"取消登记不存在：{cancellation_id}") from None

    def _require_candidate(
        self, record: CancellationRecord, candidate_id: str
    ) -> MakeupCandidate:
        for candidate in record.candidates:
            if candidate.id == candidate_id:
                return candidate
        raise NotFoundError(f"补办候选不存在：{candidate_id}")

    def _selected_candidate(self, record: CancellationRecord) -> MakeupCandidate | None:
        for candidate in record.candidates:
            if candidate.status is CandidateStatus.SELECTED:
                return candidate
        return None

    def _assert_student_not_in_event(self, student_id: str, event_id: str) -> None:
        for booking in self.store.bookings_of_event(event_id):
            if booking.student_id == student_id and booking.status is BookingStatus.ACTIVE:
                raise ConflictError(f"学生已有该场次有效预约：{student_id}")

    def _assert_student_not_in_chain(self, student_id: str, chain_id: str) -> None:
        for booking in self.store.bookings_of_chain(chain_id):
            if booking.student_id == student_id and booking.status is BookingStatus.ACTIVE:
                raise ConflictError(f"学生已在该补办链中：{student_id}")

    def _validate_resource_plan(self, plan: dict[str, int]) -> dict[str, int]:
        normalized: dict[str, int] = {}
        for resource_id, quantity in plan.items():
            if not isinstance(quantity, int) or quantity <= 0:
                raise ValidationError(f"资源占用数量必须为正整数：{resource_id}")
            if resource_id not in self.store.resources:
                raise ValidationError(f"资源不存在：{resource_id}")
            normalized[resource_id] = quantity
        return normalized

    def _resource_usage(self, slot: str, exclude: set[str]) -> dict[str, int]:
        """统计某时间槽上、除 ``exclude`` 外各资源的当前占用。"""
        usage: dict[str, int] = defaultdict(int)
        for event in self.store.events.values():
            if not event.occupies_resources or event.id in exclude:
                continue
            if event.start_time != slot:
                continue
            for resource_id, quantity in event.resource_plan.items():
                usage[resource_id] += quantity
        return usage

    def _assert_resources_fit(
        self, slot: str, plan: dict[str, int], exclude: set[str]
    ) -> None:
        usage = self._resource_usage(slot, exclude)
        for resource_id, quantity in plan.items():
            resource = self.store.get_resource(resource_id)
            if quantity > resource.capacity:
                raise ConflictError(
                    f"资源 {resource.name} 单场占用 {quantity} 超过容量 {resource.capacity}"
                )
            if usage[resource_id] + quantity > resource.capacity:
                raise ConflictError(
                    f"资源 {resource.name} 在 {slot} 剩余容量不足："
                    f"已占用 {usage[resource_id]}，申请 {quantity}，容量 {resource.capacity}"
                )
