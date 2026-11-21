"""决策留痕：追加式 / 幂等 / 修订指针 / 无评价无聚合。

四条纪律各有对应用例。最后一条（无评分类键）是递归断言，照
test_cards_lifecycle 的 `_all_keys` 手法——它守的是「留痕不是引导」这条线。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from fd_zhengxin_api.main import app

client = TestClient(app)
AUTH = {"Authorization": "Bearer dev-tok-alice"}
AUTH_BOB = {"Authorization": "Bearer dev-tok-bob"}


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("FDZX__DB", str(tmp_path / "personal.db"))
    monkeypatch.setenv("FDZX__AUTH_DEV_TOKENS", "dev-tok-alice=alice,dev-tok-bob=bob")


def _record(note: str = "我决定继续观望", **extra) -> object:
    payload = {"target_kind": "impact", "target_ref": "item-001", "note": note, **extra}
    return client.post("/api/v1/decisions", json=payload, headers=AUTH)


# ---------------------------------------------------------------- 基本记录


def test_record_and_list() -> None:
    r = _record()
    assert r.status_code == 200
    d = r.json()["decision"]
    assert d["note"] == "我决定继续观望"
    assert d["target_kind"] == "impact"
    assert d["refs"] == []
    listed = client.get("/api/v1/decisions", headers=AUTH).json()["decisions"]
    assert len(listed) == 1


def test_refs_roundtrip() -> None:
    refs = [{"item_id": "abc", "doc_id": "cn-gov:x", "span": [0, 12], "quote": "逐字片段"}]
    d = _record(refs=refs).json()["decision"]
    assert d["refs"] == refs


# ---------------------------------------------------------------- 追加式 + 幂等


def test_same_content_is_idempotent() -> None:
    """同对象 + 同决定文本重复提交 → 返回既有记录，不产生重复条目。"""
    first = _record().json()["decision"]
    again = _record().json()["decision"]
    assert first["id"] == again["id"]
    assert len(client.get("/api/v1/decisions", headers=AUTH).json()["decisions"]) == 1


def test_different_note_creates_new_record() -> None:
    """改了文本就是新决定——产生新记录，旧记录逐字不动。"""
    first = _record("我决定观望").json()["decision"]
    second = _record("我改为关注这一条").json()["decision"]
    assert first["id"] != second["id"]
    records = client.get("/api/v1/decisions", headers=AUTH).json()["decisions"]
    assert len(records) == 2
    assert {r["id"] for r in records} == {first["id"], second["id"]}


# ---------------------------------------------------------------- 修订指针


def test_revision_points_to_original_and_leaves_it_intact() -> None:
    orig = _record("我决定观望").json()["decision"]
    rev = _record("我改为关注这一条", revises=orig["id"]).json()["decision"]
    assert rev["revises"] == orig["id"]

    after = client.get(f"/api/v1/decisions/{orig['id']}", headers=AUTH).json()["decision"]
    assert after["note"] == "我决定观望"       # 原记录逐字未变
    assert after["revises"] is None
    assert after["at"] == orig["at"]


def test_revises_unknown_id_404() -> None:
    r = _record("x", revises="doesnotexist")
    assert r.status_code == 404
    assert r.json()["detail"]["reason"] == "revises_not_found"


# ---------------------------------------------------------------- 无评价与聚合（核心口径线）

_BANNED = {
    "score", "rating", "confidence", "evaluation", "verdict", "grade",
    "aggregate", "aggregates", "percentile", "rank", "ranking",
    "评分", "置信度", "评价", "评级", "排名",
}


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


def test_no_scoring_or_aggregation_fields() -> None:
    """响应里不存在评分/置信度/聚合类键——留痕不是引导。"""
    orig = _record("我决定观望").json()["decision"]
    _record("改为关注", revises=orig["id"])
    payloads = [
        client.get("/api/v1/decisions", headers=AUTH).json(),
        client.get(f"/api/v1/decisions/{orig['id']}", headers=AUTH).json(),
    ]
    for p in payloads:
        assert not (_all_keys(p) & _BANNED), _all_keys(p) & _BANNED


def test_no_endpoint_returns_other_users_data() -> None:
    """账本按 caller 隔离：没有任何「同类用户大多选择」这类跨用户面。"""
    _record("alice 的决定")
    assert client.get("/api/v1/decisions", headers=AUTH_BOB).json()["decisions"] == []
    # 越权直取也 404（不是 403 泄露存在性）
    did = client.get("/api/v1/decisions", headers=AUTH).json()["decisions"][0]["id"]
    assert client.get(f"/api/v1/decisions/{did}", headers=AUTH_BOB).status_code == 404


# ---------------------------------------------------------------- 校验与鉴权


def test_invalid_target_kind_422() -> None:
    r = client.post("/api/v1/decisions",
                    json={"target_kind": "whatever", "target_ref": "x", "note": "y"},
                    headers=AUTH)
    assert r.status_code == 422


def test_extra_fields_rejected() -> None:
    r = client.post(
        "/api/v1/decisions",
        json={"target_kind": "impact", "target_ref": "x", "note": "y", "rating": 5},
        headers=AUTH,
    )
    assert r.status_code == 422


def test_unauthorized_401() -> None:
    assert client.get("/api/v1/decisions").status_code == 401
    r = client.post("/api/v1/decisions",
                    json={"target_kind": "impact", "target_ref": "x", "note": "y"})
    assert r.status_code == 401
