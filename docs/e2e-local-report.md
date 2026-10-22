# 政心 本地端到端验收 + 私有化断网演练报告（任务 6.2 + 6.3 本地部分）

- 日期：2026-10-23（UTC 时间戳以服务端返回为准，本机时区 +8）
- 验收人：ZCode（只读代码 + 起本地服务 + 本报告；未改任何产品代码、未 git commit）
- 两仓：`/Users/chengsishi/finddata/fd-zhengxin-reference`（参照系 MCP）、`/Users/chengsishi/finddata/fd-zhengxin`（主服务）
- 环境隔离：全部状态写 `/tmp/fdzx-e2e/`（参照系 DB/快照、主服务三库、日志、探针脚本），未触碰 `~/.fd-zhengxin`
- LLM KEY：未配置 → 全部走确定性路径（`engine=deterministic` 为预期，不计失败）
- 完整证据：`/tmp/fdzx-e2e/logs/`（ref-server.log / ref-probe.jsonl / e2e-cases.jsonl / e2e-personal.jsonl / main-server.log）

## 0. 服务起法（复现用）

参照系（断网形态，端口 8031）：

```bash
FDZX_REF_DB=/tmp/fdzx-e2e/ref/reference.db \
FDZX_SNAPSHOT_DIR=/tmp/fdzx-e2e/ref/snapshots \
FDZX_REF_PROXY=http://127.0.0.1:9 \
HTTP_PROXY=http://127.0.0.1:9 HTTPS_PROXY=http://127.0.0.1:9 \
NO_PROXY= /tmp/fdzxref-venv/bin/fd-zhengxin-reference serve --host 127.0.0.1 --port 8031
```

主服务（端口 8032）：

```bash
FDZX__DB=/tmp/fdzx-e2e/main/personal.db \
FDZX__PULSE_DB=/tmp/fdzx-e2e/main/pulse.db \
FDZX__LEDGER_DB=/tmp/fdzx-e2e/main/ledger.db \
FDZX__REFERENCE_BASE_URL=http://127.0.0.1:8031/mcp \
FDZX__AUTH_DEV_TOKENS=e2e-token-a=user_a,e2e-token-b=user_b \
.venv/bin/uvicorn fd_zhengxin_api.main:app --host 127.0.0.1 --port 8032
# FDZX__INFOSTREAM_BASE_URL 故意不配（脉搏环冻结态验收）；FDZX__LLM_API_KEY 不配（确定性路径）
```

说明：端口 8021/8011 在本机被占（8021 lsof 不可见，疑似其他沙箱残留），改用 8031/8032，非产品问题。断网手段：`FDZX_REF_PROXY` 覆盖 wikidata 摄取面（urllib opener），`HTTP(S)_PROXY` 覆盖 SEP 实时面（httpx trust_env 默认开），指向 `127.0.0.1:9`（不可达端口，连接即拒）。

---

## 1. 参照系服务起服 + 工具面（步骤 1）—— PASS

- `GET /healthz` → `200 {"status":"ok","service":"fd-zhengxin-reference","version":"0.1.0"}`
- 启动钩子自动加载 curated 层：`/tmp/fdzx-e2e/ref/` 出现 `reference.db` + `snapshots/china-seed/`、`snapshots/china-modern-index/`（内容寻址快照落盘）。
- MCP `tools/list`（fastmcp Client 连 `/mcp`）→ 5 个工具，参数签名实测：

| 工具 | 参数 |
|---|---|
| concept_lookup | term |
| ideology_query | **name** |
| relation_query | polarity, **source, target** |
| regime_timeseries | country, indicator, years |
| source_status | （无） |

四工具 + source_status 齐备。加粗参数名与主服务调用形态不一致（见缺陷 D2）。

## 2. 断网演练（任务 6.2）—— PASS（全程无静默失败）

探针：`/tmp/fdzx-e2e/ref_probe.py`（fastmcp Client 直连），逐例 JSON 见 `ref-probe.jsonl`。

### 2.1 核心闭环依赖层（种子/近现代）照常可查

`concept_lookup("仁政")` → 200，1 行带源结果（截断）：

