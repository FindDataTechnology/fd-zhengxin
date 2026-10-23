"""三闭环（task 4.2/4.3/4.4）验证：定位 / 多视角 / 谱系 + 参照系 MCP 客户端。

全离线：参照系服务未部署，用注入 transport 的响应桩模拟远端 MCP——桩的行
结构照 fd-zhengxin-reference tools_placeholder.py 的真实契约（D2 对齐后：
行锚 = provenance.stable_id，种子概念行带 school/figures/weight，种子边行带
source_name/target_name/school/weight/key_text/target_years）。LLM 全部
monkeypatch 不真调网。守卫拦路径用真实规则库 v1（当代单视角 R-BAL-001、
预测句式 R-PREDICT-001）。

跨仓真实契约的固化测试另见 test_reference_contract.py（fixture 取自参照系
真实服务的工具返回，防两仓契约再漂移）。
"""

from __future__ import annotations

import json
import threading

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fd_zhengxin_api import genealogy, llm as llm_mod, locate, looputil, perspectives
from fd_zhengxin_api import reference_mcp
from fd_zhengxin_api.reference_mcp import ReferenceClient

app = FastAPI()
app.include_router(locate.router)
app.include_router(perspectives.router)
app.include_router(genealogy.router)
client = TestClient(app)
AUTH = {"Authorization": "Bearer dev-tok-alice"}

BASE_URL = "http://reference.test/mcp"


# ---------------------------------------------------------------------------
# 响应桩：远端 MCP 参照系（真实服务未部署，测试不打网）
#
# transport 签名 (tool, arguments, timeout) -> payload dict（D1 后 envelope
# 由 mcp_http.MCPSessionTransport 解包，桩直接回工具 payload）。
# ---------------------------------------------------------------------------


def _not_ingested(tool: str) -> dict:
    """未路由调用的缺省桩：空结果 + not_ingested（照参照系真实口径）。"""
    return {"results": [], "reason": "not_ingested",
            "hint": f"桩未路由 {tool}：not_ingested（参照系空结果必带 taxonomy reason）"}


def _stub(routes: dict):
    """按 (tool, sorted(args)) 精确路由的 transport 桩；值异常则抛（注失败）。"""

    def transport(tool: str, arguments: dict, timeout: float) -> dict:
        norm = tuple(sorted(
            (k, tuple(v) if isinstance(v, list) else v) for k, v in arguments.items()
        ))
        key = (tool, norm)
        resp = routes.get(key)
        if isinstance(resp, Exception):
            raise resp
        return resp if resp is not None else _not_ingested(tool)

    return transport


def _use_reference(monkeypatch, routes: dict) -> None:
    stub_client = ReferenceClient(base_url=BASE_URL, transport=_stub(routes))
    monkeypatch.setattr(reference_mcp, "client", lambda: stub_client)


def _prov(source: str, sid: str) -> dict:
    return {"provenance": {"source": source, "stable_id": sid, "as_of": "2026-10-23",
                           "snapshot_version": f"{source}@1.0.0#abc123def456"}}


# ---- 行结构照参照系真实契约（D2 对齐） ----


def _concept_row(term: str, *, school: str = "", figures: list[dict] | None = None,
                 weight: float | None = None, snippet: str = "") -> dict:
    """种子/近现代概念行（china_lineage 物化形态 + D2 行结构补齐）。"""
    row: dict = {
        "source": "china-seed",
        "term": term,
        "title": term,
        "url": f"https://zh.wikipedia.org/wiki/{term}#china-seed",
        "snippet": snippet or f"{term}的通说界定。｜原典：《某书·某篇》（ctext 只链不抄）",
        "retrieved_at": "2026-10-23T00:00:00+00:00",
        "provenance": {
            "source": "china-seed", "stable_id": f"china-seed:concept:{term}",
            "as_of": "2026-10-23",
            "snapshot_version": "china-seed@1.0.0#5415dd20d9b3",
        },
    }
    if school:
        row["school"] = school
        row["weight"] = 1.0 if weight is None else weight
    if figures is not None:
        row["figures"] = figures
    return row


def _edge_row(eid_suffix: str, src: str, tgt: str, *, kind: str, polarity: str,
              school: str = "", weight: float | None = None,
              target_years: str | None = None, key_text: str = "") -> dict:
    """种子关系行（relations 表形态 + D2 行结构补齐）。"""
    row: dict = {
        "source": "china-seed",
        "source_id": f"seed:china/thinker/{eid_suffix}-src",
        "target_id": f"seed:china/thinker/{eid_suffix}-tgt",
        "source_name": src,
        "target_name": tgt,
        "kind": kind,
        "polarity": polarity,
        "snapshot_version": "china-seed@1.0.0#5415dd20d9b3",
        "key_text": key_text,
    }
    if school:
        row["source_school"] = school
        row["target_school"] = school
        row["school"] = school
        row["weight"] = 1.0 if weight is None else weight
    if target_years is not None:
        row["target_years"] = target_years
    row["provenance"] = {
        "source": "china-seed",
        "stable_id": f"china-seed:relation:{eid_suffix}",
        "as_of": "2026-10-23",
        "snapshot_version": "china-seed@1.0.0#5415dd20d9b3",
    }
    return row


