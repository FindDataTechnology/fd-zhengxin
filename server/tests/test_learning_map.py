"""task 5.4 验证：学习地图合成（15 卡产出）+ 变更重合成新旧版本对比 + 降级标注 + 隔离。

坐标系两形态都测：参照系可达（monkeypatch 分类表）按学派/思潮维度归类、
未触及区域可见；未配置/不可达时按卡自身 topic 锚点降级合成并标注
（degraded + reasons，未触及不可知）。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from fd_zhengxin_api import personal, reference_client
from fd_zhengxin_api.main import app

client = TestClient(app)
AUTH = {"Authorization": "Bearer dev-tok-alice"}
AUTH_BOB = {"Authorization": "Bearer dev-tok-bob"}

KP_CONTRACT = ["自然状态", "让渡权利", "主权者"]
FULL_CONTRACT = "霍布斯认为自然状态下人人相互为敌，人们为自保让渡权利给主权者。"


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("FDZX__DB", str(tmp_path / "personal.db"))
    monkeypatch.setenv("FDZX__AUTH_DEV_TOKENS", "dev-tok-alice=alice,dev-tok-bob=bob")
    monkeypatch.delenv("FDZX__REFERENCE_BASE_URL", raising=False)
    monkeypatch.delenv("FDZX__LLM_API_KEY", raising=False)


def _understood(topic: str, kp: list[str]) -> dict:
    digest = "复述要点：" + "；".join(f"必须讲清{p}" for p in kp) + "。"
    r = client.post("/api/v1/cards",
                    json={"topic": topic, "digest": digest, "key_points": kp},
                    headers=AUTH)
    assert r.status_code == 201, r.text
    assert r.json()["card"]["state"] == "understood", r.text
    return r.json()["card"]


def _restating(topic: str, kp: list[str]) -> dict:
    r = client.post("/api/v1/cards",
                    json={"topic": topic, "digest": "我还没想清楚怎么复述这个论证。",
                          "key_points": kp},
                    headers=AUTH)
    assert r.status_code == 201, r.text
    assert r.json()["card"]["state"] == "restating"
    return r.json()["card"]


def _shelved(topic: str, kp: list[str]) -> dict:
    card = _restating(topic, kp)
    r = client.post(f"/api/v1/cards/{card['id']}/shelve", headers=AUTH)
    assert r.status_code == 201 and r.json()["card"]["state"] == "shelved"
    return r.json()["card"]


def _synthesize() -> dict:
    r = client.post("/api/v1/map/synthesize", headers=AUTH)
    assert r.status_code == 201, r.text
    return r.json()


# ---- 主线：15 张卡产出地图；变更 3 张后重合成，新旧版本可对比 ----

def test_map_synthesis_versioning_and_comparison() -> None:
    for _ in range(6):
        _understood("功利主义", ["最大幸福", "后果计算"])
    for _ in range(6):
        _restating("社会契约论", KP_CONTRACT)
    for _ in range(2):
        _shelved("古典共和主义", ["公民美德"])
    _restating("无政府主义", ["反强制"])
    conn = personal.connect()
    try:
        assert len(personal.list_cards(conn, "alice", "active")) == 15
    finally:
        conn.close()

    # 参照系未配置：降级合成（按卡自身 topic 锚点），未触及不可知、如实标注
    v1 = _synthesize()
    assert v1["version"] == 1 and v1["title"] == "学习地图 v1"
    snap = v1["snapshot"]
    assert snap["degraded"] is True and snap["coordinate_source"] == "card-anchors"
    assert snap["degradation_reasons"] and any("参照系" in r for r in snap["degradation_reasons"])
    assert snap["counts"] == {"total": 15, "understood": 6, "restating": 7, "shelved": 2}
    assert len(snap["based_on_cards"]) == 15
    assert snap["understood_regions"] == ["功利主义"]
    assert set(snap["in_progress_regions"]) == {"社会契约论", "古典共和主义", "无政府主义"}
    assert snap["untouched_regions"] == []           # 降级：未触及不可知，不猜测
    by_name = {e["name"]: e for e in snap["regions"]}
    assert by_name["古典共和主义"]["shelved"] == 2
    assert by_name["古典共和主义"]["status"] == "in_progress"

    assert client.get("/api/v1/map", headers=AUTH).json()["version"] == 1

    # 变更 3 张：社会契约论的三张「复述中」卡补复述达「已理解」
    b_cards = [
        c for c in client.get("/api/v1/cards?state=restating", headers=AUTH).json()["results"]
        if c["topic"] == "社会契约论"
    ]
    assert len(b_cards) == 6
    for c in b_cards[:3]:
        r = client.post(f"/api/v1/cards/{c['id']}/resubmit",
                        json={"digest": FULL_CONTRACT}, headers=AUTH)
        assert r.status_code == 201 and r.json()["card"]["state"] == "understood"

    # 重合成 → 新版本：社会契约论进入已理解区域
    v2 = _synthesize()
    assert v2["version"] == 2
    assert "社会契约论" in v2["snapshot"]["understood_regions"]
    assert "社会契约论" not in v2["snapshot"]["in_progress_regions"]
    assert v2["snapshot"]["counts"]["understood"] == 9

    # 新旧版本可对比：历史全保留，旧快照原貌可查
    hist = client.get("/api/v1/map/history", headers=AUTH).json()["results"]
    assert [h["version"] for h in hist] == [2, 1]
    old = client.get("/api/v1/map/history/1", headers=AUTH).json()
    assert "社会契约论" in old["snapshot"]["in_progress_regions"]
    assert old["snapshot"]["counts"]["understood"] == 6
    assert client.get("/api/v1/map/history/9", headers=AUTH).status_code == 404

    # 合成动作留痕
    conn = personal.connect()
    n = conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE user_id='alice' AND action='map.synthesize'"
    ).fetchone()[0]
    conn.close()
    assert n == 2


# ---- 参照系坐标系形态（学派/思潮维度归类 + 未触及区域） ----

def test_map_with_reference_taxonomy(monkeypatch) -> None:
    monkeypatch.setenv("FDZX__REFERENCE_BASE_URL", "http://reference.local")
    monkeypatch.setattr(
        reference_client, "fetch_region_taxonomy",
        lambda: (["社会契约论", "功利主义", "保守主义"], None),
    )
    _understood("霍布斯的社会契约论", ["自然状态"])
    _restating("密尔论功利主义", ["最大幸福"])
    stray = _understood("老子的小国寡民", ["无为"])

    out = _synthesize()
    snap = out["snapshot"]
    assert snap["degraded"] is False and snap["coordinate_source"] == "reference"
    assert snap["degradation_reasons"] == []
    assert snap["understood_regions"] == ["社会契约论"]
    assert snap["in_progress_regions"] == ["功利主义"]
    assert snap["untouched_regions"] == ["保守主义"]      # 坐标系上没卡的区域
    assert snap["unclassified_cards"] == [{"card_id": stray["id"], "topic": "老子的小国寡民"}]


def test_map_degrades_when_reference_unreachable(monkeypatch) -> None:
    monkeypatch.setenv("FDZX__REFERENCE_BASE_URL", "http://reference.local")
    monkeypatch.setattr(reference_client, "fetch_region_taxonomy",
                        lambda: (None, "unreachable:ConnectError"))
    _understood("功利主义", ["最大幸福"])
    out = _synthesize()
    snap = out["snapshot"]
    assert snap["degraded"] is True and snap["coordinate_source"] == "card-anchors"
    assert any("不可达" in r for r in snap["degradation_reasons"])
    assert snap["understood_regions"] == ["功利主义"]    # 降级不丢已理解归集


# ---- 空态与隔离 ----

def test_map_404_before_first_synthesis() -> None:
    assert client.get("/api/v1/map", headers=AUTH).status_code == 404
    assert client.get("/api/v1/map/history", headers=AUTH).json()["results"] == []
    assert client.get("/api/v1/map/history/1", headers=AUTH).status_code == 404


def test_map_user_isolation() -> None:
    _understood("功利主义", ["最大幸福"])
    _synthesize()                                        # alice v1
    assert client.get("/api/v1/map", headers=AUTH_BOB).status_code == 404
    assert client.get("/api/v1/map/history", headers=AUTH_BOB).json()["results"] == []

    r = client.post("/api/v1/map/synthesize", headers=AUTH_BOB)
    assert r.status_code == 201
    bob = r.json()
    assert bob["version"] == 1 and bob["snapshot"]["counts"]["total"] == 0

    alice = client.get("/api/v1/map", headers=AUTH).json()
    assert alice["version"] == 1 and alice["snapshot"]["counts"]["total"] == 1
    assert len(client.get("/api/v1/map/history", headers=AUTH_BOB).json()["results"]) == 1


def test_map_requires_auth() -> None:
    assert client.get("/api/v1/map").status_code == 401
    assert client.post("/api/v1/map/synthesize").status_code == 401
    conn = personal.connect()
    denied = conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE outcome='denied' AND resource LIKE '/api/v1/map%'"
    ).fetchone()[0]
    conn.close()
    assert denied >= 2
