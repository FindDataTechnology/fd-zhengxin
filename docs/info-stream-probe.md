# info-stream 只读查询面侦察报告（政心视角）

> 侦察时间：2026-10-23。侦察对象：`fd-info`（柏讯信息流索引，product_line=wire）。
> 方法：读代码 + 实跑测试套件 + kubectl 只读探测 + 本地起服实演冻结降级。
> 所有结论均基于下述实跑证据，非推测。本侦察未改动 fd-info 任何代码。

## 结论速览

| 项 | 状态 |
|---|---|
| 三接口 `list_since` / `get_item` / `search_info` | **已实现且有测试**（另有 `locate_evidence`、`stream_health`，共 5 工具） |
| 测试 | **35 passed**（0.14s，全离线）；`selfcheck_surface.py` 双向一致 |
| 部署 | **未上产**：集群无 `fd-prod` 命名空间、无 fd-info 服务；tasks 2.6（网关登记）、1.5.1（CronJob）、1.5.4（24h 验收）未勾 |
| 冻结降级实演 | **通过**：政治源不可用时 `stream_health` 带原因（11 部委逐条错误），`list_since` 空结果需交叉健康度才不误读；`search_info` 在 text-core 失联时**无原因字段**（缺口，见 §6） |

---

## 1. 三接口实现现状（代码位置）

实现双层：`InfoService`（服务层）+ FastMCP 工具注册（传输层）。政心消费的三个只读接口都在。

| 接口 | 服务层 | 传输层（MCP 工具） | 行为要点 |
|---|---|---|---|
| `list_since` | `fd-info/fd_info/storage.py:172`（`Store.list_since`）+ `fd_info/service.py:35` | `fd_info/server.py:34` | `(published_at DESC, item_id DESC)` 键集分页，游标为 base64(JSON `{p,i}`)；limit 夹在 1..500，缺省 50；返回 `{items, next_cursor, has_more}` |
| `get_item` | `fd_info/storage.py:166` + `fd_info/service.py:38` | `fd_info/server.py:51` | 返回索引行全量（含 `doc_id`、`body_status`、`url`）；不存在时返回 `{"error": "'条目不存在：<id>'"}`（HTTP 200、MCP `isError:false`，靠 payload 的 `error` 键区分） |
| `search_info` | `fd_info/service.py:54` | `fd_info/server.py:59` | 配了 `FD_TEXT_CORE_URL` 时转调 text-core 混合检索（`fd_info/textcore_search.py`，MCP streamable-http + Bearer），否则标题/摘要 LIKE 兜底；**必带 `mode` 字段**（`text-core` / `index`）声明走了哪条路 |

InfoItem 契约核对（政心关心的四点，全部落实在 `fd_info/models.py`）：

- **`item_id`** = `sha1(source:source_id)`（`models.py:28`），与标题/正文无关，公告修订不产生新条目；库层 `UNIQUE(source, source_id)` 兜底幂等（`storage.py:37`）。
- **`raw_hash`** = sha256(title+summary+body)（`models.py:58`），内容变更时 upsert 更新并盖 `updated_at`，同内容零副作用（`storage.py:73`）。
- **三态返回** = `ok/unavailable/partial`（`models.py:126` `SourceResult`）：`unavailable` 必带 `reason`，`partial` 必带 `reason+missing`，`ok` 必须显式 `window_checked=True`——空列表冒充 ok 会在构造时直接抛 `InfoError`。
- **证据 span** = `locate_evidence(doc_id, start, end)` → 逐字片段；正文缺失/区间越界返回结构化错误 `{"ok": false, "error": ...}`（`service.py:47`、`body_store.py:40`）。

fetcher 注册表（`fd_info/fetchers/__init__.py`）共 3 个：

| fetcher | source | source_type | 状态 |
|---|---|---|---|
| `CninfoSource`（巨潮公告） | `cninfo` | `announcement` | 默认启用 |
| `MinistrySource`（11 部委，动态导入 `fd_cn_gov`） | `cn-gov` | `ministry` | 默认启用；**政心的政治源就是它** |
| `InteractionSource`（SSE 互动已实装、SZSE 如实报不可用） | `sse-interaction` | `interaction` | 非默认（`DEFAULT_ENABLED` 只有前两个），tasks 1.4.1/1.4.2 未勾 |

