# 政心 E2E 缺陷修复联调报告（D1–D4）

- 日期：2026-10-23
- 范围：修复 `docs/e2e-local-report.md` 抓出的四个跨仓接线缺陷（D1 P1 / D2 P1 / D3 P2 / D4 P3），本报告为修复后**真实跨仓联调冒烟**记录（两仓真实服务对跑，非桩传输）
- 两仓：`fd-zhengxin-reference`（参照系，修后 commit 见 §5）、`fd-zhengxin`（主服务）
- 环境：全部状态隔离在 `/tmp/fdzx-fix/`（参照系 DB/快照、主服务三库、日志）；LLM KEY 不配（确定性路径为预期）；断网形态与原验收一致（`FDZX_REF_PROXY`/`HTTP(S)_PROXY` → `127.0.0.1:9` 不可达端口）
- 完整证据：`/tmp/fdzx-fix/logs/`（ref-server.log / main-server.log / smoke.jsonl / offline-check.json / mcp_contract_raw.json）

## 0. 服务起法（与原验收同口径）

参照系（端口 8031，断网形态）：

```bash
FDZX_REF_DB=/tmp/fdzx-fix/ref/reference.db FDZX_SNAPSHOT_DIR=/tmp/fdzx-fix/ref/snapshots \
FDZX_REF_PROXY=http://127.0.0.1:9 HTTP_PROXY=http://127.0.0.1:9 HTTPS_PROXY=http://127.0.0.1:9 NO_PROXY= \
fd-zhengxin-reference serve --host 127.0.0.1 --port 8031
```

主服务（端口 8032，FDZX__REFERENCE_BASE_URL 指向参照系 MCP 端点）：

```bash
FDZX__DB=/tmp/fdzx-fix/main/personal.db FDZX__PULSE_DB=/tmp/fdzx-fix/main/pulse.db \
FDZX__LEDGER_DB=/tmp/fdzx-fix/main/ledger.db \
FDZX__REFERENCE_BASE_URL=http://127.0.0.1:8031/mcp \
FDZX__AUTH_DEV_TOKENS=e2e-token-a=user_a \
uvicorn fd_zhengxin_api.main:app --host 127.0.0.1 --port 8032
```

## 1. 修法摘要（文件级）

| 缺陷 | 修法 | 主仓改动 | 参照仓改动 |
|---|---|---|---|
| D1 MCP 会话握手缺失 | 传输层提取共用 `mcp_http.py`（initialize → notifications/initialized → tools/call，Mcp-Session-Id 会话头，JSON 与 SSE 双响应体解析），reference_mcp 换用该传输；infostream_client 同步收敛到共用实现 | `server/fd_zhengxin_api/mcp_http.py`（新）、`reference_mcp.py`、`infostream_client.py` | 无 |
| D2 MCP 契约不对齐 | 以参照系契约为准：参数名 ideology_query(name)/relation_query(source,target[,polarity])；行锚=provenance.stable_id；主服务解析层重写（locate/perspectives/genealogy 字段映射）；参照系仅对行结构确缺字段最小补齐（种子概念行 +school/figures/weight、种子边行 +端点名/学派/weight/key_text/target_years、近现代行 +school/weight）并加测试；补跨仓契约 fixture 测试 | `locate.py`、`perspectives.py`、`genealogy.py`、`reference_mcp.py`；`tests/fixtures/reference_mcp_contract.json`（真实服务抓取）+ `tests/test_reference_contract.py`（新） | `tools_placeholder.py`、`china_lineage.py`（seed_snapshot_payload/seed_maps/figure_id_of 查询辅助）、`tests/test_tools_wiring.py` |
| D3 个人层 REST 面无端点 | 参照系加只读 REST 锚点面（GET /api/v1/regions、GET /api/v1/concepts/{topic}/anchor，从库内 currents/concepts 出数，空结果带 reason+source_status）；主服务 reference_client 以 service_root() 剥 /mcp 后缀复用同一 FDZX__REFERENCE_BASE_URL（一个变量两副面孔不再冲突） | `reference_client.py`；`tests/test_reference_client.py`（新） | `rest_api.py`（新）、`server.py`（build_http_app）、`cli.py`、`tests/test_rest_api.py`（新） |
| D4 词表未对齐 | LEXICON 扩容 24→115 词条：近现代五思潮（5）+ 先秦种子概念（46，单字概念因子串误命中不入）+ 种子人物全名（40）+ 原 24 词（自由主义/韩非/孔子等易撞车别名收敛）；文件头注明来源与再生成方式，静态固化不读对方仓文件 | `looputil.py` | 无 |

## 2. 跨仓契约测试形态（防再漂移）

