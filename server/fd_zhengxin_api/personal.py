"""个人层存储（骨架期）：理解卡 / 学习地图 / 理解年轮。

物理分库铁律（照 fd-zhesi「个人层私有归属」先例）：本库是 fd-zhengxin
产品自有存储（默认 ~/.fd-zhengxin/personal.db，env FDZX__DB），与参照系
（政治思想参照系，独立服务独立库，地址见 config.reference_base_url）
物理分离——参照系是可重建缓存，个人层是用户理解记录，永不混入共享底座。

口径纪律（模型层）：理解卡**不含任何表态字段**——没有立场/赞同/倾向/
评价类列，只记「用户理解了什么」（主题、复述、要点、来源）。呈现分歧
是参照系的事，个人层只沉淀理解。

migration 链：MIGRATIONS 列表按序执行、幂等（user_version 门），
新增迁移只追加不改动历史项。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import config

MIGRATIONS = [
    # 0001：理解卡 + 学习地图 + 理解年轮 + 审计（骨架）
    """
    CREATE TABLE understanding_cards (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        topic TEXT NOT NULL,                      -- 理解对象（概念/文本/脉络）
        digest TEXT NOT NULL,                     -- 我的理解（用自己的话复述）
        key_points TEXT,                          -- JSON 要点列表
        refs TEXT,                                -- JSON 来源引用（参照系坐标）
        status TEXT NOT NULL DEFAULT 'active',    -- active | sealed
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        sealed_at TEXT
    );
    CREATE INDEX idx_cards_user ON understanding_cards(user_id, status);
    CREATE TABLE learning_maps (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        title TEXT NOT NULL,
        snapshot TEXT NOT NULL,                   -- JSON 学习地图快照
        version INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL
    );
    CREATE INDEX idx_maps_user ON learning_maps(user_id, created_at);
    CREATE TABLE understanding_annals (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        card_id TEXT NOT NULL,
        kind TEXT NOT NULL,                       -- sealed | superseded
        snapshot TEXT NOT NULL,                   -- 卡片完整 JSON 快照
        at TEXT NOT NULL
    );
    CREATE INDEX idx_annals_user ON understanding_annals(user_id, at);
    CREATE TABLE audit_log (
        id TEXT PRIMARY KEY,
        user_id TEXT,
        action TEXT NOT NULL,
        resource TEXT,
        outcome TEXT NOT NULL,                    -- allowed | denied
        detail TEXT,
        at TEXT NOT NULL
    );
    """,
]


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or config.db_path()
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def migrate(conn: sqlite3.Connection) -> int:
    """按序应用未执行的迁移（user_version 门，幂等可重放）。"""
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    for i, ddl in enumerate(MIGRATIONS, start=1):
        if i > current:
            conn.executescript(ddl)
            conn.execute(f"PRAGMA user_version = {i}")
            conn.commit()
    return len(MIGRATIONS)


def new_id() -> str:
    return uuid.uuid4().hex[:12]


# ---- 理解卡（骨架数据面；API 挂载接线期补） ----

def card_row_to_dict(r: sqlite3.Row) -> dict:
    return {
        "id": r["id"],
        "topic": r["topic"],
        "digest": r["digest"],
        "key_points": json.loads(r["key_points"]) if r["key_points"] else [],
        "refs": json.loads(r["refs"]) if r["refs"] else [],
        "status": r["status"],
        "created_at": r["created_at"],
        "updated_at": r["updated_at"],
        "sealed_at": r["sealed_at"],
    }


def create_card(
    conn: sqlite3.Connection, user_id: str, topic: str, digest: str,
    key_points: list[str] | None = None, refs: list[dict] | None = None,
) -> dict:
    ts = now()
    cid = new_id()
    conn.execute(
        "INSERT INTO understanding_cards (id, user_id, topic, digest, key_points,"
        " refs, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        (
            cid, user_id, topic, digest,
            json.dumps(key_points or [], ensure_ascii=False),
            json.dumps(refs or [], ensure_ascii=False), ts, ts,
        ),
    )
    conn.commit()
    return get_card(conn, user_id, cid)


def get_card(conn: sqlite3.Connection, user_id: str, card_id: str) -> dict | None:
    r = conn.execute(
        "SELECT * FROM understanding_cards WHERE id=? AND user_id=?",
        (card_id, user_id),
    ).fetchone()
    return card_row_to_dict(r) if r else None


def list_cards(conn: sqlite3.Connection, user_id: str, status: str = "active") -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM understanding_cards WHERE user_id=? AND status=?"
        " ORDER BY created_at",
        (user_id, status),
    ).fetchall()
    return [card_row_to_dict(r) for r in rows]


def list_annals(conn: sqlite3.Connection, user_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM understanding_annals WHERE user_id=? ORDER BY at", (user_id,)
    ).fetchall()
    return [
        {
            "id": r["id"],
            "card_id": r["card_id"],
            "kind": r["kind"],
            "snapshot": json.loads(r["snapshot"]),
            "at": r["at"],
        }
        for r in rows
    ]


# ---- 审计（访问控制的留痕面） ----

def audit(
    conn: sqlite3.Connection, user_id: str | None, action: str,
    resource: str | None, outcome: str, detail: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO audit_log (id, user_id, action, resource, outcome, detail, at)"
        " VALUES (?,?,?,?,?,?,?)",
        (new_id(), user_id, action, resource, outcome, detail, now()),
    )
    conn.commit()
