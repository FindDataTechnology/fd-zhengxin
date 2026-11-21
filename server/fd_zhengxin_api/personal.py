"""个人层存储（task 5.1-5.4）：理解卡 / 学习地图 / 理解年轮。

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
    # 0002：理解进度状态（task 5.2）——restating | understood | shelved。
    # 这是「复述推进到哪一步」的进度位，不是表态位：不含任何立场/赞同/
    # 倾向语义，已理解=复述覆盖了关键前提，仅此而已。
    """
    ALTER TABLE understanding_cards ADD COLUMN state TEXT NOT NULL DEFAULT 'restating';
    """,
    # 0003：处境档案（change zhengxin-entity-and-impact-loops）。
    # **只有经济属性四轴**，无党派/意识形态/政治态度——一旦允许那类字段，
    # 政心就自己造了一个政治档案，正是它对理解卡禁掉的东西。
    # 值不富化（不映射行业标准码表）：映射会暗示「系统知道你的行业在上下游
    # 哪个位置」，而那正是处境交叠明确禁止的推断能力。
    """
    CREATE TABLE situation_declarations (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('region','industry','occupation','asset_class')),
        value TEXT NOT NULL,
        declared_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        revoked_at TEXT
    );
    CREATE INDEX idx_situation_user ON situation_declarations(user_id, kind);
    """,
    # 0004：决策留痕（change zhengxin-entity-and-impact-loops）。
    # 追加式、永不就地改写：修订产生新记录 + revises 指针。记录的是
    # 「我读完之后决定了什么、当时引用了什么」，**没有任何评分类列**——
    # 评分/聚合会把它从「我的留痕」变成「系统在引导」。
    # 幂等键含 note：同样的对象 + 同样的决定文本重复提交 → 返回既有记录；
    # 改了文本就是新决定。
    """
    CREATE TABLE decision_notes (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        at TEXT NOT NULL,
        target_kind TEXT NOT NULL,
        target_ref TEXT NOT NULL,
        target_label TEXT,
        note TEXT NOT NULL,
        refs TEXT,
        revises TEXT,
        UNIQUE (user_id, target_kind, target_ref, note)
    );
    CREATE INDEX idx_decision_user ON decision_notes(user_id, at);
    """,
    # 0005：判断偏好（change zhengxin-ai-judgment-and-workbench）。
    # 三层 AI 判断的开关，**默认全开**（用户 2026-11-22 明示）。
    # 不预建行：无行 = 默认全开，读时 COALESCE——新用户不该先有行才能用。
    """
    CREATE TABLE judgment_prefs (
        user_id TEXT PRIMARY KEY,
        statistical INTEGER NOT NULL DEFAULT 1,
        means INTEGER NOT NULL DEFAULT 1,
        personal_j INTEGER NOT NULL DEFAULT 1,
        updated_at TEXT NOT NULL
    );
    """,
]

# 理解进度状态机（task 5.2）：已理解只能经复述校验达阈值进入（API 层把关）
CARD_STATES = ("restating", "understood", "shelved")


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


# ---- 理解卡（task 5.2 数据面：状态流转 + 理解年轮） ----

def card_row_to_dict(r: sqlite3.Row) -> dict:
    return {
        "id": r["id"],
        "topic": r["topic"],
        "digest": r["digest"],
        "key_points": json.loads(r["key_points"]) if r["key_points"] else [],
        "refs": json.loads(r["refs"]) if r["refs"] else [],
        "status": r["status"],
        "state": r["state"],
        "created_at": r["created_at"],
        "updated_at": r["updated_at"],
        "sealed_at": r["sealed_at"],
    }


def create_card(
    conn: sqlite3.Connection, user_id: str, topic: str, digest: str,
    key_points: list[str] | None = None, refs: list[dict] | None = None,
    state: str = "restating",
) -> dict:
    if state not in CARD_STATES:
        raise ValueError(f"state 只支持 {list(CARD_STATES)}")
    ts = now()
    cid = new_id()
    conn.execute(
        "INSERT INTO understanding_cards (id, user_id, topic, digest, key_points,"
        " refs, state, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            cid, user_id, topic, digest,
            json.dumps(key_points or [], ensure_ascii=False),
            json.dumps(refs or [], ensure_ascii=False), state, ts, ts,
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


def list_cards(
    conn: sqlite3.Connection, user_id: str, status: str = "active",
    state: str | None = None,
) -> list[dict]:
    sql = "SELECT * FROM understanding_cards WHERE user_id=? AND status=?"
    params: list = [user_id, status]
    if state:
        sql += " AND state=?"
        params.append(state)
    sql += " ORDER BY created_at"
    rows = conn.execute(sql, params).fetchall()
    return [card_row_to_dict(r) for r in rows]


def set_card_state(
    conn: sqlite3.Connection, user_id: str, card_id: str, state: str,
) -> dict | None:
    """仅 active 卡可转状态（「已理解」只经复述校验达阈值进入，把关在 API 层）。"""
    if state not in CARD_STATES:
        raise ValueError(f"state 只支持 {list(CARD_STATES)}")
    card = get_card(conn, user_id, card_id)
    if not card or card["status"] != "active":
        return None
    conn.execute(
        "UPDATE understanding_cards SET state=?, updated_at=? WHERE id=?",
        (state, now(), card_id),
    )
    conn.commit()
    return get_card(conn, user_id, card_id)


def supersede_card(
    conn: sqlite3.Connection, user_id: str, card_id: str, *,
    kind: str, state: str,
    digest: str | None = None, key_points: list[str] | None = None,
) -> dict | None:
    """旧状态封存入理解年轮（append-only），新状态开卡（spec「理解年轮」）。

    kind：superseded=重复提交换新复述；sealed=搁置。
    年轮快照取封存前的完整卡（含当时的 state），历史不覆盖。
    """
    card = get_card(conn, user_id, card_id)
    if not card or card["status"] != "active":
        return None
    if state not in CARD_STATES:
        raise ValueError(f"state 只支持 {list(CARD_STATES)}")
    ts = now()
    conn.execute(
        "UPDATE understanding_cards SET status='sealed', sealed_at=?, updated_at=?"
        " WHERE id=?",
        (ts, ts, card_id),
    )
    conn.execute(
        "INSERT INTO understanding_annals (id, user_id, card_id, kind, snapshot, at)"
        " VALUES (?,?,?,?,?,?)",
        (
            new_id(), user_id, card_id, kind,
            json.dumps(card, ensure_ascii=False), ts,
        ),
    )
    new_cid = new_id()
    conn.execute(
        "INSERT INTO understanding_cards (id, user_id, topic, digest, key_points,"
        " refs, state, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            new_cid, user_id, card["topic"],
            digest or card["digest"],
            json.dumps(key_points if key_points is not None else card["key_points"],
                       ensure_ascii=False),
            json.dumps(card["refs"], ensure_ascii=False), state, ts, ts,
        ),
    )
    conn.commit()
    return get_card(conn, user_id, new_cid)


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


# ---- 学习地图（task 5.4 数据面：版本化保留历史） ----

def map_row_to_dict(r: sqlite3.Row, with_snapshot: bool = True) -> dict:
    out = {
        "id": r["id"],
        "title": r["title"],
        "version": r["version"],
        "created_at": r["created_at"],
    }
    if with_snapshot:
        out["snapshot"] = json.loads(r["snapshot"])
    return out


def save_map(
    conn: sqlite3.Connection, user_id: str, snapshot: dict,
    title: str | None = None,
) -> dict:
    """存一版学习地图：版本号按用户自增（旧版本全保留，spec「地图更新保留历史」）。"""
    latest = conn.execute(
        "SELECT COALESCE(MAX(version), 0) FROM learning_maps WHERE user_id=?",
        (user_id,),
    ).fetchone()[0]
    version = int(latest) + 1
    mid = new_id()
    ts = now()
    conn.execute(
        "INSERT INTO learning_maps (id, user_id, title, snapshot, version, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (
            mid, user_id, title or f"学习地图 v{version}",
            json.dumps(snapshot, ensure_ascii=False), version, ts,
        ),
    )
    conn.commit()
    return {"id": mid, "title": title or f"学习地图 v{version}", "version": version,
            "snapshot": snapshot, "created_at": ts}


def latest_map(conn: sqlite3.Connection, user_id: str) -> dict | None:
    r = conn.execute(
        "SELECT * FROM learning_maps WHERE user_id=? ORDER BY version DESC LIMIT 1",
        (user_id,),
    ).fetchone()
    return map_row_to_dict(r) if r else None


def get_map_version(conn: sqlite3.Connection, user_id: str, version: int) -> dict | None:
    r = conn.execute(
        "SELECT * FROM learning_maps WHERE user_id=? AND version=?",
        (user_id, version),
    ).fetchone()
    return map_row_to_dict(r) if r else None


def list_map_versions(conn: sqlite3.Connection, user_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM learning_maps WHERE user_id=? ORDER BY version DESC",
        (user_id,),
    ).fetchall()
    return [map_row_to_dict(r, with_snapshot=False) for r in rows]


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


# ---- 处境档案（change zhengxin-entity-and-impact-loops 0003） ----

# 四轴闭集：全部是经济属性，无党派/意识形态/政治态度
SITUATION_KINDS = ("region", "industry", "occupation", "asset_class")
SITUATION_TTL_DAYS = 180  # 过期视为未声明，不静默沿用


def declare_situation(
    conn: sqlite3.Connection, user_id: str, kind: str, value: str,
    ttl_days: int = SITUATION_TTL_DAYS,
) -> dict:
    """声明一项处境（kind 闭集校验在 SQL CHECK + 这里双保险）。"""
    if kind not in SITUATION_KINDS:
        raise ValueError(f"kind 只支持 {list(SITUATION_KINDS)}")
    if not (value or "").strip():
        raise ValueError("value 不能为空")
    from datetime import timedelta

    ts = datetime.now(timezone.utc)
    sid = new_id()
    conn.execute(
        "INSERT INTO situation_declarations (id, user_id, kind, value,"
        " declared_at, expires_at) VALUES (?,?,?,?,?,?)",
        (sid, user_id, kind, value.strip(), ts.isoformat(),
         (ts + timedelta(days=ttl_days)).isoformat()),
    )
    conn.commit()
    return get_situation(conn, user_id, sid)


def situation_row_to_dict(r: sqlite3.Row) -> dict:
    return {k: r[k] for k in r.keys()}


def get_situation(conn: sqlite3.Connection, user_id: str, sid: str) -> dict | None:
    r = conn.execute(
        "SELECT * FROM situation_declarations WHERE id=? AND user_id=?",
        (sid, user_id),
    ).fetchone()
    return situation_row_to_dict(r) if r else None


def list_situation(
    conn: sqlite3.Connection, user_id: str, *, include_expired: bool = False,
) -> list[dict]:
    """列出处境。默认只返回**有效**声明（未撤销且未过期）——过期视为未声明。

    include_expired=True 用于导出：用户有权拿走自己的全部数据，包括过期的。
    """
    rows = conn.execute(
        "SELECT * FROM situation_declarations WHERE user_id=? ORDER BY declared_at DESC",
        (user_id,),
    ).fetchall()
    out = [situation_row_to_dict(r) for r in rows]
    if include_expired:
        return out
    ts = now()
    return [
        s for s in out
        if s["revoked_at"] is None and s["expires_at"] > ts
    ]


def revoke_situation(conn: sqlite3.Connection, user_id: str, sid: str) -> bool:
    r = conn.execute(
        "SELECT 1 FROM situation_declarations WHERE id=? AND user_id=?",
        (sid, user_id),
    ).fetchone()
    if not r:
        return False
    conn.execute(
        "UPDATE situation_declarations SET revoked_at=? WHERE id=?",
        (now(), sid),
    )
    conn.commit()
    return True


def clear_situation(conn: sqlite3.Connection, user_id: str) -> int:
    """一键清空：删除该用户全部处境声明（导出/清空是隐私面的硬能力，不是待办）。"""
    cur = conn.execute("DELETE FROM situation_declarations WHERE user_id=?", (user_id,))
    conn.commit()
    return cur.rowcount


# ---- 决策留痕（change zhengxin-entity-and-impact-loops 0004） ----

def record_decision(
    conn: sqlite3.Connection, user_id: str, target_kind: str, target_ref: str,
    note: str, *, target_label: str | None = None,
    refs: list[dict] | None = None, revises: str | None = None,
) -> dict:
    """记录一项决定（追加式；幂等键 (user_id, target_kind, target_ref, note)）。

    修订 = 新记录 + revises 指向被修订记录，**原记录逐字不动**。
    内容、评价、置信度、聚合一律不在此表——只有「我说了什么、引了什么」。
    """
    if not (note or "").strip():
        raise ValueError("note 不能为空")
    existing = conn.execute(
        "SELECT * FROM decision_notes WHERE user_id=? AND target_kind=?"
        " AND target_ref=? AND note=?",
        (user_id, target_kind, target_ref, note.strip()),
    ).fetchone()
    if existing:
        return decision_row_to_dict(existing)
    did = new_id()
    conn.execute(
        "INSERT INTO decision_notes (id, user_id, at, target_kind, target_ref,"
        " target_label, note, refs, revises) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            did, user_id, now(), target_kind, target_ref, target_label,
            note.strip(), json.dumps(refs or [], ensure_ascii=False), revises,
        ),
    )
    conn.commit()
    return get_decision(conn, user_id, did)


def decision_row_to_dict(r: sqlite3.Row) -> dict:
    d = {k: r[k] for k in r.keys()}
    d["refs"] = json.loads(d["refs"]) if d.get("refs") else []
    return d


def get_decision(conn: sqlite3.Connection, user_id: str, did: str) -> dict | None:
    r = conn.execute(
        "SELECT * FROM decision_notes WHERE id=? AND user_id=?", (did, user_id)
    ).fetchone()
    return decision_row_to_dict(r) if r else None


def list_decisions(conn: sqlite3.Connection, user_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM decision_notes WHERE user_id=? ORDER BY at DESC", (user_id,)
    ).fetchall()
    return [decision_row_to_dict(r) for r in rows]


# ---- 判断偏好（change zhengxin-ai-judgment-and-workbench 0005） ----

# 三层判断的键名 → 列名（对外用语义名，对内是列名）
JUDGMENT_LAYERS = ("statistical", "means", "personal")
_LAYER_COLUMNS = {"statistical": "statistical", "means": "means", "personal": "personal_j"}
# 默认全开：无行即全开（用户 2026-11-22 明示「默认全开，不要默认全关」）
DEFAULT_PREFS = {"statistical": True, "means": True, "personal": True}


def get_judgment_prefs(conn: sqlite3.Connection, user_id: str) -> dict:
    """读三层开关。库里无行即默认全开——不预建行。

    `unavailable` 与 `disabled` 的区分是另一层的事（judgment.py），这里只管开关本身。
    """
    row = conn.execute(
        "SELECT statistical, means, personal_j FROM judgment_prefs WHERE user_id=?",
        (user_id,),
    ).fetchone()
    if row is None:
        return dict(DEFAULT_PREFS)
    return {
        "statistical": bool(row["statistical"]),
        "means": bool(row["means"]),
        "personal": bool(row["personal_j"]),
    }


def set_judgment_prefs(
    conn: sqlite3.Connection, user_id: str, prefs: dict,
) -> dict:
    """写三层开关（只认三层键，未知键抛 ValueError——不静默忽略）。

    部分更新：只写传入的层，未传的层沿用当前值（含「无行=默认全开」）。
    """
    unknown = set(prefs) - set(JUDGMENT_LAYERS)
    if unknown:
        raise ValueError(f"未知判断层 {sorted(unknown)}（合法值 {list(JUDGMENT_LAYERS)}）")
    merged = get_judgment_prefs(conn, user_id)
    merged.update({k: bool(v) for k, v in prefs.items()})
    conn.execute(
        "INSERT INTO judgment_prefs (user_id, statistical, means, personal_j, updated_at)"
        " VALUES (?,?,?,?,?)"
        " ON CONFLICT(user_id) DO UPDATE SET statistical=excluded.statistical,"
        " means=excluded.means, personal_j=excluded.personal_j,"
        " updated_at=excluded.updated_at",
        (
            user_id, int(merged["statistical"]), int(merged["means"]),
            int(merged["personal"]), now(),
        ),
    )
    conn.commit()
    return merged
