"""演示样例：场馆临时停用导致学校场次取消后的完整补办流程。"""
from __future__ import annotations

from datetime import datetime

from .service import MakeupService
from .models import SessionStatus


def build_sample_service() -> tuple[MakeupService, dict]:
    """构造一条确定性的取消-补办链路，供演示与接口联调使用。"""
    service = MakeupService()

    session = service.add_session(
        "三年级科普专场", "城东小学", "主馆", datetime(2026, 10, 12, 9, 30)
    )
    students = [
        service.register_participant(name, "城东小学")
        for name in ("林晓", "陈果", "周正", "吴忧", "郑好", "孙然")
    ]
    reservation = service.add_reservation(
        session.session_id, "城东小学", [p.participant_id for p in students]
    )
    docent = service.add_resource("讲解员", "讲解员-小李", capacity=1)
    hall = service.add_resource("展厅", "科普展厅", capacity=1)
    service.allocate_resource(session.session_id, docent.resource_id)
    service.allocate_resource(session.session_id, hall.resource_id)

    record = service.register_cancellation(
        session.session_id,
        reason="场馆临时停用通知：消防检修",
        notice_id="NOTICE-20261005-01",
        actor="客服-王芳",
    )
    first = service.propose_candidate(
        record.cancellation_id, "主馆", datetime(2026, 10, 13, 9, 30)
    )
    service.propose_candidate(record.cancellation_id, "主馆", datetime(2026, 10, 14, 14, 0))

    service.withdraw_participant(students[-1].participant_id, "家长请假")

    link_v1 = service.confirm_makeup(
        record.cancellation_id, first.candidate_id, idempotency_key="DEMO-CONFIRM-1"
    )

    later = service.propose_candidate(
        record.cancellation_id, "主馆", datetime(2026, 10, 15, 10, 0)
    )
    link_v2 = service.reschedule_makeup(
        record.cancellation_id, later.candidate_id, idempotency_key="DEMO-RESCHEDULE-2"
    )

    service.dispatch_notifications()
    service.advance_session(link_v2.makeup_session_id, SessionStatus.IN_PROGRESS)
    service.advance_session(link_v2.makeup_session_id, SessionStatus.SETTLED)

    context = {
        "cancellation_id": record.cancellation_id,
        "original_session_id": session.session_id,
        "final_session_id": link_v2.makeup_session_id,
        "reservation_id": reservation.reservation_id,
        "link_ids": [link_v1.link_id, link_v2.link_id],
        "participant_ids": [p.participant_id for p in students],
    }
    return service, context