def _ids(*rows: dict) -> set[str]:
    """桩行的引用锚集合（provenance.stable_id，D2 行结构）。"""
    return {r["provenance"]["stable_id"] for r in rows}


SEP_ROW = {
    "source": "sep",
    "title": "Contractarianism",
    "url": "https://plato.stanford.edu/entries/contractarianism/",
    "snippet": "",
    "provenance": {"source": "sep", "stable_id": "https://plato.stanford.edu/entries/contractarianism/",
                   "as_of": "2026-10-23", "snapshot_version": None},
}

FAZHI_FIGURES = [
    {"name": "韩非", "years": "约前280–前233"},
    {"name": "卫鞅（商鞅）", "years": "前390–前338"},
]
FAZHI_CONCEPT = {
    "results": [
        _concept_row("法治（以法为本）", school="法家", figures=FAZHI_FIGURES,
                     snippet="以法为本、刑过不避大臣的治理方案。｜原典：《韩非子·定法》"),
    ],
    **_prov("china-seed", "china-seed:concept:法治（以法为本）"),
}
RENZHENG_CONCEPT = {
    "results": [
        _concept_row("仁政", school="儒家", figures=[{"name": "孟轲（孟子）", "years": "约前372–前289"}],
                     snippet="以不忍人之心行不忍人之政。｜原典：《孟子·梁惠王上》"),
    ],
    **_prov("china-seed", "china-seed:concept:仁政"),
}

EDGE_SHANGYANG_HANFEI = _edge_row(
    "fa1", "卫鞅（商鞅）", "韩非", kind="influence", polarity="positive",
    school="法家", target_years="约前280–前233", key_text="韩非称「商君教秦孝公以壹赏、壹刑、壹教」",
)
EDGE_HANFEI_HANFEI_CRIT = _edge_row(
    "fa2", "韩非", "卫鞅（商鞅）", kind="critique", polarity="critical",
    school="法家", target_years="前390–前338", key_text="「二子之于法术，皆未尽善也」",
)
EDGE_NO_YEAR = _edge_row(
    "fa3", "韩非", "卫鞅（商鞅）", kind="debate", polarity="critical",
    school="法家", target_years=None, key_text="年代不可考的论战边",
)
EDGE_HOPOUT = _edge_row(
    "fa4", "李斯", "韩非", kind="influence", polarity="positive",
    school="法家", target_years=None, key_text="hop2 边（人物对闭合）",
)

FA_ROUTES = {
    ("concept_lookup", (("term", "法治（以法为本）"),)): FAZHI_CONCEPT,
    ("concept_lookup", (("term", "仁政"),)): RENZHENG_CONCEPT,
    ("concept_lookup", (("term", "social contract"),)): {"results": [SEP_ROW], **_prov("sep", SEP_ROW["url"])},
    ("relation_query", (("source", "韩非"), ("target", "卫鞅（商鞅）"))):
        {"results": [EDGE_SHANGYANG_HANFEI, EDGE_HANFEI_HANFEI_CRIT, EDGE_NO_YEAR],
         **_prov("china-seed", "china-seed:relation:fa1")},
    # hop2：人物对闭合再配对（空结果带原因 → 下钻断链如实可见）
    ("relation_query", (("source", "李斯"), ("target", "韩非"))):
        {"results": [], "reason": "not_ingested", "hint": "李斯 与 韩非 均在种子谱系，但两者之间无边"},
}


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("FDZX__DB", str(tmp_path / "personal.db"))
    monkeypatch.setenv("FDZX__LEDGER_DB", str(tmp_path / "ledger.db"))
    monkeypatch.setenv("FDZX__AUTH_DEV_TOKENS", "dev-tok-alice=alice")
    monkeypatch.setenv("FDZX__REFERENCE_BASE_URL", BASE_URL)
    monkeypatch.delenv("FDZX__LLM_API_KEY", raising=False)


def _llm_on(monkeypatch, reply) -> None:
    """LLM 网关桩：is_configured=True + chat 返回固定文本（不真调网）。"""
    monkeypatch.setattr(llm_mod, "is_configured", lambda: True)
    monkeypatch.setattr(llm_mod, "chat_with_usage", lambda *a, **k: {"content": reply, "usage": None})


# ---------------------------------------------------------------------------
# 参照系 MCP 客户端（契约封装与结构化降级；D1 会话传输）
# ---------------------------------------------------------------------------


