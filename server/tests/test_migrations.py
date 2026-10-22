"""骨架验证：migration 幂等 + 理解卡模型口径（无任何表态字段）。"""

from __future__ import annotations

import pytest

from fd_zhengxin_api import personal


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("FDZX__DB", str(tmp_path / "personal.db"))


def test_migrate_idempotent() -> None:
    """重复 migrate：user_version 稳定在迁移数，表结构不重建不报错。"""
    conn = personal.connect()
    try:
        assert personal.migrate(conn) == len(personal.MIGRATIONS)
        v1 = conn.execute("PRAGMA user_version").fetchone()[0]
        assert personal.migrate(conn) == len(personal.MIGRATIONS)  # 重放幂等
        v2 = conn.execute("PRAGMA user_version").fetchone()[0]
        assert v1 == v2 == len(personal.MIGRATIONS)

        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert {
            "understanding_cards", "learning_maps", "understanding_annals", "audit_log",
        } <= tables
    finally:
        conn.close()

    # 新连接再跑一遍（模拟服务重启）：同样幂等
    conn2 = personal.connect()
    try:
        assert personal.migrate(conn2) == len(personal.MIGRATIONS)
    finally:
        conn2.close()


def test_understanding_card_has_no_stance_columns() -> None:
    """口径纪律（模型层）：理解卡不含任何表态字段（立场/赞同/倾向/评价）。"""
    conn = personal.connect()
    try:
        personal.migrate(conn)
        cols = {
            r[1]
            for r in conn.execute("PRAGMA table_info(understanding_cards)").fetchall()
        }
        assert cols == {
            "id", "user_id", "topic", "digest", "key_points", "refs",
            "status", "created_at", "updated_at", "sealed_at",
        }
        banned = {"position", "stance", "attitude", "agreement", "endorsement",
                  "opinion", "stance_stability", "evaluation"}
        assert not cols & banned
    finally:
        conn.close()


def test_card_roundtrip(tmp_path) -> None:
    """骨架数据面最小闭环：建卡、读卡、列卡（个人层，双账号隔离）。"""
    conn = personal.connect()
    try:
        personal.migrate(conn)
        card = personal.create_card(
            conn, "alice", "社会契约", "霍布斯与洛克对自然状态的预设不同",
            key_points=["自然状态", "让渡范围"],
            refs=[{"source": "reference:leviathan", "loc": "ch.13"}],
        )
        assert card["status"] == "active" and card["topic"] == "社会契约"
        assert card["key_points"] == ["自然状态", "让渡范围"]
        assert card["refs"][0]["source"] == "reference:leviathan"

        assert personal.get_card(conn, "alice", card["id"])["digest"]
        assert personal.get_card(conn, "bob", card["id"]) is None  # 用户隔离
        assert len(personal.list_cards(conn, "alice")) == 1
        assert personal.list_cards(conn, "bob") == []
    finally:
        conn.close()
