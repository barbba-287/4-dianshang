# dev_task.p2s3-quality — P2-3 数据质量门禁（留存回放 + 六维校验 + 观察态门禁）

> 生成时间：2026-10-28
> 生成方式：WorkBuddy 只读调研（`shopify.py _retain_response`、`docs/p2-retention-design-20261027.md`、`app/connectors.py normalize 族`、`app/analytics.py data_completeness 先例`、run 表统计列）
> 执行角色：Claude Code（实施）；WorkBuddy（复核）
> 阶段归属：P2 主体 · 切片 3/4（P2-3 质量）——**零 live 依赖**
> 上位依据：`docs/engineering-roadmap.md` P2 行；`docs/p2-entry-readiness-20261028.md` B2 裁定（P2-3 在 fixture + 关闭态完成）；B3 裁定（R-36 与本片无依赖）

---

## 1. 背景与目标

外部事实层的入库正确性目前**只靠两层**：normalize 族的强校验（坏行直接抛错、整页失败）
和 R-31 幂等（重复拉取不重复写数）。两者都是"通过/炸掉"二值，**没有中间态**：一页 100 行里
混了 3 行脏数据，现状是整页 500 或全量入库，没有"记下来、报出来、按批次看质量"的能力。
同时，P2-0b 建立的 live 留存（`artifacts/shopify_sync/<run_id>/<request_id>.json` + meta，
含 query/variables 全量）目前只有"人可取回"能力，没有机器消费。

本片做三件事：
1. **六维质量校验模块**（离线、纯函数、可单测）；
2. **留存回放**：质量校验可直接吃留存目录（零网络），让 P2-0 的留存从"证据"升级为"可复算的测试夹具"；
3. **观察态门禁**：run 收口时自动跑质量，结果落 run 表三态，**首版只观察不拦截**（门禁行为写死见 T3）。

**不做什么**：
- **零 live 依赖**：全部用例在 fixture/留存件/关闭态完成；本片**不发起任何真实网络请求**，
  也**不需要** runbook §5.2 四条件（不开 live）；
- **不拦截入库**（首版门禁行为，写死见 T3——拦截语义留给 P2-4 补偿评估后再定）；
- 不做质量看板/报表 UI（只做 run 表字段 + run 详情透出 + CLI 读取）；
- 不改 normalize 族既有行为（质量层在 normalize **外围**计数，不改其抛错语义）；
- 不做跨批次趋势分析（单 run 粒度）。

---

## 2. 现状事实（Fact）🔒

| # | 事实 | 位置 / 实测方法 |
|---|---|---|
| F1 | 留存件双层：`<request_id>.json`（原始响应体）+ `<request_id>.meta.json`（request_id/attempt/http_status/**query**/**variables**/captured_at/truncated/stored_bytes/original_size/resource）；超限为 `.json.truncated` | `app/platform_adapters/shopify.py:117-161`（整函数已读）；设计文档 `docs/p2-retention-design-20261027.md` v1.2 |
| F2 | 留存目录结构 `artifacts/shopify_sync/<run_id>/<request_id>.json[.truncated]`，台账 `retention{status,files,dir}` 落 run 行，CLI `retention list|get` 可读 | P2-0b 项 4（`932a0d6`+`d23d54f`）；`grep -n "def cmd_retention" app/cli.py` |
| F3 | normalize 族强校验抛 `ConnectorError`（缺字段/类型/值域），坏行导致**整页失败**，无中间态 | `app/connectors.py:192-300`（整函数已读） |
| F4 | `data_completeness` 概念只存在于 analytics（DailySkuSale 行级），外部事实层/同步链路无质量字段 | `grep -rn data_completeness app/`（仅 analytics/skills 命中） |
| F5 | run 表已有统计列 total/inserted/updated/no_op/conflict/stale——质量维度可与统计列互补但**不得重复造义** | `app/db.py:420-425`（已读） |
| F6 | `payload_hash` 由 `_payload_hash(row)` 计算，幂等键+payload_hash 是 R-31 去重依据 | `app/connectors.py:232` |
| F7 | 留存文件的 response 是 Shopify GraphQL 原始 JSON（含 `data`/可能含 `errors` 节点）；meta.query/variables 足以离线复算 | F1 同源；演练留存件 `.testtmp/drill-evidence-20261028/step5-retention-list.txt`（结构旁证） |
| F8 | 基线：全量 **393 passed / 0 failed** | §16.3 |