class TestReferenceClient:
    def test_ok_passthrough(self) -> None:
        c = ReferenceClient(base_url=BASE_URL, transport=_stub(FA_ROUTES))
        r = c.concept_lookup("法治（以法为本）")
        assert r.ok and r.data["results"][0]["school"] == "法家"
        assert r.data["results"][0]["provenance"]["stable_id"] == "china-seed:concept:法治（以法为本）"

    def test_contract_tool_signatures(self) -> None:
        """D2 参数名照参照系 tools/list 实测：ideology_query(name)、
        relation_query(source,target[,polarity])。"""
        seen: list[tuple[str, dict]] = []

        def transport(tool, arguments, timeout):
            seen.append((tool, arguments))
            return {"results": [], "reason": "not_ingested", "hint": "x"}

        c = ReferenceClient(base_url=BASE_URL, transport=transport)
        c.ideology_query("Q42")
        c.relation_query("孔子", "曾参（曾子）")
        c.relation_query("孔子", "曾参（曾子）", polarity="positive")
        assert seen[0] == ("ideology_query", {"name": "Q42"})
        assert seen[1] == ("relation_query", {"source": "孔子", "target": "曾参（曾子）"})
        assert seen[2][1]["polarity"] == "positive"

    def test_other_tools_on_contract(self) -> None:
        routes = {
            ("source_status", ()): {"results": [{"source": "china-seed", "status": "ok"}]},
            ("regime_timeseries", (("country", "sweden"), ("years", (1990, 2020)))):
                {"results": [{"country_code": "SWE", "year": 1990, "indicators": {"v2x_polyarchy": 0.9}}]},
        }
        c = ReferenceClient(base_url=BASE_URL, transport=_stub(routes))
        assert c.status().ok
        assert c.regime_timeseries("sweden", years=[1990, 2020]).ok
        # 契约外的调用（未路由）→ 空结果带 not_ingested 原因
        r = c.ideology_query("Q40430")
        assert not r.ok and r.reason == "not_ingested" and "not_ingested" in r.detail

    def test_empty_with_taxonomy_reason_degrades(self) -> None:
        c = ReferenceClient(base_url=BASE_URL, transport=_stub({}))
        r = c.relation_query("甲", "乙")
        assert not r.ok and r.reason == "not_ingested"
        assert "not_ingested" in r.detail

    def test_unreachable_vs_upstream(self) -> None:
        down = ReferenceClient(
            base_url=BASE_URL,
            transport=_stub({("concept_lookup", (("term", "x"),)):
                             reference_mcp.RefUnreachable("ConnectError: refused")}),
        )
        assert down.concept_lookup("x").reason == "unreachable"
        bad = ReferenceClient(
            base_url=BASE_URL,
            transport=_stub({("concept_lookup", (("term", "x"),)):
                             reference_mcp.RefUpstream("HTTP 503")}),
        )
        assert bad.concept_lookup("x").reason == "upstream"

    def test_raw_transport_error_classification(self) -> None:
        """共用会话传输抛出的原始异常分类：网络层→unreachable，协议层→upstream。"""
        net = ReferenceClient(
            base_url=BASE_URL,
            transport=_stub({("concept_lookup", (("term", "x"),)): OSError("connection reset")}),
        )
        assert net.concept_lookup("x").reason == "unreachable"
        proto = ReferenceClient(
            base_url=BASE_URL,
            transport=_stub({("concept_lookup", (("term", "x"),)):
                             ValueError("JSON-RPC 错误响应：{'code': -32601}")}),
        )
        assert proto.concept_lookup("x").reason == "upstream"
        assert "-32601" in proto.concept_lookup("x").detail

    def test_empty_without_reason_is_upstream(self) -> None:
        bad = ReferenceClient(
            base_url=BASE_URL,
            transport=_stub({("concept_lookup", (("term", "x"),)): {"results": []}}),
        )
        r = bad.concept_lookup("x")
        assert not r.ok and r.reason == "upstream" and "未带合法 reason" in r.detail

    def test_not_configured_short_circuits(self) -> None:
        called = []

        def transport(tool, arguments, timeout):  # pragma: no cover —— 不该被调
            called.append(tool)
            return {}

        r = ReferenceClient(base_url="   ", transport=transport).concept_lookup("x")
        assert not r.ok and r.reason == "not_configured"
        assert called == []  # 未配置不发包，不冒充远端故障

    def test_session_handshake_roundtrip(self) -> None:
        """D1 核心回归：真实传输必须 initialize → 带 Mcp-Session-Id 的
        tools/call（双响应体：initialize 回 SSE 帧、tools/call 回 JSON）。"""
        import http.server
        import socketserver

        from fd_zhengxin_api.mcp_http import MCPSessionTransport

        seen: list[tuple[str, str | None]] = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):  # 静音
                pass

            def _send(self, status, payload: bytes, headers: dict | None = None):
                self.send_response(status)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                if payload:
                    self.wfile.write(payload)

            def do_POST(self):  # noqa: N802
                req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                seen.append((str(req.get("method")), self.headers.get("Mcp-Session-Id")))
                if req.get("method") == "initialize":
                    body = json.dumps({
                        "jsonrpc": "2.0", "id": req["id"],
                        "result": {"protocolVersion": "2025-03-26", "capabilities": {},
                                   "serverInfo": {"name": "t", "version": "0"}},
                    }).encode()
                    sse = b"event: message\ndata: " + body + b"\n\n"
                    self._send(200, sse, {"Content-Type": "text/event-stream",
                                          "Mcp-Session-Id": "sess-7"})
                elif req.get("method") == "notifications/initialized":
                    self._send(202, b"")
                else:  # tools/call（JSON 响应体）
                    inner = {"results": [{"term": "仁政", "school": "儒家"}]}
                    body = json.dumps({
                        "jsonrpc": "2.0", "id": req["id"],
                        "result": {"content": [{"type": "text",
                                                "text": json.dumps(inner, ensure_ascii=False)}],
                                   "isError": False},
                    }).encode()
                    self._send(200, body, {"Content-Type": "application/json"})

        socketserver.TCPServer.allow_reuse_address = True
        srv = socketserver.TCPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            c = ReferenceClient(base_url=f"http://127.0.0.1:{srv.server_address[1]}/mcp")
            r = c.concept_lookup("仁政")
            assert r.ok and r.data["results"][0]["school"] == "儒家"
            methods = [m for m, _ in seen]
            assert methods[0] == "initialize"          # 先握手（D1）
            assert "notifications/initialized" in methods
            assert ("tools/call", "sess-7") in seen     # 会话头随 tools/call 回传
        finally:
            srv.shutdown()


