"""政心 MCP 服务端（change zhengxin-mcp-surface）。

存在理由：谦面（facet pack）/ 萬星（registry + A2A）/ 壹座（platform）**都按
MCP 工具面消费**，而 `fd-zhengxin-api` 此前只有 REST。结果是我们做出的所有能力
（影响面环 / AI 判断三层 / 处境档案 / 决策留痕 / 贴链接传文件）只在网页上活着，
三处一个都用不上。

工具粒度按「智能体能独立完成一个任务」切，不按 REST 端点切——给智能体 25 个
端点没有意义，它不知道什么时候该调哪个。

    无状态（三处都能直接用）：
        analyze_document   一份文件（链接/上传内容/条目 id）→ 作用面 + 判断
        analyze_subject    一个人物/事件 → 谱系 + 关联文件
        locate_concept     一段话 → 落在哪个学派
        read_spectrum      一个议题 → 立场光谱

    个人层（需调用者身份，三处须透传用户 token）：
        get_my_situation / get_my_decisions / get_my_judgment_prefs

身份：MCP 层从 `Authorization: Bearer <token>` 取 token，复用 auth.py 的
`resolve_user` 得到 user_id，再显式传给下游函数体（绕过 FastAPI 的 Depends）。
**工具签名里没有 user_id**——身份只能来自 header，不能来自工具参数（否则
任何调用方都能冒充别人）。

口径：与 REST 面完全一致——判断仍带完整信封（含「我可能错在哪」「什么会推翻它」），
不裁定利害，群体倡导不开放。MCP 不是放宽口径的后门。
"""

from __future__ import annotations

import argparse
from typing import Any

from fastmcp import FastMCP

from . import __version__

SERVER_NAME = "fd-zhengxin"


def _user_from_token(token: str | None) -> str:
    """token → user_id。未带/无效一律抛 ValueError（工具层转结构化错误）。"""
    from . import auth

    return auth.resolve_user_from_token(token or "")


def _bearer() -> str | None:
    """从当前 MCP 请求里取 Bearer token。

    FastMCP 在工具执行期把当前请求放在 contextvar 里；取不到（stdio 传输）时
    返回 None，由调用方决定是否要求身份。
    """
    try:
        from fastmcp.server.dependencies import get_http_headers
        headers = get_http_headers() or {}
    except Exception:  # noqa: BLE001 —— stdio 或版本差异
        return None
    authz = headers.get("authorization") or headers.get("Authorization") or ""
    if authz.lower().startswith("bearer "):
        return authz[7:].strip()
    return None


