"""处境档案：四轴闭集 / 拒收政治字段 / 过期语义 / 导出可清空。

口径线是核心：处境只记经济属性。党派、意识形态、政治态度不属于本档案——
这不是遗漏，是这条功能在「呈现分歧、不裁断」品牌下成立的前提。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from fd_zhengxin_api import personal
from fd_zhengxin_api.main import app

client = TestClient(app)
AUTH = {"Authorization": "Bearer dev-tok-alice"}
AUTH_BOB = {"Authorization": "Bearer dev-tok-bob"}


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("FDZX__DB", str(tmp_path / "personal.db"))
    monkeypatch.setenv("FDZX__AUTH_DEV_TOKENS", "dev-tok-alice=alice,dev-tok-bob=bob")


def _declare(**body) -> object:
    return client.post("/api/v1/situation", json=body, headers=AUTH)


# ---------------------------------------------------------------- 声明与列举


def test_declare_and_list() -> None:
    r = _declare(kind="industry", value="新能源汽车零部件")
    assert r.status_code == 200
    item = r.json()["situation"]
    assert item["kind"] == "industry"
    assert item["value"] == "新能源汽车零部件"
    assert item["expires_at"] > item["declared_at"]

    listed = client.get("/api/v1/situation", headers=AUTH).json()["situation"]
    assert [s["value"] for s in listed] == ["新能源汽车零部件"]


def test_all_four_axes_accepted() -> None:
    for kind in personal.SITUATION_KINDS:
        r = _declare(kind=kind, value=f"测试-{kind}")
        assert r.status_code == 200, (kind, r.text)


def test_fifth_axis_rejected() -> None:
    r = _declare(kind="religion", value="X")
    assert r.status_code == 422
    assert r.json()["detail"]["reason"] == "invalid_situation"


def test_empty_value_rejected() -> None:
    assert _declare(kind="region", value="   ").status_code == 422


# ---------------------------------------------------------------- 拒收政治身份字段（核心口径线）


def test_political_identity_fields_rejected() -> None:
    """党派/意识形态/政治态度投进来 → 422（extra=forbid 拦，且原因可读）。"""
    for field in ("party", "ideology", "political_stance", "vote"):
        r = client.post(
            "/api/v1/situation",
            json={"kind": "industry", "value": "X", field: "Y"},
            headers=AUTH,
        )
        assert r.status_code == 422, (field, r.text)


def test_chinese_political_field_rejected() -> None:
    r = client.post(
        "/api/v1/situation",
        json={"kind": "industry", "value": "X", "党派": "某党"},
        headers=AUTH,
    )
    assert r.status_code == 422


def test_banned_kind_rejected_with_readable_reason() -> None:
    """kind 直接投「party」这种值也要拒，且给出可读原因（不是只说不）。"""
    r = _declare(kind="party", value="某党")
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["reason"] == "political_identity_not_accepted"
    assert "经济处境" in detail["detail"]


# ---------------------------------------------------------------- 过期语义


def test_expired_declaration_not_listed(tmp_path, monkeypatch) -> None:
    """过期声明不出现在有效列表里，但仍可导出（导出是用户权利）。"""
    conn = personal.connect()
    try:
        personal.migrate(conn)
        item = personal.declare_situation(conn, "alice", "region", "广东", ttl_days=1)
        # 人为把过期时间拨到过去
        conn.execute(
            "UPDATE situation_declarations SET expires_at=? WHERE id=?",
            ("2000-01-01T00:00:00+00:00", item["id"]),
        )
        conn.commit()
        assert personal.list_situation(conn, "alice") == []
        exported = personal.list_situation(conn, "alice", include_expired=True)
        assert len(exported) == 1
    finally:
        conn.close()


# ---------------------------------------------------------------- 隐私硬能力：导出 / 撤销 / 清空


def test_export_includes_revoked_and_expired() -> None:
    item = _declare(kind="region", value="广东").json()["situation"]
    client.delete(f"/api/v1/situation/{item['id']}", headers=AUTH)
    assert client.get("/api/v1/situation", headers=AUTH).json()["situation"] == []
    exported = client.get("/api/v1/situation/export", headers=AUTH).json()["situation"]
    assert [s["id"] for s in exported] == [item["id"]]


def test_clear_removes_everything_unrecoverably() -> None:
    _declare(kind="industry", value="A")
    _declare(kind="region", value="B")
    r = client.delete("/api/v1/situation", headers=AUTH)
    assert r.json()["deleted"] == 2
    assert client.get("/api/v1/situation", headers=AUTH).json()["situation"] == []
    # 清空后连导出也为空——删除不可被后续查询恢复
    assert client.get("/api/v1/situation/export", headers=AUTH).json()["situation"] == []


def test_cross_user_isolation() -> None:
    _declare(kind="industry", value="alice的行业")
    assert client.get("/api/v1/situation", headers=AUTH_BOB).json()["situation"] == []


def test_unauthorized_401() -> None:
    assert client.get("/api/v1/situation").status_code == 401
    assert client.post("/api/v1/situation",
                       json={"kind": "region", "value": "X"}).status_code == 401
