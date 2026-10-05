"""启动取消补办只读查询接口（标准库 HTTP 实现）。"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from makeup_service import MakeupAPI, MakeupService, create_server
from makeup_service.sample import build_sample_service


def main() -> None:
    parser = argparse.ArgumentParser(description="活动取消补办查询接口")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--seed-demo", action="store_true", help="载入演示数据")
    args = parser.parse_args()

    service = build_sample_service()[0] if args.seed_demo else MakeupService()
    server = create_server(MakeupAPI(service), host=args.host, port=args.port)
    print(f"查询接口已启动：http://{args.host}:{args.port}")
    print("GET /api/attributions        每个参与者最终归属")
    print("GET /api/reports/completion  按补办关系去重的完成量报表")
    print("GET /api/cancellations/<id>  取消单全景")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
