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
from .judgment_api import router as judgment_router
from .learning_map import router as learning_map_router
from .situation_api import router as situation_router

# MCP 面（谦面 / 萬星 / 壹座 三处的消费入口）。
# 必须在 FastAPI 构造**之前**建好——父 app 只能接受一个 lifespan，
# 而 FastMCP 的 session manager 要靠它初始化（实测 fastmcp 3.4：只 mount
# 不传 lifespan，任何 /mcp 请求都抛「task group was not initialized」）。
from .mcp_server import build_server as _build_mcp  # noqa: E402

_mcp = _build_mcp()
# 做成一个**挂在根上的子应用**（path="/"），再由下面那段把它挂到 /mcp。
# 关键：不能把 FastMCP 的 Route 直接塞进 app.router.routes——那样拿到的路由
# 在 ASGI 层丢掉了方法/生命周期处理，POST 会被判成未匹配（实测 405 Allow: GET）。
_mcp_app = _mcp.http_app(path="/")

app = FastAPI(title="fd-zhengxin-api", version=__version__, lifespan=_mcp_app.lifespan)

app.include_router(locate.router)  # 定位闭环（task 4.2，/api/v1/loops/locate）
app.include_router(perspectives.router)  # 多视角闭环（task 4.3，/api/v1/loops/perspectives）
app.include_router(genealogy.router)  # 谱系闭环（task 4.4，/api/v1/loops/genealogy）
app.include_router(pulse.router)  # 脉搏环：议题立场光谱查询面（/api/v1/pulse）
app.include_router(cards_router)  # 理解卡（task 5.2，/api/v1/cards）
app.include_router(learning_map_router)  # 学习地图（task 5.4，/api/v1/map）
app.include_router(impact.router)  # 影响面环（/api/v1/loops/impact，不裁定利害）
app.include_router(situation_router)  # 处境档案（/api/v1/situation，经济处境四轴）
app.include_router(decisions_router)  # 决策留痕（/api/v1/decisions，追加式不评价）
app.include_router(judgment_router)  # 判断偏好（/api/v1/judgment/prefs，默认全开）

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)



@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok", "service": "fd-zhengxin-api", "version": __version__}


# ---- MCP 面（谦面 / 萬星 / 壹座 三处的消费入口） ----
# 挂成 /mcp 子应用：同一个进程、同一个 PID、同一份 REST。
# 不把 /mcp 当 k8s 探针——它只接 POST，GET 的 400/405 是协议正常形态
# （fd-quant 的教训：拿 /mcp 当探针会把健康读成故障）。

@app.get("/api/version")
def version() -> dict:
    return {"service": "fd-zhengxin-api", "version": __version__}


@app.get("/api/v1/me")
def me(user_id: str = Depends(require_user)) -> dict:
    """鉴权自检（骨架占位）：携带有效用户授权返回 user_id，否则 401。"""
    return {"user_id": user_id}


# MCP 面在路由全部挂完之后再挂：mount 会把自己的路由表并入 app。
# mount 成子应用（FastMCP 的标准 ASGI 集成方式）。
app.mount("/mcp", _mcp_app)


# 无尾斜杠时 Starlette 会先 307 到 `/mcp/`；而网关**不跟随重定向**，
# 于是外部永远吃 307/405（实测：经网关 405 Allow: GET，直连 `/mcp/` 才 200）。
# 这里把 `/mcp`（无斜杠）直接交给子应用，把改写后的 scope 传下去——
# 不是重定向，是一次 forward。
@app.middleware("http")
async def _mcp_no_trailing_slash(request, call_next):
    if request.url.path == "/mcp":
        request.scope["path"] = "/mcp/"
        request.scope["raw_path"] = b"/mcp/"
    return await call_next(request)