---

## 3. 目标（Target）

**T1 六维质量模型（写死，实施不得增删维度）**——新增 `app/source_quality.py`（纯函数 +
数据类，不依赖网络/db 连接）：

| 维度 | id | 判定 | 级别 |
|---|---|---|---|
| 结构性 | `structural` | GraphQL `errors` 节点存在、JSON 不可解析、缺 `data.<resource>` 键 | **hard** |
| 完整性 | `integrity` | 行缺必填字段/类型错/金额或数量为负——即 normalize 会抛的错，质量层**预检计数**（不替代 normalize） | **hard** |
| 唯一性 | `uniqueness` | 同批内幂等键重复出现 | soft |
| 一致性 | `consistency` | 同幂等键在本 run 与库中既有行的 payload_hash 不一致（R-31 conflict 的质量视角） | soft |
| 时效性 | `freshness` | meta.captured_at 或行 as_of 距校验时点超过 `shopify_sync_budget_seconds` 同源的过期阈值（阈值复用既有配置，不新增配置项） | soft |
| 完备性 | `completeness` | 宣告资源与实际页集不符：留存件/页结果缺失、truncated 文件存在（该文件 structural 降级为 skip 并标注） | soft |

**truncated 与 unknown 的语义分界（写死，不混淆）**：
- 留存文件**存在**且 `meta.truncated == true`：**跳过该页校验**（数据不全，校验会误报），
  记 `completeness` finding，**run 级 `quality_status` = `warn`**——"有证据但不全"；
- 留存目录**完全缺失** / `meta` 找不到：**无法校验**，run 级 `quality_status` = **`unknown`**——
  两者不得互相冒充：warn 表示"校验过但证据有缺口"，unknown 表示"根本没得校验"。

输出形态：`QualityReport{run_id, status: ok|warn|failed, dims: {id: {level, findings: int, samples: [..最多3条]}}, checked_at}`。
**hard 维度任一 findings>0 → status=failed；仅 soft → warn；全零 → ok。**

**T2 留存回放（零网络）**：新增回放入口 `replay_quality(run_id)`（模块函数 + CLI
`quality replay <run_id>`）：按 F1 目录结构读留存件 → 用 meta.variables 定位资源 →
解析 response → 走 T1 校验。**回放路径断言零网络——打桩目标写死为出站 HTTP 客户端，
不是底层 socket**：monkeypatch `httpx.Client.send` / `requests.Session.request`
（按项目实际客户端选用；改前先 `grep -rn "import httpx\|import requests\|urllib.request" app/`
确认真实客户端），**命中即 fail**。禁止 monkeypatch `socket.socket`——它会把 SQLite、
localhost 探测等无关调用一起误杀，制造成片假红（Windows 尤甚）。
truncated 文件按 T1「truncated 与 unknown 的分界」处理（跳过校验 + warn，非 failed）。
留存件缺失（目录不存在/空/meta 缺失）→ 返回 `status=unknown`（非报错）。

**T3 门禁行为（写死：观察态 observe-only）**——migration 0021 给 `external_sync_runs`
加 2 列（nullable）：`quality_status: String(16) NULL`、`quality_json: Text NULL`。
- **同步收口时自动执行**：run finalize 成功路径（succeeded）后跑 T1（数据来自本 run
  已入库行 + 统计列 + 留存台账；**fixture/mock 路径同样跑**），结果落两列；
- **只观察不拦截**：quality failed **不改变** run 既有 status/error_code/HTTP 响应语义，
  数据照常入库（拦截会改入库契约，风险大且 P2-4 补偿需要质量结果作输入而非闸门；
  P2-4 若依赖拦截语义，届时单独裁定升级）；
- run 详情响应**追加** `quality_status`（值对象不透出全量 json，CLI `quality show <run_id>`
  读全量）；
- 失败运行（failed/cancelled）**不跑**质量（无完整页集，跑了必然误报），两列保持 NULL。

