"""参照系只读 REST 消费面（task 5.2/5.4：学习地图坐标系 + 复述校验前提锚）。

参照系（fd-zhengxin-reference）的 HTTP 面有三块：/mcp（MCP 工具面，
reference_mcp 消费）、/api/v1/regions 与 /api/v1/concepts/{topic}/anchor
（个人层只读 REST 锚点面，本模块消费）。地址共用 FDZX__REFERENCE_BASE_URL
（该变量对 MCP 面是 streamable-http 端点，通常以 /mcp 结尾）——本模块经
service_root() 把 /mcp 后缀剥掉得到 REST 根，一个变量两副面孔不再冲突
（D3 修复；配服务根亦可，两种形态都认）。

物理分库铁律下本模块对参照系**只读**、降级友好：

- 未配置地址 → 明确返回 (None, "not_configured")，调用方走降级路径并标注；
- 不可达 / 非 2xx / 坏负载 → (None, "unreachable:<原因类型>")，绝不静默为空
  （照参照系「失败口径」纪律：空结果必附原因）；
- 解析保持宽容：认 regions / results / 裸列表，认 key_points / anchors /
  premises，条目可以是字符串或带 name/text 的对象——参照系 REST 面已按
  {regions:[{name}]} / {key_points:[str]} 形态落定，宽容层留给形态演进。

超时短（默认 5s）、无重试：个人层体验优先，参照系慢即降级。
"""

from __future__ import annotations

from typing import Any

from . import config


def service_root() -> str:
    """FDZX__REFERENCE_BASE_URL → REST 根（剥掉 MCP 端点的 /mcp 后缀）。

    - ``http://host:8031/mcp`` → ``http://host:8031``（MCP 端点形态，惯用）；
    - ``http://host:8031``     → 原样（已是服务根）。
    """
    base = config.reference_base_url().rstrip("/")
    if base.endswith("/mcp"):
        return base[: -len("/mcp")]
    return base


def _get_json(path: str, timeout: float = 5.0) -> Any:
    """GET {REST 根}{path} → JSON；未配置返回 None；任何失败抛异常（调用方归一）。"""
    base = service_root()
    if not base:
        return None
    import httpx  # 延迟导入：环境缺 httpx 时仅参照系消费不可用，降级不受影响

    resp = httpx.get(f"{base}{path}", timeout=timeout, headers={"accept": "application/json"})
    resp.raise_for_status()
    return resp.json()


def _as_list(data: Any, *keys: str) -> list[Any]:
    """从 {keys[0]: [...]} / {keys[1]: [...]} / 裸列表 里宽容取出条目列表。"""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in keys:
            v = data.get(k)
            if isinstance(v, list):
                return v
    return []


def _item_text(item: Any, *keys: str) -> str | None:
    if isinstance(item, str) and item.strip():
        return item.strip()
    if isinstance(item, dict):
        for k in keys:
            v = item.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return None


def fetch_region_taxonomy() -> tuple[list[str] | None, str | None]:
    """取参照系学派/思潮维度（学习地图的坐标系）。

    返回 (区域名列表, None) 或 (None, 原因)。原因枚举：
    not_configured / unreachable:<ExcType> / empty_taxonomy。
    """
    if not config.reference_base_url():
        return None, "not_configured"
    try:
        data = _get_json("/api/v1/regions")
    except Exception as exc:  # noqa: BLE001 —— 任何失败归一为不可达，带原因返回
        return None, f"unreachable:{type(exc).__name__}"
    names = [
        t for t in (
            _item_text(item, "name", "name_zh", "title", "id")
            for item in _as_list(data, "regions", "results", "items")
        ) if t
    ]
    if not names:
        return None, "empty_taxonomy"
    return names, None


def fetch_concept_anchor(topic: str) -> tuple[list[str] | None, str | None]:
    """取某概念/论证在参照系的关键前提锚（复述校验的前提集来源之一）。

    返回 (前提列表, None) 或 (None, 原因)。原因枚举：
    not_configured / unreachable:<ExcType> / no_anchor。
    """
    if not config.reference_base_url():
        return None, "not_configured"
    from urllib.parse import quote

    try:
        data = _get_json(f"/api/v1/concepts/{quote(topic)}/anchor")
    except Exception as exc:  # noqa: BLE001
        return None, f"unreachable:{type(exc).__name__}"
    premises = [
        t for t in (
            _item_text(item, "text", "premise", "name", "label")
            for item in _as_list(data, "key_points", "anchors", "premises", "results")
        ) if t
    ]
    if not premises:
        return None, "no_anchor"
    return premises, None