`server/tests/fixtures/reference_mcp_contract.json`：对**真实参照系服务**（隔离 /tmp 库、断网形态）用 fastmcp Client 逐工具实调抓取的原样返回——种子概念行（仁政）、近现代思潮行（自由主义思潮）、空结果口径（螺旋史学本体论，断网下 SEP 腿主导 reason=unreachable）、种子关系行（孔子×曾参、孟轲×孔伋）、思潮坐标空结果（Q42 not_ingested）、source_status 台账，共 8 份样本（`_meta` 记录抓取与再生成方式）。

`tests/test_reference_contract.py`（9 例）把这些 payload 经注入 transport 喂给主服务解析层：行结构断言（ref=provenance.stable_id、school/figures/weight、target_name/target_years/key_text、约前505→-505 年代串保守解析）+ 三闭环端到端断言（locate 仁政/自由主义思潮 positioned=true 带源、生造词 positioned=false、perspectives views≥1 带锚、genealogy 链或如实缺口）。参照侧行结构变更而 fixture 未更新时该文件先红。

## 3. 真实跨仓联调冒烟（逐例结论，请求/响应全文见 /tmp/fdzx-fix/logs/smoke.jsonl）

| # | 用例 | 请求 | 结果（一句话） |
|---|---|---|---|
| 1 | locate 先秦 | POST /api/v1/loops/locate {"text":"仁政"} | 200，positioned=true，归派「儒家」强度 1.0，evidence=[china-seed:concept:仁政]（种子快照 china-seed@1.0.0#5415dd20d9b3），engine=deterministic |
| 2 | locate 近现代 | POST /api/v1/loops/locate {"text":"自由主义思潮"} | 200，positioned=true，归派「自由主义思潮」evidence=[modern:china/current/liberalism]（modern-index 锚，另带 sep 腿锚） |
| 3 | locate 生造词 | POST /api/v1/loops/locate {"text":"螺旋史学本体论"} | 200，positioned=false，gaps=[no_term_match]（不凭空定义） |
| 4 | perspectives 先秦 | POST /api/v1/loops/perspectives {"topic":"仁政"} | 200，views=1（儒家，form=sourced-stance，anchors=[china-seed:concept:仁政]），守卫干净放行 |
| 5 | genealogy 民本 | POST /api/v1/loops/genealogy {"term":"民本"} | 200，anchor=china-seed:concept:民本（儒家，关联孟轲（孟子）），chain=[] + gaps=[no_pair_to_query]（单人物无对可配，如实标注不编造） |
| 6 | genealogy 链非空对照 | POST /api/v1/loops/genealogy {"term":"忠恕"} | 200，chain=[曾参（曾子）（-505，「参乎！吾道一以贯之」，ref=china-seed:relation:…kongzi>…zengzi）]，年代串「约前505–前435」保守解析 |
| 7 | D3 复述校验前提锚 | POST /api/v1/cards {"topic":"仁政","digest":…}（不声明 key_points） | 201，premise_source=reference，premises=参照系 REST 锚点的两前提（通说界定+原典锚），coverage=0.5 如实 |
| 8 | D3 学习地图 | POST /api/v1/map/synthesize | 201 v1，degraded=false、coordinate_source=reference、untouched_regions=13（先秦八学派+近现代五思潮；此前恒 degraded:unreachable） |

主服务日志无 error/traceback；参照系日志仅正常访问日志。

## 4. 断网口径不回归（FDZX_REF_PROXY 指向不可达）

参照系全程以 `FDZX_REF_PROXY=http://127.0.0.1:9`（进程 env 已核）运行：

- `concept_lookup("仁政")` → 种子行照常返回（source=china-seed，stable_id/snapshot_version 齐全），sep 分项如实 `unreachable`（ConnectError）——好源不受坏源影响；
- `concept_lookup("nocturnal council")` → 空结果 reason=unreachable + 逐源 hint（china-seed 精确标签未命中/modern-index 未命中/SEP 不可达），无静默失败。

## 5. 测试与提交

- 两仓 pytest 全绿：参照系 `112 passed, 1 skipped`（原 100+1，新增 REST 面 12 例）；主服务 `110 passed`（原 91，新增契约 9 例 + REST 消费面 4 例 + loops 重写后 31 例等）。
- 提交（中文信息，注明 D1–D4）：
  - fd-zhengxin-reference：`8ab74ec`（修复 D2/D3：种子/近现代行结构最小补齐 + 个人层只读 REST 锚点面）
  - fd-zhengxin：`424d8fd`（修复 D1-D4：共用 MCP 会话传输 + 契约对齐解析层 + 契约 fixture + REST 消费面 service_root + LEXICON 扩容）

未触碰：k8s 清单、guard 子系统、pulse/infostream/llm_ledger 行为（infostream_client 仅传输实现收敛到共用模块，接口与超时口径不变）、docs/e2e-local-report.md 原文。