**方案选择**：质量入口放 finalize 后同步执行（A，已定），不放后台任务（B 否决理由：
引入异步时序，本片要求"run 收口即有质量结论"的确定性）。

---

## 3b. 前置校验 🔒

| # | 前置条件 | 校验命令 | 期望 | 实测结果 |
|---|---|---|---|---|
| P1 | P2-1 已合入（P2-2 可未合入——本片与 P2-2 无文件冲突，若 P2-2 已先行合入则以其实际 head 为基线） | `git merge-base --is-ancestor 6bed25a HEAD && echo OK` | OK | 待贴 |
| P2/P3 | 工作区干净 / 无并行 CLI | 照模板 | 照模板 | 待贴 |
| P4 | 基线全量绿 | 照模板命令 | **393 passed**（若 P2-2 已合入则按其收口数字） | 待贴 |
| P5 | F1–F7 成立 | 逐条跑 §2 实测列 | 一致 | 待贴 |
| P6 | migration head 核对（**防并行撞号**） | `env -u PYTHONPATH /d/python/python/python.exe -m alembic heads` | 输出唯一 head；生成 revision **前**必须实跑并把输出原文写进交付说明 | 待贴 |
| P7 | 改动规模（提交前） | `git diff --cached --numstat` | 与 §4 一致 | 待贴 |

---

## 4. 边界与红线 🔒

- **允许改**：新增 `app/source_quality.py`、新增 `migrations/versions/0021_*.py`（若 P2-2
  先合入则顺延 0022，**以开工时实际 head+1 为准**并在交付说明声明）、`app/db.py`（run 表 2 列）、
  `app/external_sync.py`（finalize 后挂质量执行，**单点挂接**）、`app/cli.py`（quality 子命令）、
  run 详情响应追加字段、新增 `tests/test_p2s3_quality.py`。
- **禁止改**：`app/connectors.py` normalize 族、`app/platform_adapters/*`（含留存写入逻辑——
  只**读**留存件）、R4/R6/取消/预算实现、`app/analytics.py`（data_completeness 先例不动）、
  P2-2 的归因字段（若已存在，质量 json 里**可以引用**但不修改）。
- **数据/契约**：migration 两约束照 P2-2 §4（down 逐列还原；本轮预期不用 SQLite batch，
  必须用时停下按 R-33 先例报告）；run 响应仅新增键。
- **migration 防撞红线（P2-2/P2-3 并行必守）**：生成 revision **前**手动跑
  `alembic heads` 确认当前 head 并写进交付说明；若 head 已被**另一个切片**推进，
  必须基于**新 head** 重新生成 revision——**严禁硬编码基于旧 head 强行 merge**。
- **零 live 红线**：全测试文件不得出现真实域名/真实 transport 构造；CI 语义上即关闭态可跑。

---

## 4b. 已知限制与残留登记 🔒

| 编号 | 限制/残留 | 影响面 | 本期处置 | 后续归属 |
|---|---|---|---|---|
| R-44 | 门禁为观察态：quality failed 不拦截入库，脏数据**已落库**（可按 run_id 定位、R-31 幂等保证可重拉覆盖，但首版无自动修复） | 依赖质量结果的下游（P2-4 补偿、看板） | 接受并登记（写死为 T3 设计的一部分） | P2-4 评估拦截/补偿语义 |
| R-45 | 时效性阈值复用预算配置，属"够用的近似"而非独立质量 SLA | freshness 维度灵敏度 | 接受并登记 | 出现真实时效投诉时再立配置 |
| R-46 | 质量只看 run 粒度页/行，不做跨批次漂移检测（如同 SKU 全网占比突变） | 长周期数据质量问题不可见 | 接受并登记 | 看板切片再议 |

---

## 5. 用例（最小 12 条）🔒

新增 `tests/test_p2s3_quality.py`（留存回放用例的留存件**程序化构造**，不依赖演练本地证据）：

