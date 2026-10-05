"""基于标准库 ``http.server`` 的 JSON HTTP 接口。

路由（均为 JSON 输入输出）：

``POST   /resources``                                 建立有限资源
``POST   /events``                                    建场次
``GET    /events``                                    场次列表
``POST   /events/{id}/bookings``                      学生预约
``POST   /bookings/{id}/checkin``                     签到
``POST   /bookings/{id}/withdraw``                    学生退出
``POST   /events/{id}/complete``                      场次结算
``POST   /cancellations``                             登记取消（原因+原场次）
``GET    /cancellations/{id}``                        登记详情（可迁移预约/候选/通知）
``POST   /cancellations/{id}/candidates``             添加补办候选
``POST   /cancellations/{id}/confirm``                确认补办（原子转移）
``POST   /cancellations/{id}/reschedule``             多次改期
``POST   /cancellations/{id}/restore``                原场次恢复
``POST   /cancellations/{id}/notifications``          更新通知状态
``GET    /cancellations/{id}/participants``           每个参与者最终归属
``GET    /report/completions``                        按补办关系去重的完成量
"""
from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from .errors import ConflictError, DomainError, NotFoundError, ValidationError
from .services import Services
from .store import InMemoryStore


def _json_default(value: Any) -> Any:
    return value.value if hasattr(value, "value") else str(value)


class MakeupApp:
    """把 HTTP 路由映射到 ``Services``。"""

    def __init__(self, services: Services | None = None) -> None:
        self.services = services or Services(InMemoryStore())

    def dispatch(self, method: str, path: str, body: dict[str, Any]) -> Any:
        rules: list[tuple[str, str, Callable[..., Any]]] = [
            ("POST", r"^/resources$", lambda: self.services.create_resource(
                body.get("id"), _need(body, "name"), _need(body, "capacity"))),
            ("POST", r"^/events$", lambda: self.services.create_event(
                body.get("id"), _need(body, "title"), _need(body, "start_time"),
                _need(body, "capacity"), body.get("resource_plan"))),
            ("GET", r"^/events$", lambda: [
                e.to_dict() for e in self.services.store.events.values()
            ]),
            ("GET", r"^/events/(?P<eid>[^/]+)$", lambda eid:
                self.services._require_event(eid).to_dict()),
            ("POST", r"^/events/(?P<eid>[^/]+)/bookings$", lambda eid:
                self.services.create_booking(
                    eid, _need(body, "student_id"), body.get("student_name", ""),
                    body.get("contact", ""), body.get("id"),
                    bool(body.get("checked_in", False)))),
            ("POST", r"^/events/(?P<eid>[^/]+)/complete$", lambda eid:
                self.services.complete_event(eid)),
            ("POST", r"^/cancellations$", lambda: self.services.register_cancellation(
                _need(body, "event_id"), _need(body, "reason_code"),
                body.get("reason_note", ""))),
            ("GET", r"^/cancellations/(?P<cid>[^/]+)$", lambda cid:
                self.services.get_cancellation(cid)),
            ("POST", r"^/cancellations/(?P<cid>[^/]+)/candidates$", lambda cid:
                self.services.add_candidate(
                    cid, _need(body, "scheduled_start"), _need(body, "capacity"),
                    body.get("resource_plan"))),
            ("POST", r"^/cancellations/(?P<cid>[^/]+)/confirm$", lambda cid:
                self.services.confirm_makeup(
                    cid, _need(body, "candidate_id"), body.get("makeup_event_id"))),
            ("POST", r"^/cancellations/(?P<cid>[^/]+)/reschedule$", lambda cid:
                self.services.reschedule_makeup(
                    cid, _need(body, "scheduled_start"), _need(body, "capacity"),
                    body.get("resource_plan"))),
            ("POST", r"^/cancellations/(?P<cid>[^/]+)/restore$", lambda cid:
                self.services.restore_origin(cid)),
            ("POST", r"^/cancellations/(?P<cid>[^/]+)/notifications$", lambda cid:
                self.services.update_notification(
                    cid, _need(body, "kind"), _need(body, "status"))),
            ("GET", r"^/cancellations/(?P<cid>[^/]+)/participants$", lambda cid:
                self.services.participant_destinations(cid)),
            ("POST", r"^/bookings/(?P<bid>[^/]+)/checkin$", lambda bid:
                self.services.check_in(bid)),
            ("POST", r"^/bookings/(?P<bid>[^/]+)/withdraw$", lambda bid:
                self.services.withdraw_participant(bid, body.get("reason", ""))),
            ("GET", r"^/report/completions$", lambda: self.services.completion_report()),
        ]
        for rule_method, pattern, handler in rules:
            if rule_method != method:
                continue
            match = re.match(pattern, path)
            if match:
                return handler(**match.groupdict())
        raise NotFoundError(f"无此接口：{method} {path}")


def _need(body: dict[str, Any], key: str) -> Any:
    if key not in body:
        raise ValidationError(f"缺少必填字段：{key}")
    return body[key]


_ERROR_STATUS = {
    ValidationError: 400,
    NotFoundError: 404,
    ConflictError: 409,
}


class _Handler(BaseHTTPRequestHandler):
    app: MakeupApp

    def log_message(self, fmt: str, *args: Any) -> None:  # 静音默认访问日志
        return

    def _send(self, status: int, payload: Any) -> None:
        data = json.dumps(payload, ensure_ascii=False, default=_json_default).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _handle(self, method: str) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
            if not isinstance(body, dict):
                raise ValidationError("请求体必须是 JSON 对象")
            result = self.app.dispatch(method, self.path.split("?")[0], body)
            self._send(200 if method == "GET" else 201, result)
        except DomainError as exc:
            status = _ERROR_STATUS[type(exc)]
            self._send(status, {"error": type(exc).__name__, "message": str(exc)})
        except json.JSONDecodeError:
            self._send(400, {"error": "ValidationError", "message": "请求体不是合法 JSON"})

    def do_GET(self) -> None:  # noqa: N802
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")


def create_server(host: str = "127.0.0.1", port: int = 0,
                  app: MakeupApp | None = None) -> ThreadingHTTPServer:
    """建立 HTTP 服务；``port=0`` 时由系统分配端口。"""
    app = app or MakeupApp()
    handler_cls = type("BoundHandler", (_Handler,), {"app": app})
    server = ThreadingHTTPServer((host, port), handler_cls)
    server.app = app  # type: ignore[attr-defined]
    return server


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="活动取消补办管理后端")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    server = create_server(args.host, args.port)
    print(f"服务监听 http://{args.host}:{server.server_address[1]}")  # noqa: T201
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