```json
{"results":[{"source":"china-seed","term":"仁政","title":"仁政",
  "url":"https://zh.wikipedia.org/wiki/%E4%BB%81%E6%94%BF#china-seed",
  "snippet":"以不忍人之心行不忍人之政，省刑罚、薄税敛的王政方案。｜原典：《孟子·梁惠王上》（ctext 只链不抄）",
  "provenance":{"source":"china-seed","stable_id":"china-seed:concept:仁政","as_of":"2026-10-23",
    "attribution":"先秦诸子种子谱系（curated 结构化数据）：…CC BY-SA 4.0 条目名署名 + Wikidata QID 对齐（CC0）+ ctext.org 篇目锚",
    "snapshot_version":"china-seed@1.0.0#5415dd20d9b3"}}],
 "sources":{"china-seed":{"status":"ok","rows":1},
   "modern-index":{"status":"miss","reason":"not_ingested","hint":"近现代五思潮索引无 '仁政'…"},
   "sep":{"status":"miss","reason":"unreachable","hint":"SEP 搜索端点不可达（ConnectError: All connection attempts failed）"}}}
```

- 先秦种子概念 5/5 可查：仁政、兼爱、无为、王道、法治（以法为本）——全部带 provenance（source/stable_id/as_of/attribution/snapshot_version），快照版本 `china-seed@1.0.0#5415dd20d9b3`。
- 近现代索引 2/2 可查：维新变法思潮、自由主义思潮（`modern:china/current/*`，快照 `china-modern-index@2026-10-23#4836380d4e7d`，含 summary 与文献条目）。
- 种子关系边（断网）可查：`relation_query("孟轲（孟子）","孔伋（子思）")` → 1 条 influence/positive 边带 provenance 与 snapshot_version。注意人物匹配是**精确全名**（"孟子"单名不命中，hint 如实说明）。

### 2.2 SEP 类实时查询：带 reason 的空结果 / curated 降级（均为预期，见任务注记）

- 非白名单词 `concept_lookup("nocturnal council")` → 空结果，`reason:"unreachable"`，hint 逐源给出：`"sep：SEP 搜索端点不可达（ConnectError…）"`。**PASS（unreachable 口径）**。
- 白名单词 `concept_lookup("democracy")` → 1 行 curated 离线锚点（title+URL，snippet 空），`sources.sep` 带 `note:"curated 离线锚点（docs/sep-coverage.md 2026-10-23 实测；本轮未能实时核验）"`，provenance.as_of=2026-10-23（实测日期）。**PASS（curated 回退 + note 标注，任务注记的预期路径）**。`liberty` 同样回退且 note 附带硬映射说明。

### 2.3 外源类工具如实降级

- `ideology_query("Q30")` → 空，reason=not_ingested：wikidata 未摄取（查询面只读库，断网不影响该口径）；marpor 附 `gate_status:"license_unconfirmed"` + gate_detail（许可门第一道门）。如实、不编造。
- `regime_timeseries("USA")` → 空，reason=not_ingested，hint "V-Dem 已过许可门（CC BY-SA 4.0）但数据尚未摄取…先 ingest_vdem 落库"。
- wikidata/marpor 查询路径不触网（摄取面才走网络），断网演练中它们报 not_ingested 而非 unreachable——口径自洽（「未摄取」≠「不可达」），如实记录。

### 2.4 source_status 如实反映六源状态

```json
v-dem:        not_ingested（未摄取）
marpor:       not_ingested, reason=license_unconfirmed（许可门）
wikidata:     not_ingested
sep:          not_ingested（on-demand 缓存 0 条；curated 兜底不写缓存，口径如实）
china-seed:   ok  + snapshot china-seed@1.0.0#5415dd20d9b3（fetched_at 2026-10-23）
modern-index: ok  + snapshot china-modern-index@2026-10-23#4836380d4e7d
```

### 2.5 静默失败检查

- 参照系服务日志无 error/traceback（仅正常访问日志）；所有空结果均带 reason+hint；多源融合 sources 分项逐源标注。**无静默失败。**
- 附：参照仓自带测试套件 `100 passed, 1 skipped`（隔离 env 下跑，逻辑层佐证）。

