"""三闭环（task 4.2/4.3/4.4）验证：定位 / 多视角 / 谱系 + 参照系 MCP 客户端。

全离线：参照系服务未部署，用注入 transport 的响应桩模拟远端 MCP（走真实
JSON-RPC envelope 解析路径）；LLM 全部 monkeypatch 不真调网。守卫拦路径用
真实规则库 v1（当代单视角 R-BAL-001、预测句式 R-PREDICT-001）。
"""

from __future__ import annotations

import json

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
# ---------------------------------------------------------------------------


def _envelope(tool_payload: dict) -> dict:
    """远端 MCP 的 JSON-RPC 响应 envelope（content[0].text = 工具 JSON 输出）。"""
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "content": [{"type": "text", "text": json.dumps(tool_payload, ensure_ascii=False)}],
            "isError": False,
        },
    }


def _not_ingested(tool: str) -> dict:
    """未路由调用的缺省桩：空结果 + not_ingested（照参照系骨架期真实口径）。"""
    return {"results": [], "reason": "not_ingested",
            "hint": f"桩未路由 {tool}（骨架期参照系四类查询均为 not_ingested）"}


def _stub(routes: dict):
    """按 (tool, sorted(args)) 精确路由的 transport 桩；值异常则抛（注失败）。"""

    def transport(url: str, payload: dict, timeout: float) -> dict:
        assert url == BASE_URL
        params = payload["params"]
        key = (params["name"], tuple(sorted(params["arguments"].items())))
        resp = routes.get(key)
        if isinstance(resp, Exception):
            raise resp
        return _envelope(resp if resp is not None else _not_ingested(params["name"]))

    return transport


def _use_reference(monkeypatch, routes: dict) -> None:
    stub_client = ReferenceClient(base_url=BASE_URL, transport=_stub(routes))
    monkeypatch.setattr(reference_mcp, "client", lambda: stub_client)


def _prov(source: str, sid: str) -> dict:
    return {"provenance": {"source": source, "stable_id": sid, "as_of": "2026-10-23"}}


def _edge(eid, target_qid, name, year, weight, school, key_text, polarity, stance=""):
    return {
        "id": eid, "target_qid": target_qid, "target_name": name,
        "target_kind": "person", "polarity": polarity, "pid": "P4581",
        "weight": weight, "school": school, "year": year,
        "key_text": key_text, "stance": stance,
    }


SEP_CONTRACT = {
    "results": [
        {"id": "sep:contractarianism", "term": "social contract", "title": "Contractarianism",
         "definition": "政治权威的正当性源于被治理者同意的契约传统。",
         "source": "sep", "url": "https://plato.stanford.edu/entries/contractarianism/",
         "qid": "Q19384"},
        {"id": "sep:contractarianism-contemporary", "term": "social contract",
         "title": "Contemporary Approaches to the Social Contract",
         "definition": "当代契约主义进路（罗尔斯以降）。",
         "source": "sep",
         "url": "https://plato.stanford.edu/entries/contractarianism-contemporary/",
         "qid": "Q19384"},
    ],
    **_prov("sep", "https://plato.stanford.edu/entries/contractarianism/"),
}

# 召回序故意不按年代（洛克→霍布斯→卢梭）：先例链必须自行按年排序
EDGES_INFLUENCED = [
    _edge("wd:Q19384-Q11935:P4581", "Q11935", "洛克", 1689, 0.85, "社会契约论",
          "《政府论》", "influenced_by"),
    _edge("wd:Q19384-Q41887:P4581", "Q41887", "霍布斯", 1651, 0.90, "社会契约论",
          "《利维坦》", "influenced_by"),
    _edge("wd:Q19384-Q162668:P4581", "Q162668", "卢梭", 1762, 0.80, "社会契约论",
          "《社会契约论》", "influenced_by"),
]
EDGE_CRITICAL = [
    _edge("wd:Q19384-Q37425:P4581", "Q37425", "休谟", 1740, 0.75, "保守主义",
          "《论原始契约》", "critical", stance="契约从未被任何人真正签署过"),
]
IDEOLOGY = {
    "results": [
        {"id": "marpor:42312", "qid": "Q19384", "name": "自由立宪主义",
         "anchors": [
             {"axis": "P1142", "value": "自由主义", "basis": "P1142"},
             {"axis": "P1387", "value": "中间", "basis": "P1387"},
             {"axis": "marpor:rile", "value": -9.8, "basis": "marpor:rile"},
         ]},
    ],
    **_prov("marpor", "marpor:42312"),
}

