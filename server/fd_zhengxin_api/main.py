"""政心门面 API（骨架期）。

/healthz 探活、/api/version 版本。骨架期 CORS 全开（照 fd-zhesi 骨架先例），
接线期收敛。个人层 API（理解卡/学习地图/理解年轮）后续在此挂载；
/api/v1/me 是鉴权自检占位（验证拒绝未授权与审计留痕）。
"""

from __future__ import annotations

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import __version__
from .auth import require_user

app = FastAPI(title="fd-zhengxin-api", version=__version__)

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