`tool-surface.json`：5 工具全部声明 `readOnlyHint: true`，query/search/evidence/health 四组，`expected_tool_count: 5`。

## 2. 测试结论（实跑）

仓内 venv（`.venv`，Python 3.12.13）直接可跑，无需另装依赖：

```
$ cd /Users/chengsishi/finddata/fd-info && .venv/bin/python -m pytest -q
...................................                                      [100%]
35 passed in 0.14s
```

- 7 个测试文件（models / storage+pipeline / search / body_store / body_versioning / withdrawal / fetchers），全离线，fetcher 测试注入固定响应不打网。
- `scripts/selfcheck_surface.py`：`OK: 5 tools 双向一致`（exit 0），即 tool-surface.json 声明与 server.py 注册双向吻合（tasks 2.5 的验收口径）。
- 注意：`.venv` 未装 `fastmcp`（它是可选 `[server]` extra）。`server.py` 传输层标了 `# pragma: no cover`，pytest 不覆盖 MCP 装载——传输层正确性由本次本地起服实演补证（§4 实际跑通了 5 工具）。

## 3. 部署状态（实探，非猜测）

**集群里没有 fd-info。**

- kubectl 可用（context `tencent`，真实集群：argocd/fd-web/mcp/librechat 等 ns 均在）。`kubectl get svc -A` 全量输出中无任何 info 相关服务；`kubectl get ns` 无 `fd-prod` 命名空间；`kubectl get deploy,cronjob -n fd-prod` → `No resources found`。
- 部署物**已备好未应用**：`fd-info/k8s/deployment.yaml`（Deployment + 5Gi PVC + **Service 类型 NodePort，nodePort 31897**，ns fd-prod，镜像占位 `REGISTRY/finddata/fd-info:0.1.0` 待替换）与 `k8s/cronjob-fetch.yaml`（`*/5 * * * *` 拉取）。健康探针走 18081 端口 `/health`，验收口径 `{"status":"ok","tools":5}`（本次本地实演已复现该返回）。
- tasks 勾选状态（`openspec/changes/wire-info-stream-program/tasks.md`）与实探吻合：
  - 查询面 **2.1–2.5 全部 [x]**（含 tool-surface 落地与 selfcheck）；
  - **2.6 网关登记 [ ]**、**1.5.1 CronJob 上线 [ ]**、**1.5.4 24h 新鲜度 [ ]**、阶段 1.1（text-core news 垂域）与阶段 3/5 未做。
- 判断：**代码与测试就绪、部署与网关未走**，即「能力在库、未上产」。政心当前没有任何可指向的线上端点（内网 NodePort 31897 也未开）。

## 4. 冻结降级实演记录（本地实跑）

目的：政心脉搏环在上游不可用时必须**冻结降级并声明原因**，验证 fd-info 查询面在「无政治源」场景下是「带原因的空」还是「静默空」。

**环境**：隔离 venv `/tmp/fdzx-probe/venv`（从 fd-info 的 /tmp 副本安装 `.[server]`，fastmcp 3.4.11；原仓零改动）。原仓 `.venv` 无 fastmcp，无法直接起服，故按隔离方式处理。

**播种**（进程内，与 CLI/工具同一 `InfoService`）：
1. 摄入 2 条 announcement 条目（cninfo，离线构造）→ `created: 2`。
2. **真实调用** `MinistrySource().fetch("2026-10-20","2026-10-23")` —— 环境未装 `fd_cn_gov`，fetcher 如实返回：

```json
{"source": "cn-gov", "status": "unavailable",
 "reason": "mee_gsgg_archive: No module named 'fd_cn_gov'; mem_tzgg_archive: No module named 'fd_cn_gov'; …（11 部委逐条列出）…",
 "items": 0, "window_checked": false}
```

3. 摄入该结果（失败写 `source_state`，不抛异常）。

