"""接口层：对外暴露 JSON 可序列化的查询视图，并提供标准库 HTTP 服务。"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

from .errors import DomainError, NotFoundError
from .service import MakeupService


class MakeupAPI:
    """只读查询接口：参与者最终归属、完成量报表、取消单全景。"""

    def __init__(self, service: MakeupService) -> None:
        self._service = service

    def participant_attribution(self, participant_id: str | None = None) -> dict:
        return self._service.participant_attribution(participant_id)

    def completion_report(self) -> dict:
        return self._service.completion_report()

    def cancellation_detail(self, cancellation_id: str) -> dict:
        return self._service.cancellation_detail(cancellation_id)

    @staticmethod
    def to_json(payload: dict) -> str:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def create_server(api: MakeupAPI, host: str = "127.0.0.1", port: int = 8000) -> HTTPServer:
    """创建只读查询 HTTP 服务。

    路由：
    - GET /health                     健康检查
    - GET /api/attributions           每个参与者最终归属
    - GET /api/attributions/<id>      单个参与者最终归属
    - GET /api/reports/completion     按补办关系去重的完成量报表
    - GET /api/cancellations/<id>     取消单全景（原因/候选/通知/补办关系）
    """

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, payload: dict) -> None:
            body = api.to_json(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - 标准库约定
            path = urlparse(self.path).path.rstrip("/") or "/"
            try:
                if path == "/health":
                    self._send(200, {"status": "ok"})
                elif path == "/api/attributions":
                    self._send(200, api.participant_attribution())
                elif path.startswith("/api/attributions/"):
                    self._send(200, api.participant_attribution(path.rsplit("/", 1)[1]))
                elif path == "/api/reports/completion":
                    self._send(200, api.completion_report())
                elif path.startswith("/api/cancellations/"):
                    self._send(200, api.cancellation_detail(path.rsplit("/", 1)[1]))
                else:
                    self._send(404, {"error": "接口不存在"})
            except NotFoundError as exc:
                self._send(404, {"error": str(exc)})
            except DomainError as exc:
                self._send(409, {"error": str(exc)})

        def log_message(self, *args: object) -> None:
            pass

    return HTTPServer((host, port), Handler)
