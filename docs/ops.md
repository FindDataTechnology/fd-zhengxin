# 政心运维手册

范围：fd-zhengxin 产品服务（`server/fd_zhengxin_api`）及其外部依赖（fd-zhengxin-reference 参照系、fd-info 柏讯信息流）的运维口径。事实以代码为准（`config.py` / `guard_rules.py` / `guard_overlay.py` / `guard_llm.py` / `llm_ledger.py` / `pulse.py` / `infostream_client.py`；参照系侧 `license.py` / `tools_registry.py` / `snapshot.py`），本文只给状态口径与运维动作，不复制全部规则细节。

## 1. 六源清单与状态口径

参照系 `source_status` 工具的台账是六源（四外部源 + 两 curated 层）。状态口径与运维含义：

| 源 | 状态口径 | 运维含义 |
|---|---|---|
| v-dem | **已过门**（CC BY-SA 4.0，非 CC BY；证据 `fd-zhengxin-reference/docs/data-license.md` §1，2026-10-23 官网一手核验） | 人工下载官方 CSV（表单式下载、免账号）后 `ingest_vdem` 落库；摄取限定 **Core / Country-Date 纯变体**（Full+Others 捆绑第三方列须单独核验）。ShareAlike 只在**导出/下载面**触发：政心查询面合规，若产品加数据导出功能，导出面构成分发、须同许可并附署名（含 DOI 的官方建议引用格式，Suggested Citations 随快照原样保留） |
| marpor | **关门**（`license_unconfirmed`：Terms of Use 明文再分发默认禁止、须 WZB 书面授权；三点未确认——主数据集下载是否强制注册 / 产品语境是否满足 scientific research / 查询导出面是否构成 Redistribution） | 不摄取、不查询：`ideology_query` 的 marpor 分项与 `regime_timeseries` 的并轨源一律报 `not_ingested`（台账另带 `gate_status=license_unconfirmed`，两层口径不混用）。真摄取逻辑已就位但被门封住，registry 翻 confirmed 前不可达。**授权联系：manifesto-communication@wzb.eu**（WZB 官方联系邮箱，见 data-license.md §2 未确认清单） |
| wikidata | 已过门（CC0） | 政治面摄取器已就位（四类实体查询姿势 + 极性判据表，探针样本 36 份）；快照 dataset 名为 `wikidata-politics`（与源名 wikidata 显式映射防漂移）。「批判」极性档在 Wikidata 结构性缺失（P3323 全库仅 4 条），批判边由 SEP 概念层与中国线 curated 层承载 |
| sep | 已过门（站点条款，**只链不抄**） | on-demand 缓存层，无批量快照：`concept_lookup` 调用时自动建缓存（只存标题/短引用/entries 链接，模块内无正文抓取代码路径）；覆盖面实测 26/26 命中（`docs/sep-coverage.md`） |
| china-seed | **curated**（先秦诸子种子谱系，编辑产出的结构化数据） | 不走许可门。`create_server` 启动钩子幂等加载（DB 为 `:memory:` 时跳过；seed 文件缺失跳过并如实回报，缺口在 `source_status` 可见）。种子不造数值坐标（coordinates 恒 NULL）；结构契约自检 `python3 scripts/check_seed.py`（粒度：学派 8 / 人物 40 / 概念 50 / 边 83，2026-10-23 版本 1.0.0） |
| modern-index | **curated**（近现代五思潮文献索引，`docs/china-modern-sources.md` 固化） | 不走许可门。启动钩子加载，以不可变快照入库；只存条目元数据与链接（维基文库 / Commons 国图 NLC 扫描），不存正文 |

另有第七个许可门条目 `china-line`（中国线**语料**层，状态 pending：源清单待定，未过门不摄取）——它管语料全文层，与上表两个 curated 层是两回事，也不在六源台账之列。

两层口径不混用（易误读处）：查询失败口径是 `errors.py` taxonomy（`blocked` / `upstream` / `unreachable` / `not_ingested`），台账缺口口径是 `license_unconfirmed` 等——MARPOR 门关时查询报 `not_ingested`、台账行另带 `gate_status` 与评审待办；`source_status` 还附最近一次可得性探测（probe）与在用快照版本，让「源挂了」与「解析器坏了」在台账上可分。

## 2. 快照与版本化机制

