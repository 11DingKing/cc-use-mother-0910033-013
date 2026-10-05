"""取消补办管理后端的规则回归测试。"""
from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from makeup_backend.api import MakeupApp, create_server
from makeup_backend.errors import ConflictError, ValidationError
from makeup_backend.services import Services
from makeup_backend.store import InMemoryStore


def _seed(svc: Services, capacity: int = 30, n_students: int = 3) -> tuple[str, list[str]]:
    svc.create_resource("hall", "主展厅", capacity)
    event = svc.create_event("orig", "校史参观", "2026-10-01T09:00", 30, {"hall": 10})
    booking_ids: list[str] = []
    for i in range(n_students):
        bk = svc.create_booking(
            "orig", f"S{i}", f"学生{i}", f"1380000000{i}", booking_id=f"bk{i}"
        )
        booking_ids.append(bk["id"])
    return event["id"], booking_ids


class CancellationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = Services(InMemoryStore())

    def test_register_links_origin_bookings_and_freezes_resources(self) -> None:
        _seed(self.svc)
        self.svc.check_in("bk0")
        record = self.svc.register_cancellation("orig", "VENUE_CLOSURE", "场馆临时停用")
        self.assertEqual(record["status"], "open")
        self.assertEqual(record["cancel_notify_status"], "pending")
        self.assertEqual(len(record["migratable_bookings"]), 3)

        origin = self.svc.store.get_event("orig")
        self.assertEqual(origin.status.value, "cancelled")
        self.assertTrue(origin.occupies_resources, "取消后占用应冻结，不能被他人抢占")
        for booking in self.svc.store.bookings_of_event("orig"):
            self.assertEqual(booking.chain_id, record["id"])

        # 冻结占用：同一时间再排 10 人厅，容量 30 已满 → 冲突。
        self.svc.create_event("other", "其他活动", "2026-10-01T11:00", 30, {"hall": 20})
        with self.assertRaises(ConflictError):
            self.svc.create_event("clash", "冲突活动", "2026-10-01T09:00", 30, {"hall": 21})

    def test_duplicate_registration_rejected(self) -> None:
        _seed(self.svc)
        self.svc.register_cancellation("orig", "VENUE_CLOSURE")
        with self.assertRaises(ConflictError):
            self.svc.register_cancellation("orig", "WEATHER")

    def test_completed_event_cannot_cancel(self) -> None:
        _seed(self.svc)
        self.svc.complete_event("orig")
        with self.assertRaises(ConflictError):
            self.svc.register_cancellation("orig", "VENUE_CLOSURE")


class ConfirmMakeupTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = Services(InMemoryStore())
        _seed(self.svc)
        self.record = self.svc.register_cancellation("orig", "VENUE_CLOSURE")
        self.cid = self.record["id"]

    def _candidate(self, slot: str = "2026-10-08T09:00", capacity: int = 30) -> str:
        return self.svc.add_candidate(self.cid, slot, capacity, {"hall": 10})["id"]

    def test_confirm_atomically_transfers_participants_and_resources(self) -> None:
        self.svc.check_in("bk0")
        candidate_id = self._candidate()
        view = self.svc.confirm_makeup(self.cid, candidate_id)
        self.assertEqual(view["status"], "confirmed")
        self.assertEqual(view["makeup_notify_status"], "pending")
        self.assertEqual(len(view["confirmations"]), 1)

        makeup_id = view["current_makeup_event_id"]
        moved = self.svc.store.active_bookings_of_event(makeup_id)
        self.assertEqual({b.student_id for b in moved}, {"S0", "S1", "S2"})
        # 原签到记录随人保留在同一预约对象上，补办链可关联。
        self.assertTrue(self.svc.store.get_booking("bk0").checked_in)
        origin = self.svc.store.get_event("orig")
        self.assertEqual(origin.replaced_by, makeup_id)
        self.assertFalse(origin.occupies_resources, "确认后原场次占用应迁出")
        # 原时间槽冻结释放后，同槽新活动可使用资源。
        self.svc.create_event("new_at_old_slot", "补位活动", "2026-10-01T09:00", 30, {"hall": 30})

    def test_other_candidates_are_superseded(self) -> None:
        first = self._candidate("2026-10-08T09:00")
        second = self._candidate("2026-10-09T09:00")
        self.svc.confirm_makeup(self.cid, first)
        statuses = {c["id"]: c["status"] for c in self.svc.get_cancellation(self.cid)["candidates"]}
        self.assertEqual(statuses[first], "selected")
        self.assertEqual(statuses[second], "superseded")

    def test_capacity_shortage_rolls_back_entire_confirmation(self) -> None:
        tight = self.svc.add_candidate(self.cid, "2026-10-08T09:00", 2, {"hall": 10})["id"]
        with self.assertRaises(ConflictError):
            self.svc.confirm_makeup(self.cid, tight)
        # 回滚后登记仍 open，预约仍在原场次，没有产生补办场次。
        record = self.svc.get_cancellation(self.cid)
        self.assertEqual(record["status"], "open")
        self.assertEqual(len(self.svc.store.active_bookings_of_event("orig")), 3)
        self.assertIsNone(record["current_makeup_event_id"])
        self.assertEqual(len(self.svc.store.events), 1)

    def test_resource_shortage_at_new_slot_rolls_back(self) -> None:
        # 新时间槽已被其他活动占用 25，补办再要 10 → 容量不足，整笔回滚。
        self.svc.create_event("blocker", "占用者", "2026-10-08T09:00", 30, {"hall": 25})
        candidate_id = self._candidate("2026-10-08T09:00")
        with self.assertRaises(ConflictError):
            self.svc.confirm_makeup(self.cid, candidate_id)
        self.assertEqual(self.svc.get_cancellation(self.cid)["status"], "open")
        self.assertEqual(len(self.svc.store.events), 2, "不应创建补办场次")

    def test_repeated_confirmation_is_idempotent_other_candidate_conflicts(self) -> None:
        first = self._candidate("2026-10-08T09:00")
        second = self.svc.add_candidate(self.cid, "2026-10-09T09:00", 30, {"hall": 10})["id"]
        first_view = self.svc.confirm_makeup(self.cid, first)
        replay = self.svc.confirm_makeup(self.cid, first)
        self.assertTrue(replay.get("idempotent_replay"))
        self.assertEqual(replay["current_makeup_event_id"], first_view["current_makeup_event_id"])
        self.assertEqual(len(self.svc.store.events), 2, "重复确认不得新建场次")
        with self.assertRaises(ConflictError):
            self.svc.confirm_makeup(self.cid, second)


class WithdrawAndRescheduleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = Services(InMemoryStore())
        _seed(self.svc)
        self.svc.check_in("bk0")  # 原场次签到记录须随补办链保留
        self.cid = self.svc.register_cancellation("orig", "VENUE_CLOSURE")["id"]
        candidate = self.svc.add_candidate(self.cid, "2026-10-08T09:00", 30, {"hall": 10})["id"]
        self.svc.confirm_makeup(self.cid, candidate)
        self.makeup1 = self.svc.get_cancellation(self.cid)["current_makeup_event_id"]

    def test_withdrawal_before_confirm_excludes_student(self) -> None:
        svc = Services(InMemoryStore())
        _seed(svc)
        cid = svc.register_cancellation("orig", "VENUE_CLOSURE")["id"]
        svc.withdraw_participant("bk1", reason="时间冲突")
        candidate = svc.add_candidate(cid, "2026-10-08T09:00", 30, {"hall": 10})["id"]
        view = svc.confirm_makeup(cid, candidate)
        moved = {b["student_id"] for b in view["migratable_bookings"]}
        # 注意：确认后 migratable 来源变为当前补办场次，退出者不在其中。
        makeup = view["current_makeup_event_id"]
        active = {b.student_id for b in svc.store.active_bookings_of_event(makeup)}
        self.assertEqual(active, {"S0", "S2"})
        withdrawn = svc.store.get_booking("bk1")
        self.assertEqual(withdrawn.status.value, "withdrawn")
        self.assertFalse(withdrawn.checked_in)
        self.assertEqual(moved, {"S0", "S2"})

    def test_multiple_reschedules_versions_and_transfer(self) -> None:
        self.svc.withdraw_participant("bk2")
        v2 = self.svc.reschedule_makeup(self.cid, "2026-10-15T09:00", 30, {"hall": 10})
        self.assertEqual(v2["reschedule_count"], 1)
        self.assertEqual(v2["candidates"][-1]["version"], 2)
        makeup2 = v2["current_makeup_event_id"]
        old = self.svc.store.get_event(self.makeup1)
        self.assertEqual(old.status.value, "cancelled")
        self.assertEqual(old.replaced_by, makeup2)
        active = {b.student_id for b in self.svc.store.active_bookings_of_event(makeup2)}
        self.assertEqual(active, {"S0", "S1"}, "退出者不随改期迁移")

        # 再次改期 v3。
        v3 = self.svc.reschedule_makeup(self.cid, "2026-10-22T09:00", 30, {"hall": 10})
        self.assertEqual(v3["reschedule_count"], 2)
        makeup3 = v3["current_makeup_event_id"]
        active = {b.student_id for b in self.svc.store.active_bookings_of_event(makeup3)}
        self.assertEqual(active, {"S0", "S1"})
        bk0 = self.svc.store.get_booking("bk0")
        self.assertEqual(bk0.event_id, makeup3)
        self.assertTrue(bk0.checked_in, "签到始终跟随同一预约")
        migrations = [h for h in bk0.history if h["action"] == "migrate"]
        self.assertEqual([m["candidate_version"] for m in migrations], [1, 2, 3])
        # 每次改期都产生一条新的补办通知（版本化）。
        self.assertEqual(len(v3["notification_attempts"]["makeup"]), 3)

    def test_reschedule_capacity_shortage_rolls_back(self) -> None:
        before = self.svc.get_cancellation(self.cid)
        with self.assertRaises(ConflictError):
            self.svc.reschedule_makeup(self.cid, "2026-10-15T09:00", 1, {"hall": 10})
        after = self.svc.get_cancellation(self.cid)
        self.assertEqual(after["current_makeup_event_id"], before["current_makeup_event_id"])
        self.assertEqual(after["reschedule_count"], 0)

    def test_withdrawal_is_irreversible_and_reschedule_idempotent_state(self) -> None:
        self.svc.withdraw_participant("bk0")
        self.svc.withdraw_participant("bk0")  # 重复退出幂等，不报错
        self.svc.reschedule_makeup(self.cid, "2026-10-15T09:00", 30, {"hall": 10})
        self.assertEqual(self.svc.store.get_booking("bk0").status.value, "withdrawn")


class RestoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = Services(InMemoryStore())
        _seed(self.svc)
        self.cid = self.svc.register_cancellation("orig", "VENUE_CLOSURE")["id"]

    def test_restore_only_when_open_voids_candidates_keeps_active_students(self) -> None:
        self.svc.add_candidate(self.cid, "2026-10-08T09:00", 30, {"hall": 10})
        self.svc.withdraw_participant("bk1")
        view = self.svc.restore_origin(self.cid)
        self.assertEqual(view["status"], "restored")
        self.assertTrue(all(c["status"] == "void" for c in view["candidates"]))
        origin = self.svc.store.get_event("orig")
        self.assertEqual(origin.status.value, "scheduled")
        active = {b.student_id for b in self.svc.store.active_bookings_of_event("orig")}
        self.assertEqual(active, {"S0", "S2"}, "退出者不自动回归")

    def test_restore_rejected_after_confirm_and_after_restore(self) -> None:
        candidate = self.svc.add_candidate(self.cid, "2026-10-08T09:00", 30, {"hall": 10})["id"]
        self.svc.confirm_makeup(self.cid, candidate)
        with self.assertRaises(ConflictError):
            self.svc.restore_origin(self.cid)

        svc = Services(InMemoryStore())
        _seed(svc)
        cid = svc.register_cancellation("orig", "VENUE_CLOSURE")["id"]
        svc.restore_origin(cid)
        with self.assertRaises(ConflictError):
            svc.restore_origin(cid)
        with self.assertRaises(ConflictError):
            svc.register_cancellation("orig", "AGAIN")

    def test_new_booking_after_restore_does_not_join_old_chain(self) -> None:
        self.svc.restore_origin(self.cid)
        booking = self.svc.create_booking("orig", "S9", "新生", "139")
        self.assertIsNone(booking["chain_id"])