---

## 3. 主服务起服（步骤 3）—— PASS

- `GET /healthz` → `200 {"status":"ok","service":"fd-zhengxin-api","version":"0.1.0"}`
- `GET /api/v1/me`：无 token → 401（审计 denied 2 条落库）；`Bearer e2e-token-a`（auth.py dev-token 机制：`FDZX__AUTH_DEV_TOKENS="token=user,…"`）→ `{"user_id":"user_a"}`。

---

## 4. E2E 四闭环（任务 6.3 本地部分）

逐例完整请求/响应在 `/tmp/fdzx-e2e/logs/e2e-cases.jsonl` 与 `e2e-personal.jsonl`。先给结论，再分面明细。

| 面 | 用例数 | 通过 | 失败 | 结论 |
|---|---|---|---|---|
| 定位闭环·正例（带源结论） | 10 | 0 | 10 | **FAIL**（缺陷 D1+D2：参照系召回全断） |
| 定位闭环·无依据样例（positioned=false） | 3 | 3 | 0 | PASS |
| 多视角闭环（带源+分歧三段） | 5 | 1\* | 4 | 4 个 200 但 views=0（D1+D2）；\*当代单视角守卫 422 拦 = PASS |
| 谱系闭环（年序链+gap 标注） | 3 | 0 | 3 | 链空；gap 标注行为本身合格（如实透传 upstream） |
| 脉搏环（冻结态带原因） | 2 | 2 | 0 | PASS（设计内行为） |
| 守卫拦截（5 类 rule_id） | 6 | 6 | 0 | PASS（5 条 API 实调 + 1 条单测直调，如实分记） |
| 个人层全流程 | 22 | 22 | 0 | PASS |
| 单测佐证（两仓 pytest） | — | 191 过 1 跳 | 0 | 三闭环逻辑在桩传输下全部绿（缺陷只在跨仓接线） |

### 4.1 定位闭环 /api/v1/loops/locate

正例 10 条（5 先秦：仁政 / 法家韩非的法治思想 / 兼爱非攻的墨家主张 / 无为而治 / 王道与霸道之辨；3 近现代：自由主义人权论战 / 社会主义论战财产权 / 文化保守主义与宪政；2 当代：现行选举制度下的政治代表 / 执政党主权与正当性）。

代表性响应（`POST {"text":"仁政"}`，200）：

```json
{"engine":"deterministic","positioned":false,"anchors":[],"attributions":[],
 "precedent_chain":[],"coordinates":[],
 "gaps":[{"stage":"reference","reason":"upstream",
   "detail":"concept_lookup('confucian political thought')：参照系响应异常（concept_lookup）：HTTP 400"}],
 "guard":{"action":null,"reasons":[],"ruleset_version":"v1"}}
```

- 10/10 未产出带源结论：参照系 MCP 对主服务的单发 `tools/call` 一律回 **HTTP 400 "Missing session ID"**（缺陷 D1）。
- 兼爱/无为/王道 3 例本地词表不命中（gaps `no_term_match`，缺陷 D4——参照系种子里其实有这些概念）。
- 2 个当代样例被 publish 门 R-BAL-001 拦（422，零视角）——守卫按设计工作，但叠加 D1 后当代定位完全不可用（观察 O2）。
- 无依据样例 3/3 PASS：量子宪政主义 / 螺旋史学本体论 / 月相政体学 → 全部 `positioned=false`。其中「量子宪政主义」误触词表「宪政」→ 转 concept_lookup 仍 400 → 未编造结论（缺口可见，处置安全）；另两例 `no_term_match` 直陈无锚。

### 4.2 多视角闭环 /api/v1/loops/perspectives

5 议题（社会契约的正当性 / 自由与秩序 / 财产权与分配正义 / 革命权 / 2026 大选政党立场）。