- 每条参照系查询结果带 provenance（source / stable_id / as_of / attribution / snapshot_version）。静态数据集的 `snapshot_version` 回到具体快照 id，使「同一查询在不同时间给出不同结果」可由快照版本解释；动态/按需层（SEP 缓存）该字段为 None。
- 快照存储**内容寻址、不可变**：路径 `<root>/<dataset>/<sha256>/<原文件名>`，root 由 `FDZX_SNAPSHOT_DIR` 控制（默认 `~/.fd-zhengxin/snapshots`）。同内容重复写入复用同一路径不覆盖（写一次即冻结）；同路径内容与清单 sha256 不符视为损坏，抛 `SnapshotCorrupt`——绝不"修好"它，人工用原始下载件重建。
- 快照 id 形如 `<dataset>@<version>#<sha256 前 12 位>`；清单字段 version / fetched_at / source_url / sha256 / attribution / probe_status 另存库（snapshots 表）。
- Wikidata 无官方版本号：version = 摄取时间(UTC) + 查询指纹（种子集+限额+四类查询文本的 sha256 前 12 位）；原始 SPARQL 响应整体存快照（CC0 归属），wd_* 三表是「当前快照」视图（新快照落库后清旧行，全量刷新语义）。
- 参照系库（`FDZX_REF_DB`，默认 `~/.fd-zhengxin/reference.db`）整体是**可重建缓存**：库损坏可删，重放摄取脚本 + 启动钩子（curated 层）即恢复；个人层库（personal.db）不是缓存，永不参与这套重建。

## 3. 守卫规则库 v1 与客户侧 overlay

基线规则库（`guard_rules.py`，`RULESET_VERSION = "v1"`）：8 条确定性规则、五类口径。正则规则扫产出 body，结构化规则检查 claims/views/is_contemporary：

| rule_id | 类别 | 检查面 | severity |
|---|---|---|---|
| R-PREDICT-001 | prediction | 结果预言句式（将会赢得 / 预测…当选 / 必然走向 / X 年内会） | block |
| R-PREDICT-002 | prediction | 倾向性概率断言（大概率会 / 注定会 / almost certainly） | grey |
| R-RECO-001 | recommendation | 立场推荐句式（应该支持 / 更优的立场是 / 明智的选择是） | block |
| R-RECO-002 | recommendation | 立场优劣评价句式（更值得认同 / 显然更有道理） | grey |
| R-ADVO-001 | advocacy | 行动号召句式（我们应当 / 必须反对 / 捍卫X） | block |
| R-ADVO-002 | advocacy | 动员式号召句式（共同抵制 / join the movement） | grey |
| R-BARE-001 | bare-assertion | claims 强断言缺 source 引用（结构化检查） | block |
| R-BAL-001 | unbalanced | 当代议题 views 未覆盖 ≥2 个对立学派视角（结构化检查） | block |

版本纪律：规则**只增不改**——同 rule_id 的行为变更必须升 `RULESET_VERSION` 并在变更说明留痕（口径审计要可回放，裁决的 ruleset_version 随每次拦截带回）。动作只有 block / grey；多条命中取最严并聚合全部 Reason；放行（action=None）不是动作。

**客户侧 overlay**（`FDZX__GUARD_OVERLAY`，私有化部署自带的 JSON 规则文件，`guard_overlay.load_ruleset` 与基线组合成生效规则集）。只允许两种操作，其余一律抛 `OverlayError` 拒绝**整份**加载（宁可整份不叠加，也不放宽一格）：

- 允许：新增确定性正则规则（severity 起步 block 或 grey 均可，新增不是放宽）；把已有 grey 规则收紧为 block（同 severity 幂等重放允许）。
- 拒绝：block 降 grey（放宽）；删规则（remove/deleted）；改写已有规则的 pattern/category；坏格式（缺 rule_id / severity 拼错 / 新增缺 pattern 或 category / pattern 非法正则 / 文件不存在或非合法 JSON）。

文件格式：

```json
{"rules": [
    {"rule_id": "R-PREDICT-002", "severity": "block"},
    {"rule_id": "R-CUSTOM-001", "category": "prediction",
     "pattern": "客户自留风险词", "severity": "grey"}
]}
```

未设 `FDZX__GUARD_OVERLAY` 即无叠加，用基线规则库 v1。