class ReportTest(unittest.TestCase):
    def test_report_deduplicates_by_makeup_relation(self) -> None:
        svc = Services(InMemoryStore())
        _seed(svc)
        cid = svc.register_cancellation("orig", "VENUE_CLOSURE")["id"]
        candidate = svc.add_candidate(cid, "2026-10-08T09:00", 30, {"hall": 10})["id"]
        svc.confirm_makeup(cid, candidate)
        makeup1 = svc.get_cancellation(cid)["current_makeup_event_id"]
        svc.withdraw_participant("bk0")
        svc.reschedule_makeup(cid, "2026-10-15T09:00", 30, {"hall": 10})
        makeup2 = svc.get_cancellation(cid)["current_makeup_event_id"]
        svc.complete_event(makeup2)

        # 一个与补办链无关的独立已完成场次。
        svc.create_event("solo", "周末讲座", "2026-10-20T09:00", 10)
        svc.create_booking("solo", "S2", "重复学生", "x")  # S2 已在补办链完成
        svc.complete_event("solo")

        report = svc.completion_report()
        items = {item["event_id"]: item for item in report["items"]}
        self.assertFalse(items["orig"]["counted"], "被取代原场次不得计入")
        self.assertFalse(items[makeup1]["counted"], "被改期取代的旧补办不得计入")
        self.assertTrue(items[makeup2]["counted"], "仅最终补办计一次")
        self.assertTrue(items["solo"]["counted"])
        self.assertEqual(items[makeup2]["active_participants"], 2)
        # 完成场次 2：最终补办 + 独立场次。
        self.assertEqual(report["total_completed_events"], 2)
        # 去重人数：S1、S2（S2 虽出现在两场，只算一次；S0 已退出）。
        self.assertEqual(report["total_unique_participants_completed"], 2)

    def test_open_chain_counts_nothing(self) -> None:
        svc = Services(InMemoryStore())
        _seed(svc)
        svc.register_cancellation("orig", "VENUE_CLOSURE")
        report = svc.completion_report()
        self.assertEqual(report["total_completed_events"], 0)
        self.assertFalse(report["items"][0]["counted"])


class ParticipantViewTest(unittest.TestCase):
    def test_destinations_show_final_attribution_and_path(self) -> None:
        svc = Services(InMemoryStore())
        _seed(svc)
        cid = svc.register_cancellation("orig", "VENUE_CLOSURE")["id"]
        c1 = svc.add_candidate(cid, "2026-10-08T09:00", 30, {"hall": 10})["id"]
        svc.confirm_makeup(cid, c1)
        svc.withdraw_participant("bk1")
        svc.reschedule_makeup(cid, "2026-10-15T09:00", 30, {"hall": 10})
        view = svc.participant_destinations(cid)
        by_student = {row["student_id"]: row for row in view["participants"]}
        s0 = by_student["S0"]
        self.assertTrue(s0["final_attribution"].startswith("makeup_v"))
        self.assertEqual([p["label"] for p in s0["path"]], ["origin", "makeup_v1", "makeup_v2"])
        self.assertEqual(by_student["S1"]["final_attribution"], "withdrawn")
        self.assertEqual(by_student["S1"]["path"][-1]["label"], "withdrawn")