- 4 个非当代议题 200：`views=[]`、`disagreement` 三段结构在但为空（无学派锚点可并置）、gaps 逐条 `upstream`（HTTP 400）。**带源三段断言 FAIL（D1+D2）**；失败可见（gaps 非空），非静默。
- 当代议题 `{"topic":"2026 年大选…","is_contemporary":true}` → **422 R-BAL-001**：

```json
{"detail":{"reason":"guard_blocked",
  "guard":{"action":"block","reasons":[{"rule_id":"R-BAL-001","category":"unbalanced",
    "detail":"当代议题视角失衡：去重后学派 0 个、立场 0 种（要求 ≥2 个对立学派视角）"}]},
  "gaps":[…upstream…]}}
```

单视角/零视角守卫拦截 PASS。

### 4.3 谱系闭环 /api/v1/loops/genealogy

民本 / 自由 / 社会契约 → 均 200、`engine=deterministic`、`rings=0`、`anchor=null`。

```json
{"term":"民本","resolved_term":"民本","chain":[],
 "gaps":[{"stage":"reference","reason":"upstream",
   "detail":"concept_lookup('民本')：参照系响应异常（concept_lookup）：HTTP 400"}]}
```

词表归一行为正确（自由→liberty、社会契约→social contract）。**年序链断言 FAIL（D1+D2）；gap 标注机制合格（断链处如实记录 reason，未编造环节）。** 若参照系召回接通，年序/gap 逻辑由 `tests/test_loops.py`（桩传输）覆盖并通过。

### 4.4 脉搏环 —— PASS（设计内行为）

`FDZX__INFOSTREAM_BASE_URL` 未配置。摄取触发面：无 API（摄取是后台函数 `pulse.ingest_spectrum_update`，查询面只有 GET），故摄取用主服务 venv 直调该函数（如实记录：非 API 路径），查询走 API：

```
ingest_spectrum_update("延迟退休改革") →
{"frozen":true,"frozen_reason":{"code":"not_configured",
 "detail":"FDZX__INFOSTREAM_BASE_URL 未配置（fd-info 未上产，端点可配置）"},
 "pages":0,"items_seen":0,…,"updated_at":"2026-10-22T21:29:33Z"}

GET /api/v1/pulse/spectrum/延迟退休改革 → 200
{"spectrum":[],"frozen":true,"frozen_reason":{"code":"not_configured","detail":"…"},
 "updated_at":"2026-10-22T21:29:33Z",
 "guard":{"action":"block","reasons":[{"rule_id":"R-BAL-001",…}]}}
```

- 冻结态带结构化原因 PASS；空光谱被守卫判 block 且**带裁决返回**（查询面不替呈现面决定，口径正确）。
- 未知议题 `GET /api/v1/pulse/spectrum/从未摄取的议题` → 404 带原因。PASS。

### 4.5 守卫拦截样例（≥5 条，全部带 rule_id）

| # | rule_id | 路径 | 输入（截断） | 结果 |
|---|---|---|---|---|
| 1 | R-PREDICT-001 | API /locate | 「预测这位候选人将赢得大选…」 | 422 block，命中『预测…赢得』 |
| 2 | R-RECO-001 | API /locate | 「…明智的选择是支持洛克一系」 | 422 block，命中『明智的选择是』 |
| 3 | R-ADVO-001 | API /locate | 「…我们必须反对征收，捍卫私有制」 | 422 block，命中『我们必须』 |
| 4 | R-BAL-001 | API /perspectives | 当代议题+单/零视角 | 422 block（见 4.2） |
| 5 | R-BARE-001 | **单测直调 guard_output** | claims 一条缺 source | BLOCK，「1 条强断言缺 source 引用：『该政策必…』」 |
| 6 | R-PREDICT-002 | API /locate | 「…大概率会赢得多数…」 | 200 + `guard.action="grey"`（标灰放行，原因随响应） |

如实分记：#1-4、#6 为 API 实调；#5 走单测路径——三闭环 API 面 claims 恒带 source（结构上造不出裸断言），只能直调守卫函数构造。

### 4.6 个人层（建卡→校验→已理解→年轮→学习地图，双账号隔离）—— 22/22 PASS

user_a 全流程：

