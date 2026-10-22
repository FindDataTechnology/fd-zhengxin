"""task 5.2/5.3 验证：理解卡全生命周期 + 复述校验口径 + 访问控制。

生命周期线：建卡（校验差距 → 复述中）→ 显式校验（低于阈值拒绝转已理解）
→ 补复述重复提交（旧卡封存入年轮）→ 已理解 → 搁置 → 年轮回溯 →
封存后拒改。口径线：卡与校验输出无任何立场评价字段、表态字段写入被拒
（422）、覆盖判定保守（纯函数直测）。访问线：未授权 401 留痕、双账号隔离。
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from fd_zhengxin_api import cards_api, llm as llm_mod, personal, reference_client
from fd_zhengxin_api.llm import LLMError
from fd_zhengxin_api.main import app

client = TestClient(app)
AUTH = {"Authorization": "Bearer dev-tok-alice"}
AUTH_BOB = {"Authorization": "Bearer dev-tok-bob"}

KP = ["自然状态", "让渡权利", "主权者"]
FULL_DIGEST = "霍布斯认为自然状态下人人相互为敌，人们为自保让渡权利给主权者。"
MID_DIGEST = "自然状态下人们相互让渡权利。"      # 覆盖 2/3，低于阈值 0.8
WEAK_DIGEST = "霍布斯讲了一个丛林里的故事。"     # 覆盖 0


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("FDZX__DB", str(tmp_path / "personal.db"))
    monkeypatch.setenv("FDZX__AUTH_DEV_TOKENS", "dev-tok-alice=alice,dev-tok-bob=bob")
    monkeypatch.delenv("FDZX__REFERENCE_BASE_URL", raising=False)
    monkeypatch.delenv("FDZX__LLM_API_KEY", raising=False)


def _create(digest: str, topic: str = "社会契约论", key_points: list | None = None) -> dict:
    r = client.post(
        "/api/v1/cards",
        json={"topic": topic, "digest": digest, "key_points": key_points or KP},
        headers=AUTH,
    )
    assert r.status_code == 201, r.text
    return r.json()


# ---- 全生命周期（spec「建卡与校验」「理解年轮」两条 Scenario） ----

def test_full_lifecycle() -> None:
    # 建卡：弱复述 → 复述中，校验输出差距（未覆盖清单 + 差距说明）
    body = _create(WEAK_DIGEST)
    card, v = body["card"], body["verification"]
    assert card["state"] == "restating" and card["status"] == "active"
    assert v["coverage_ratio"] == 0.0 and v["passed"] is False
    assert v["uncovered_premises"] == KP
    assert v["gaps"] and any("自然状态" in g for g in v["gaps"])
    cid = card["id"]

    # 显式再校验：仍低于阈值，拒绝转「已理解」
    r = client.post(f"/api/v1/cards/{cid}/verify", headers=AUTH)
    assert r.status_code == 200
    assert r.json()["card"]["state"] == "restating"
    assert r.json()["verification"]["coverage_ratio"] == 0.0

    # 补复述（重复提交）：旧卡封存入年轮，新卡达阈值即「已理解」
    r = client.post(f"/api/v1/cards/{cid}/resubmit",
                    json={"digest": FULL_DIGEST}, headers=AUTH)
    assert r.status_code == 201, r.text
    new_card = r.json()["card"]
    assert new_card["id"] != cid and new_card["state"] == "understood"
    assert new_card["key_points"] == KP            # 不填则沿用旧卡声明
    assert r.json()["verification"]["coverage_ratio"] == 1.0

    # 旧卡：sealed 可读（快照态），一切改动被拒（封存后拒改）
    r = client.get(f"/api/v1/cards/{cid}", headers=AUTH)
    assert r.status_code == 200
    assert r.json()["status"] == "sealed" and r.json()["sealed_at"]
    for path, payload in (
        (f"/api/v1/cards/{cid}/verify", None),
        (f"/api/v1/cards/{cid}/resubmit", {"digest": "再试一次"}),
        (f"/api/v1/cards/{cid}/shelve", None),
    ):
        assert client.post(path, json=payload, headers=AUTH).status_code == 404

    # 年轮回溯：1 条 superseded，快照是旧卡全貌（旧复述 + 旧状态）
    annals = client.get("/api/v1/cards/annals", headers=AUTH).json()["results"]
    assert len(annals) == 1 and annals[0]["kind"] == "superseded"
    snap = annals[0]["snapshot"]
    assert snap["id"] == cid and snap["digest"] == WEAK_DIGEST
    assert snap["state"] == "restating"

    # 已理解卡再搁置：旧状态封存（kind=sealed），同题开 shelved 新卡
    r = client.post(f"/api/v1/cards/{new_card['id']}/shelve", headers=AUTH)
    assert r.status_code == 201
    shelved = r.json()["card"]
    assert shelved["state"] == "shelved" and shelved["id"] != new_card["id"]
    assert shelved["digest"] == FULL_DIGEST and shelved["topic"] == "社会契约论"

    annals = client.get("/api/v1/cards/annals", headers=AUTH).json()["results"]
    assert len(annals) == 2
    assert annals[1]["kind"] == "sealed"
    assert annals[1]["snapshot"]["state"] == "understood"

    # 年轮 append-only：后续操作不改动既有快照
    frozen = [(a["id"], a["snapshot"]["digest"]) for a in annals]
    client.post(f"/api/v1/cards/{shelved['id']}/resubmit",
                json={"digest": MID_DIGEST}, headers=AUTH)
    annals2 = client.get("/api/v1/cards/annals", headers=AUTH).json()["results"]
    assert len(annals2) == 3
    assert [(a["id"], a["snapshot"]["digest"]) for a in annals2[:2]] == frozen


def test_threshold_rejects_partial_coverage() -> None:
    """阈值拒绝路径：覆盖 2/3（<0.8）建卡/显式校验都不转「已理解」。"""
    body = _create(MID_DIGEST)
    assert body["card"]["state"] == "restating"
    v = body["verification"]
    assert v["coverage_ratio"] == pytest.approx(2 / 3, abs=1e-3)
    assert v["passed"] is False and v["threshold"] == cards_api.VERIFY_THRESHOLD
    assert v["covered_premises"] == ["自然状态", "让渡权利"]
    assert v["uncovered_premises"] == ["主权者"]

    r = client.post(f"/api/v1/cards/{body['card']['id']}/verify", headers=AUTH)
    assert r.status_code == 200 and r.json()["card"]["state"] == "restating"


def test_list_state_filter() -> None:
    _create("功利主义以最大幸福为最终标准。", topic="功利主义",
            key_points=["最大幸福"])
    _create(WEAK_DIGEST, topic="自由主义", key_points=["消极自由"])
    r = client.get("/api/v1/cards?state=understood", headers=AUTH)
    assert [c["topic"] for c in r.json()["results"]] == ["功利主义"]
    r = client.get("/api/v1/cards?state=restating", headers=AUTH)
    assert [c["topic"] for c in r.json()["results"]] == ["自由主义"]
    assert client.get("/api/v1/cards?state=bogus", headers=AUTH).status_code == 422
    assert client.get("/api/v1/cards?status=bogus", headers=AUTH).status_code == 422


# ---- 口径纪律：无立场评价字段（输出）/ 拒收表态字段（写入） ----

_BANNED = {"position", "stance", "attitude", "agreement", "endorsement", "opinion",
           "evaluation", "verdict", "leaning", "bias", "sentiment",
           "立场", "赞同", "反对", "倾向", "评价", "态度", "赞成"}


def _all_keys(obj) -> set:
    out: set = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.add(str(k))
            out |= _all_keys(v)
    elif isinstance(obj, list):
        for it in obj:
            out |= _all_keys(it)
    return out


def test_outputs_have_no_stance_fields() -> None:
    """建卡/校验/读卡/年轮输出：递归查键，无任何立场评价字段（spec 场景）。"""
    body = _create(WEAK_DIGEST)
    cid = body["card"]["id"]
    client.post(f"/api/v1/cards/{cid}/resubmit",
                json={"digest": FULL_DIGEST}, headers=AUTH)
    payloads = [
        body,
        client.post(f"/api/v1/cards/{cid}/verify", headers=AUTH).json(),
        client.get(f"/api/v1/cards/{cid}", headers=AUTH).json(),
        client.get("/api/v1/cards", headers=AUTH).json(),
        client.get("/api/v1/cards/annals", headers=AUTH).json(),
    ]
    for p in payloads:
        assert not _all_keys(p) & _BANNED, _all_keys(p) & _BANNED


def test_stance_fields_rejected_on_write() -> None:
    """spec「卡上不存在也不接受表态字段」：多投的立场/赞同类字段 → 422。"""
    r = client.post(
        "/api/v1/cards",
        json={"topic": "功利主义", "digest": "最大幸福原理",
              "key_points": ["最大幸福"], "position": "支持功利主义"},
        headers=AUTH,
    )
    assert r.status_code == 422
    card = _create(FULL_DIGEST)["card"]
    r = client.post(f"/api/v1/cards/{card['id']}/resubmit",
                    json={"digest": FULL_DIGEST, "agreement": "agree"},
                    headers=AUTH)
    assert r.status_code == 422


# ---- 覆盖判定算法口径（纯函数直测：保守，宁低勿高） ----

def test_deterministic_coverage_rules() -> None:
    # 归一化子串：全角/标点/大小写不影响
    assert cards_api.premise_covered("自然状态", "《自然状态》一章指出丛林预设")
    assert cards_api.premise_covered("Free Will", "free, will and determinism")
    # 二元组 ≥ 2/3：容忍虚词插入（「让渡范围」vs「让渡的范围」）
    assert cards_api.premise_covered("让渡范围", "人们让渡的范围由契约划定")
    # 语义相左的改写跌破线：宁低勿高
    assert not cards_api.premise_covered("让渡全部权利", "人们只让渡部分权利")
    assert not cards_api.premise_covered("主权者", "没有人拥有主权")
    # 拉丁内容词全包含（词序无关）
    assert cards_api.premise_covered("social contract", "the contract is social in nature")
    assert not cards_api.premise_covered("determinism", "libertarianism defended")
    # 单字符前提只认子串：字符不出现即未覆盖（不放宽）
    assert cards_api.premise_covered("义", "正义之辩由此展开")
    assert not cards_api.premise_covered("义", "正与不正的争论")


# ---- LLM 同义判别增强（可选，输出标 engine） ----

def test_llm_synonym_enhancement(monkeypatch) -> None:
    """有 KEY：确定性未覆盖的前提交 LLM 同义判别，达阈值转已理解。"""
    monkeypatch.setenv("FDZX__LLM_API_KEY", "test-key")
    calls: list = []

    def fake_chat(messages, **kw):
        calls.append(messages)
        return '{"covered": true}'

    monkeypatch.setattr(llm_mod, "chat", fake_chat)
    body = _create(MID_DIGEST)
    v = body["verification"]
    assert v["engine"] == "deterministic+llm"
    assert v["coverage_ratio"] == 1.0 and v["passed"] is True
    assert body["card"]["state"] == "understood"
    # LLM 只被确定性未命中的前提触发，且提示词只判覆盖不评立场
    assert calls and all("主权者" in json.dumps(m, ensure_ascii=False) for m in calls)
    blob = json.dumps(calls[0], ensure_ascii=False)
    assert "覆盖" in blob and "立场" in blob  # system 提示显式声明不评价立场


def test_llm_failure_falls_back(monkeypatch) -> None:
    """有 KEY 但网关失败：回退纯确定性（engine 标 fallback，覆盖度只低不高）。"""
    monkeypatch.setenv("FDZX__LLM_API_KEY", "test-key")

    def boom(*a, **kw):
        raise LLMError("down")

    monkeypatch.setattr(llm_mod, "chat", boom)
    body = _create(MID_DIGEST)
    v = body["verification"]
    assert v["engine"] == "deterministic+llm-fallback"
    assert v["coverage_ratio"] == pytest.approx(2 / 3, abs=1e-3) and v["passed"] is False
    assert body["card"]["state"] == "restating"
    assert any("回退" in g for g in v["gaps"])


def test_llm_disabled_pure_deterministic() -> None:
    """无 KEY：纯确定性，engine=deterministic，不触发任何 LLM 调用。"""
    body = _create(MID_DIGEST)
    assert body["verification"]["engine"] == "deterministic"


# ---- 前提集来源：卡声明优先，参照系概念锚兜底（留位期宽容解析） ----

def test_reference_anchor_premises(monkeypatch) -> None:
    monkeypatch.setenv("FDZX__REFERENCE_BASE_URL", "http://reference.local")
    monkeypatch.setattr(reference_client, "fetch_concept_anchor",
                        lambda topic: (["自然状态", "同意的让渡"], None))
    r = client.post(
        "/api/v1/cards",
        json={"topic": "社会契约", "digest": "自然状态下人们一致同意地让渡",
              "key_points": []},
        headers=AUTH,
    )
    assert r.status_code == 201
    v = r.json()["verification"]
    assert v["premise_source"] == "reference"
    assert v["premises"] == ["自然状态", "同意的让渡"]
    assert v["coverage_ratio"] == 0.5  # 「同意的让渡」虚词不同，保守判未覆盖


def test_reference_unreachable_reported(monkeypatch) -> None:
    """参照系配置了但不可达：如实声明无可对照前提（原因可见），不猜测。"""
    monkeypatch.setenv("FDZX__REFERENCE_BASE_URL", "http://reference.local")
    monkeypatch.setattr(reference_client, "fetch_concept_anchor",
                        lambda topic: (None, "unreachable:ConnectError"))
    r = client.post(
        "/api/v1/cards",
        json={"topic": "社会契约", "digest": "随便说说", "key_points": []},
        headers=AUTH,
    )
    assert r.status_code == 201
    v = r.json()["verification"]
    assert v["premise_source"] == "none" and v["coverage_ratio"] is None
    assert any("unreachable" in g for g in v["gaps"])
    assert r.json()["card"]["state"] == "restating"


def test_no_key_points_and_no_reference() -> None:
    """无 key_points 且未配参照系：声明无法校验，卡停「复述中」。"""
    r = client.post(
        "/api/v1/cards",
        json={"topic": "自发秩序", "digest": "还不确定怎么复述"},
        headers=AUTH,
    )
    assert r.status_code == 201
    v = r.json()["verification"]
    assert v["premise_source"] == "none" and v["coverage_ratio"] is None
    assert v["passed"] is False and v["gaps"]
    assert r.json()["card"]["state"] == "restating"


# ---- 访问控制（task 5.3：未授权 401 留痕 + 双账号隔离） ----

def test_unauthorized_denied_and_audited() -> None:
    for method, path, payload in (
        ("get", "/api/v1/cards", None),
        ("post", "/api/v1/cards",
         {"topic": "x", "digest": "y", "key_points": []}),
        ("get", "/api/v1/cards/annals", None),
        ("get", "/api/v1/map", None),
        ("post", "/api/v1/map/synthesize", None),
    ):
        r = client.request(method, path, json=payload)  # 无授权头
        assert r.status_code == 401, (method, path, r.status_code)
    conn = personal.connect()
    rows = conn.execute(
        "SELECT user_id, outcome FROM audit_log WHERE outcome='denied'"
    ).fetchall()
    conn.close()
    assert len(rows) >= 5 and all(r["user_id"] is None for r in rows)


def test_user_isolation() -> None:
    card = _create(FULL_DIGEST)["card"]
    r = client.get("/api/v1/cards", headers=AUTH_BOB)
    assert r.status_code == 200 and r.json()["results"] == []
    assert client.get(f"/api/v1/cards/{card['id']}", headers=AUTH_BOB).status_code == 404
    r = client.post(f"/api/v1/cards/{card['id']}/resubmit",
                    json={"digest": "bob 的复述"}, headers=AUTH_BOB)
    assert r.status_code == 404
    assert client.get("/api/v1/cards/annals", headers=AUTH_BOB).json()["results"] == []
    assert len(client.get("/api/v1/cards", headers=AUTH).json()["results"]) == 1


def test_audit_trail_for_card_actions() -> None:
    body = _create(WEAK_DIGEST)
    cid = body["card"]["id"]
    client.post(f"/api/v1/cards/{cid}/verify", headers=AUTH)
    new = client.post(f"/api/v1/cards/{cid}/resubmit",
                      json={"digest": FULL_DIGEST}, headers=AUTH).json()["card"]
    client.post(f"/api/v1/cards/{new['id']}/shelve", headers=AUTH)
    conn = personal.connect()
    rows = conn.execute(
        "SELECT action, outcome, detail FROM audit_log"
        " WHERE user_id='alice' AND action LIKE 'card.%' ORDER BY rowid"
    ).fetchall()
    conn.close()
    assert {r["action"] for r in rows} >= {
        "card.create", "card.verify", "card.resubmit", "card.shelve",
    }
    assert all(r["outcome"] == "allowed" for r in rows)
