"""查询接口与 HTTP 服务的回归测试。"""
from __future__ import annotations

import http.client
import json
import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from makeup_service import MakeupAPI, NotFoundError, create_server
from makeup_service.sample import build_sample_service


class ApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service, self.context = build_sample_service()
        self.api = MakeupAPI(self.service)

    def test_completion_report_is_json_serializable_and_deduplicated(self) -> None:
        report = self.api.completion_report()
        json.dumps(report, ensure_ascii=False)
        self.assertEqual(report["totals"]["已完成"], 1)
        self.assertEqual(report["totals"]["chains"], 1)
        chain = report["chains"][0]
        self.assertEqual(chain["versions"], 2)
        self.assertEqual(chain["final_session_id"], self.context["final_session_id"])
        self.assertEqual(chain["participant_count"], 5)

    def test_attribution_shows_final_home_for_every_participant(self) -> None:
        payload = self.api.participant_attribution()
        json.dumps(payload, ensure_ascii=False)
        items = payload["items"]
        self.assertEqual(len(items), 6)
        withdrawn = [row for row in items if row["state"] == "已退出"]
        assigned = [row for row in items if row["state"] == "已归属"]
        self.assertEqual(len(withdrawn), 1)
        self.assertEqual(len(assigned), 5)
        for row in assigned:
            self.assertEqual(row["session_id"], self.context["final_session_id"])
            self.assertEqual(row["session_status"], "已结算")
            self.assertEqual(len(row["chain"]), 3)

    def test_cancellation_detail_contains_registration_context(self) -> None:
        detail = self.api.cancellation_detail(self.context["cancellation_id"])
        json.dumps(detail, ensure_ascii=False)
        self.assertEqual(detail["cancellation"]["reason"], "场馆临时停用通知：消防检修")
        self.assertEqual(detail["cancellation"]["status"], "已补办")
        self.assertEqual(detail["original_session"]["session_id"], self.context["original_session_id"])
        self.assertEqual(detail["migratable_reservations"], [])
        self.assertEqual(len(detail["candidates"]), 3)
        self.assertEqual(len(detail["links"]), 2)
        kinds = [n["kind"] for n in detail["notifications"]]
        self.assertEqual(kinds, ["取消通知", "补办确认通知", "改期通知"])
        self.assertTrue(all(n["status"] == "已发送" for n in detail["notifications"]))

    def test_unknown_entities_raise_not_found(self) -> None:
        with self.assertRaises(NotFoundError):
            self.api.cancellation_detail("CAN-9999")
        with self.assertRaises(NotFoundError):
            self.api.participant_attribution("PTP-9999")


class HttpServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        service, cls.context = build_sample_service()
        cls.server = create_server(MakeupAPI(service), port=0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def get(self, path: str) -> tuple[int, dict]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request("GET", path)
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        connection.close()
        return response.status, payload

    def test_completion_report_endpoint(self) -> None:
        status, payload = self.get("/api/reports/completion")
        self.assertEqual(status, 200)
        self.assertEqual(payload["totals"]["已完成"], 1)

    def test_attribution_endpoints(self) -> None:
        status, payload = self.get("/api/attributions")
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["items"]), 6)

        pid = self.context["participant_ids"][0]
        status, payload = self.get(f"/api/attributions/{pid}")
        self.assertEqual(status, 200)
        self.assertEqual(payload["items"][0]["participant_id"], pid)

        status, payload = self.get("/api/attributions/PTP-9999")
        self.assertEqual(status, 404)

    def test_cancellation_detail_endpoint(self) -> None:
        cid = self.context["cancellation_id"]
        status, payload = self.get(f"/api/cancellations/{cid}")
        self.assertEqual(status, 200)
        self.assertEqual(payload["cancellation"]["cancellation_id"], cid)

        status, _ = self.get("/api/cancellations/CAN-9999")
        self.assertEqual(status, 404)

    def test_unknown_route_returns_404(self) -> None:
        status, payload = self.get("/nope")
        self.assertEqual(status, 404)
        self.assertIn("error", payload)


if __name__ == "__main__":
    unittest.main()