灰区判别器（`guard_llm.classify_grey`）：LLM 只给人工分诊**参考意见**（risky / clean / unavailable），**动作恒为 GREY、没有放行权**——KEY 缺失、调用失败、超时、不可解析一律按 grey。标灰落 `grey_queue` 表（pending），人工分诊后进规则库版本（新口径先人工进 `guard_rules`，不在判别器里私设规则），处理后置 resolved。

## 4. LLM 成本台账

库：独立 sqlite（`FDZX__LEDGER_DB`，默认 `~/.fd-zhengxin/llm_ledger.db`），运营层账目不混个人层库。每次真实网关调用记一行：surface / model / prompt_tokens / completion_tokens / usage_method / outcome（ok | error）/ detail / at。`outcome=error` 的行也记账（失败的调用同样花预算）。

token 口径（如实）：llm 网关只返回补全文本不暴露 usage，token 用确定性启发式估算（CJK 1 字 ≈ 1 token，其余 ≈ 4 字符/token），`usage_method` 恒为 `estimated`，不冒充实测值。无 KEY 路径（`llm.chat` 直接返回 None、没有发生调用）不记账不崩。

**六 surface**（成本归属面，枚举封闭，归属错了比漏记更糟）：

| surface | 归属 |
|---|---|
| locate | 定位闭环的 LLM 重排 |
| perspectives | 多视角闭环的 steel-man 构造 |
| genealogy | 谱系闭环的环重排 |
| pulse | 脉搏环的立场证据抽取增强 |
| guard-grey | 守卫灰区判别器 |
| personal-verify | 理解卡复述校验的 LLM 同义判别 |

接线状态（如实记录）：locate / perspectives / genealogy / pulse 四面已走 `llm_ledger.chat_logged` 记账；**personal-verify（cards_api 的复述覆盖判别）与 guard-grey（灰区判别器）当前直调 `llm.chat`、尚未过台账**——枚举已为两面留位，接线前这两面的成本不在账上。

查询（Python 直调，暂无 CLI）：

```python
from fd_zhengxin_api import llm_ledger
llm_ledger.ledger_summary()                            # 全量聚合 + by_surface 分解
llm_ledger.ledger_summary(surface="pulse")             # 单面聚合
llm_ledger.list_entries(surface="locate", limit=100)   # 明细，时间倒序（审计用）
```

## 5. 脉搏环冻结/解冻语义

- **消费顺序（D7 纪律，硬性）**：先 `stream_health`（政治源 = `source_type:"ministry"`，源名 `cn-gov`），再 `list_since` 增量拉取。`list_since` 空结果本体不带原因，单看会误读为「确实无信息」，必须交叉健康度。
- **冻结**（`pulse.freeze`，ingest 内置）：只写 `frozen_reason`（JSON `{code, detail}`）与 `updated_at`；**光谱快照、键集游标、last_updated_at 原样保留**——数据停在最后一次成功更新，查询面（`GET /api/v1/pulse/spectrum/{issue}`）照常可读旧光谱与冻结原因。
- 冻结原因 code 取值：`not_configured`（`FDZX__INFOSTREAM_BASE_URL` 未设，fd-info 未上产）/ `unreachable` / `upstream_error` / `protocol_error`（客户端短退避 ×2 重试后仍失败的结构化归类）/ `source_unavailable`（健康度显示政治源不 ok，detail 带上游 last_error 原文）。
- **解冻**：无手工解冻动作——上游恢复后下一次 `ingest_spectrum_update` 自动续拉（游标持久化，从断点继续、不重拉不漏），成功后 `frozen_reason` 置 NULL。恢复判定以 `stream_health` 的 `status` 回 `ok` 且连续失败归零为准，避免闪断抖动；建议冻结期间降频探测（如 60s 一次），不要每环都打。
- **运行方式（现状）**：`ingest_spectrum_update` 是库函数，当前无 HTTP 路由、无 CronJob——由运维/调度侧按议题驱动调用；每次调用翻页上限 50 页防失控循环。
- 口径铁律：缺证据的强断言不产出（有断言但拿不到逐字校验通过的证据 → 该立场不进光谱，计入 `dropped_no_evidence`）；证据写校验 `quote ≡ body[start:end]`（LLM 给的 span 同样过校验，不过即丢）；同 item 修订（raw_hash 变）先撤旧证据再补新；光谱产物过 `guard_output`（当代议题单立场光谱会被 R-BAL-001 判 block，查询面带裁决返回）。
- 已知上游缺口（`docs/info-stream-probe.md` §4）：fd-info 的 `search_info` 在 text-core 失联时返回空结果**无降级原因字段**——政心侧不把 `mode:"text-core"` + `count:0` 当真无命中；网关登记与 CronJob 上线（fd-info tasks 2.6 / 1.5.1）完成前，政治源链路没有生产可用性，上线计划须与之对齐或自备冻结态。