class NotificationTest(unittest.TestCase):
    def test_notification_status_transitions(self) -> None:
        svc = Services(InMemoryStore())
        _seed(svc)
        cid = svc.register_cancellation("orig", "VENUE_CLOSURE")["id"]
        view = svc.update_notification(cid, "cancel", "sent")
        self.assertEqual(view["cancel_notify_status"], "sent")
        view = svc.update_notification(cid, "cancel", "delivered")
        self.assertEqual(view["cancel_notify_status"], "delivered")
        self.assertEqual(len(view["notification_attempts"]["cancel"]), 3)  # pending+sent+delivered
        with self.assertRaises(ValidationError):
            svc.update_notification(cid, "cancel", "unknown")
        with self.assertRaises(ValidationError):
            svc.update_notification(cid, "weird", "sent")


class HttpApiTest(unittest.TestCase):
    def test_end_to_end_http(self) -> None:
        app = MakeupApp(Services(InMemoryStore()))
        server = create_server(app=app)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://{server.server_address[0]}:{server.server_address[1]}"

            def call(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
                data = json.dumps(payload or {}).encode("utf-8")
                req = urllib.request.Request(
                    base + path, data=data, method=method,
                    headers={"Content-Type": "application/json"},
                )
                try:
                    with urllib.request.urlopen(req) as resp:
                        return resp.status, json.loads(resp.read())
                except urllib.error.HTTPError as exc:
                    return exc.code, json.loads(exc.read())

            status, _ = call("POST", "/resources", {"id": "hall", "name": "主展厅", "capacity": 30})
            self.assertEqual(status, 201)
            status, _ = call("POST", "/events", {
                "id": "orig", "title": "校史参观", "start_time": "2026-10-01T09:00",
                "capacity": 30, "resource_plan": {"hall": 10}})
            self.assertEqual(status, 201)
            for i in range(3):
                status, _ = call("POST", "/events/orig/bookings", {
                    "id": f"bk{i}", "student_id": f"S{i}", "student_name": f"学生{i}"})
                self.assertEqual(status, 201)
            status, record = call("POST", "/cancellations", {
                "event_id": "orig", "reason_code": "VENUE_CLOSURE", "reason_note": "临时停用"})
            self.assertEqual(status, 201)
            cid = record["id"]
            status, candidate = call("POST", f"/cancellations/{cid}/candidates", {
                "scheduled_start": "2026-10-08T09:00", "capacity": 30,
                "resource_plan": {"hall": 10}})
            self.assertEqual(status, 201)
            status, confirmed = call("POST", f"/cancellations/{cid}/confirm", {
                "candidate_id": candidate["id"]})
            self.assertEqual(status, 201)
            # 重复确认 → 幂等 201，且带标记。
            status, replay = call("POST", f"/cancellations/{cid}/confirm", {
                "candidate_id": candidate["id"]})
            self.assertEqual(status, 201)
            self.assertTrue(replay["idempotent_replay"])

            status, destinations = call("GET", f"/cancellations/{cid}/participants")
            self.assertEqual(status, 200)
            self.assertEqual(len(destinations["participants"]), 3)
            status, report = call("GET", "/report/completions")
            self.assertEqual(status, 200)
            self.assertEqual(report["total_completed_events"], 0)

            # 错误码映射：资源不足冲突 → 409。
            status, err = call("POST", "/events", {
                "id": "bad", "title": "超额", "start_time": "2026-10-08T09:00",
                "capacity": 1, "resource_plan": {"hall": 99}})
            self.assertEqual(status, 409)
            self.assertEqual(err["error"], "ConflictError")
            # 404。
            status, _ = call("GET", "/events/nope")
            self.assertEqual(status, 404)
            # 400。
            status, _ = call("POST", "/events", {"title": "缺字段"})
            self.assertEqual(status, 400)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