RICH_ROUTES = {
    ("concept_lookup", (("term", "social contract"),)): SEP_CONTRACT,
    ("relation_query", (("polarity", "influenced_by"), ("qid", "Q19384"))):
        {"results": EDGES_INFLUENCED, **_prov("wikidata", "wd:Q19384")},
    ("relation_query", (("polarity", "critical"), ("qid", "Q19384"))):
        {"results": EDGE_CRITICAL, **_prov("wikidata", "wd:Q19384")},
    ("ideology_query", (("qid", "Q19384"),)): IDEOLOGY,
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
    monkeypatch.setattr(llm_mod, "chat", lambda *a, **k: reply)


# ---------------------------------------------------------------------------
# 参照系 MCP 客户端（契约封装与结构化降级）
# ---------------------------------------------------------------------------


class TestReferenceClient:
    def test_ok_passthrough(self) -> None:
        c = ReferenceClient(base_url=BASE_URL, transport=_stub(RICH_ROUTES))
        r = c.concept_lookup("social contract")
        assert r.ok and len(r.data["results"]) == 2
        assert r.data["provenance"]["source"] == "sep"

    def test_other_tools_on_contract(self) -> None:
        routes = {
            ("source_status", ()): {"results": [{"source": "sep", "status": "ok"}]},
            ("regime_timeseries", (("country", "sweden"), ("years", "1990-2020"))):
                {"results": [{"country": "sweden", "year": 1990, "v2x": 0.9}]},
        }
        c = ReferenceClient(base_url=BASE_URL, transport=_stub(routes))
        assert c.status().ok
        assert c.regime_timeseries("sweden", "1990-2020").ok
        # 契约外的调用（未路由）→ 空结果带 not_ingested 原因
        r = c.ideology_query("Q40430")
        assert not r.ok and r.reason == "not_ingested" and "not_ingested" in r.detail

    def test_empty_with_taxonomy_reason_degrades(self) -> None:
        c = ReferenceClient(base_url=BASE_URL, transport=_stub({}))
        r = c.relation_query("Q1", "influenced_by")
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

    def test_jsonrpc_error_envelope(self) -> None:
        def transport(url, payload, timeout):
            return {"jsonrpc": "2.0", "id": 1,
                    "error": {"code": -32601, "message": "no such tool"}}

        r = ReferenceClient(base_url=BASE_URL, transport=transport).status()
        assert not r.ok and r.reason == "upstream" and "-32601" in r.detail

    def test_not_configured_short_circuits(self) -> None:
        called = []

        def transport(url, payload, timeout):  # pragma: no cover —— 不该被调
            called.append(url)
            return {}

        r = ReferenceClient(base_url="   ", transport=transport).concept_lookup("x")
        assert not r.ok and r.reason == "not_configured"
        assert called == []  # 未配置不发包，不冒充远端故障

    def test_decode_body_sse_and_json(self) -> None:
        env = {"jsonrpc": "2.0", "id": 1, "result": {"content": []}}
        sse = "event: message\ndata: " + json.dumps(env) + "\n\n"
        assert reference_mcp.decode_body(sse) == env
        assert reference_mcp.decode_body(json.dumps(env)) == env
        with pytest.raises(reference_mcp.RefUpstream):
            reference_mcp.decode_body("event: message\ndata: not-json\n\n")


# ---------------------------------------------------------------------------
# 定位闭环（4.2）
# ---------------------------------------------------------------------------


def test_locate_success_deterministic_with_sources(monkeypatch) -> None:
    """定位成功：概念锚带 SEP 源、归派带边 id 依据、先例链按年排序、engine 标注。"""
    _use_reference(monkeypatch, RICH_ROUTES)
    r = client.post("/api/v1/loops/locate",
                    json={"text": "社会契约思想的源流与批评", "is_contemporary": False},
                    headers=AUTH)
    assert r.status_code == 200, r.text
    out = r.json()

    assert out["engine"] == "deterministic"  # 无 KEY 确定性路径必须标注
    assert out["positioned"] is True
    # 概念锚：全部来自参照系 concept_lookup 结果项（id/SEP 链接）
    assert {a["ref"] for a in out["anchors"]} >= {"sep:contractarianism"}
    assert all(a["url"].startswith("https://plato.stanford.edu/") for a in out["anchors"])
    # 归派：强度阈值之上、依据指向参照系边 id
    edge_ids = {e["id"] for e in EDGES_INFLUENCED + EDGE_CRITICAL}
    assert [a["school"] for a in out["attributions"]] == ["社会契约论", "保守主义"]
    for a in out["attributions"]:
        assert a["strength"] >= out["min_position_strength"]
        assert a["evidence"] and set(a["evidence"]) <= edge_ids
    # 先例链：召回序乱序进，年代升序出（含 critical 边的 1740）
    years = [ring["year"] for ring in out["precedent_chain"]]
    assert years == sorted(years) == [1651, 1689, 1740, 1762]
    assert all(ring["ref"] in edge_ids for ring in out["precedent_chain"])
    assert out["guard"]["action"] is None  # 干净放行
    assert out["gaps"] == []


def test_locate_contemporary_coordinates(monkeypatch) -> None:
    """当代对象：定位之外补 ideology_query 坐标（P1142/P1387/MARPOR）。"""
    _use_reference(monkeypatch, RICH_ROUTES)
    r = client.post("/api/v1/loops/locate",
                    json={"text": "社会契约思想的源流与批评", "is_contemporary": True},
                    headers=AUTH)
    assert r.status_code == 200, r.text
    out = r.json()
    coords = out["coordinates"]
    assert len(coords) == 1 and coords[0]["ref"] == "marpor:42312"
    bases = {a["basis"] for a in coords[0]["anchors"]}
    assert bases == {"P1142", "P1387", "marpor:rile"}


def test_locate_low_evidence_not_forced(monkeypatch) -> None:
    """不硬归派：全部候选低于阈值 → positioned=false + 缺口说明，不凑数。"""
    weak = {
        ("concept_lookup", (("term", "social contract"),)): SEP_CONTRACT,
        ("relation_query", (("polarity", "influenced_by"), ("qid", "Q19384"))):
            {"results": [
                _edge("wd:weak-1", "Q1", "甲", 1800, 0.10, "边缘小传统", "《残篇》", "influenced_by"),
                _edge("wd:weak-2", "Q2", "乙", 1810, 0.12, "另一小传统", "《笔记》", "influenced_by"),
            ], **_prov("wikidata", "wd:weak")},
        ("relation_query", (("polarity", "critical"), ("qid", "Q19384"))):
            {"results": [], "reason": "not_ingested", "hint": "批判边未摄取"},
    }
    _use_reference(monkeypatch, weak)
    r = client.post("/api/v1/loops/locate",
                    json={"text": "社会契约思想的源流与批评"}, headers=AUTH)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["positioned"] is False
    assert out["attributions"] == []
    insuff = [g for g in out["gaps"] if g["reason"] == "insufficient_evidence"]
    assert insuff and "不硬归派" in insuff[0]["detail"]
    assert any(g["reason"] == "not_ingested" for g in out["gaps"])  # 批判边缺口如实可见


def test_locate_llm_rerank_engine(monkeypatch) -> None:
    """有 KEY：LLM 只做重排（index-only），engine=llm；候选与依据仍来自参照系。"""
    _use_reference(monkeypatch, RICH_ROUTES)
    _llm_on(monkeypatch, '{"indices": [2, 1]}')  # 保守主义提到首位
    r = client.post("/api/v1/loops/locate",
                    json={"text": "社会契约思想的源流与批评"}, headers=AUTH)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["engine"] == "llm"
    assert [a["school"] for a in out["attributions"]] == ["保守主义", "社会契约论"]
    edge_ids = {e["id"] for e in EDGES_INFLUENCED + EDGE_CRITICAL}
    assert all(set(a["evidence"]) <= edge_ids for a in out["attributions"])


def test_locate_llm_invalid_falls_back(monkeypatch) -> None:
    """LLM 返回非法（越界编号）→ 降级确定性路径并记 llm_fallback 缺口。"""
    _use_reference(monkeypatch, RICH_ROUTES)
    _llm_on(monkeypatch, '{"indices": [99]}')
    r = client.post("/api/v1/loops/locate",
                    json={"text": "社会契约思想的源流与批评"}, headers=AUTH)
    out = r.json()
    assert out["engine"] == "deterministic"
    assert any(g["reason"] == "llm_fallback" for g in out["gaps"])


def test_locate_reference_unreachable_degrades(monkeypatch) -> None:
    """远端参照系 unreachable → 200 + 结构化降级原因（gaps 带 reason）。"""
    _use_reference(monkeypatch, {
        ("concept_lookup", (("term", "social contract"),)):
            reference_mcp.RefUnreachable("ConnectError: refused"),
    })
    r = client.post("/api/v1/loops/locate",
                    json={"text": "社会契约思想的源流与批评"}, headers=AUTH)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["positioned"] is False and out["anchors"] == []
    assert out["gaps"][0]["stage"] == "reference"
    assert out["gaps"][0]["reason"] == "unreachable"
    assert "concept_lookup" in out["gaps"][0]["detail"]


def test_loops_unauthorized(monkeypatch) -> None:
    _use_reference(monkeypatch, RICH_ROUTES)
    for path, body in (
        ("/api/v1/loops/locate", {"text": "社会契约"}),
        ("/api/v1/loops/perspectives", {"topic": "社会契约论"}),
        ("/api/v1/loops/genealogy", {"term": "社会契约"}),
    ):
        assert client.post(path, json=body).status_code == 401


# ---------------------------------------------------------------------------
# 多视角闭环（4.3）
# ---------------------------------------------------------------------------


def test_perspectives_sourced_views_deterministic(monkeypatch) -> None:
    """多视角带源：确定性模板转述各学派出典立场（form=sourced-stance，锚=边 id）。"""
    _use_reference(monkeypatch, RICH_ROUTES)
    r = client.post("/api/v1/loops/perspectives",
                    json={"topic": "社会契约论的思想史争论"}, headers=AUTH)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["is_contemporary"] is False  # 词面无当代标记，保守默认
    assert out["engine"] == "deterministic"
    views = out["views"]
    assert [v["school"] for v in views] == ["社会契约论", "保守主义"]  # 强度排序
    edge_ids = {e["id"] for e in EDGES_INFLUENCED + EDGE_CRITICAL}
    for v in views:
        assert v["form"] == "sourced-stance"  # 无 KEY 不冒充 steel-man
        assert v["anchors"] and set(v["anchors"]) <= edge_ids
    # 学派内锚点按强度排序取最强：社会契约论最强边是霍布斯 0.90
    assert views[0]["anchors"][0] == "wd:Q19384-Q41887:P4581"
    dis = out["disagreement"]
    assert dis["shared_premises"] and dis["real_disagreement"] and dis["conditions"]
    assert out["guard"]["action"] is None


def test_perspectives_single_view_blocked(monkeypatch) -> None:
    """当代议题单视角：views<2 学派被守卫 R-BAL-001 拦（422 结构化返回）。"""
    routes = {
        ("concept_lookup", (("term", "social contract"),)): SEP_CONTRACT,
        ("relation_query", (("polarity", "influenced_by"), ("qid", "Q19384"))):
            {"results": EDGES_INFLUENCED, **_prov("wikidata", "wd:Q19384")},
        # critical 未路由 → not_ingested：只有一侧学派可召回
    }
    _use_reference(monkeypatch, routes)
    r = client.post("/api/v1/loops/perspectives",
                    json={"topic": "社会契约论的思想史争论", "is_contemporary": True},
                    headers=AUTH)
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["reason"] == "guard_blocked"
    rule_ids = {x["rule_id"] for x in detail["guard"]["reasons"]}
    assert rule_ids == {"R-BAL-001"}
    assert any(g["reason"] == "not_ingested" for g in detail["gaps"])
    assert detail["is_contemporary"] is True


def test_perspectives_weak_anchor_not_steelman(monkeypatch) -> None:
    """弱解读不冒充 steel-man：最强锚低于阈值的学派不进 views，记入 gaps。"""
    routes = {
        ("concept_lookup", (("term", "social contract"),)): SEP_CONTRACT,
        ("relation_query", (("polarity", "influenced_by"), ("qid", "Q19384"))):
            {"results": [
                _edge("wd:Q19384-Q41887:P4581", "Q41887", "霍布斯", 1651, 0.9,
                      "社会契约论", "《利维坦》", "influenced_by"),
                _edge("wd:Q19384-Q9266:P4581", "Q9266", "边沁", 1789, 0.2,
                      "功利主义", "《道德与立法原理导论》", "influenced_by"),
            ], **_prov("wikidata", "wd:Q19384")},
    }
    _use_reference(monkeypatch, routes)
    r = client.post("/api/v1/loops/perspectives",
                    json={"topic": "社会契约论的思想史争论", "is_contemporary": False},
                    headers=AUTH)
    assert r.status_code == 200, r.text
    out = r.json()
    schools = [v["school"] for v in out["views"]]
    assert schools == ["社会契约论"]  # 弱学派未呈现
    weak = [g for g in out["gaps"] if g["reason"] == "weak_anchor"]
    assert weak and "功利主义" in weak[0]["detail"]
    assert "功利主义" not in schools


def test_perspectives_llm_steelman_citations_validated(monkeypatch) -> None:
    """LLM steel-man：编造引用剥除（宁删勿假），engine=llm，正文过守卫。"""
    _use_reference(monkeypatch, RICH_ROUTES)
    reply = json.dumps({
        "views": [
            {"index": 1, "steel_man": "契约论传统的最强形态是：把政治义务解释为自由理性人对公平合作条款的同意，权威由此获得正当性。",
             "citations": ["wd:Q19384-Q41887:P4581", "wd:FAKE:madeup"]},
            {"index": 2, "steel_man": "保守主义立场的最强形态是：社会契约从未被任何人真正签署，政治义务的实际根基是历史传承与习惯。",
             "citations": ["wd:Q19384-Q37425:P4581"]},
        ],
        "disagreement": {
            "shared_premises": ["双方都承认政治权威需要说明正当性来源"],
            "real_disagreement": ["正当性来自同意还是历史传承"],
            "conditions": ["若「同意」可默示成立，保守主义的传承论需回应契约论的规范力量"],
        },
    }, ensure_ascii=False)
    _llm_on(monkeypatch, reply)
    r = client.post("/api/v1/loops/perspectives",
                    json={"topic": "社会契约论的思想史争论"}, headers=AUTH)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["engine"] == "llm"
    assert [v["form"] for v in out["views"]] == ["steel-man", "steel-man"]
    edge_ids = {e["id"] for e in EDGES_INFLUENCED + EDGE_CRITICAL}
    for v in out["views"]:
        assert set(v["anchors"]) <= edge_ids
    assert "wd:FAKE:madeup" not in out["views"][0]["anchors"]  # 编造引用剥除
    assert out["disagreement"]["real_disagreement"] == ["正当性来自同意还是历史传承"]
    assert out["guard"]["action"] is None


def test_perspectives_llm_no_citation_falls_to_template(monkeypatch) -> None:
    """LLM 视图无有效引用锚 → 该学派回退确定性模板（无锚不出）。"""
    _use_reference(monkeypatch, RICH_ROUTES)
    reply = json.dumps({
        "views": [{"index": 1, "steel_man": "没有引用的解读。",
                   "citations": ["wd:FAKE:madeup"]}],
    }, ensure_ascii=False)
    _llm_on(monkeypatch, reply)
    r = client.post("/api/v1/loops/perspectives",
                    json={"topic": "社会契约论的思想史争论"}, headers=AUTH)
    out = r.json()
    assert out["engine"] == "deterministic"  # 全部回退模板
    assert {v["form"] for v in out["views"]} == {"sourced-stance"}


def test_perspectives_prediction_body_blocked(monkeypatch) -> None:
    """守卫拦路径：LLM steel-man 里混入预测句式（将会赢得）→ 整产物被拦。"""
    _use_reference(monkeypatch, RICH_ROUTES)
    reply = json.dumps({
        "views": [
            {"index": 1,
             "steel_man": "该阵营断言其候选人将会赢得来年选举，并以此论证契约授权的民意基础。",
             "citations": ["wd:Q19384-Q41887:P4581"]},
            {"index": 2, "steel_man": "保守主义转述：契约从未被签署。",
             "citations": ["wd:Q19384-Q37425:P4581"]},
        ],
    }, ensure_ascii=False)
    _llm_on(monkeypatch, reply)
    r = client.post("/api/v1/loops/perspectives",
                    json={"topic": "社会契约论的思想史争论", "is_contemporary": False},
                    headers=AUTH)
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["reason"] == "guard_blocked"
    rule_ids = {x["rule_id"] for x in detail["guard"]["reasons"]}
    assert "R-PREDICT-001" in rule_ids


# ---------------------------------------------------------------------------
# 谱系闭环（4.4）
# ---------------------------------------------------------------------------

GEN_HOP1 = [
    _edge("wd:Q19384-Q41887:P4581", "Q41887", "霍布斯", 1651, 0.90, "社会契约论",
          "《利维坦》", "influenced_by"),
    _edge("wd:Q19384-Q11935:P4581", "Q11935", "洛克", 1689, 0.85, "社会契约论",
          "《政府论》", "influenced_by"),
    _edge("wd:Q19384-Q99999:P4581", "Q99999", "无名编者", None, 0.50, "经院余脉",
          "《辑稿》", "influenced_by"),
]
GEN_HOP2_LOCKE = [
    _edge("wd:Q11935-Q7110:P4581", "Q7110", "奥卡姆的威廉", 1340, 0.60, "唯名论",
          "《对话录》", "influenced_by"),
]
GEN_ROUTES = {
    ("concept_lookup", (("term", "social contract"),)): SEP_CONTRACT,
    ("relation_query", (("polarity", "influenced_by"), ("qid", "Q19384"))):
        {"results": GEN_HOP1, **_prov("wikidata", "wd:Q19384")},
    # 霍布斯下钻：空结果带原因（断链如实可见，不编造环节）
    ("relation_query", (("polarity", "influenced_by"), ("qid", "Q41887"))):
        {"results": [], "reason": "not_ingested", "hint": "人物先行影响层未摄取"},
    ("relation_query", (("polarity", "influenced_by"), ("qid", "Q11935"))):
        {"results": GEN_HOP2_LOCKE, **_prov("wikidata", "wd:Q11935")},
    # Q99999 未路由 → 缺省 not_ingested
}


def test_genealogy_chain_sorted_with_gaps(monkeypatch) -> None:
    """谱系链按年代排序；断链处标注 gap（缺年代/下钻断链/年代跳变）不编造。"""
    _use_reference(monkeypatch, GEN_ROUTES)
    r = client.post("/api/v1/loops/genealogy", json={"term": "社会契约"}, headers=AUTH)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["resolved_term"] == "social contract"
    assert out["engine"] == "deterministic"
    years = [ring["year"] for ring in out["chain"]]
    assert years == sorted(years) == [1340, 1651, 1689]  # hop2 的 1340 排最前
    names = [ring["name"] for ring in out["chain"]]
    assert names == ["奥卡姆的威廉", "霍布斯", "洛克"]
    edge_ids = {e["id"] for e in GEN_HOP1 + GEN_HOP2_LOCKE}
    assert all(ring["ref"] in edge_ids for ring in out["chain"])  # 无锚不出
    assert all(ring["key_text"] for ring in out["chain"])         # 每环关键文本
    reasons = {g["reason"] for g in out["gaps"]}
    # 三类断链全部如实标注：缺年代、下钻 not_ingested、年代跳变无中间环节
    assert reasons == {"no_year", "not_ingested", "reference_break"}
    no_year = next(g for g in out["gaps"] if g["reason"] == "no_year")
    assert "无名编者" in no_year["detail"]
    brk = next(g for g in out["gaps"] if g["reason"] == "reference_break")
    assert "1340" in brk["detail"] and "1651" in brk["detail"]
    drill = [g for g in out["gaps"] if g["reason"] == "not_ingested"]
    assert any("霍布斯" in g["detail"] for g in drill)
    assert out["guard"]["action"] is None


def test_genealogy_llm_select_keeps_chronology(monkeypatch) -> None:
    """LLM 选环（index-only）：可剪枝但链保持年代序，engine=llm。"""
    _use_reference(monkeypatch, GEN_ROUTES)
    _llm_on(monkeypatch, '{"indices": [1, 3]}')  # 奥卡姆 + 洛克（剪掉霍布斯）
    r = client.post("/api/v1/loops/genealogy", json={"term": "社会契约"}, headers=AUTH)
    out = r.json()
    assert out["engine"] == "llm"
    assert [ring["name"] for ring in out["chain"]] == ["奥卡姆的威廉", "洛克"]
    years = [ring["year"] for ring in out["chain"]]
    assert years == sorted(years)


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
    # 当代判定：显式优先；词面标记兜底；默认保守非当代
    assert looputil.detect_contemporary("任何文本", True) is True
    assert looputil.detect_contemporary("任何文本", False) is False
    assert looputil.detect_contemporary("2026 年选举改革讨论") is True
    assert looputil.detect_contemporary("现行移民配额之争") is True
    assert looputil.detect_contemporary("自然状态与利维坦") is False


def test_looputil_parse_and_refs() -> None:
    assert looputil.parse_llm_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert looputil.parse_llm_json("前缀 {\"b\": 2} 后缀") == {"b": 2}
    assert looputil.parse_llm_json("不是 json") is None
    assert looputil.keep_valid_refs(
        ["wd:1", "wd:FAKE", "wd:1", 3, None], {"wd:1", "wd:2"}
    ) == ["wd:1"]
