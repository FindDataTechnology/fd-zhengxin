# fd-zhengxin

政心（zhengxin）独立产品线单仓：web 三入口（`/` 官网 landing、`/workbench` 工作台、`/admin` 管理后台）+ FastAPI 服务端（三闭环 + 脉搏环 + 个人层自有存储）。lex 线四模块的第四位：识（律法）、实论（实证）、哲思（哲学）、政心（政治）。

技术栈照 fd 舰队配方、形制照姊妹仓 fd-zhesi：React 19 + Vite + TS + Tailwind v4 + i18next 双语；服务端 Python FastAPI；部署 cheap fd-prod + TCR + ArgoCD（见 `deploy/`）。参照系独立部署在姊妹仓 fd-zhengxin-reference，分仓先例照 fd-zhesi / fd-zhesi-reference。

## 产品定位

政心是政治思想理解工具。帮助用户以「理解」而非「表态」的方式进入政治思想史：读原典、问概念、辨脉络，用自己的话复述理解；理解沉淀为理解卡，积累长成学习地图，修订沉积为理解年轮。政心呈现分歧，从不裁断分歧；它增长你的理解力，不替你持有立场。

## 四闭环

- **定位（`POST /api/v1/loops/locate`）**——事件/政策/概念描述进，参照系坐标出：概念锚定（确定性词表命中 → 参照系 concept_lookup 归一，查不到不编造）→ 谱系定位（沿 influenced_by / critical 极性边找学派归属，最强边权重低于 0.30 不归派，只出缺口）→ 历史先例链（带年代的边按年排序，缺年代不入链）；当代对象（is_contemporary）另补 ideology_query 思潮坐标。
- **多视角（`POST /api/v1/loops/perspectives`）**——已定位议题进，各学派 steel-man 解读 + 分歧结构出：按参照系边强度召回各学派最强论证锚点（锚强度低于 0.30 不呈现，弱解读不冒充 steel-man）；有 LLM KEY 时只准引用候选锚点写 steel-man（引用校验过滤编造引用），无 KEY / LLM 失败走确定性转述（form=sourced-stance，engine=deterministic）；当代议题 views 不足 2 个对立学派会被守卫 R-BAL-001 拦（422 结构化返回，不呈现）。
- **谱系（`POST /api/v1/loops/genealogy`）**——政治概念/思潮进，按年代排序的演变链出：两跳 influenced_by 边召回（深度有界），链保持年代序、LLM 只做环选择不改年序；断链处如实标注 gap（边缺年代 / 下钻失败原因透传 / 相邻环跨度超 300 年标注中间无参照系环节），不编造环节。
- **脉搏（`GET /api/v1/pulse/spectrum/{issue}`）**——议题立场光谱：增量消费柏讯 info-stream（fd-info）政治信息流（cn-gov 部委源），每次光谱移动都带逐字证据（item_id + 正文 span + quote，写校验 quote ≡ body[start:end]）；缺证据的强断言不产出（计入 dropped_no_evidence）；上游不可用时冻结降级——保留快照并声明结构化原因，绝不静默空转。

三闭环共享底座 `looputil`：确定性锚定词表（查询词面与参照系 SEP CANONICAL 26 词对齐，LLM 缺席也能锚定）、当代议题判定（保守默认非当代）、LLM JSON 解析、引用校验（宁删勿假，无锚不出）、publish 守卫门（全部产出过 `guard_output`，block 即 422 不呈现，grey 附原因放行，干净放行不带任何推荐性标注）。

## 个人层

个人层是用户理解记录，只落本仓自有库（默认 `~/.fd-zhengxin/personal.db`，env `FDZX__DB`），与参照系物理分库、永不混入共享底座；脉搏快照（`pulse.db`）与 LLM 成本台账（`llm_ledger.db`）属运营层，另行分库。三类对象：

- **理解卡（`/api/v1/cards`）**：主题 + 自己的复述 + 关键前提 + 来源引用。建卡/复述/搁置请求体 `extra=forbid`——**不含也不接受任何表态字段**（立场/赞同/倾向/评价类字段多投直接 422），模型层无表态列由 migration 测试守住，这是硬约束不是约定。复述校验（verify）只判「是否覆盖关键前提」（确定性覆盖比对保守打分宁低勿高，达 0.8 才可转「已理解」），输出覆盖度与差距说明，无任何立场评价字段。
- **学习地图（`/api/v1/map`）**：按「已理解」卡在参照系坐标系（学派/思潮维度）合成覆盖度（understood / in_progress / untouched 三桶）；参照系未配置/不可达时降级按卡自身锚点合成并标注 degraded（此时「未触及区域」不可知，如实置空不猜测）。每次合成都存新版本，旧版本全保留、可查可对比。
- **理解年轮（`/api/v1/cards/annals`）**：理解会修订而历史不覆盖——重复提交/搁置走「旧状态封存入年轮（append-only），新状态开卡」，封存后的卡拒绝一切改动（404）。回头看可见自己理解力生长的痕迹。

## 口径纪律