| # | 场景 | 期望 |
|---|---|---|
| Q-1 | migration up/down | 新 2 列在位；down 后逐列断言还原到基线形态（R6-4 同款） |
| Q-2 | structural：response 含 `errors` 节点 | 该页 structural findings≥1；run 级 status=failed |
| Q-3 | integrity：混入 3 行脏行（缺 sku/负数） | integrity findings==3；**normalize 语义不变**（既有 normalize 测试零改动全绿） |
| Q-4 | uniqueness：同批幂等键重复 | uniqueness findings 计数正确 |
| Q-5 | consistency：同幂等键 payload_hash 与库中既有行不一致 | consistency findings≥1（对齐 R-31 conflict 场景） |
| Q-6 | freshness：captured_at 超阈 | freshness=soft finding；不推高 status 至 failed |
| Q-7 | truncated 留存件（meta.truncated=true） | 该页**跳过校验**、completeness 记 finding、run 级 `quality_status = warn`（**不是 failed，也不是 unknown**） |
| Q-8 | 三态落库 | ok/warn/failed 三态分别正确落 quality_status/quality_json（JSON 可反解析、dims 齐全） |
| Q-9 | **观察态门禁**：status=failed 的 run | run.status 仍 succeeded、无新 error_code、数据已入库、HTTP 响应既有键不变——**拦截未发生**是被测行为 |
| Q-10 | 回放零网络 | `quality replay <run_id>` 对程序化构造的留存目录离线出报告；**打桩出站 HTTP 客户端**（`httpx.Client.send`/`requests.Session.request`，按真实客户端选）后回放仍成功，命中即 fail；**不得用 monkeypatch socket.socket** |
| Q-11 | 留存缺失（目录不存在/空/meta 缺失） | run 级 `quality_status = unknown`（与 Q-7 的 warn 严格区分）；不抛异常 |
| Q-12 | failed/cancelled run 不跑质量 | 两列 NULL；既有取消/失败测试零改动全绿 |

---

## 6. 验证命令 🔒

```bash
env -u PYTHONPATH PYTHONDONTWRITEBYTECODE=1 \
  /d/python/python/python.exe -m pytest -q tests/test_p2s3_quality.py tests/test_p2b_retention.py -p no:cacheprovider

env -u PYTHONPATH PYTHONDONTWRITEBYTECODE=1 \
  /d/python/python/python.exe -m pytest -q -p no:cacheprovider

env -u PYTHONPATH /d/python/python/python.exe -m alembic upgrade head
env -u PYTHONPATH /d/python/python/python.exe -m alembic downgrade -1
```

---

## 7. 完成标准 🔒

- [ ] §5 十二条定向绿（贴条数）
- [ ] 全量绿：基线 **393**（或 P2-2 后实际值），收口 `基线+新增 / 0 failed`
- [ ] migration up/down 成功且 down 逐列还原
- [ ] **零 live 证明**：`grep -rn "myshopify.com\|LiveTransport(" tests/test_p2s3_quality.py` 零命中
- [ ] R-44/45/46 同步登记进 `docs/review-checklist-20261011.md`
- [ ] `.testtmp/delivery.p2s3-quality.md` 落盘（五节齐全）

## 8. 交付说明 🔒

照模板五节。**偏差声明必须覆盖**：① migration 序号与 **`alembic heads` 实跑输出原文**
（生成 revision 前）；② Q-11 的 unknown 落库形态选择（NULL 或字面 `unknown`）；
③ 质量执行挂接点（finalize 内/调用方）与理由；④ freshness 阈值最终取值与来源配置名；
⑤ 零网络打桩实际命中的客户端类（httpx/requests/其他）与为何选它。

## 9. 提交要求 🔒

独立提交；格式 `p2s3: 数据质量门禁（六维校验+留存回放+观察态门禁，零 live 依赖）`。
**R-34 分块**：建议 5 块（①质量模块纯函数+构造留存夹具 ②migration+db ③finalize 挂接+CLI
④测试文件 ⑤自验+交付说明），纪律同 P2-2。

## 10. 复核清单（复核方用）

| 检查 | 判据 |
|---|---|
| 范围 | diff 与 §4 一致；normalize 族/适配器/analytics 零改动 |
| 零 live | 复核方复跑 grep；测试文件无真实域名/transport |
| 门禁语义 | Q-9 亲自复跑：failed 不拦截是被测行为 |
| 回放 | Q-10 亲自复跑（出站 HTTP 客户端打桩在位，不得用 socket 打桩） |
| migration | down 逐列断言亲自复跑 |
| 台账 | R-44..46 连续入库 |
