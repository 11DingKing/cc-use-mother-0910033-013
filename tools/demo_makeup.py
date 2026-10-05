"""端到端演示：取消登记 → 候选 → 部分退出 → 确认补办 → 改期 → 报表与归属。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from makeup_service import MakeupAPI
from makeup_service.sample import build_sample_service


if __name__ == "__main__":
    service, context = build_sample_service()
    api = MakeupAPI(service)
    payload = {
        "completion_report": api.completion_report(),
        "attributions": api.participant_attribution(),
        "cancellation_detail": api.cancellation_detail(context["cancellation_id"]),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
