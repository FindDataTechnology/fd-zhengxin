"""政心门面 API（骨架期）。

/healthz 探活、/api/version 版本。骨架期 CORS 全开（照 fd-zhesi 骨架先例），
接线期收敛。个人层 API（理解卡/学习地图/理解年轮）后续在此挂载；
/api/v1/me 是鉴权自检占位（验证拒绝未授权与审计留痕）。
"""

from __future__ import annotations

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import __version__
from . import genealogy, impact, locate, perspectives, pulse
from .auth import require_user
from .cards_api import router as cards_router
from .decisions_api import router as decisions_router
from .learning_map import router as learning_map_router
from .situation_api import router as situation_router

app = FastAPI(title="fd-zhengxin-api", version=__version__)

app.include_router(locate.router)  # 定位闭环（task 4.2，/api/v1/loops/locate）
app.include_router(perspectives.router)  # 多视角闭环（task 4.3，/api/v1/loops/perspectives）
app.include_router(genealogy.router)  # 谱系闭环（task 4.4，/api/v1/loops/genealogy）
app.include_router(pulse.router)  # 脉搏环：议题立场光谱查询面（/api/v1/pulse）
app.include_router(cards_router)  # 理解卡（task 5.2，/api/v1/cards）
app.include_router(learning_map_router)  # 学习地图（task 5.4，/api/v1/map）
app.include_router(impact.router)  # 影响面环（/api/v1/loops/impact，不裁定利害）
app.include_router(situation_router)  # 处境档案（/api/v1/situation，经济处境四轴）
app.include_router(decisions_router)  # 决策留痕（/api/v1/decisions，追加式不评价）

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok", "service": "fd-zhengxin-api", "version": __version__}


@app.get("/api/version")
def version() -> dict:
    return {"service": "fd-zhengxin-api", "version": __version__}


@app.get("/api/v1/me")
def me(user_id: str = Depends(require_user)) -> dict:
    """鉴权自检（骨架占位）：携带有效用户授权返回 user_id，否则 401。"""
    return {"user_id": user_id}