# ---------------------------------------------------------------------------
# 定位闭环（4.2）
# ---------------------------------------------------------------------------


def test_locate_success_deterministic_with_sources(monkeypatch) -> None:
    """定位成功：概念锚直挂归派（儒家，锚=概念行 stable_id）+ 边归派（法家，
    边行 stable_id）+ 先例链按年排序（-390/-280），engine 标注。"""
    _use_reference(monkeypatch, FA_ROUTES)
    r = client.post("/api/v1/loops/locate",
                    json={"text": "法治（以法为本）与仁政之辨", "is_contemporary": False},
                    headers=AUTH)
    assert r.status_code == 200, r.text
    out = r.json()

    assert out["engine"] == "deterministic"  # 无 KEY 确定性路径必须标注
    assert out["positioned"] is True
    # 概念锚：来自参照系 concept_lookup 行（ref=provenance.stable_id）
    refs = {a["ref"] for a in out["anchors"]}
    assert "china-seed:concept:法治（以法为本）" in refs
    assert all(a["url"].startswith("https://zh.wikipedia.org/wiki/") or a["url"] == ""
               for a in out["anchors"])
    # 归派：法家=概念直挂+边聚合按学派合并；儒家=仁政概念行直挂；依据指向行 stable_id
    edge_ids = _ids(EDGE_SHANGYANG_HANFEI, EDGE_HANFEI_HANFEI_CRIT, EDGE_NO_YEAR)
    anchor_ids = {a["ref"] for a in out["anchors"] if a["school"]}
    assert [a["school"] for a in out["attributions"]] == ["法家", "儒家"]  # 证据数并列排序
    for a in out["attributions"]:
        assert a["strength"] >= out["min_position_strength"]
        assert a["evidence"] and set(a["evidence"]) <= (edge_ids | anchor_ids)
    # 先例链：curated 年代串保守解析（前390–前338→-390；约前280–前233→-280）
    years = [ring["year"] for ring in out["precedent_chain"]]
    assert years == sorted(years) == [-390, -280]
    assert all(ring["ref"] in edge_ids for ring in out["precedent_chain"])
    assert all(ring["note"] for ring in out["precedent_chain"])  # 关键文本随环转述
    # 缺年代边如实记缺口（hop 对里有无年代边），不编造年代
    assert any(g["reason"] == "no_year" for g in out["gaps"])
    assert out["guard"]["action"] is None  # 干净放行


def test_locate_anchor_direct_attribution_only(monkeypatch) -> None:
    """单人物概念锚（无关系边）：概念行 school 直挂即带源定位（D2 主路径）。"""
    _use_reference(monkeypatch, {
        ("concept_lookup", (("term", "仁政"),)): RENZHENG_CONCEPT,
    })
    r = client.post("/api/v1/loops/locate", json={"text": "仁政"}, headers=AUTH)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["positioned"] is True
    assert [a["school"] for a in out["attributions"]] == ["儒家"]
    assert out["attributions"][0]["evidence"] == ["china-seed:concept:仁政"]
    assert "不忍人之政" in out["attributions"][0]["stance"]
    assert out["precedent_chain"] == []  # 无边无链，缺口可见不编造
    assert any(g["reason"] == "no_pair_to_query" for g in out["gaps"])


