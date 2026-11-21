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
    """口径纪律（模型层）：理解卡不含任何表态字段（立场/赞同/倾向/评价）。

    0002 追加的 state 是理解进度位（复述中/已理解/搁置），非表态字段。
    """
    conn = personal.connect()
    try:
        personal.migrate(conn)
        cols = {
            r[1]
            for r in conn.execute("PRAGMA table_info(understanding_cards)").fetchall()
        }
        assert cols == {
            "id", "user_id", "topic", "digest", "key_points", "refs",
            "status", "state", "created_at", "updated_at", "sealed_at",
        }
        banned = {"position", "stance", "attitude", "agreement", "endorsement",
                  "opinion", "stance_stability", "evaluation"}
        assert not cols & banned
    finally:
        conn.close()


def test_situation_table_axis_closed_set() -> None:
    """0003 口径线：处境只有经济属性四轴，且 SQL CHECK 真的拦第五个值。

    「党派/意识形态/政治态度」相关政策写在 spec 的 MUST NOT 里；这里守的是
    数据面那一半——即便绕过 API 直写 SQL，CHECK 也不放行。
    """
    conn = personal.connect()
    try:
        personal.migrate(conn)
        cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(situation_declarations)").fetchall()}
        assert cols == {"id", "user_id", "kind", "value",
                        "declared_at", "expires_at", "revoked_at"}
        banned = {"party", "party_affiliation", "ideology", "political_stance",
                  "stance", "attitude", "vote", "voting_preference"}
        assert not cols & banned

        for k in personal.SITUATION_KINDS:
            conn.execute(
                "INSERT INTO situation_declarations (id, user_id, kind, value,"
                " declared_at, expires_at) VALUES (?,?,?,?,?,?)",
                (f"id-{k}", "u", k, "v", "2026-01-01T00:00:00+00:00",
                 "2027-01-01T00:00:00+00:00"),
            )
        with pytest.raises(Exception):  # IntegrityError（CHECK 拒绝第五个值）
            conn.execute(
                "INSERT INTO situation_declarations (id, user_id, kind, value,"
                " declared_at, expires_at) VALUES (?,?,?,?,?,?)",
                ("id-x", "u", "religion", "v", "2026-01-01T00:00:00+00:00",
                 "2027-01-01T00:00:00+00:00"),
            )
            conn.commit()
    finally:
        conn.close()


def test_decision_table_has_no_scoring_columns() -> None:
    """0004 口径线：决策留痕无评分/置信度/聚合列——留痕不是引导。"""
    conn = personal.connect()
    try:
        personal.migrate(conn)
        cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(decision_notes)").fetchall()}
        assert cols == {"id", "user_id", "at", "target_kind", "target_ref",
                        "target_label", "note", "refs", "revises"}
        banned = {"score", "rating", "confidence", "evaluation", "verdict",
                  "grade", "aggregate", "percentile", "rank"}
        assert not cols & banned
    finally:
        conn.close()


def test_card_roundtrip(tmp_path) -> None:
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
