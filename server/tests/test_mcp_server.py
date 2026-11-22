"""政心 MCP 面：谦面 / 萬星 / 壹座 三处的消费入口。

这套测试守三件事：
1. 工具面存在且命名是「动词_名词」；
2. **身份只能来自 header，不能来自工具参数**——工具签名里出现 user_id 就是漏洞
   （任何调用方都能冒充别人）；
3. 口径与 REST 面一致：判断仍带完整信封，不裁定利害。
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

from fd_zhengxin_api import auth, mcp_server


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("FDZX__DB", str(tmp_path / "personal.db"))
    monkeypatch.setenv("FDZX__AUTH_DEV_TOKENS", "tok-alice=alice,tok-bob=bob")


def _tools() -> list:
    m = mcp_server.build_server()
    return asyncio.run(m.list_tools())


# ---------------------------------------------------------------- 工具面

def test_tool_surface_is_stable() -> None:
    names = {t.name for t in _tools()}
    assert names == {
        "analyze_document", "analyze_subject", "locate_concept", "read_spectrum",
        "get_my_situation", "get_my_decisions", "get_my_judgment_prefs",
    }


def test_tool_names_are_verb_noun() -> None:
    """新工具一律「动词_名词」（tool-surface 的 naming_convention）。"""
    verbs = ("analyze", "locate", "read", "get")
    for t in _tools():
        assert t.name.split("_")[0] in verbs, f"{t.name} 不是动词_名词"


def test_every_tool_has_description() -> None:
    """智能体靠描述决定调不调——没描述的工具等于不存在。"""
    for t in _tools():
        assert (t.description or "").strip(), f"{t.name} 缺描述"


# ---------------------------------------------------------------- 身份边界（最重要）

def test_no_tool_takes_user_id_parameter() -> None:
    """身份只能来自 Authorization header，不能来自工具参数。

    如果签名里出现 user_id，任何调用方传别人的 id 就能读别人的处境与决策——
    这是整个个人层最致命的一个口子。
    """
    for t in _tools():
        params = set((t.parameters or {}).get("properties", {}).keys())
        for banned in ("user_id", "uid", "user", "owner"):
            assert banned not in params, f"{t.name} 暴露了身份参数 {banned!r}"


def test_resolve_user_from_token_dev_tokens() -> None:
    assert auth.resolve_user_from_token("tok-alice") == "alice"
    assert auth.resolve_user_from_token("tok-bob") == "bob"


def test_resolve_user_from_token_rejects_empty_and_garbage() -> None:
    with pytest.raises(ValueError, match="缺 Bearer token"):
        auth.resolve_user_from_token("")
    with pytest.raises(ValueError, match="token 无效"):
        auth.resolve_user_from_token("not-a-real-token")


def test_anonymous_analysis_allowed_personal_layer_denied() -> None:
    """无 token：分析类可匿名（降为 anon 占位），个人层直接抛错。"""
    assert mcp_server._bearer_user_or_anon() == mcp_server._ANON_USER
    with pytest.raises(ValueError):
        mcp_server._bearer_user_or_raise()


# ---------------------------------------------------------------- 口径一致

def test_analyze_document_inline_text_returns_envelope() -> None:
    """inline 正文路径：走同一套抽取与判断，信封六字段齐全。"""
    m = mcp_server.build_server()
    fn = next(t.fn for t in asyncio.run(m.list_tools()) if t.name == "analyze_document")
    out = fn(text="发文机关：财政部\n文 号：财预〔2026〕1号\n各省、自治区人民政府：\n"
                  "第六条 禁止以不合理条件限制供应商。")
    assert out["match_mode"] == "inline"
    els = {e["element"]: e for e in out["claims"][0]["elements"]}
    assert els["发文机关"]["value"] == "财政部"
    assert els["文号"]["value"] == "财预〔2026〕1号"
    # 元数据不带方向（与 REST 同一规则）
    assert els["文号"]["direction"] is None


def test_analyze_document_requires_one_input() -> None:
    m = mcp_server.build_server()
    fn = next(t.fn for t in asyncio.run(m.list_tools()) if t.name == "analyze_document")
    out = fn()
    assert out["error"] == "need_one_of"


def test_analyze_subject_reports_not_configured() -> None:
    """参照系没配时如实说，不是静默空。"""
    import os
    old = os.environ.pop("FDZX__REFERENCE_BASE_URL", None)
    try:
        m = mcp_server.build_server()
        fn = next(t.fn for t in asyncio.run(m.list_tools()) if t.name == "analyze_subject")
        out = fn(name="习近平")
        assert out["error"] == "not_configured"
    finally:
        if old is not None:
            os.environ["FDZX__REFERENCE_BASE_URL"] = old


# ---------------------------------------------------------------- 挂载契约（实撞过）

def test_app_mounts_mcp_at_slash_mcp() -> None:
    """主 app 必须在 /mcp 挂上 MCP 子应用——三处就靠这个路径连。"""
    from fd_zhengxin_api.main import app

    assert any(getattr(r, "path", "") == "/mcp" for r in app.routes)


def test_app_lifespan_is_mcp_lifespan() -> None:
    """父 app 的 lifespan 必须是 MCP 子应用的那个。

    实测（fastmcp 3.4）：只 mount 不传 lifespan，任何 /mcp 请求都抛
    「StreamableHTTPSessionManager task group was not initialized」——
    握手直接 500，工具面永远连不上。
    """
    from fd_zhengxin_api.main import app

    assert app.router.lifespan_context is not None


# ---------------------------------------------------------------- 网关注册面（实撞：Dockerfile 没 COPY）

def test_line_manifest_exists_and_shaped() -> None:
    """网关靠 line-manifest.json 注册这一条线；缺它三处永远发现不了这个服务。"""
    import json
    from pathlib import Path

    p = Path(__file__).resolve().parents[1] / "line-manifest.json"
    assert p.is_file(), "line-manifest.json 缺失"
    d = json.loads(p.read_text())
    assert d["schema"] == "line-manifest/v1"
    assert d["line"] == "zhengxin"
    assert d["surfaces"]["mcp"] is True


def test_dockerfile_copies_line_manifest() -> None:
    """清单要进镜像才读得到。

    实测坑：Dockerfile 只 `COPY pyproject.toml README.md` + `COPY fd_zhengxin_api`，
    根目录的 line-manifest.json 根本不在镜像里——网关读不到，注册面静默缺失。
    """
    from pathlib import Path

    df = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text()
    copy_lines = [l for l in df.splitlines() if l.startswith("COPY")]
    assert any("line-manifest.json" in l for l in copy_lines), \
        "Dockerfile 没 COPY line-manifest.json：清单进不了镜像"


# ---------------------------------------------------------------- 网关注册面（实撞：Dockerfile 没 COPY）

def test_line_manifest_exists_and_shaped() -> None:
    """网关靠 line-manifest.json 注册这一条线；缺它三处永远发现不了这个服务。"""
    import json
    from pathlib import Path

    p = Path(__file__).resolve().parents[1] / "line-manifest.json"
    assert p.is_file(), "line-manifest.json 缺失"
    d = json.loads(p.read_text())
    assert d["schema"] == "line-manifest/v1"
    assert d["line"] == "zhengxin"
    assert d["surfaces"]["mcp"] is True


def test_dockerfile_copies_line_manifest() -> None:
    """清单要进镜像才读得到。

    实测坑：Dockerfile 只 `COPY pyproject.toml README.md` + `COPY fd_zhengxin_api`，
    根目录的 line-manifest.json 根本不在镜像里——网关读不到，注册面静默缺失。
    """
    from pathlib import Path

    df = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text()
    copy_lines = [l for l in df.splitlines() if l.startswith("COPY")]
    assert any("line-manifest.json" in l for l in copy_lines), \
        "Dockerfile 没 COPY line-manifest.json：清单进不了镜像"


# ---------------------------------------------------------------- 授权头获取（实撞）

def test_bearer_reads_authorization_which_is_excluded_by_default() -> None:
    """`get_http_headers()` 默认排除 authorization——不显式 include 就永远读不到。

    实测生产：带合法 token 调 get_my_* 仍报「缺 Bearer token」，因为 fastmcp
    把 authorization 当敏感头挡住了。这条测试用假 header 源验证 include 生效。
    """
    import fd_zhengxin_api.mcp_server as ms

    calls = []

    class FakeDeps:
        @staticmethod
        def get_http_headers(include=None, **kw):
            calls.append(include)
            if include and "authorization" in include:
                return {"authorization": "Bearer tok-alice"}
            return {}

    import sys
    import types
    fake = types.ModuleType("fastmcp.server.dependencies")
    fake.get_http_headers = FakeDeps.get_http_headers
    saved = sys.modules.get("fastmcp.server.dependencies")
    sys.modules["fastmcp.server.dependencies"] = fake
    try:
        assert ms._bearer() == "tok-alice"
        assert calls and "authorization" in calls[0], "必须显式 include authorization"
    finally:
        if saved is not None:
            sys.modules["fastmcp.server.dependencies"] = saved
        else:
            sys.modules.pop("fastmcp.server.dependencies", None)


def test_personal_tools_run_migrations_on_fresh_db(tmp_path, monkeypatch) -> None:
    """个人层工具要自己建表——冷启动时库里还没有 judgment_prefs。

    实测：鉴权修好后报 "no such table: judgment_prefs"——连接了但没迁移。
    """
    from fd_zhengxin_api import personal

    conn = personal.connect(str(tmp_path / "fresh.db"))
    try:
        # 新库：表确实不存在
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "judgment_prefs" not in tables
        personal.migrate(conn)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "judgment_prefs" in tables
    finally:
        conn.close()


def test_every_personal_tool_calls_migrate() -> None:
    """三个个人层工具都必须先 migrate 再读——漏一个就是那条工具的冷启动故障。"""
    import inspect
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1]
           / "fd_zhengxin_api" / "mcp_server.py").read_text()
    # get_my_* 三个函数体里各出现一次 personal.migrate(conn)
    assert src.count("personal.migrate(conn)") >= 3


def test_mcp_both_path_forms_reach_the_app() -> None:
    """`/mcp` 与 `/mcp/` 都必须直达 MCP 应用（不经 307）。

    实测：Starlette 的 mount 会让无斜杠形态先 307；网关不跟随重定向，
    于是经网关**永远**吃 307/405。中间件把 `/mcp` 改写成 `/mcp/` 后直通。
    """
    import inspect
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1]
           / "fd_zhengxin_api" / "main.py").read_text()
    assert 'request.url.path == "/mcp"' in src, "缺无斜杠直通的中间件"
    assert '"/mcp/"' in src


def test_mcp_path_without_trailing_slash_is_the_gateway_form() -> None:
    """`/mcp`（无尾斜杠）必须直接可用——网关转发的就是这一形态。

    实测：`http_app(path="/")` + `app.mount("/mcp", ...)` 会让 `/mcp` 返 307
    重定向，而网关不带尾斜杠 → 外部永远连不上（生产实测：经网关 307/405，
    直连另一个同形态服务 200）。
    """
    from fd_zhengxin_api.main import app

    paths = {getattr(r, "path", "") for r in app.routes}
    assert "/mcp" in paths
