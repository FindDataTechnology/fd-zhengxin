"""参照系 REST 消费面（D3）：service_root 地址语义 + regions/anchor 实调形态。

参照系 REST 锚点面（/api/v1/regions、/api/v1/concepts/{topic}/anchor）已落定
（fd-zhengxin-reference rest_api.py），本文件固化主服务消费面的接线：同一
FDZX__REFERENCE_BASE_URL 配 MCP 端点（…/mcp）时 REST 面自动剥后缀走服务根，
不再恒 degraded；宽容解析两层（regions/results、key_points/premises）保留。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, socketserver

from fd_zhengxin_api import reference_client


def test_service_root_strips_mcp_suffix(monkeypatch) -> None:
    """/mcp 端点形态 → REST 根；服务根形态原样（一个变量两副面孔，D3 修复）。"""
    monkeypatch.setenv("FDZX__REFERENCE_BASE_URL", "http://ref.local:8031/mcp")
    assert reference_client.service_root() == "http://ref.local:8031"
    monkeypatch.setenv("FDZX__REFERENCE_BASE_URL", "http://ref.local:8031/")
    assert reference_client.service_root() == "http://ref.local:8031"
    monkeypatch.delenv("FDZX__REFERENCE_BASE_URL", raising=False)
    assert reference_client.service_root() == ""


class _RefRestHandler(BaseHTTPRequestHandler):
    """最小参照系 REST 桩：按真实 rest_api.py 的响应形态回放。"""

    regions = {
        "regions": [{"name": "儒家", "kind": "school", "id": "seed:china/school/ru"},
                    {"name": "自由主义思潮", "kind": "current", "id": "modern:china/current/liberalism"}],
        "source_status": {"china-seed": {"status": "ok", "snapshot": "china-seed@1.0.0#x"}},
    }
    anchor = {
        "key_points": ["以不忍人之心行不忍人之政。", "原典：《孟子·梁惠王上》（ctext 只链不抄）"],
        "term": "仁政", "source": "china-seed",
    }
    miss = {"key_points": [], "reason": "not_ingested", "hint": "库内概念锚无 'zzz'"}

    def log_message(self, *args):  # 静音
        pass

    def do_GET(self):  # noqa: N802
        from urllib.parse import unquote

        path = unquote(self.path)
        if path == "/api/v1/regions":
            body, status = json.dumps(self.regions, ensure_ascii=False), 200
        elif path == "/api/v1/concepts/仁政/anchor":
            body, status = json.dumps(self.anchor, ensure_ascii=False), 200
        elif path == "/api/v1/concepts/zzz/anchor":
            body, status = json.dumps(self.miss, ensure_ascii=False), 200
        else:
            body, status = "{}", 404
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def _start_stub() -> socketserver.TCPServer:
    socketserver.TCPServer.allow_reuse_address = True
    srv = socketserver.TCPServer(("127.0.0.1", 0), _RefRestHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_fetch_region_taxonomy_and_anchor_over_http(monkeypatch) -> None:
    """MCP 端点形态的 base URL 下 regions/anchor 实调通（D3 接线后的正路）。"""
    srv = _start_stub()
    try:
        monkeypatch.setenv(
            "FDZX__REFERENCE_BASE_URL",
            f"http://127.0.0.1:{srv.server_address[1]}/mcp",
        )
        names, reason = reference_client.fetch_region_taxonomy()
        assert reason is None and names == ["儒家", "自由主义思潮"]

        premises, reason = reference_client.fetch_concept_anchor("仁政")
        assert reason is None
        assert premises == [
            "以不忍人之心行不忍人之政。",
            "原典：《孟子·梁惠王上》（ctext 只链不抄）",
        ]

        # 未命中：200 + 空 key_points → no_anchor（区别于不可达，不冒充故障）
        premises, reason = reference_client.fetch_concept_anchor("zzz")
        assert premises is None and reason == "no_anchor"
    finally:
        srv.shutdown()


def test_fetch_degrades_when_unreachable(monkeypatch) -> None:
    monkeypatch.setenv("FDZX__REFERENCE_BASE_URL", "http://127.0.0.1:9/mcp")
    names, reason = reference_client.fetch_region_taxonomy()
    assert names is None and reason.startswith("unreachable:")
    premises, reason = reference_client.fetch_concept_anchor("仁政")
    assert premises is None and reason.startswith("unreachable:")


def test_fetch_not_configured(monkeypatch) -> None:
    monkeypatch.delenv("FDZX__REFERENCE_BASE_URL", raising=False)
    assert reference_client.fetch_region_taxonomy() == (None, "not_configured")
    assert reference_client.fetch_concept_anchor("仁政") == (None, "not_configured")
