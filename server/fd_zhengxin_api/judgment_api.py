"""判断偏好 API（change zhengxin-ai-judgment-and-workbench / 0005）。

三层 AI 判断的开关。**默认全开**——用户 2026-11-22 明示「默认全开，不要默认全关」，
所以无行即全开，新用户不需要先设置什么。

语义区分（judgment.py 里落成状态）：
- 层关 = `disabled`（用户选择，不产出）
- 层开但能力缺 = `unavailable`（如统计判断缺数据）

两者不可混：前者是「我不要」，后者是「现在还做不到」。混了会让人以为是 bug。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict

from . import personal
from .auth import require_user

router = APIRouter(prefix="/api/v1/judgment", tags=["judgment"])


class PrefsIn(BaseModel):
    """部分更新：只传要改的层。extra=forbid 拦未知字段。"""

    model_config = ConfigDict(extra="forbid")

    statistical: bool | None = None
    means: bool | None = None
    personal: bool | None = None


@router.get("/prefs")
def get_prefs(user_id: str = Depends(require_user)) -> dict:
    """读三层开关（无行 = 默认全开）。"""
    conn = personal.connect()
    try:
        return {"prefs": personal.get_judgment_prefs(conn, user_id)}
    finally:
        conn.close()


@router.put("/prefs")
def put_prefs(body: PrefsIn, user_id: str = Depends(require_user)) -> dict:
    """写三层开关（部分更新，未传的层沿用当前值）。"""
    given = {k: v for k, v in body.model_dump().items() if v is not None}
    conn = personal.connect()
    try:
        out = personal.set_judgment_prefs(conn, user_id, given) if given \
            else personal.get_judgment_prefs(conn, user_id)
        personal.audit(conn, user_id, "judgment.prefs", None, "allowed",
                       detail=str(given))
        return {"prefs": out}
    except ValueError as exc:
        raise HTTPException(status_code=422,
                            detail={"reason": "invalid_prefs", "detail": str(exc)}) from exc
    finally:
        conn.close()
