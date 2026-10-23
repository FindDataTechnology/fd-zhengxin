"""LLM 灰区判别器（design.md D5）：灰区内容交 LLM 复核，动作恒为标灰。

铁律（口径红线）：
- 判别器**没有放行权**：无论 LLM 结论如何，动作只有 GREY；LLM 只给人工
  分诊参考意见（risky / clean），KEY 缺失、调用失败、超时、空响应、无法
  解析一律按 grey 处理，绝不因「LLM 说干净」而放行；
- 每次标灰落 sqlite 表 grey_queue（pending），驱动**人工**规则库更新
  队列——新口径先人工进规则库版本（guard_rules，规则只增不改），不在
  判别器里私设规则。

依赖 llm.py 网关习惯：不设 FDZX__LLM_API_KEY 时 chat() 返回 None、
失败抛 LLMError 不重试；本模块捕获后降级为 grey，绝不放行。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from . import llm, llm_ledger
from .guard import Action

SYSTEM_PROMPT = (
    "你是「政心」内容口径守卫的灰区判别器。政心对政治思想内容只做理解"
    "不做表态。判断给定候选产出是否包含以下口径风险：①结果预测（选举/"
    "走势预言）；②立场推荐或优劣评价；③行动号召；④无来源强断言；"
    "⑤当代议题视角失衡。只回答 risky 或 clean 两个词之一，"
    "不要输出任何其他内容。"
)


@dataclass
class GreyDecision:
    """灰区判别结果：action 恒为 GREY；llm_opinion 仅作人工分诊参考。"""

    action: Action          # 恒为 Action.GREY
    llm_opinion: str        # risky | clean | unavailable
    reason_code: str        # ""（正常）| llm_unavailable | llm_error | llm_unparseable


def classify_grey(
    payload: dict, conn: sqlite3.Connection | None = None,
    ruleset_version: str = "",
) -> GreyDecision:
    """灰区判别：LLM 只出参考意见，动作恒为标灰；conn 给了即落 grey_queue。"""
    if not llm.is_configured():
        decision = GreyDecision(Action.GREY, "unavailable", "llm_unavailable")
    else:
        try:
            raw = llm_ledger.chat_logged(
                [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": _user_prompt(payload)},
                ],
                surface="guard-grey",
                temperature=0.0,
                max_tokens=16,
            )
        except llm.LLMError:
            # 调用失败 / 超时 / 非 2xx：一律按 grey 处理，绝不放行
            decision = GreyDecision(Action.GREY, "unavailable", "llm_error")
        else:
            decision = _parse(raw)
    if conn is not None:
        enqueue_grey(conn, payload, decision, ruleset_version=ruleset_version)
    return decision


def _user_prompt(payload: dict) -> str:
    keys = ("topic", "is_contemporary", "claims", "views", "body")
    slim = {k: payload.get(k) for k in keys if isinstance(payload, dict) and k in payload}
    return json.dumps(slim, ensure_ascii=False)


def _parse(raw: str | None) -> GreyDecision:
    """解析 LLM 一词答复；空响应 / 无法解析按 grey（llm_unparseable）处理。"""
    if not raw:
        return GreyDecision(Action.GREY, "unavailable", "llm_unparseable")
    text = raw.strip().lower()
    if "risky" in text:
        return GreyDecision(Action.GREY, "risky", "")
    if "clean" in text:
        return GreyDecision(Action.GREY, "clean", "")
    return GreyDecision(Action.GREY, "unavailable", "llm_unparseable")


# ---- 人工规则库更新队列（grey_queue） ----

GREY_QUEUE_DDL = """
CREATE TABLE IF NOT EXISTS grey_queue (
    id TEXT PRIMARY KEY,
    payload TEXT NOT NULL,                     -- 候选产出 JSON 快照
    llm_opinion TEXT NOT NULL,                 -- risky | clean | unavailable
    reason_code TEXT NOT NULL DEFAULT '',      -- llm_unavailable / llm_error / llm_unparseable / ''
    ruleset_version TEXT NOT NULL DEFAULT '',  -- 判别时所用规则库版本（审计回放）
    status TEXT NOT NULL DEFAULT 'pending',    -- pending | resolved（人工规则库更新后置 resolved）
    created_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _rows(conn: sqlite3.Connection) -> sqlite3.Connection:
    """确保行按名字可取（照 personal.connect 习惯补 Row 工厂，幂等）。"""
    if not isinstance(conn.row_factory, sqlite3.Row):  # pragma: no branch
        conn.row_factory = sqlite3.Row
    return conn


def enqueue_grey(
    conn: sqlite3.Connection, payload: dict, decision: GreyDecision,
    ruleset_version: str = "",
) -> str:
    """标灰入队：人工规则库更新队列落一行 pending，返回队列 id。"""
    conn = _rows(conn)
    conn.execute(GREY_QUEUE_DDL)
    qid = _new_id()
    conn.execute(
        "INSERT INTO grey_queue (id, payload, llm_opinion, reason_code,"
        " ruleset_version, status, created_at) VALUES (?,?,?,?,?,?,?)",
        (
            qid,
            json.dumps(payload, ensure_ascii=False),
            decision.llm_opinion,
            decision.reason_code,
            ruleset_version,
            "pending",
            _now(),
        ),
    )
    conn.commit()
    return qid


def list_grey_queue(
    conn: sqlite3.Connection, status: str | None = None,
) -> list[dict]:
    """列出灰区队列（默认 pending），供人工分诊与规则库更新。"""
    conn = _rows(conn)
    conn.execute(GREY_QUEUE_DDL)
    if status:
        rows = conn.execute(
            "SELECT * FROM grey_queue WHERE status=? ORDER BY created_at", (status,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM grey_queue ORDER BY created_at"
        ).fetchall()
    return [
        {
            "id": r["id"],
            "payload": json.loads(r["payload"]),
            "llm_opinion": r["llm_opinion"],
            "reason_code": r["reason_code"],
            "ruleset_version": r["ruleset_version"],
            "status": r["status"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]
