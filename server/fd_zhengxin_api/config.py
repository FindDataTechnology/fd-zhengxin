"""配置读取（骨架期）：环境变量统一入口，前缀 FDZX__。

照 fd-zhesi 的环境变量习惯（各处直接读环境变量、测试用 monkeypatch
即时生效），本仓集中到 config.py 一个入口：读取即时、不缓存。

命名对照（fd-zhesi → fd-zhengxin）：
- FD_ZHESI_DB                  → FDZX__DB（个人层库路径）
- FD_ZHESI_AUTH_DEV_TOKENS     → FDZX__AUTH_DEV_TOKENS
- FD_ZHESI_AUTH_JWKS/AUDIENCE  → FDZX__AUTH_JWKS / FDZX__AUTH_AUDIENCE
- FD_ZHESI_LLM_API_KEY 等      → FDZX__LLM_API_KEY / FDZX__LLM_BASE_URL / FDZX__LLM_MODEL
- （本仓新增）参照系服务地址    → FDZX__REFERENCE_BASE_URL——参照系与个人层
  物理分库（照 fd-zhesi / fd-zhesi-reference 先例），地址只在此留位，
  实现与数据都在参照系仓，不入本仓。
"""

from __future__ import annotations

import os
from pathlib import Path

PREFIX = "FDZX__"


def env(name: str, default: str = "") -> str:
    """读 FDZX__<name>；即时读取不缓存（monkeypatch 测试可见）。"""
    return os.environ.get(PREFIX + name, default)


# ---- 个人层库 ----

def db_path() -> str:
    """个人层自有库路径（默认 ~/.fd-zhengxin/personal.db）。"""
    return env("DB") or str(Path.home() / ".fd-zhengxin" / "personal.db")


# ---- 访问控制 ----

def auth_dev_tokens() -> str:
    """开发态固定令牌："token1=user_a,token2=user_b"（本地联调/测试；生产不设）。"""
    return env("AUTH_DEV_TOKENS")


def auth_jwks() -> str:
    """JWKS 端点（如 Logto）；设了才启用 JWT 校验。"""
    return env("AUTH_JWKS")


def auth_audience() -> str:
    return env("AUTH_AUDIENCE")


# ---- LLM 网关 ----

LLM_DEFAULT_BASE_URL = "https://token.finddatatech.cloud/v1"
LLM_DEFAULT_MODEL = "gpt-5.4-mini"


def llm_api_key() -> str:
    return env("LLM_API_KEY").strip()


def llm_base_url() -> str:
    return env("LLM_BASE_URL") or LLM_DEFAULT_BASE_URL


def llm_model() -> str:
    return env("LLM_MODEL") or LLM_DEFAULT_MODEL


# ---- 参照系服务（分库留位） ----

def reference_base_url() -> str:
    """参照系服务地址（独立部署，如政治思想参照系 MCP/HTTP 服务）。

    骨架期仅配置留位；未设时调用方不得访问参照系（照「缺口如实可见」）。
    """
    return env("REFERENCE_BASE_URL")


# ---- info-stream（fd-info 只读查询面，任务 4.5 脉搏环上游） ----


def infostream_base_url() -> str:
    """fd-info（柏讯信息流索引，MCP streamable-http）服务地址。

    fd-info 未上产（端点可配置）：未设时 infostream_client 一律抛结构化
    InfoStreamUnavailable(not_configured)，绝不静默返回空结果。
    """
    return env("INFOSTREAM_BASE_URL")


# ---- 脉搏环 / LLM 台账（产品运营层自有库，不混入个人层库） ----


def pulse_db_path() -> str:
    """脉搏环库路径（默认 ~/.fd-zhengxin/pulse.db）。

    议题立场光谱是从 info-stream 可重建的缓存快照，属产品运营层，
    物理上不与个人层理解记录（config.db_path）混库。
    """
    return env("PULSE_DB") or str(Path.home() / ".fd-zhengxin" / "pulse.db")


def ledger_db_path() -> str:
    """LLM 成本台账库路径（默认 ~/.fd-zhengxin/llm_ledger.db）。"""
    return env("LEDGER_DB") or str(Path.home() / ".fd-zhengxin" / "llm_ledger.db")


# ---- 口径守卫 ----

def guard_overlay_path() -> str:
    """客户侧规则叠加文件路径（FDZX__GUARD_OVERLAY，私有化部署自带）。

    overlay 只能新增规则或收紧 severity（grey→block），放宽/删规则会被
    guard_overlay.load_ruleset 拒绝；未设即无叠加，用基线规则库 v1。
    """
    return env("GUARD_OVERLAY")
