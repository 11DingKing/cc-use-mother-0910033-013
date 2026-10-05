"""取消补办服务的确定性规则回归测试。"""
from __future__ import annotations

import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from domain_contract.validator import load_contract
from makeup_service import (
    AlreadyConfirmedError,
    InvalidStateError,
    MakeupService,
    NotFoundError,
    ResourceConflictError,
    RestorationClosedError,
    SessionStatus,
)
from makeup_service.models import (
    CancellationStatus,
    CandidateStatus,
    LinkStatus,
    NotificationKind,
    NotificationStatus,
    ReservationStatus,
)

START = datetime(2026, 10, 12, 9, 30)


class Clock:
    def __init__(self) -> None:
        self.moment = datetime(2026, 10, 5, 9, 0, 0)

    def __call__(self) -> datetime:
        return self.moment

    def advance(self, **kwargs) -> None:
        self.moment += timedelta(**kwargs)


class ServiceCase(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock()
        self.service = MakeupService(now=self.clock)
        self.session = self.service.add_session("三年级科普专场", "城东小学", "主馆", START)
        self.students = [
            self.service.register_participant(f"学生{i}", "城东小学") for i in range(4)
        ]
        self.student_ids = [p.participant_id for p in self.students]
        self.reservation = self.service.add_reservation(
            self.session.session_id, "城东小学", self.student_ids
        )
        self.docent = self.service.add_resource("讲解员", "讲解员-小李", capacity=1)
        self.hall = self.service.add_resource("展厅", "科普展厅", capacity=1)
        self.service.allocate_resource(self.session.session_id, self.docent.resource_id)
        self.service.allocate_resource(self.session.session_id, self.hall.resource_id)

    def cancel(self, **kwargs):
        params = {
            "reason": "场馆临时停用：消防检修",
            "notice_id": "NOTICE-001",
            "actor": "客服-王芳",
        }
        params.update(kwargs)
        return self.service.register_cancellation(self.session.session_id, **params)

    def propose(self, cancellation_id, day=13, hour=9, minute=30):
        return self.service.propose_candidate(
            cancellation_id, "主馆", datetime(2026, 10, day, hour, minute)
        )


class CancellationRegistrationTest(ServiceCase):
    def test_register_cancellation_records_full_context(self) -> None:
        record = self.cancel()
        self.assertEqual(record.status, CancellationStatus.OPEN)
        self.assertEqual(record.reason, "场馆临时停用：消防检修")
        self.assertEqual(record.notice_id, "NOTICE-001")
        self.assertEqual(record.registered_by, "客服-王芳")
        self.assertEqual(record.registered_at, self.clock.moment)

        session = self.service.get_session(self.session.session_id)
        self.assertTrue(session.cancelled)

        reservation = self.service.get_reservation(self.reservation.reservation_id)
        self.assertEqual(reservation.status, ReservationStatus.MIGRATABLE)

        notifications = self.service.list_notifications(record.cancellation_id)
        self.assertEqual(len(notifications), 1)
        self.assertEqual(notifications[0].kind, NotificationKind.CANCELLATION)
        self.assertEqual(notifications[0].school, "城东小学")
        self.assertEqual(notifications[0].status, NotificationStatus.PENDING)

        detail = self.service.cancellation_detail(record.cancellation_id)
        self.assertEqual(
            detail["migratable_reservations"][0]["participants"], self.student_ids
        )

    def test_duplicate_registration_is_idempotent(self) -> None:
        first = self.cancel()
        second = self.cancel(reason="另一条原因")
        self.assertEqual(first.cancellation_id, second.cancellation_id)
        self.assertEqual(len(self.service.list_notifications()), 1)

    def test_cannot_cancel_session_in_progress(self) -> None:
        self.service.advance_session(self.session.session_id, SessionStatus.IN_PROGRESS)
        with self.assertRaises(InvalidStateError):
            self.cancel()

    def test_session_status_matches_contract(self) -> None:
        contract = load_contract(ROOT / "domain" / "contract.json")
        self.assertEqual([s.value for s in SessionStatus], contract["states"])


class ConfirmMakeupTest(ServiceCase):
    def test_confirm_moves_participants_and_resources_atomically(self) -> None:
        record = self.cancel()
        candidate = self.propose(record.cancellation_id)
        link = self.service.confirm_makeup(
            record.cancellation_id, candidate.candidate_id, idempotency_key="K1"
        )

        self.assertEqual(link.version, 1)
        self.assertEqual(link.status, LinkStatus.ACTIVE)
        self.assertEqual(link.participant_count, 4)
        self.assertEqual(link.original_session_id, self.session.session_id)

        makeup = self.service.get_session(link.makeup_session_id)
        self.assertEqual(makeup.status, SessionStatus.SCHEDULED)
        self.assertFalse(makeup.cancelled)
        self.assertEqual(makeup.start_time, datetime(2026, 10, 13, 9, 30))

        moved = self.service.list_reservations(makeup.session_id)
        self.assertEqual(len(moved), 1)
        self.assertEqual(tuple(self.student_ids), moved[0].participant_ids)
        self.assertEqual(moved[0].previous_reservation_id, self.reservation.reservation_id)
        self.assertEqual(
            self.service.get_reservation(self.reservation.reservation_id).status,
            ReservationStatus.MIGRATED,
        )

        allocations = self.service.list_allocations(makeup.session_id)
        self.assertEqual(len(allocations), 2)
        self.assertEqual(self.service.list_allocations(self.session.session_id), [])

        self.assertEqual(
            self.service.get_cancellation(record.cancellation_id).status,
            CancellationStatus.FULFILLED,
        )
        self.assertEqual(
            self.service.list_candidates(record.cancellation_id)[0].status,
            CandidateStatus.CONFIRMED,
        )
        kinds = {n.kind for n in self.service.list_notifications(record.cancellation_id)}
        self.assertEqual(kinds, {NotificationKind.CANCELLATION, NotificationKind.MAKEUP_CONFIRMED})

    def test_confirm_fails_when_resource_short_and_changes_nothing(self) -> None:
        other = self.service.add_session(
            "另一场次", "城西小学", "主馆", datetime(2026, 10, 13, 9, 30)
        )
        self.service.allocate_resource(other.session_id, self.docent.resource_id)

        record = self.cancel()
        candidate = self.propose(record.cancellation_id, day=13)
        sessions_before = len(self.service.list_sessions())
        notifications_before = len(self.service.list_notifications())

        with self.assertRaises(ResourceConflictError):
            self.service.confirm_makeup(record.cancellation_id, candidate.candidate_id)

        self.assertEqual(len(self.service.list_sessions()), sessions_before)
        self.assertEqual(len(self.service.list_notifications()), notifications_before)
        self.assertEqual(self.service.list_links(record.cancellation_id), [])
        self.assertEqual(
            self.service.list_candidates(record.cancellation_id)[0].status,
            CandidateStatus.PENDING,
        )
        self.assertEqual(
            self.service.get_reservation(self.reservation.reservation_id).status,
            ReservationStatus.MIGRATABLE,
        )
        self.assertEqual(
            self.service.get_cancellation(record.cancellation_id).status,
            CancellationStatus.OPEN,
        )
        self.assertEqual(len(self.service.list_allocations(self.session.session_id)), 2)

    def test_duplicate_confirmation_is_idempotent(self) -> None:
        record = self.cancel()
        candidate = self.propose(record.cancellation_id)
        first = self.service.confirm_makeup(
            record.cancellation_id, candidate.candidate_id, idempotency_key="K1"
        )
        by_key = self.service.confirm_makeup(
            record.cancellation_id, candidate.candidate_id, idempotency_key="K1"
        )
        by_candidate = self.service.confirm_makeup(
            record.cancellation_id, candidate.candidate_id, idempotency_key="K2"
        )
        self.assertEqual(first.link_id, by_key.link_id)
        self.assertEqual(first.link_id, by_candidate.link_id)
        self.assertEqual(len(self.service.list_links(record.cancellation_id)), 1)

        other_candidate = self.propose(record.cancellation_id, day=14)
        with self.assertRaises(AlreadyConfirmedError):
            self.service.confirm_makeup(record.cancellation_id, other_candidate.candidate_id)

        makeup_reservations = self.service.list_reservations(first.makeup_session_id)
        self.assertEqual(len(makeup_reservations), 1)
        self.assertEqual(len(makeup_reservations[0].participant_ids), 4)

    def test_concurrent_confirmation_is_atomic(self) -> None:
        record = self.cancel()
        candidate = self.propose(record.cancellation_id)
        with ThreadPoolExecutor(max_workers=8) as pool:
            links = list(
                pool.map(
                    lambda i: self.service.confirm_makeup(
                        record.cancellation_id,
                        candidate.candidate_id,
                        idempotency_key=f"K{i}",
                    ),
                    range(8),
                )
            )
        self.assertEqual({link.link_id for link in links}, {links[0].link_id})
        self.assertEqual(len(self.service.list_links(record.cancellation_id)), 1)
        makeup_reservations = self.service.list_reservations(links[0].makeup_session_id)
        self.assertEqual(len(makeup_reservations), 1)
        self.assertEqual(len(makeup_reservations[0].participant_ids), 4)


class WithdrawalTest(ServiceCase):
    def test_withdrawal_before_and_after_confirmation(self) -> None:
        record = self.cancel()
        self.service.withdraw_participant(self.student_ids[0], "家长请假")
        detail = self.service.cancellation_detail(record.cancellation_id)
        self.assertEqual(
            detail["migratable_reservations"][0]["participants"], self.student_ids[1:]
        )

        candidate = self.propose(record.cancellation_id)
        link = self.service.confirm_makeup(record.cancellation_id, candidate.candidate_id)
        self.assertEqual(link.participant_count, 3)

        self.service.withdraw_participant(self.student_ids[1], "转学")
        makeup_reservations = self.service.list_reservations(link.makeup_session_id)
        remaining = [
            pid
            for pid in makeup_reservations[0].participant_ids
            if pid not in (self.student_ids[1],)
        ]
        self.assertEqual(len(remaining), 2)

        attribution = self.service.participant_attribution()["items"]
        by_id = {row["participant_id"]: row for row in attribution}
        self.assertEqual(by_id[self.student_ids[0]]["state"], "已退出")
        self.assertEqual(by_id[self.student_ids[1]]["state"], "已退出")
        for pid in self.student_ids[2:]:
            self.assertEqual(by_id[pid]["state"], "已归属")
            self.assertEqual(by_id[pid]["session_id"], link.makeup_session_id)

    def test_withdrawal_is_final_and_validated(self) -> None:
        self.service.withdraw_participant(self.student_ids[0])
        with self.assertRaises(InvalidStateError):
            self.service.withdraw_participant(self.student_ids[0])
        with self.assertRaises(NotFoundError):
            self.service.withdraw_participant("PTP-9999")

    def test_cannot_withdraw_after_session_started(self) -> None:
        self.service.advance_session(self.session.session_id, SessionStatus.IN_PROGRESS)
        with self.assertRaises(InvalidStateError):
            self.service.withdraw_participant(self.student_ids[0])


class RescheduleTest(ServiceCase):
    def test_multiple_reschedules_version_and_move_everything(self) -> None:
        record = self.cancel()
        first_candidate = self.propose(record.cancellation_id, day=13)
        link_v1 = self.service.confirm_makeup(
            record.cancellation_id, first_candidate.candidate_id, idempotency_key="C1"
        )
        second_candidate = self.propose(record.cancellation_id, day=14)
        link_v2 = self.service.reschedule_makeup(
            record.cancellation_id, second_candidate.candidate_id, idempotency_key="R2"
        )
        third_candidate = self.propose(record.cancellation_id, day=15)
        link_v3 = self.service.reschedule_makeup(
            record.cancellation_id, third_candidate.candidate_id, idempotency_key="R3"
        )

        self.assertEqual((link_v1.version, link_v2.version, link_v3.version), (1, 2, 3))
        self.assertEqual(link_v1.status, LinkStatus.SUPERSEDED)
        self.assertEqual(link_v2.status, LinkStatus.SUPERSEDED)
        self.assertEqual(link_v3.status, LinkStatus.ACTIVE)
        self.assertEqual(link_v3.supersedes, link_v2.link_id)

        for old in (link_v1.makeup_session_id, link_v2.makeup_session_id):
            self.assertTrue(self.service.get_session(old).cancelled)
        final = self.service.get_session(link_v3.makeup_session_id)
        self.assertFalse(final.cancelled)
        self.assertEqual(final.start_time, datetime(2026, 10, 15, 9, 30))

        final_reservations = self.service.list_reservations(final.session_id)
        self.assertEqual(len(final_reservations), 1)
        self.assertEqual(tuple(self.student_ids), final_reservations[0].participant_ids)
        self.assertEqual(len(self.service.list_allocations(final.session_id)), 2)

        candidates = {c.candidate_id: c.status for c in self.service.list_candidates(record.cancellation_id)}
        self.assertEqual(candidates[first_candidate.candidate_id], CandidateStatus.SUPERSEDED)
        self.assertEqual(candidates[second_candidate.candidate_id], CandidateStatus.SUPERSEDED)
        self.assertEqual(candidates[third_candidate.candidate_id], CandidateStatus.CONFIRMED)

        kinds = [n.kind for n in self.service.list_notifications(record.cancellation_id)]
        self.assertEqual(
            kinds,
            [
                NotificationKind.CANCELLATION,
                NotificationKind.MAKEUP_CONFIRMED,
                NotificationKind.RESCHEDULED,
                NotificationKind.RESCHEDULED,
            ],
        )

        again = self.service.reschedule_makeup(
            record.cancellation_id, third_candidate.candidate_id, idempotency_key="R3"
        )
        self.assertEqual(again.link_id, link_v3.link_id)
        same_candidate = self.service.reschedule_makeup(
            record.cancellation_id, third_candidate.candidate_id
        )
        self.assertEqual(same_candidate.link_id, link_v3.link_id)
        self.assertEqual(len(self.service.list_links(record.cancellation_id)), 3)

    def test_reschedule_after_makeup_started_rejected(self) -> None:
        record = self.cancel()
        candidate = self.propose(record.cancellation_id, day=13)
        link = self.service.confirm_makeup(record.cancellation_id, candidate.candidate_id)
        self.service.advance_session(link.makeup_session_id, SessionStatus.IN_PROGRESS)
        later = self.propose(record.cancellation_id, day=14)
        with self.assertRaises(InvalidStateError):
            self.service.reschedule_makeup(record.cancellation_id, later.candidate_id)

    def test_reschedule_requires_fulfilled_cancellation(self) -> None:
        record = self.cancel()
        candidate = self.propose(record.cancellation_id, day=13)
        with self.assertRaises(InvalidStateError):
            self.service.reschedule_makeup(record.cancellation_id, candidate.candidate_id)


class RestoreTest(ServiceCase):
    def test_restore_before_confirmation(self) -> None:
        record = self.cancel()
        candidate = self.propose(record.cancellation_id, day=13)
        restored = self.service.restore_original_session(record.cancellation_id)

        self.assertFalse(restored.cancelled)
        self.assertEqual(restored.close_reason, "")
        reservation = self.service.get_reservation(self.reservation.reservation_id)
        self.assertEqual(reservation.status, ReservationStatus.ACTIVE)
        self.assertEqual(
            self.service.list_candidates(record.cancellation_id)[0].status,
            CandidateStatus.VOID,
        )
        self.assertEqual(
            self.service.get_cancellation(record.cancellation_id).status,
            CancellationStatus.RESTORED,
        )
        kinds = {n.kind for n in self.service.list_notifications(record.cancellation_id)}
        self.assertEqual(kinds, {NotificationKind.CANCELLATION, NotificationKind.RESTORED})

        again = self.service.restore_original_session(record.cancellation_id)
        self.assertEqual(again.session_id, restored.session_id)

        attribution = self.service.participant_attribution()["items"]
        self.assertTrue(all(row["state"] == "已归属" for row in attribution))
        self.assertTrue(
            all(row["session_id"] == self.session.session_id for row in attribution)
        )
        self.assertEqual(candidate.status, CandidateStatus.VOID)

    def test_restore_after_confirmation_moves_everything_back(self) -> None:
        record = self.cancel()
        candidate = self.propose(record.cancellation_id, day=13)
        link = self.service.confirm_makeup(record.cancellation_id, candidate.candidate_id)
        restored = self.service.restore_original_session(record.cancellation_id)

        self.assertFalse(restored.cancelled)
        makeup = self.service.get_session(link.makeup_session_id)
        self.assertTrue(makeup.cancelled)
        self.assertEqual(link.status, LinkStatus.REVOKED)

        back = self.service.list_reservations(restored.session_id)
        active = [r for r in back if r.status == ReservationStatus.ACTIVE]
        self.assertEqual(len(active), 1)
        self.assertEqual(tuple(self.student_ids), active[0].participant_ids)
        self.assertEqual(len(self.service.list_allocations(restored.session_id)), 2)
        self.assertEqual(self.service.list_allocations(makeup.session_id), [])

        report = self.service.completion_report()
        chain = next(c for c in report["chains"] if c["chain_id"] == record.cancellation_id)
        self.assertEqual(chain["final_session_id"], restored.session_id)
        self.assertEqual(chain["outcome"], "未开始")

        attribution = self.service.participant_attribution()["items"]
        self.assertTrue(all(row["state"] == "已归属" for row in attribution))
        self.assertTrue(all(row["session_id"] == restored.session_id for row in attribution))

    def test_restore_after_makeup_started_rejected(self) -> None:
        record = self.cancel()
        candidate = self.propose(record.cancellation_id, day=13)
        link = self.service.confirm_makeup(record.cancellation_id, candidate.candidate_id)
        self.service.advance_session(link.makeup_session_id, SessionStatus.IN_PROGRESS)
        with self.assertRaises(RestorationClosedError):
            self.service.restore_original_session(record.cancellation_id)


class ReportTest(ServiceCase):
    def test_completion_report_deduplicates_makeup_chain(self) -> None:
        record = self.cancel()
        candidate = self.propose(record.cancellation_id, day=13)
        link = self.service.confirm_makeup(record.cancellation_id, candidate.candidate_id)
        self.service.advance_session(link.makeup_session_id, SessionStatus.IN_PROGRESS)
        self.service.advance_session(link.makeup_session_id, SessionStatus.SETTLED)

        other = self.service.add_session(
            "独立场次", "城西小学", "主馆", datetime(2026, 10, 20, 9, 30)
        )
        self.service.advance_session(other.session_id, SessionStatus.IN_PROGRESS)
        self.service.advance_session(other.session_id, SessionStatus.SETTLED)

        abandoned = self.service.add_session(
            "取消未补办场次", "城南小学", "主馆", datetime(2026, 10, 21, 9, 30)
        )
        self.service.register_cancellation(
            abandoned.session_id, "场馆临时停用：暴雨", "NOTICE-002", "客服-王芳"
        )

        report = self.service.completion_report()
        self.assertEqual(report["totals"]["已完成"], 2)
        self.assertEqual(report["totals"]["已取消"], 1)
        self.assertEqual(report["totals"]["chains"], 3)

        chain = next(c for c in report["chains"] if c["chain_id"] == record.cancellation_id)
        self.assertEqual(chain["final_session_id"], link.makeup_session_id)
        self.assertEqual(chain["versions"], 1)
        self.assertEqual(chain["participant_count"], 4)
        self.assertEqual(chain["outcome"], "已完成")

        seen = [sid for row in report["chains"] for sid in row["session_ids"]]
        self.assertEqual(len(seen), len(set(seen)))
        self.assertEqual(
            sum(report["totals"][key] for key in ("已完成", "执行中", "未开始", "已取消")),
            report["totals"]["chains"],
        )

    def test_attribution_pending_placement_before_makeup(self) -> None:
        self.cancel()
        attribution = self.service.participant_attribution()["items"]
        self.assertEqual(len(attribution), 4)
        self.assertTrue(all(row["state"] == "待安置" for row in attribution))
        self.assertTrue(all(row["session_id"] is None for row in attribution))


class NotificationTest(ServiceCase):
    def test_dispatch_marks_pending_as_sent(self) -> None:
        record = self.cancel()
        candidate = self.propose(record.cancellation_id, day=13)
        self.service.confirm_makeup(record.cancellation_id, candidate.candidate_id)

        self.assertEqual(self.service.dispatch_notifications(), 2)
        notifications = self.service.list_notifications(record.cancellation_id)
        self.assertTrue(all(n.status == NotificationStatus.SENT for n in notifications))
        self.assertTrue(all(n.sent_at == self.clock.moment for n in notifications))
        self.assertEqual(self.service.dispatch_notifications(), 0)


if __name__ == "__main__":
    unittest.main()