1. 建卡（弱复述「就是大家签一份合同」）→ 201，`state=restating`，coverage 0.0，gaps 逐前提列明「归一化比对（子串/关键词/二元组）均未达覆盖线」。
2. `POST /{id}/verify` → 200，coverage 0.0 未达阈值 80%，仍 restating（保守打分生效）。
3. `POST /{id}/resubmit`（高质量复述）→ 201：旧卡封存（superseded）入年轮，新卡 `state=understood`、coverage 1.0、`premise_source=card`、`engine=deterministic`。
4. 建卡即达线（自由：消极/积极自由两前提全覆盖）→ 直接 `understood`。
5. 搁置（仁政卡）→ 旧状态 sealed 入年轮，同题开 shelved 新卡。
6. 封存卡不可变：`GET /{sealed_id}` 200 只读快照（改动面 404：「拒绝一切改动」口径，读面保留）；`POST /{sealed_id}/verify` → 404。
7. 年轮 `GET /cards/annals` → 2 条 append-only 快照（superseded 社会契约 / sealed 仁政，时间戳齐全，历史不覆盖）。
8. 学习地图：`POST /map/synthesize` → v1，**degraded=true**（参照系 REST 坐标面不可达，缺陷 D3），`coordinate_source=card-anchors`，`degradation_reasons=["参照系不可达（unreachable:HTTPStatusError）：按卡自身 topic 锚点归类降级合成，未触及区域不可知"]`——降级如实标注 PASS；understood/in_progress 分桶正确（社会契约/自由=understood，仁政=in_progress）。再加「民主」卡 → v2 regions 扩为 4；`GET /map` 最新 v2；`GET /map/history` 两个版本；`GET /map/history/1` 取回 v1 完整快照（regions 3、counts total=3/understood=2/shelved=1）——版本化保留 PASS。

双账号隔离（user_b）：

- b 建卡（民本）→ 正常；b 列表只含自己 1 张；`GET /cards/{a 的卡 id}` → 404「卡不存在或不属于该用户」；b 年轮 0 条；b 地图 404（未合成）。PASS。

口径纪律：

- 表态字段拒收：`POST /cards` 带 `"立场":"支持功利主义"` → 422 `extra_forbidden`（loc=body.立场）。PASS。
- 无 token → 401 `missing bearer token`，audit_log 落 2 条 denied。审计台账（/tmp 库只读查询）：auth denied 2 / allowed 50，card.create 5、card.resubmit 1、card.shelve 1、card.verify 1、map.synthesize 2、loops.locate 17、loops.perspectives 6、loops.genealogy 3——全操作留痕。PASS。

---

## 5. 缺陷与缺口清单（只记录，未改代码）

### D1｜P1（阻断三闭环）：参照系 MCP 客户端无会话握手，单发 tools/call 被服务端 400 拒绝
- 位置：`fd-zhengxin/server/fd_zhengxin_api/reference_mcp.py`（http_transport/_call）
- 现象/复现：起参照系后，`curl -X POST http://127.0.0.1:8031/mcp -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"concept_lookup","arguments":{"term":"仁政"}}}'` → `HTTP 400 {"error":{"code":-32600,"message":"Bad Request: Missing session ID"}}`。FastMCP streamable-http 要求 initialize 握手 + `Mcp-Session-Id` 头；fastmcp Client 正常调用同端点成功（本报告 6.2 全部经 fastmcp Client 打通）。
- 影响：三闭环全部参照系召回失败（归一为 `reason=upstream` gap——降级路径本身带原因，非静默），定位/多视角/谱系 0 带源产出。
- 修复方向（建议）：客户端实现 MCP initialize→tools/call 会话（或每次调用按需建会话），或参照系改挂无状态 JSON-RPC 面。