def test_locate_contemporary_coordinates(monkeypatch) -> None:
    """当代对象：定位之外补 ideology_query 坐标（name=标签或 QID；行锚=stable_id）。"""
    ideology = {
        "results": [
            {"source": "wikidata", "qid": "Q42", "anchor_pid": "P1142",
             "anchor_qid": "Q8337", "anchor_label": "自由主义",
             "provenance": {"source": "wikidata", "stable_id": "Q42", "as_of": "2026-10-23"}},
        ],
        **_prov("wikidata", "Q42"),
    }
    routes = dict(FA_ROUTES)
    routes[("concept_lookup", (("term", "仁政"),))] = RENZHENG_CONCEPT
    routes[("ideology_query", (("name", "法治（以法为本）"),))] = ideology
    routes[("ideology_query", (("name", "仁政"),))] = {
        "results": [], "reason": "not_ingested", "hint": "无坐标锚（如实降级）"}
    _use_reference(monkeypatch, routes)
    r = client.post("/api/v1/loops/locate",
                    json={"text": "法治（以法为本）与仁政之辨", "is_contemporary": True},
                    headers=AUTH)
    assert r.status_code == 200, r.text
    coords = r.json()["coordinates"]
    assert len(coords) == 1
    assert coords[0]["ref"] == "Q42"
    assert coords[0]["anchors"][0]["axis"] == "P1142"
    assert coords[0]["anchors"][0]["value"] == "自由主义"


def test_locate_low_evidence_not_forced(monkeypatch) -> None:
    """不硬归派：全部候选低于阈值 → positioned=false + 缺口说明，不凑数。"""
    weak_concept = {
        "results": [
            _concept_row("仁政", school="儒家", figures=[{"name": "孟轲（孟子）", "years": "约前372–前289"}],
                         weight=0.10),
        ],
    }
    _use_reference(monkeypatch, {
        ("concept_lookup", (("term", "仁政"),)): weak_concept,
    })
    r = client.post("/api/v1/loops/locate", json={"text": "仁政"}, headers=AUTH)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["positioned"] is False
    assert out["attributions"] == []
    insuff = [g for g in out["gaps"] if g["reason"] == "insufficient_evidence"]
    assert insuff and "不硬归派" in insuff[0]["detail"]


def test_locate_llm_rerank_engine(monkeypatch) -> None:
    """有 KEY：LLM 只做重排（index-only），engine=llm；候选与依据仍来自参照系。"""
    routes = dict(FA_ROUTES)
    routes[("concept_lookup", (("term", "仁政"),))] = RENZHENG_CONCEPT
    _use_reference(monkeypatch, routes)
    _llm_on(monkeypatch, '{"indices": [2, 1]}')
    r = client.post("/api/v1/loops/locate",
                    json={"text": "法治（以法为本）与仁政之辨"}, headers=AUTH)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["engine"] == "llm"
    assert [a["school"] for a in out["attributions"]] == ["儒家", "法家"]
    edge_ids = _ids(EDGE_SHANGYANG_HANFEI, EDGE_HANFEI_HANFEI_CRIT, EDGE_NO_YEAR)
    anchor_ids = {a["ref"] for a in out["anchors"] if a["school"]}
    assert all(set(a["evidence"]) <= (edge_ids | anchor_ids) for a in out["attributions"])


def test_locate_llm_invalid_falls_back(monkeypatch) -> None:
    """LLM 返回非法（越界编号）→ 降级确定性路径并记 llm_fallback 缺口。"""
    _use_reference(monkeypatch, FA_ROUTES)
    _llm_on(monkeypatch, '{"indices": [99]}')
    r = client.post("/api/v1/loops/locate",
                    json={"text": "法治（以法为本）之辨"}, headers=AUTH)
    out = r.json()
    assert out["engine"] == "deterministic"
    assert any(g["reason"] == "llm_fallback" for g in out["gaps"])


def test_locate_reference_unreachable_degrades(monkeypatch) -> None:
    """远端参照系 unreachable → 200 + 结构化降级原因（gaps 带 reason）。"""
    _use_reference(monkeypatch, {
        ("concept_lookup", (("term", "仁政"),)):
            reference_mcp.RefUnreachable("ConnectError: refused"),
    })
    r = client.post("/api/v1/loops/locate", json={"text": "仁政"}, headers=AUTH)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["positioned"] is False and out["anchors"] == []
    assert out["gaps"][0]["stage"] == "reference"
    assert out["gaps"][0]["reason"] == "unreachable"
    assert "concept_lookup" in out["gaps"][0]["detail"]