**起服**：`FD_INFO_DB=/tmp/fdzx-probe/fd_info.db FD_TEXT_CORE_URL=http://127.0.0.1:59999/mcp（故意死端口） python -m fd_info.server --http 18080`。
探针 `GET :18081/health` → `{"status": "ok", "tools": 5}`（与 k8s README 验收口径一致）。

**MCP streamable-http 实调**（initialize 成功，`tools/list` = `[list_since, get_item, search_info, locate_evidence, stream_health]`），返回原文：

```
[list_since {"source_types": ["ministry"], "limit": 10}]
{"items": [], "next_cursor": null, "has_more": false}          ← 本体是静默空

[stream_health {}]
{"sources": [
  {"source": "cn-gov", "status": "unavailable", "watermark_published": null,
   "watermark_item_id": null, "last_success_at": null,
   "last_error": "mee_gsgg_archive: No module named 'fd_cn_gov'; …（11 条原文）",
   "consecutive_failures": 1, "lag_p95_seconds": null},
  {"source": "cninfo", "status": "ok", "watermark_published": "2026-10-22T00:00:00Z",
   "watermark_item_id": "a00d589e…", "last_success_at": "…", "consecutive_failures": 0,
   "lag_p95_seconds": null}]}

[search_info {"query": "政策", "source_types": ["ministry"]}]   ← text-core 死端口
{"items": [], "count": 0, "mode": "text-core"}

[get_item {"item_id": "a00d589e2060d404a10b1596bbdbd50a67c4061c"}]   ← 存在
{"item_id": "a00d589e…", "source": "cninfo", "source_type": "announcement",
 "published_at": "2026-10-22T00:00:00Z", "title": "某公司关于子公司中标政府项目的公告",
 "summary": "公司中标市政工程，金额 3.2 亿元。", "raw_hash": "3cebfcea…", "doc_id": null,
 "body_status": "failed", "withdrawn": false, …}

[get_item {"item_id": "deadbeef"}]                              ← 不存在
{"error": "'条目不存在：deadbeef'"}
```

**对照服 B**（同库，不设 `FD_TEXT_CORE_URL`，索引检索）：

```
[search_info {"query": "中标"}]        → {"items": [（中标公告那条）], "count": 1, "mode": "index"}
[list_since {"limit": 1}]              → {…, "next_cursor": "eyJwIjoiMjAyNi0xMC0yMi…", "has_more": true}
[locate_evidence {"doc_id": "cninfo:1228987", "start": 0, "end": 5}]
                                       → {"ok": false, "error": "正文不存在：cninfo:1228987", "doc_id": "cninfo:1228987"}
```

**结论（冻结降级实演）**：

1. **通过的部分**：政治源不可用时，原因链条完整可取——fetcher 三态 `unavailable(reason)` → 摄取失败计数 → `stream_health.last_error` 原文可见。政心按 design D7「引用前先查健康度」交叉 `stream_health`，即可把「空列表」正确解读为「源不可用，原因是 fd_cn_gov 缺失/上游故障」，冻结脉搏环并展示 `last_error`，而非静默空转。
2. **缺口（重要）**：`list_since` 的空结果本体**不带原因**（`{items:[], next_cursor:null, has_more:false}`），单看会误读为「确实无信息」；必须配 `stream_health`。这与 tool-surface.json 的 D7 纪律一致（健康度先行），政心侧要把它作为硬性消费顺序。
3. **缺口（重要）**：`search_info` 在 text-core 失联时返回 `{"items":[],"count":0,"mode":"text-core"}` —— **没有降级原因字段，mode 也不回退**；同一查询在索引模式下明明有 1 条命中（`中标` 对照）。这与 tool-surface.json 宣称的「text-core 失联时结构化降级（空结果带原因），不冒充无命中」**不符**（`textcore_search.py:40` 捕获异常后静默返回 `[]`，`service.py:63-81` 未区分）。政心不能把 `mode:"text-core" + count:0` 当作真无命中。

## 5. 政心侧对接建议

**消费方式**