## 6. 私有化部署 env 全清单（FDZX__ 前缀，逐项）

读取全部即时、不缓存，单一入口 `server/fd_zhengxin_api/config.py`：

| 变量 | 默认 | 用途 |
|---|---|---|
| `FDZX__DB` | `~/.fd-zhengxin/personal.db` | 个人层库：理解卡 / 学习地图 / 理解年轮 / 审计。用户理解记录，独立分库，不参与任何重建 |
| `FDZX__AUTH_DEV_TOKENS` | （无） | 开发态固定令牌，`token1=user_a,token2=user_b`。本地联调/测试用；**生产不设** |
| `FDZX__AUTH_JWKS` | （无） | JWKS 端点（如 Logto）；设了才启用 JWT 校验（RS256/ES384） |
| `FDZX__AUTH_AUDIENCE` | （无） | JWT audience；未设时显式关 aud 校验 |
| `FDZX__LLM_API_KEY` | （无） | LLM 网关 KEY；不设 → 一切 LLM 增强走确定性回退（engine=deterministic），无网关调用无成本 |
| `FDZX__LLM_BASE_URL` | `https://token.finddatatech.cloud/v1` | OpenAI 兼容网关地址（私有化可指自建网关） |
| `FDZX__LLM_MODEL` | `gpt-5.4-mini` | 网关模型名 |
| `FDZX__REFERENCE_BASE_URL` | （无） | 参照系 MCP streamable-http 端点；未设 → 参照系相关功能降级并如实标注（not_configured，不冒充远端故障） |
| `FDZX__INFOSTREAM_BASE_URL` | （无） | fd-info（柏讯信息流）MCP 端点；未设 → 脉搏环冻结（not_configured） |
| `FDZX__PULSE_DB` | `~/.fd-zhengxin/pulse.db` | 脉搏环库：议题立场光谱快照，info-stream 可重建的缓存，运营层 |
| `FDZX__LEDGER_DB` | `~/.fd-zhengxin/llm_ledger.db` | LLM 成本台账库，运营层 |
| `FDZX__GUARD_OVERLAY` | （无） | 客户侧守卫规则叠加文件路径（只能收紧，见 §3；未设即基线 v1） |

私有化收紧的两面：守卫 overlay（§3）与不设 LLM KEY（纯确定性路径）；**不存在放宽型配置**——overlay 放宽会被拒载，无「关守卫」开关。

配套参照系服务（独立部署，前缀 `FDZX_REF_`）：`FDZX_REF_DB`（默认 `~/.fd-zhengxin/reference.db`）、`FDZX_REF_PROXY`（摄取出站代理，空=直连；本地维基数据/维基百科被拦时设 `http://127.0.0.1:7892`）、`FDZX_REF_CHINA_SEED`（种子 JSON 路径，默认随包 `seed/china_seed.json`）、`FDZX_SNAPSHOT_DIR`（快照根，默认 `~/.fd-zhengxin/snapshots`）。详见 fd-zhengxin-reference 仓 README。

## 7. 服务面与部署物

- API 面：`/healthz`、`/api/version` 探活；`/api/v1/me` 鉴权自检；`POST /api/v1/loops/locate|perspectives|genealogy`；`GET /api/v1/pulse/spectrum/{issue}`；`/api/v1/cards`（建卡/列表/单卡/verify/resubmit/shelve/annals）；`/api/v1/map`（synthesize/列表/history/history/{version}）。全个人层路由要求用户授权（未授权 401 并落审计）。
- 部署物：`deploy/k8s/`（web 与 api 两镜像，tag 随 CI 出片 `.github/workflows/image.yml`）+ ArgoCD Application；雷池/证书见 `deploy/SAFELINE-CERTS.md`。参照系服务不经本仓清单管理（分仓先例）。
- 库盘点：personal.db（个人层，四类表 + migration 链幂等）、pulse.db（脉搏快照）、llm_ledger.db（台账）、grey_queue（灰区分诊队列，随注入连接所在库）。三库物理分立，备份策略按此分层：personal.db 需备份，pulse.db / llm_ledger.db 可按运营需要处置，reference.db 可重建。