def test_loops_unauthorized(monkeypatch) -> None:
    _use_reference(monkeypatch, FA_ROUTES)
    for path, body in (
        ("/api/v1/loops/locate", {"text": "仁政"}),
        ("/api/v1/loops/perspectives", {"topic": "仁政"}),
        ("/api/v1/loops/genealogy", {"term": "仁政"}),
    ):
        assert client.post(path, json=body).status_code == 401


# ---------------------------------------------------------------------------
# 多视角闭环（4.3）
# ---------------------------------------------------------------------------


def test_perspectives_sourced_views_deterministic(monkeypatch) -> None:
    """多视角带源：确定性模板转述各学派出典立场（锚=概念行/边行 stable_id）。"""
    routes = dict(FA_ROUTES)
    routes[("concept_lookup", (("term", "仁政"),))] = RENZHENG_CONCEPT
    _use_reference(monkeypatch, routes)
    r = client.post("/api/v1/loops/perspectives",
                    json={"topic": "法治（以法为本）与仁政之辨"}, headers=AUTH)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["is_contemporary"] is False  # 词面无当代标记，保守默认
    assert out["engine"] == "deterministic"
    views = out["views"]
    assert [v["school"] for v in views] == ["法家", "儒家"]  # 强度并列按召回序
    edge_ids = _ids(EDGE_SHANGYANG_HANFEI, EDGE_HANFEI_HANFEI_CRIT, EDGE_NO_YEAR)
    anchor_ids = {"china-seed:concept:仁政", "china-seed:concept:法治（以法为本）"}
    for v in views:
        assert v["form"] == "sourced-stance"  # 无 KEY 不冒充 steel-man
        assert v["anchors"] and set(v["anchors"]) <= (edge_ids | anchor_ids)
    # 概念锚直挂学派（儒家）无关系边也能呈现带源视角
    ru = next(v for v in views if v["school"] == "儒家")
    assert ru["anchors"] == ["china-seed:concept:仁政"]
    dis = out["disagreement"]
    assert dis["shared_premises"] and dis["real_disagreement"] and dis["conditions"]
    assert out["guard"]["action"] is None


def test_perspectives_single_view_blocked(monkeypatch) -> None:
    """当代议题单视角：views<2 学派被守卫 R-BAL-001 拦（422 结构化返回）。"""
    _use_reference(monkeypatch, {
        ("concept_lookup", (("term", "仁政"),)): RENZHENG_CONCEPT,
    })
    r = client.post("/api/v1/loops/perspectives",
                    json={"topic": "仁政", "is_contemporary": True},
                    headers=AUTH)
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["reason"] == "guard_blocked"
    rule_ids = {x["rule_id"] for x in detail["guard"]["reasons"]}
    assert rule_ids == {"R-BAL-001"}
    assert detail["is_contemporary"] is True


def test_perspectives_weak_anchor_not_steelman(monkeypatch) -> None:
    """弱解读不冒充 steel-man：最强锚低于阈值的学派不进 views，记入 gaps。"""
    weak = {
        "results": [
            _concept_row("功利主义概念", school="功利主义", figures=[], weight=0.2),
        ],
    }
    _use_reference(monkeypatch, {
        ("concept_lookup", (("term", "义利之辨"),)): weak,
    })
    r = client.post("/api/v1/loops/perspectives",
                    json={"topic": "义利之辨之争", "is_contemporary": False},
                    headers=AUTH)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["views"] == []  # 弱学派未呈现
    weak_g = [g for g in out["gaps"] if g["reason"] == "weak_anchor"]
    assert weak_g and "功利主义" in weak_g[0]["detail"]


def test_perspectives_llm_steelman_citations_validated(monkeypatch) -> None:
    """LLM steel-man：编造引用剥除（宁删勿假），engine=llm，正文过守卫。"""
    routes = dict(FA_ROUTES)
    routes[("concept_lookup", (("term", "仁政"),))] = RENZHENG_CONCEPT
    _use_reference(monkeypatch, routes)
    reply = json.dumps({
        "views": [
            {"index": 1, "steel_man": "法家的最强形态是：以法为本、信赏必罚，把治理从个人德性移到公开规则。",
             "citations": ["china-seed:concept:法治（以法为本）", "wd:FAKE:madeup"]},
            {"index": 2, "steel_man": "儒家的最强形态是：以不忍人之心行不忍人之政，德治教化先于刑罚。",
             "citations": ["china-seed:concept:仁政"]},
        ],
        "disagreement": {
            "shared_premises": ["双方都承认治理需要秩序与民心"],
            "real_disagreement": ["秩序靠公开规则还是德性教化"],
            "conditions": ["若规则可自我执行，德治论需回应其冗余风险"],
        },
    }, ensure_ascii=False)
    _llm_on(monkeypatch, reply)
    r = client.post("/api/v1/loops/perspectives",
                    json={"topic": "法治（以法为本）与仁政之辨"}, headers=AUTH)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["engine"] == "llm"
    assert [v["form"] for v in out["views"]] == ["steel-man", "steel-man"]
    valid = {"china-seed:concept:法治（以法为本）", "china-seed:concept:仁政"}
    for v in out["views"]:
        assert set(v["anchors"]) <= valid
    assert "wd:FAKE:madeup" not in out["views"][0]["anchors"]  # 编造引用剥除
    assert out["disagreement"]["real_disagreement"] == ["秩序靠公开规则还是德性教化"]
    assert out["guard"]["action"] is None