def build_server() -> FastMCP:
    mcp: FastMCP = FastMCP(SERVER_NAME)

    # ---------------------------------------------------------- 无状态分析

    @mcp.tool
    def analyze_document(
        url: str | None = None,
        item_id: str | None = None,
        text: str | None = None,
    ) -> dict:
        """解读一份政策文件：它管了谁、往哪个方向、哪些环节待检验、与用户处境是否相交。

        三种给法任选其一（按优先级）：
        - `url`：网页或 PDF 链接（会抓取正文）
        - `item_id`：既有资讯条目的编号（fd-info 的 item_id）
        - `text`：直接给正文

        产出：材料层（作用面要素 + 逐字证据）+ 判断层（统计/手段/对我三层，
        每层可关）+ gaps（缺口，如实说明缺什么）。
        **不裁定对你有利或不利，也不给行动建议。**
        """
        from .impact import ImpactIn, impact

        if not any((url, item_id, text)):
            return {"error": "need_one_of", "detail": "url / item_id / text 至少给一个"}
        if url:
            body = ImpactIn(target_kind="url", target_ref=url)
        elif item_id:
            body = ImpactIn(target_kind="claim", target_ref=item_id)
        else:
            from types import SimpleNamespace
            from . import judgment, looputil, personal
            from .impact import (
                SCOPE_ELEMENTS, _render_body, elements_from_body, infer_direction,
                situation_overlap, transmission_candidate,
            )

            gaps: list[dict] = []
            elements = elements_from_body(text or "", title=None,
                                          item_id="inline", gaps=gaps)
            claims = [{"item_id": "inline", "title": "(直接给定的正文)",
                       "published_at": None, "doc_id": None,
                       "body_available": True, "elements": elements}]
            jblock = judgment.build_judgment_block(
                prefs=personal.DEFAULT_PREFS, elements=elements, situation=[],
            )
            return {
                "match_mode": "inline",
                "claims": claims,
                "transmission_candidates": [transmission_candidate(e) for e in elements],
                "situation_overlap": situation_overlap(elements, []),
                "judgment": jblock,
                "gaps": gaps,
                "notice": "以上为正文要素转述，不裁定利害。",
            }
        # REST 函数体以 user_id 为必填（个人层用它读处境/偏好）；
        # 无身份时用匿名占位——分析本身不依赖身份，只有「对我判断」依赖。
        return impact(body, user_id=_bearer_user_or_anon())

    @mcp.tool
    def analyze_subject(name: str) -> dict:
        """追踪一个人物 / 事件 / 政党：身份 + 学派 + 出入关系边全集 + 关联文件。

        `name` 可为 QID、curated id（seed:china/…）或标签（中英文、繁简皆可）。
        响应第一层带 `coverage` 三态（available / not_in_snapshot / not_ingested）
        ——空关系边不等于实体不存在，务必读它。
        """
        from . import reference_mcp

        rc = reference_mcp.client()
        if not rc.base_url:
            return {"error": "not_configured",
                    "detail": "FDZX__REFERENCE_BASE_URL 未设置：参照系是实体读面的硬前置"}
        r = rc.entity_dossier(name)
        if not r.ok:
            return {"error": r.reason, "detail": r.detail}
        return r.data or {}

    @mcp.tool
    def locate_concept(text: str) -> dict:
        """把一段政治文本定位到参照系：它落在哪个学派、有哪些历史先例。

        有充分证据才归派；强度低于阈值时如实说「无法定位」而不硬凑。
        当代议题另补思潮坐标。
        """
        from .locate import LocateIn, locate

        return locate(LocateIn(text=text[:4000]), user_id=_bearer_user_or_anon())

    @mcp.tool
    def read_spectrum(issue: str) -> dict:  # noqa: F811
        """一个议题的立场光谱：各方怎么说、每条的逐字证据。

        增量消费柏讯 info-stream 的政治信息流。上游不可用时**冻结降级**并带
        结构化原因，不静默空转；缺证据的强断言不产出（计入 dropped_no_evidence）。
        """
        from . import pulse

        conn = pulse.connect()
        try:
            snap = pulse.get_spectrum_guarded(issue, conn=conn)
            if snap is None:
                return {"error": "unknown_issue", "detail": f"未知议题：{issue}"}
            return snap
        finally:
            conn.close()

    # ---------------------------------------------------------- 个人层

    @mcp.tool
    def get_my_situation() -> dict:
        """我声明的处境（地区/行业/职业/资产类别）。需要调用者身份。"""
        from . import personal

        uid = _bearer_user_or_raise()
        conn = personal.connect()
        try:
            return {"situation": personal.list_situation(conn, uid)}
        finally:
            conn.close()

    @mcp.tool
    def get_my_decisions() -> dict:
        """我记下的决策留痕（追加式、永不改写、无评分）。需要调用者身份。"""
        from . import personal

        uid = _bearer_user_or_raise()
        conn = personal.connect()
        try:
            return {"decisions": personal.list_decisions(conn, uid)}
        finally:
            conn.close()

    @mcp.tool
    def get_my_judgment_prefs() -> dict:
        """三层 AI 判断的开关（默认全开）。需要调用者身份。"""
        from . import personal

        uid = _bearer_user_or_raise()
        conn = personal.connect()
        try:
            return {"prefs": personal.get_judgment_prefs(conn, uid)}
        finally:
            conn.close()

    return mcp


_ANON_USER = "__anon__"   # 分析类工具无身份时的占位：分析不依赖身份，只「对我判断」依赖


def _bearer_user_or_anon() -> str:
    """有 token 就用它解析身份，没有就返回匿名占位（分析类工具可匿名调用）。"""
    tok = _bearer()
    if not tok:
        return _ANON_USER
    try:
        return _user_from_token(tok)
    except Exception:  # noqa: BLE001 —— token 无效不阻塞分析，降为匿名
        return _ANON_USER


def _bearer_user_or_raise() -> str:
    """个人层工具必须有身份：无 token → 抛错（工具层转结构化 401）。"""
    return _user_from_token(_bearer())


def main(argv: list[str] | None = None) -> int:  # pragma: no cover —— 传输层
    ap = argparse.ArgumentParser(prog="fd-zhengxin-mcp")
    ap.add_argument("--http", type=int, default=None, help="HTTP 端口（默认 stdio）")
    args = ap.parse_args(argv)
    server = build_server()
    if args.http:
        server.run(transport="streamable-http", port=args.http, host="0.0.0.0")
    else:
        server.run()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["build_server", "SERVER_NAME", "__version__"]