### D2｜P1：两仓 MCP 工具契约不对齐（参数名 + 行结构）
- 证据：
  - 参数：主服务发 `ideology_query(qid=…)`、`relation_query(qid, polarity)`；参照系签名 `ideology_query(name)`、`relation_query(source, target, polarity)`（tools/list 实测）。
  - 行结构：主服务要求概念行带 `id/term/definition/url/source/qid`、关系行带 `id/target_qid/pid/weight/school/year/key_text/stance`（locate.py `_anchor_concepts/_norm_edge`、genealogy.py `_lookup_anchor`）；参照系实际概念行 `{source,term,title,url,snippet,retrieved_at,provenance}`、关系行 `{source_id,target_id,kind,polarity,snapshot_version,provenance}`——无 `id/qid/definition/school/weight/year`。
- 影响：即使修好 D1，锚点行也会被 `entry.get("id")` 全部滤掉（anchors 恒空），闭环仍无产出。两仓测试各自用桩传输全绿（主仓 91 passed / 参照仓 100 passed），缺一份跨仓契约测试（对真实服务或共享 fixture）。
- 修复方向：对齐契约（建议参照系行内补 `id`(=stable_id) 与可选 `qid`，或主服务消费面适配 provenance/stable_id 形态），并补跨仓集成测试。

### D3｜P2：学习地图/复述校验的参照系 REST 消费面在参照系上无端点，base URL 语义冲突
- 位置：`fd_zhengxin_api/reference_client.py`（GET `{base}/api/v1/regions`、`{base}/api/v1/concepts/{topic}/anchor`）；参照系只有 `/mcp` 与 `/healthz`。
- 且 `FDZX__REFERENCE_BASE_URL` 对 reference_mcp 需指向 `…/mcp`、对 reference_client 需是 REST 根，同一变量无法同时满足 → 学习地图恒 degraded（unreachable:HTTPStatusError），复述校验前提集只能走卡声明 key_points。留位期文档已自知（「真实契约定型后收敛」），验收口径记为缺口：**坐标系/概念锚 REST 面未接线**。

### D4｜P3：主服务锚定词表与参照系种子词表未对齐（可用性缺口）
- `looputil.LEXICON` 只覆盖 SEP CANONICAL 26 词的中文别名；参照系种子 50 先秦概念（兼爱/无为/王道/民本/霸道/性善论…）与 40 人物（精确全名如「孟轲（孟子）」）不在词表/别名内 → `no_term_match` 或映射到英文伞形词（仁政→confucian political thought，丢失种子直查与中文原典锚）。非安全问题（宁可无锚不编造），但核心卖点「先秦概念定位」在本地面基本不可达。

### 观察项（不计缺陷）
- O1：断网下 wikidata/marpor/vdem 类查询报 `not_ingested` 而非 `unreachable`——查询面只读库、摄取面才触网，口径自洽；source_status 的 sep=not_ingested（缓存 0 条）与白名单词 curated 兜底并存，两处各自如实。
- O2：当代议题在参照系断链时 locate/perspectives 必然 422 R-BAL-001（零视角）。守卫按设计工作（不呈现失衡产物），但与 D1 叠加后当代两闭环完全不可用。
- O3：脉搏环摄取无 API 触发面（仅后台函数 + GET 查询面）；本次冻结态以函数直调制造、API 验查询。上线前需接定时任务/管理端点。
- O4：参照系人物精确标签匹配拒绝「孟子」这类简称（hint 如实提示用全名），可用性取舍，建议后续加别名。

---

## 6. 总结判定

- **6.2 私有化断网演练：通过。** curated 基座（先秦种子 + 近现代索引）断网全量可查且逐行带源（provenance + 快照版本）；SEP 实时查询空结果带 reason（unreachable）或白名单 curated 降级带 note；source_status 六源如实（许可门/未摄取/在用快照）；全程无静默失败。
- **6.3 本地 E2E：守卫、脉搏环、个人层（含双账号隔离、年轮、地图版本化、口径纪律）全部通过；三闭环的参照系召回被 D1（MCP 会话缺失）+ D2（契约不对齐）阻断，正例 0 带源产出**——闭环自身的三段式/引用校验/阈值门/守卫逻辑在桩传输单测（两仓 191 过）下验证通过，缺陷集中在两仓接线层，修复面清晰。
- 缺陷：D1(P1)、D2(P1)、D3(P2)、D4(P3) + 4 条观察项，均已留复现步骤，未改产品代码。