def test_perspectives_llm_no_citation_falls_to_template(monkeypatch) -> None:
    """LLM 视图无有效引用锚 → 该学派回退确定性模板（无锚不出）。"""
    _use_reference(monkeypatch, FA_ROUTES)
    reply = json.dumps({
        "views": [{"index": 1, "steel_man": "没有引用的解读。",
                   "citations": ["wd:FAKE:madeup"]}],
    }, ensure_ascii=False)
    _llm_on(monkeypatch, reply)
    r = client.post("/api/v1/loops/perspectives",
                    json={"topic": "法治（以法为本）之辨"}, headers=AUTH)
    out = r.json()
    assert out["engine"] == "deterministic"  # 全部回退模板
    assert {v["form"] for v in out["views"]} == {"sourced-stance"}


def test_perspectives_prediction_body_blocked(monkeypatch) -> None:
    """守卫拦路径：LLM steel-man 里混入预测句式（将会赢得）→ 整产物被拦。"""
    routes = dict(FA_ROUTES)
    routes[("concept_lookup", (("term", "仁政"),))] = RENZHENG_CONCEPT
    _use_reference(monkeypatch, routes)
    reply = json.dumps({
        "views": [
            {"index": 1,
             "steel_man": "该阵营断言其候选人将会赢得来年选举，并以此论证规则之治的民意基础。",
             "citations": ["china-seed:concept:法治（以法为本）"]},
            {"index": 2, "steel_man": "儒家转述：以不忍人之心行不忍人之政。",
             "citations": ["china-seed:concept:仁政"]},
        ],
    }, ensure_ascii=False)
    _llm_on(monkeypatch, reply)
    r = client.post("/api/v1/loops/perspectives",
                    json={"topic": "法治（以法为本）与仁政之辨", "is_contemporary": False},
                    headers=AUTH)
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["reason"] == "guard_blocked"
    rule_ids = {x["rule_id"] for x in detail["guard"]["reasons"]}
    assert "R-PREDICT-001" in rule_ids


# ---------------------------------------------------------------------------
# 谱系闭环（4.4）
# ---------------------------------------------------------------------------

HOP2_MISS = {
    ("relation_query", (("source", "卫鞅（商鞅）"), ("target", "韩非"))):
        {"results": [], "reason": "not_ingested", "hint": "重查对：已在 hop1 召回（桩去重路径）"},
}
GEN_ROUTES = dict(FA_ROUTES)
GEN_ROUTES.update(HOP2_MISS)


def test_genealogy_chain_sorted_with_gaps(monkeypatch) -> None:
    """谱系链按年代排序；断链处标注 gap（缺年代/下钻断链/年代跳变）不编造。"""
    _use_reference(monkeypatch, GEN_ROUTES)
    r = client.post("/api/v1/loops/genealogy",
                    json={"term": "法治（以法为本）"}, headers=AUTH)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["resolved_term"] == "法治（以法为本）"
    assert out["engine"] == "deterministic"
    years = [ring["year"] for ring in out["chain"]]
    assert years == sorted(years) == [-390, -280]  # curated 年代串保守解析
    names = [ring["name"] for ring in out["chain"]]
    assert set(names) == {"卫鞅（商鞅）", "韩非"}
    edge_ids = _ids(EDGE_SHANGYANG_HANFEI, EDGE_HANFEI_HANFEI_CRIT, EDGE_NO_YEAR)
    assert all(ring["ref"] in edge_ids for ring in out["chain"])  # 无锚不出
    assert all(ring["key_text"] for ring in out["chain"])         # 每环关键文本
    reasons = {g["reason"] for g in out["gaps"]}
    # 断链如实标注：缺年代、下钻 not_ingested
    assert "no_year" in reasons
    no_year = next(g for g in out["gaps"] if g["reason"] == "no_year")
    assert "1 个环节缺年代" in no_year["detail"]
    assert out["guard"]["action"] is None


def test_genealogy_llm_select_keeps_chronology(monkeypatch) -> None:
    """LLM 选环（index-only）：可剪枝但链保持年代序，engine=llm。"""
    _use_reference(monkeypatch, GEN_ROUTES)
    _llm_on(monkeypatch, '{"indices": [2]}')  # 只留韩非环
    r = client.post("/api/v1/loops/genealogy",
                    json={"term": "法治（以法为本）"}, headers=AUTH)
    out = r.json()
    assert out["engine"] == "llm"
    assert [ring["name"] for ring in out["chain"]] == ["韩非"]
    years = [ring["year"] for ring in out["chain"]]
    assert years == sorted(years)