- 形态：MCP streamable-http。就绪后的入口是集群 NodePort `http://<node>:31897/mcp`（Bearer 网关层待 2.6 登记）；政心现在**只能本地/内网联调，不能当线上依赖**。
- 硬性顺序（D7）：**先 `stream_health` 再取数**。政治源 = `source_type:"ministry"`（source 名 `cn-gov`）。脉搏环每次刷新：`stream_health` 里对应源 `status != "ok"` 或 `consecutive_failures > 0` → 冻结环（不清空已有快照，标注滞后+`last_error` 原因）；`ok` 但 `lag_p95_seconds` 超阈值（建议 >600s，k8s README 同口径）→ 照常渲染但标注「信息可能不完整」。
- 取数：`list_since(source_types=["ministry"], published_after=<游标起点>)`，翻页只信 `next_cursor`（键集分页，插入不重不漏），不要用 OFFSET 语义；详情按 `item_id` 走 `get_item`；`item_id` 稳定可作政心侧去重键与外键，修订看 `raw_hash`/`updated_at`。
- 检索：`search_info` 必须读 `mode`。`mode:"index"` 只是标题/摘要关键字匹配（弱信号，不可当语义结论）；`mode:"text-core"` 且 `count:0` 时按上文学到的缺口处理——连续 N 次（建议 3）空结果先探 `stream_health` 或独立探活 text-core，再判「真无命中」。
- 证据：政心断言引用信息时，`body_status != "stored"` 或无 `doc_id` 的条目**不产 evidence 块**；有 `doc_id` 也必须经 `locate_evidence` 复核逐字片段（返回 `ok:false` 即放弃该证据，不得手抄 title 当 quote）。

**超时**

- 服务自身无外呼（text-core 除外，内部 httpx 30s）。政心侧预算建议：`stream_health` 2s、`list_since`/`get_item` 5s、`search_info` 10s（底层 text-core 30s 会吃满，政心先掐）；连接级 3s。健康探针端口（+1 端口 `/health`）可作 TCP 级存活判断，但工具可用性以 `tools/list` 为准。

**重试**

- 三接口皆幂等只读（`idempotentHint: true`），重试无副作用：建议指数退避 3 次（1s/4s/16s），重试后仍失败 → 进入冻结降级（保留快照 + 声明原因），**不要**退化为「空结果照常渲染」。
- 恢复信号以 `stream_health` 的 `status` 回 `ok` 且 `consecutive_failures` 归零为准，避免闪断抖动；冻结期间降频探测（如 60s 一次），不要每环都打。
- `get_item` 不存在不是错误（payload 带 `error` 键），不要重试；`list_since` 游标非法会抛 `游标不合法`（HTTP 500 侧），重置游标重拉而不是重试同一游标。

**给 wire-info-stream-program 的两条反馈（政心侧阻塞点，非本侦察改动）**

1. `search_info` text-core 失联无降级原因字段，与 tool-surface.json 声明不符——建议在 change 内补（如 `degraded_reason` 或失败时回退 `mode:"index"` 并标注）。
2. 网关登记（2.6）与 CronJob 上线（1.5.1）完成前，政心的政治源链路没有生产可用性；政心上线计划应与之对齐或自备冻结态。

---

## 附：证据清单

| 证据 | 来源 |
|---|---|
| 35 passed | `cd fd-info && .venv/bin/python -m pytest -q`（2026-10-23 实跑） |
| 5 工具双向一致 | `fd-info/.venv/bin/python scripts/selfcheck_surface.py` → `OK: 5 tools 双向一致` |
| 无集群部署 | `kubectl get svc -A`（无 info）、`kubectl get ns`（无 fd-prod）、`kubectl get deploy,cronjob -n fd-prod` → No resources |
| /health 原文 | 本地起服 `curl :18081/health` → `{"status": "ok", "tools": 5}` |
| 三态带原因 | `MinistrySource().fetch` 实调返回 unavailable + 11 部委原因原文 |
| MCP 调用原文 | §4 各 `[...]` 块（streamable-http，initialize + tools/call） |
| 代码位置 | 见 §1 表格（`fd_info/{service,server,storage,models,textcore_search,body_store}.py`、`fd_info/fetchers/*`、`k8s/*.yaml`、`tool-surface.json`） |
