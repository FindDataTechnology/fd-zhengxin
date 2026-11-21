"""处境档案 API（change zhengxin-entity-and-impact-loops / 0003）。

处境是**经济处境**，四轴闭集：地区 / 行业 / 职业 / 资产类别。这不是收窄了产品，
而是这条功能在口径上成立的前提——一旦允许党派或意识形态自述，政心就自己造了
一个政治档案，正是它对理解卡禁掉的那类字段。

三条隐私硬能力（v1 就有，不是待办）：
- `GET`    导出全部（含过期）——用户有权拿走自己的全部数据；
- `DELETE /{id}` 撤销单条；
- `DELETE` 一键清空。

拒收面：请求体 `extra=forbid` + 字段名黑名单双保险（照 cards_api 的 _BANNED 手法）。
写进 spec 的那条 MUST NOT 由测试守，不由文档守。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from . import personal
from .auth import require_user

router = APIRouter(prefix="/api/v1/situation", tags=["situation"])

# 字段名黑名单：政治身份类字段投进来直接 422（extra=forbid 已拦，这里再拦一次
# 并给出可读原因——两道门的意义是让「为什么不行」可见，不只是「不行」）
_BANNED_FIELDS = {
    "party", "party_affiliation", "political_party", "ideology",
    "political_stance", "stance", "attitude", "vote", "voting_preference",
    "political_identity", "党派", "政党", "意识形态", "政治立场", "政治态度", "投票",
}


class SituationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str = Field(min_length=1, max_length=32)
    value: str = Field(min_length=1, max_length=200)
    ttl_days: int = Field(default=personal.SITUATION_TTL_DAYS, ge=1, le=3650)


@router.post("")
def declare(body: SituationIn, user_id: str = Depends(require_user)) -> dict:
    """声明一项处境。kind 只接受四轴闭集；政治身份类字段一律拒收。"""
    if body.kind in _BANNED_FIELDS:
        raise HTTPException(
            status_code=422,
            detail={
                "reason": "political_identity_not_accepted",
                "detail": (
                    "处境档案只记经济处境（地区/行业/职业/资产类别）。"
                    "党派、意识形态、政治态度不属于本档案——这是口径约束，不是遗漏。"
                ),
            },
        )
    try:
        conn = personal.connect()
        try:
            item = personal.declare_situation(
                conn, user_id, body.kind, body.value, body.ttl_days,
            )
        finally:
            conn.close()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"reason": "invalid_situation",
                                                     "detail": str(exc)}) from exc
    return {"situation": item}


@router.get("")
def list_all(user_id: str = Depends(require_user)) -> dict:
    """列有效处境（未撤销、未过期）。过期视为未声明，不静默沿用。"""
    conn = personal.connect()
    try:
        return {"situation": personal.list_situation(conn, user_id)}
    finally:
        conn.close()


@router.get("/export")
def export_all(user_id: str = Depends(require_user)) -> dict:
    """全量导出（含过期与已撤销）：用户有权拿走自己的全部数据。"""
    conn = personal.connect()
    try:
        return {"situation": personal.list_situation(conn, user_id, include_expired=True)}
    finally:
        conn.close()


@router.delete("/{sid}")
def revoke(sid: str, user_id: str = Depends(require_user)) -> dict:
    conn = personal.connect()
    try:
        if not personal.revoke_situation(conn, user_id, sid):
            raise HTTPException(status_code=404, detail={"reason": "not_found"})
        return {"revoked": sid}
    finally:
        conn.close()


@router.delete("")
def clear_all(user_id: str = Depends(require_user)) -> dict:
    """一键清空：删除该用户全部处境声明，删除不可被后续查询恢复。"""
    conn = personal.connect()
    try:
        return {"deleted": personal.clear_situation(conn, user_id)}
    finally:
        conn.close()