def test_genealogy_single_figure_honest_gap(monkeypatch) -> None:
    """单人物概念锚：参照系关系面为人物对查询，无可配对——空链 + 缺口如实。"""
    _use_reference(monkeypatch, {
        ("concept_lookup", (("term", "仁政"),)): RENZHENG_CONCEPT,
    })
    r = client.post("/api/v1/loops/genealogy", json={"term": "仁政"}, headers=AUTH)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["chain"] == []
    assert out["anchor"]["ref"] == "china-seed:concept:仁政"
    gaps = [g for g in out["gaps"] if g["reason"] == "no_pair_to_query"]
    assert gaps and "孟轲（孟子）" in gaps[0]["detail"]


def test_genealogy_unknown_term_honest_gap(monkeypatch) -> None:
    """未知概念：参照系空结果 → 空链 + not_ingested 缺口，不编造环节。"""
    _use_reference(monkeypatch, {})
    r = client.post("/api/v1/loops/genealogy",
                    json={"term": "量子政治学xyz"}, headers=AUTH)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["chain"] == [] and out["anchor"] is None
    assert out["gaps"][0]["reason"] == "not_ingested"
    assert out["guard"]["action"] is None


# ---------------------------------------------------------------------------
# 共享底座（looputil）
# ---------------------------------------------------------------------------


def test_looputil_lexicon_and_contemporary() -> None:
    assert looputil.extract_terms("自由与平等之争") == ["liberty", "equality"]
    assert looputil.extract_terms("功利主义与正义") == ["utilitarianism", "justice"]
    assert looputil.extract_terms("今天午饭吃什么") == []
    # D4：先秦种子概念/近现代思潮直查词命中（长词优先，种子/近现代先于 SEP 别名）
    assert looputil.extract_terms("仁政") == ["仁政"]
    assert looputil.extract_terms("兼爱非攻的墨家主张") == ["兼爱", "非攻"]
    assert looputil.extract_terms("民本与无为") == ["民本", "无为"]
    assert looputil.extract_terms("自由主义思潮")[:1] == ["自由主义思潮"]  # 长词优先（liberty 别名随其后）
    assert looputil.extract_terms("社会主义论战")[:1] == ["社会主义论战"]
    assert looputil.extract_terms("社会契约思想的源流与批评") == ["social contract"]
    # 当代判定：显式优先；词面标记兜底；默认保守非当代
    assert looputil.detect_contemporary("任何文本", True) is True
    assert looputil.detect_contemporary("任何文本", False) is False
    assert looputil.detect_contemporary("2026 年选举改革讨论") is True
    assert looputil.detect_contemporary("现行移民配额之争") is True
    assert looputil.detect_contemporary("自然状态与利维坦") is False


def test_looputil_lexicon_covers_seed_and_modern_lists() -> None:
    """D4 词表覆盖面：近现代五思潮 + 种子概念/人物名单（静态固化，运行时不
    读对方仓文件）。"""
    terms = {e["term"] for e in looputil.LEXICON}
    assert {"维新变法思潮", "辛亥革命思潮", "自由主义思潮",
            "文化保守主义（学衡派/新儒家早期）", "社会主义论战"} <= terms
    assert {"仁政", "兼爱", "无为", "王道", "民本", "霸道", "性善论", "法治（以法为本）"} <= terms
    assert {"孟轲（孟子）", "孔伋（子思）", "荀况（荀子）", "墨翟（墨子）",
            "韩非", "卫鞅（商鞅）", "孙武", "公孙龙"} <= terms  # 人物全名
    # 单字概念不入词表（子串误命中率高，文件头已注明）
    assert "仁" not in terms and "势" not in terms


def test_looputil_parse_and_refs() -> None:
    assert looputil.parse_llm_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert looputil.parse_llm_json("前缀 {\"b\": 2} 后缀") == {"b": 2}
    assert looputil.parse_llm_json("不是 json") is None
    assert looputil.keep_valid_refs(
        ["wd:1", "wd:FAKE", "wd:1", 3, None], {"wd:1", "wd:2"}
    ) == ["wd:1"]


def test_year_from_range_conservative() -> None:
    """curated 年代串保守解析：取首个年份，前=负；无数字不编造。"""
    from fd_zhengxin_api.locate import year_from_range

    assert year_from_range("约前372–前289") == -372
    assert year_from_range("前551–前479") == -551
    assert year_from_range("前507–?") == -507
    assert year_from_range("1895–1898") == 1895
    assert year_from_range("?") is None
    assert year_from_range("") is None
