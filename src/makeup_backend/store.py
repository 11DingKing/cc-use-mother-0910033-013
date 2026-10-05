"""线程安全的内存存储。

``transaction()`` 在同一把全局锁内做深拷贝快照；事务中任何一步抛出异常，
全部对象回滚到快照，保证“确认补办”的多步写入要么全成、要么全不成。
"""
from __future__ import annotations

import copy
import threading
from contextlib import contextmanager
from collections.abc import Iterator

from .models import Booking, CancellationRecord, Event, Resource


class InMemoryStore:
    """保存资源、场次、预约、取消登记四类聚合。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.resources: dict[str, Resource] = {}
        self.events: dict[str, Event] = {}
        self.bookings: dict[str, Booking] = {}
        self.cancellations: dict[str, CancellationRecord] = {}

    # ---- 事务 -----------------------------------------------------------
    @contextmanager
    def transaction(self) -> Iterator[None]:
        snapshot = (
            copy.deepcopy(self.resources),
            copy.deepcopy(self.events),
            copy.deepcopy(self.bookings),
            copy.deepcopy(self.cancellations),
        )
        with self._lock:
            try:
                yield
            except BaseException:
                self.resources, self.events, self.bookings, self.cancellations = (
                    copy.deepcopy(snapshot[0]),
                    copy.deepcopy(snapshot[1]),
                    copy.deepcopy(snapshot[2]),
                    copy.deepcopy(snapshot[3]),
                )
                raise

    # ---- 资源 -----------------------------------------------------------
    def add_resource(self, resource: Resource) -> None:
        self.resources[resource.id] = resource

    def get_resource(self, resource_id: str) -> Resource:
        return self.resources[resource_id]

    # ---- 场次 -----------------------------------------------------------
    def add_event(self, event: Event) -> None:
        self.events[event.id] = event

    def get_event(self, event_id: str) -> Event:
        return self.events[event_id]

    def events_of_chain(self, chain_id: str) -> list[Event]:
        return [e for e in self.events.values() if e.chain_id == chain_id]

    # ---- 预约 -----------------------------------------------------------
    def add_booking(self, booking: Booking) -> Booking:
        self.bookings[booking.id] = booking
        return booking

    def get_booking(self, booking_id: str) -> Booking:
        return self.bookings[booking_id]

    def bookings_of_event(self, event_id: str) -> list[Booking]:
        return [b for b in self.bookings.values() if b.event_id == event_id]

    def active_bookings_of_event(self, event_id: str) -> list[Booking]:
        from .models import BookingStatus

        return [
            b
            for b in self.bookings.values()
            if b.event_id == event_id and b.status is BookingStatus.ACTIVE
        ]

    def bookings_of_chain(self, chain_id: str) -> list[Booking]:
        return [b for b in self.bookings.values() if b.chain_id == chain_id]

    # ---- 取消登记 -------------------------------------------------------
    def add_cancellation(self, record: CancellationRecord) -> None:
        self.cancellations[record.id] = record

    def get_cancellation(self, cancellation_id: str) -> CancellationRecord:
        return self.cancellations[cancellation_id]

    def reset(self) -> None:
        with self._lock:
            self.resources.clear()
            self.events.clear()
            self.bookings.clear()
            self.cancellations.clear()
