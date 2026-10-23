"""LLM 成本台账测试（任务 4.6）：多 surface 记账 + 聚合 + 无 KEY 不记账不崩。"""

from __future__ import annotations

import sqlite3

import pytest

from fd_zhengxin_api import llm, llm_ledger


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / "ledger.db")
    try:
        yield c
    finally:
        c.close()


def _fake_llm(monkeypatch, content: str, usage: dict | None = None) -> None:
    """桩 chat_with_usage（ledger 实际走的入口）：无 usage → 估算口径。"""
    monkeypatch.setattr(llm, "is_configured", lambda: True)
    monkeypatch.setattr(
        llm, "chat_with_usage",
        lambda messages, **kw: {"content": content, "usage": usage},
    )


# ---- surface 清单与校验 ----


def test_surface_catalog() -> None:
    assert set(llm_ledger.SURFACES) == {
        "locate", "perspectives", "genealogy", "pulse",
        "guard-grey", "personal-verify",
    }


def test_unknown_surface_rejected(conn) -> None:
    with pytest.raises(ValueError):
        llm_ledger.chat_logged([{"role": "user", "content": "x"}], surface="bogus", conn=conn)
    with pytest.raises(ValueError):
        llm_ledger.ledger_summary(surface="bogus", conn=conn)
    assert llm_ledger.ledger_summary(conn=conn)["calls"] == 0  # 非法面不入账


# ---- 记账与聚合 ----


def test_multi_surface_recorded_and_summarized(monkeypatch, conn) -> None:
    _fake_llm(monkeypatch, "答复文本")
    llm_ledger.chat_logged(
        [{"role": "user", "content": "找出视角"}], surface="perspectives", conn=conn,
    )
    llm_ledger.chat_logged(
        [{"role": "user", "content": "谱系梳理"}], surface="genealogy", conn=conn,
    )
    llm_ledger.chat_logged(
        [{"role": "user", "content": "立场光谱抽取"}], surface="pulse", conn=conn,
    )

    summary = llm_ledger.ledger_summary(conn=conn)
    assert summary["calls"] == 3 and summary["ok"] == 3 and summary["error"] == 0
    assert set(summary["by_surface"]) == {"perspectives", "genealogy", "pulse"}
    for s in summary["by_surface"].values():
        assert s["calls"] == 1 and s["ok"] == 1
        assert s["total_tokens"] == s["prompt_tokens"] + s["completion_tokens"]
    assert summary["prompt_tokens"] > 0 and summary["completion_tokens"] > 0
    assert summary["total_tokens"] == summary["prompt_tokens"] + summary["completion_tokens"]

    only = llm_ledger.ledger_summary(surface="pulse", conn=conn)
    assert only["calls"] == 1 and only["by_surface"] == {
        "pulse": summary["by_surface"]["pulse"],
    }

    rows = llm_ledger.list_entries(conn=conn)
    assert len(rows) == 3
    assert all(r["usage_method"] == "estimated" for r in rows)  # 不冒充实测
    assert all(r["model"] and r["at"] for r in rows)


def test_chat_logged_records_model_param(monkeypatch, conn) -> None:
    _fake_llm(monkeypatch, "ok")
    llm_ledger.chat_logged(
        [{"role": "user", "content": "q"}], surface="locate",
        model="custom-model-x", conn=conn,
    )
    rows = llm_ledger.list_entries(conn=conn)
    assert rows[0]["model"] == "custom-model-x"
    assert rows[0]["outcome"] == "ok"


# ---- 失败路径：记账并上抛 ----


def test_gateway_usage_recorded_as_usage(monkeypatch, conn) -> None:
    """4.6 对账口径：网关回报 usage 时台账记实测数且 usage_method="usage"。"""
    _fake_llm(
        monkeypatch, "答复文本",
        usage={"prompt_tokens": 12, "completion_tokens": 34, "total_tokens": 46},
    )
    llm_ledger.chat_logged(
        [{"role": "user", "content": "这段活很长，估算值会跟实测差很远"}],
        surface="locate", conn=conn,
    )
    row = llm_ledger.list_entries(conn=conn)[0]
    assert row["usage_method"] == "usage"
    assert row["prompt_tokens"] == 12 and row["completion_tokens"] == 34
    summary = llm_ledger.ledger_summary(surface="locate", conn=conn)
    assert summary["total_tokens"] == 46


# ---- 失败路径：记账并上抛 ----


def test_chat_logged_records_error_and_reraises(monkeypatch, conn) -> None:
    monkeypatch.setattr(llm, "is_configured", lambda: True)

    def _boom(messages, **kw):
        raise llm.LLMError("LLM 网关调用失败: ConnectError")

    monkeypatch.setattr(llm, "chat_with_usage", _boom)
    with pytest.raises(llm.LLMError):
        llm_ledger.chat_logged(
            [{"role": "user", "content": "灰区复核"}], surface="guard-grey", conn=conn,
        )
    summary = llm_ledger.ledger_summary(surface="guard-grey", conn=conn)
    assert summary["calls"] == 1 and summary["error"] == 1 and summary["ok"] == 0
    assert summary["prompt_tokens"] > 0      # 失败调用也花了 prompt token
    assert summary["completion_tokens"] == 0
    row = llm_ledger.list_entries(conn=conn)[0]
    assert row["outcome"] == "error" and "网关调用失败" in row["detail"]


# ---- 无 KEY 路径：不记账不崩 ----


def test_no_key_path_records_nothing_and_does_not_crash(monkeypatch, conn) -> None:
    monkeypatch.delenv("FDZX__LLM_API_KEY", raising=False)
    assert llm.is_configured() is False
    out = llm_ledger.chat_logged(
        [{"role": "user", "content": "定位概念"}], surface="locate", conn=conn,
    )
    assert out is None                       # llm.chat 契约：无 KEY → None
    assert llm_ledger.ledger_summary(conn=conn)["calls"] == 0  # 无调用无成本不入账
    assert llm_ledger.list_entries(conn=conn) == []


# ---- 装饰器模式 ----


def test_ledgered_decorator(monkeypatch, conn) -> None:
    _fake_llm(monkeypatch, "个人验证答复")

    @llm_ledger.ledgered("personal-verify")
    def ask(messages, **kw):
        return llm.chat(messages, **kw)  # pragma: no cover —— 装饰后走记账直替

    out = ask([{"role": "user", "content": "验证这条理解"}], conn=conn)
    assert out == "个人验证答复"
    assert llm_ledger.ledger_summary(surface="personal-verify", conn=conn)["calls"] == 1


# ---- token 估算（确定性） ----


def test_estimate_tokens_deterministic() -> None:
    assert llm_ledger.estimate_tokens(None) == 0
    assert llm_ledger.estimate_tokens("") == 0
    assert llm_ledger.estimate_tokens("立场光谱证据抽取") == 8   # 8 个 CJK ≈ 8 token
    assert llm_ledger.estimate_tokens("abcdefgh") == 2           # 8 个 ASCII ≈ 2 token
    mixed = llm_ledger.estimate_tokens("立场光谱 spectrum")
    assert mixed == 4 + 2  # 4 CJK + ceil(8/4)
