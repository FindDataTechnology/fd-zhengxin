"""决策留痕 API（change zhengxin-entity-and-impact-loops / 0004）。

正心不替你决定，但它记下你在读完之后决定了什么，以及当时手里有什么。
这是「不推荐立场」与「帮你判断」之间的那条窄路：系统留痕，不评价。

四条硬纪律（借 decision-review-log 的范式，不借它的表——那套的幂等键要
symbol 与收益结算，政治决策两样都没有）：
1. 追加式：产出即落账，不攒批；
2. 幂等键 (user_id, target_kind, target_ref, note)：同内容重复提交返回既有记录；
3. 修订 = 新记录 + `revises` 指针，原记录逐字不动；
4. **无评价与聚合**：没有 score / rating / confidence / aggregate 列，
   也没有跨用户比较——一旦出现聚合，它就从「我的留痕」变成「系统在引导」。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from . import personal
from .auth import require_user

router = APIRouter(prefix="/api/v1/decisions", tags=["decisions"])

TARGET_KINDS = ("claim", "entity", "impact")


class DecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_kind: str = Field(min_length=1, max_length=32)
    target_ref: str = Field(min_length=1, max_length=500)
    note: str = Field(min_length=1, max_length=4000)
    target_label: str | None = Field(default=None, max_length=500)
    refs: list[dict] = Field(default_factory=list)
    revises: str | None = Field(default=None, max_length=64)


@router.post("")
def record(body: DecisionIn, user_id: str = Depends(require_user)) -> dict:
    """记录一项决定。重复提交（同内容）返回既有记录，不产生重复条目。"""
    if body.target_kind not in TARGET_KINDS:
        raise HTTPException(
            status_code=422,
            detail={"reason": "invalid_target_kind",
                    "detail": f"target_kind 只支持 {list(TARGET_KINDS)}"},
        )
    conn = personal.connect()
    try:
        if body.revises and not personal.get_decision(conn, user_id, body.revises):
            raise HTTPException(
                status_code=404,
                detail={"reason": "revises_not_found",
                        "detail": "被修订的记录不存在或不属于本用户"},
            )
        item = personal.record_decision(
            conn, user_id, body.target_kind, body.target_ref, body.note,
            target_label=body.target_label, refs=body.refs, revises=body.revises,
        )
        personal.audit(conn, user_id, "decisions.record", item["id"], "allowed")
        return {"decision": item}
    finally:
        conn.close()


@router.get("")
def list_all(user_id: str = Depends(require_user)) -> dict:
    """列自己的决策留痕（按时间倒序）。只看到自己的——账本按 caller 隔离。"""
    conn = personal.connect()
    try:
        return {"decisions": personal.list_decisions(conn, user_id)}
    finally:
        conn.close()


@router.get("/{did}")
def get_one(did: str, user_id: str = Depends(require_user)) -> dict:
    conn = personal.connect()
    try:
        item = personal.get_decision(conn, user_id, did)
        if item is None:
            raise HTTPException(status_code=404, detail={"reason": "not_found"})
        return {"decision": item}
    finally:
        conn.close()
