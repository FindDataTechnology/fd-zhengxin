"""LLM 成本台账（任务 4.6）：包一层 llm.py 的调用记录，llm.py 本身不改。

每次真实 LLM 网关调用记一行 sqlite：
- surface（成本归属面，枚举六值）：locate / perspectives / genealogy / pulse /
  guard-grey / personal-verify；
- prompt / completion token、model、时间、结局（ok / error）；
- outcome=error 的行也记账（失败的调用同样花了 prompt token 与预算）。

llm.py 契约（不改，包装层适配）：
- 不设 FDZX__LLM_API_KEY → chat() 直接返回 None，**没有发生网关调用**，
  无成本——本层不记账、不崩溃（调用方照常走确定性回退）；
- 设了 KEY 后失败抛 LLMError——先记 error 行再原样上抛，由调用方降级。

token 口径（如实）：优先采网关回报的 usage（usage_method="usage"，4.6 对账口径）；
网关不回报时退回估算——
prompt/completion token 用确定性启发式估算（CJK 1 字 ≈ 1 token，其余
≈ 4 字符/token），usage_method 列恒为 "estimated"——不冒充实测值。

用法（其他模块后续换用）：
- 包装函数：llm_ledger.chat_logged(messages, surface="pulse", ...) 直接
  替代 llm.chat(...)（多一个必填 surface）；
- 装饰器：@llm_ledger.ledgered("genealogy") 套在 (messages, **kw) -> str|None
  形态的 llm.chat 直替函数上。

库：独立 sqlite（config.ledger_db_path，默认 ~/.fd-zhengxin/llm_ledger.db），
运营层账目不混入个人层库；也接受外部 conn 注入（测试/复用连接）。
"""

from __future__ import annotations

import functools
import math
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import config, llm

SURFACES = ("locate", "perspectives", "genealogy", "pulse", "guard-grey", "personal-verify")
"""成本归属面清单：定位 / 视角 / 脉络（谱系）/ 脉搏环 / 守卫灰区 / 个人验证。"""

LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS llm_ledger (
    id TEXT PRIMARY KEY,
    surface TEXT NOT NULL,                     -- 成本归属面（SURFACES 之一）
    model TEXT NOT NULL,
    prompt_tokens INTEGER NOT NULL,
    completion_tokens INTEGER NOT NULL,
    usage_method TEXT NOT NULL DEFAULT 'estimated',  -- llm.py 不暴露 usage，恒为估算
    outcome TEXT NOT NULL,                     -- ok | error
    detail TEXT,                               -- outcome=error 时的错误描述
    at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ledger_surface ON llm_ledger(surface, at);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def connect(db_path: str | None = None) -> sqlite3.Connection:
    """台账库连接（幂等建表；照 personal.connect 习惯：WAL + Row 工厂）。"""
    path = db_path or config.ledger_db_path()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(LEDGER_DDL)  # 建表 + 索引两条语句，幂等可重放
    return conn


def _check_surface(surface: str) -> None:
    if surface not in SURFACES:
        raise ValueError(f"未知 LLM 台账 surface：{surface!r}（合法值：{SURFACES}）")


def _rows(conn: sqlite3.Connection) -> sqlite3.Connection:
    """注入的外部连接同样确保按名字取行（照 guard_llm._rows 习惯，幂等）。"""
    if not isinstance(conn.row_factory, sqlite3.Row):  # pragma: no branch
        conn.row_factory = sqlite3.Row
    return conn


# ---- token 估算（确定性启发式，不冒充实测） ----

_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")


def estimate_tokens(text: str | None) -> int:
    """确定性估算：CJK 字符 ≈ 1 token/字，其余文本 ≈ 4 字符/token。"""
    if not text:
        return 0
    cjk = len(_CJK_RE.findall(text))
    rest = len(_CJK_RE.sub("", text).strip())
    return cjk + math.ceil(rest / 4)


def _messages_tokens(messages: list[dict[str, str]]) -> int:
    return sum(estimate_tokens(m.get("content", "") if isinstance(m, dict) else "") for m in messages)


# ---- 记账与查询 ----


def record(
    conn: sqlite3.Connection, *, surface: str, model: str,
    prompt_tokens: int, completion_tokens: int,
    outcome: str = "ok", detail: str | None = None,
    usage_method: str = "estimated",
) -> str:
    """落一行台账（通常不经此函数，走 chat_logged / ledgered）。

    usage_method："usage"=网关回报的实测数；"estimated"=本地估算（不冒充实测）。
    """
    _check_surface(surface)
    conn.executescript(LEDGER_DDL)  # 幂等建表（外部注入的连接同样确保表在）
    lid = _new_id()
    conn.execute(
        "INSERT INTO llm_ledger (id, surface, model, prompt_tokens, completion_tokens,"
        " usage_method, outcome, detail, at) VALUES (?,?,?,?,?,?,?,?,?)",
        (lid, surface, model, prompt_tokens, completion_tokens, usage_method,
         outcome, detail, _now()),
    )
    conn.commit()
    return lid


def chat_logged(
    messages: list[dict[str, str]],
    *,
    surface: str,
    model: str | None = None,
    temperature: float = 0.2,
    max_tokens: int = 900,
    timeout: float = 20.0,
    conn: sqlite3.Connection | None = None,
) -> str | None:
    """llm.chat 的记账直替：真实网关调用逐次入账，无 KEY 不记账不崩。

    - surface 必填且必须在 SURFACES 内（成本归属错了比漏记更糟）；
    - 无 KEY：llm.chat 返回 None、无网关调用、无成本 → 不记账，返回 None；
    - LLMError：先记 error 行（prompt token 照记，completion 记 0）再上抛。
    """
    _check_surface(surface)
    if not llm.is_configured():
        return None  # 无 KEY 路径：没有发生调用，不记账、不崩
    resolved_model = model or config.llm_model()
    prompt_tokens = _messages_tokens(messages)
    own = conn is None
    conn = conn or connect()
    try:
        try:
            out = llm.chat_with_usage(
                messages, model=model, temperature=temperature,
                max_tokens=max_tokens, timeout=timeout,
            )
        except llm.LLMError as exc:
            record(
                conn, surface=surface, model=resolved_model,
                prompt_tokens=prompt_tokens, completion_tokens=0,
                outcome="error", detail=str(exc),
            )
            raise
        content = out["content"]
        usage = out.get("usage") or {}
        if usage.get("prompt_tokens") is not None:
            # 网关回报实测值：台账数字与网关侧同源（4.6 对账口径）
            record(
                conn, surface=surface, model=resolved_model,
                prompt_tokens=int(usage.get("prompt_tokens") or 0),
                completion_tokens=int(usage.get("completion_tokens") or 0),
                outcome="ok", usage_method="usage",
            )
        else:
            record(
                conn, surface=surface, model=resolved_model,
                prompt_tokens=prompt_tokens,
                completion_tokens=estimate_tokens(content),
                outcome="ok",
            )
        return content
    finally:
        if own:
            conn.close()


def ledgered(surface: str):
    """装饰器：把 (messages, **kw) -> str|None 形态的 llm.chat 直替函数记账。"""
    _check_surface(surface)

    def deco(fn):
        @functools.wraps(fn)
        def wrapper(messages: list[dict[str, str]], **kw):
            return chat_logged(messages, surface=surface, **kw)

        return wrapper

    return deco


def ledger_summary(
    surface: str | None = None, conn: sqlite3.Connection | None = None,
) -> dict:
    """台账聚合：总调用/ok/error 次数与 token 合计；surface=None 时全量
    并附 by_surface 分解，指定 surface 时只看该面。"""
    if surface is not None:
        _check_surface(surface)
    own = conn is None
    conn = _rows(conn or connect())
    try:
        conn.executescript(LEDGER_DDL)  # 注入连接同样幂等确保表在
        where, params = ("WHERE surface=?", [surface]) if surface else ("", [])
        rows = conn.execute(
            "SELECT surface, outcome, COUNT(*) AS n, COALESCE(SUM(prompt_tokens),0) AS p,"
            " COALESCE(SUM(completion_tokens),0) AS c FROM llm_ledger"
            f" {where} GROUP BY surface, outcome",
            params,
        ).fetchall()
        by_surface: dict[str, dict] = {}
        calls = ok = error = prompt = completion = 0
        for r in rows:
            s = by_surface.setdefault(
                r["surface"], {"calls": 0, "ok": 0, "error": 0,
                               "prompt_tokens": 0, "completion_tokens": 0},
            )
            s["calls"] += r["n"]
            s[r["outcome"]] += r["n"]
            s["prompt_tokens"] += r["p"]
            s["completion_tokens"] += r["c"]
            calls += r["n"]
            ok += r["n"] if r["outcome"] == "ok" else 0
            error += r["n"] if r["outcome"] == "error" else 0
            prompt += r["p"]
            completion += r["c"]
        for s in by_surface.values():
            s["total_tokens"] = s["prompt_tokens"] + s["completion_tokens"]
        return {
            "surface": surface,
            "calls": calls, "ok": ok, "error": error,
            "prompt_tokens": prompt, "completion_tokens": completion,
            "total_tokens": prompt + completion,
            "by_surface": by_surface,
        }
    finally:
        if own:
            conn.close()


def list_entries(
    conn: sqlite3.Connection | None = None, surface: str | None = None, limit: int = 100,
) -> list[dict]:
    """台账明细（审计用）：按时间倒序，可按 surface 过滤。"""
    if surface is not None:
        _check_surface(surface)
    own = conn is None
    conn = _rows(conn or connect())
    try:
        conn.executescript(LEDGER_DDL)  # 注入连接同样幂等确保表在
        where, params = ("WHERE surface=?", [surface]) if surface else ("", [])
        rows = conn.execute(
            f"SELECT * FROM llm_ledger {where} ORDER BY at DESC, id DESC LIMIT ?",
            [*params, limit],
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        if own:
            conn.close()


__all__ = [
    "SURFACES", "chat_logged", "ledgered", "ledger_summary", "list_entries",
    "record", "estimate_tokens", "connect",
]