政心对政治思想内容只做「理解」不做「表态」：不预测、不推荐、不号召。纪律落在守卫层（`guard.py` / `guard_rules.py`）：全部闭环产物与脉搏光谱 publish 前统一过 `guard_output`，**动作只有两个——拦（BLOCK，422 不呈现）与标灰（GREY，呈现但置灰降权）**，每次命中产出结构化原因（rule_id / category / detail / ruleset_version）可查；无命中是干净放行（action=None），**放行不是动作，越线内容没有第三种处置，放行也不得伪装成推荐**。规则库 v1 为 8 条确定性规则，覆盖五类口径（prediction / recommendation / advocacy / bare-assertion / unbalanced），规则只增不改、整库版本化可回放。私有化部署可带客户侧 overlay（`FDZX__GUARD_OVERLAY`）叠加规则，**只能收紧（新增规则或 grey→block），放宽/删规则一律拒绝加载**。LLM 灰区判别器（guard_llm）没有放行权：无论判别结论如何动作恒为标灰，灰区落人工分诊队列驱动规则库更新。

## 架构

```
  外部真源（许可门纪律见 fd-zhengxin-reference）
  ├─ V-Dem（CC BY-SA 4.0，已过门）
  ├─ MARPOR（license confirmed，2026-10-23 裁决先行开门；非再分发约束，不开放原始数据导出）
  ├─ Wikidata（CC0） / SEP（只链不抄）
  └─ 中国线 curated 层（先秦种子 + 近现代五思潮索引）
          │
          │  MCP（/mcp：concept_lookup / ideology_query /
          │       relation_query / regime_timeseries / source_status）
          ▼
  ┌──────────────────────────────┐
  │ fd-zhengxin-reference 参照系  │  独立部署、独立库（reference.db），
  │ （姊妹仓，本仓只读消费）       │  快照内容寻址版本化（snapshots/）
  └──────────────┬───────────────┘
                 │ reference_mcp / reference_client（只读、降级友好、空结果必带原因）

  柏讯 info-stream（fd-info，MCP streamable-http）
  └─ cn-gov 部委政治源 ── infostream_client（D7：先 stream_health 再取数）
                 │
      ┌──────────┴───────────┐
      ▼                      ▼
  脉搏环 pulse            三闭环 locate / perspectives / genealogy
  （pulse.db，光谱快照，    （looputil 共享底座 + LLM 可选增强 +
   冻结降级保留快照）         引用校验 + guard publish 门）
      │                      │
      └──────────┬───────────┘
                 ▼
  ┌──────────────────────────────────────────────┐
  │ fd-zhengxin 服务端（本仓 server/fd_zhengxin_api）│
  │ 个人层 API：/api/v1/cards（理解卡+年轮）          │
  │           /api/v1/map（学习地图）                │
  └──────────────────┬───────────────────────────┘
                     ▼
  个人层自有库 personal.db（物理分库，永不混入参照系/运营层库）
  运营层分库：pulse.db（脉搏快照）/ llm_ledger.db（LLM 成本台账）
```

## 快速起服

两服务：web（Vite dev）与 server（uvicorn）。参照系服务与 fd-info 为外部依赖，本地按需另起（见姊妹仓 fd-zhengxin-reference README 与 `docs/info-stream-probe.md`）。

server 端 env（最小集，全量清单见 `docs/ops.md`）：

| 变量 | 用途 | 不设的行为 |
|---|---|---|
| `FDZX__AUTH_DEV_TOKENS` | 开发态固定令牌，如 `token1=user_a` | 未设且未配 JWKS → 个人层 API 一律 401 |
| `FDZX__AUTH_JWKS` / `FDZX__AUTH_AUDIENCE` | JWT 校验（Logto 等，RS256/ES384） | 未设走开发态令牌 |
| `FDZX__REFERENCE_BASE_URL` | 参照系 MCP 端点（如 `http://127.0.0.1:8000/mcp`） | 参照系相关功能降级并如实标注（not_configured） |
| `FDZX__INFOSTREAM_BASE_URL` | fd-info MCP 端点 | 未设 → 脉搏环冻结（not_configured）；已设 → 正常取数（fd-info 已上产，fd-prod 已接线，2026-11-06 实查 `http://fd-info.fd-prod.svc.cluster.local:8080/mcp` 可达） |
| `FDZX__LLM_API_KEY` | LLM 网关 KEY（可选增强） | 全部走确定性路径（engine=deterministic），无成本 |

```bash
# server（含三闭环/脉搏/个人层，111 个测试全离线）
cd server
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
FDZX__AUTH_DEV_TOKENS="token1=user_a" \
FDZX__REFERENCE_BASE_URL="http://127.0.0.1:8000/mcp" \
uvicorn fd_zhengxin_api.main:app --port 8011
```

```bash
# web（三入口：/ 官网、/workbench 工作台、/admin 管理）
cd web && npm install && npm run dev
```

现状口径：服务端 API 面已接线（探活 `/healthz`、`/api/version`、鉴权自检 `/api/v1/me`、三闭环、脉搏查询、理解卡全生命周期、学习地图）；web 三入口均可渲染，工作台/管理页为占位页（业务界面接线期落）。运维手册（六源状态、快照机制、守卫规则库与 overlay、LLM 台账、脉搏冻结语义、私有化 env 全清单）见 `docs/ops.md`。
