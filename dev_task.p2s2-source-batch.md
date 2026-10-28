# dev_task.p2s2-source-batch — P2-2 来源批次生产化（批次归因 + 授权引用 + live 验证）

> 生成时间：2026-10-28
> 生成方式：WorkBuddy 只读调研（`app/db.py:389-428`、`app/external_sync.py begin_sync_run`、`app/main.py` 中间件与 sync 路径、`app/identity.py Principal`、缺口矩阵 A2/A3、R-38）
> 执行角色：Claude Code（实施）；WorkBuddy（复核）
> 阶段归属：P2 主体 · 切片 2/4（P2-2 source batch）——**live 验证切片**
> 上位依据：`docs/engineering-roadmap.md` P2 行；`docs/p2-entry-readiness-20261028.md` B2/B3 裁定；Q2 裁定（授权引用 + D4 审计归因并入本片）

---

## 1. 背景与目标

**批次已有一半**：`external_inventory_snapshots` / `external_orders` / `external_events` 都带
`sync_run_id` 关联（R-31 幂等 + 演练实锤），run 表已有资源级台账、游标、attempt 链。
**缺另一半（归因）**：无法回答"这次拉取是谁、从哪个 HTTP 请求、用哪个授权、上游 request-id
是什么"——A2（run 无 actor/request_id）、A3（上游 request-id 未落库）、R-38（凭证无实体引用，
只躺在 `.env`/Settings）三个缺口全开着。

本片把 run 正式升格为**来源批次（source batch）**：归因四件套（actor / request_id /
credential_ref / upstream_request_ids）落库，授权引用实体化（存引用与指纹、**永不存明文**），
并做一次**受控 live 验证**证明归因链在真实路径成立。

**R-27 单进程/多进程定调（用户指令写死，本片全程遵守）**：
本片**按单进程设计并写死**——actor/request_id 的赋值、取消 token、热开关、预算同为进程内态，
`R-39`（多 worker 下能力状态不一致）在本片**自然延伸为归因字段同样单进程**。不做任何
跨进程协调（无 Redis/DB 锁改造、无 leader 选举）。多 worker 外部化统一归技术债 D1，
P12 受控开放前处置。**实施方不得以"多进程兼容"为由扩大范围**。

**不做什么**：
- 不做批次级数据快照/对账报表（那是 P2-3 质量与后续看板的事）；
- 不改 R4/R6/取消/预算/留存语义（只**追加**归因字段，不改任何既有行为）；
- 不做多平台授权（只做 shopify 一家的 credential ref，taobao 仍是骨架）；
- 不做 OAuth/凭证轮换自动化（引用实体只是"指得着"，不是"换得了"）。

---

## 2. 现状事实（Fact）🔒

| # | 事实 | 位置 / 实测方法 |
|---|---|---|
| F1 | `external_sync_runs` 表**无** actor / request_id / credential / upstream_request_ids 列；已有 workspace_id/run_id/platform/source_mode/attempt/retry 链/cursor_json/资源台账/统计列 | `app/db.py:389-428`（整段已读） |
| F2 | `begin_sync_run(db, *, workspace_id, platform, sync_type, account_ref, store_ref, source_mode, simulated, attempt, retry_of_run_id, retry_idempotency_key, …)` 签名无归因参数 | `app/external_sync.py:475` 起整函数已读 |
| F3 | HTTP 中间件已生成请求 id：`request_id = request.headers.get("X-Request-ID") or f"req-{uuid.uuid4().hex[:12]}"`，并写入响应头 | `app/main.py:306`（及 :329/:337/:362/:372 响应头写入） |
| F4 | `Principal` 有 `subject / user_id / workspace_id / session_id / roles`——actor 取值来源存在 | `app/identity.py:48` 起；`grep -o "principal\.\w*" app/main.py` 命中 subject/user_id 等 |
| F5 | 适配器每页生成上游请求 id 并随页返回：`AdapterPage.request_id`；shopify 形如 `shopify-<uuid12>` 放进 `X-Request-ID` 头 | `app/platform_adapters/base.py:22`、`shopify.py:117-118`（缺口矩阵 A3 原文） |
| F6 | 凭证在 `Settings`（env_file=".env"）：`shopify_store_domain/shopify_access_token/shopify_api_version` 等；无任何凭证实体表 | `app/config.py`；`grep -n "class.*Credential" app/db.py` 零命中 |
| F7 | live 放行三条件（settings + runtime flag + 域名 + token）在 sync/resume/preview 三处重复出现 | `app/main.py:1770、1899、2215` 附近（P2-1 diff 已核） |
| F8 | 基线：全量 **393 passed / 0 failed**（P2-1 收口，复核方自跑 146.93s） | `docs/review-checklist-20261011.md` §16.3 |

**取证红线**照模板 v1.1 §2（禁中段采样、控制流读整函数、推断标注）。

---

## 3. 目标（Target）

**T1 批次归因四件套落库（migration 0020）**，`external_sync_runs` 新增 4 列（全部 nullable，
旧行与 mock 路径零强制）：
- `actor: String(128) NULL`——触发者，取 `principal.subject`；无 principal 的系统路径
  （demo seed CLI 等）写死固定值 `system:<来源>`（如 `system:seed-demo`）；
- `request_id: String(64) NULL`——**双路径写死（不得随机应变）**：
  - HTTP 路径：复用中间件既有请求 id（F3，`X-Request-ID` 头或其生成的 `req-` 值）；
  - **非 HTTP 路径（CLI / 后台脚本 / 定时触发）：由生成器生成 `req-bg-<uuid12>` 前缀值**，
    同场景 actor 走 `system:*` 常量——CLI 与后台触发都能被归因追踪；
- `credential_ref_id: Integer NULL FK`——见 T2；
- `upstream_request_ids_json: Text NULL`——本次 run 各页上游请求 id（来源
  `AdapterPage.request_id`），**落库形态写死为 JSON 对象**：
  `{"ids": ["shopify-...", ...], "truncated": false, "total": 3}`；
  **上限写死：`ids` 只保留最近 50 个**，超出时 `"truncated": true` 且 `"total"` 记全量页数
  （无界增长字段必须封顶）；run 收口时随 finalize 落库。

**T2 授权引用实体化（R-38 关闭）**：新表 `external_credential_refs`（migration 0020 同批）：
- 字段：id / workspace_id / platform / ref_name（唯一引用名，如 `shopify-dev-store`）/
  env_key（指向 `.env` 变量名，如 `SHOPIFY_ACCESS_TOKEN`，**存键名不存值**）/
  secret_fingerprint（凭证明文 sha256 前 8 位十六进制，仅用于人工比对"是否同一把钥匙"）/
  created_at / created_by；
- **红线：明文凭据任何形态不得入库**——值、可逆编码、全文 hash 均禁止；只有键名 + 8 位指纹；
- live 同步（sync/resume）构造 run 时解析当前 `SHOPIFY_ACCESS_TOKEN` 指纹匹配的 ref；
  无匹配 ref → 维持既有 `409 SHOPIFY_LIVE_NOT_CONFIGURED` 语义不变（不新增错误码）；
- **凭证注入链路（一句话写死）**：`credential_ref_id` 只做**归因与一致性校验**——
  `ShopifyLiveTransport` 的 token 获取路径**完全不变**（仍由现有代码从 `Settings`/`.env`
  读 `shopify_access_token` 构造，经 P2-1 注册表工厂）；ref 的 `env_key`/指纹仅用于
  "run 行指得着用的是哪把钥匙 + 指纹比对确认是同一把"。**全程明文 token 只在内存
  瞬态存在，不落库、不进 run 表、不进日志**；
- mock/json/fixture 路径 `credential_ref_id` 允许 NULL（fixture 无授权概念）；
- 提供最小管理面：CLI 子命令 `credential-ref list|add|remove`（add 只收键名+指纹自动计算，
  **不收明文**；remove 软删或硬删自定，测试锁死语义）。

**T3 run 归因可见**：既有 run 详情/列表响应（`/api/external/sync/runs*`）**追加**
actor / request_id / upstream 计数字段——只增不改，既有键值逐字节不变。

**T4 live 验证（本片收口硬条件，见 §5）**。

**方案选择**：批次=run（追加列）而非新表 `external_source_batches`。理由：sync_run_id 关联
已全线存在（F1 前置），新表会造出两套批次 id 与回填迁移，纯增险。**推荐 A（已定）**。

---

## 3b. 前置校验 🔒

| # | 前置条件 | 校验命令 | 期望 | 实测结果 |
|---|---|---|---|---|
| P1 | P2-1 已合入 | `git merge-base --is-ancestor 6bed25a HEAD && echo OK` | OK | 待贴 |
| P2 | 工作区干净 | `git status --porcelain` | 空 | 待贴 |
| P3 | 无并行 AI CLI | `ps -W \| grep -Ei "claude\|codex" \| grep -v grep; ls .git/*.lock 2>/dev/null` | 无 | 待贴 |
| P4 | 基线全量绿 | `env -u PYTHONPATH PYTHONDONTWRITEBYTECODE=1 /d/python/python/python.exe -m pytest -q -p no:cacheprovider` | **393 passed / 0 failed** | 待贴 |
| P5 | F1–F7 成立 | 逐条跑 §2 实测列 | 一致 | 待贴 |
| P6 | migration head 核对（**防并行撞号**） | `env -u PYTHONPATH /d/python/python/python.exe -m alembic heads` | 输出唯一 head；生成 migration **前**必须实跑本命令并把输出写进交付说明 | 待贴 |
| P7 | 改动规模（提交前） | `git diff --cached --numstat` | 与 §4 预期一致；删除非 0 须解释 | 待贴 |

红线照模板；任一不满足即停。

---

## 4. 边界与红线 🔒

- **允许改**：`app/db.py`（run 表 4 列 + 新表）、新增 `migrations/versions/0020_*.py`、
  `app/external_sync.py`（begin_sync_run/finalize 追加参数与落库）、`app/main.py`
  （sync/resume 调用点传归因 + 响应字段追加）、新增 `app/credential_refs.py`（或等价模块）、
  `app/cli.py`（credential-ref 子命令）、新增 `tests/test_p2s2_source_batch.py`。
- **禁止改**：`app/platform_adapters/*`（上游 request_id 已随 AdapterPage 返回，不动适配器）、
  `app/runtime_flags.py`、`app/identity.py`、R4/R6/取消/预算/留存实现与其测试、
  `dev_task.*` 其他任务文件；**不得顺手把三处放行条件抽公共函数**（属重构，另行立项）。
- **数据/契约**：migration 0020 两约束——① down 必须把两张表还原到 0019 形态
  （逐列断言，含列序/类型/默认值，R6-4 同款）；② 新列全部 nullable + 新表无外部默认数据，
  **SQLite batch 例外不使用**（本轮无 NOT NULL 回填需求；若实施中发现必须用，
  停下报告按 R-33 先例处理，不得自行引入）。
- **migration 防撞红线（P2-2/P2-3 并行必守）**：生成 revision **前**手动跑
  `alembic heads` 确认当前 head 并写进交付说明；若 head 已被**另一个切片**推进，
  必须基于**新 head** 重新生成 revision——**严禁硬编码基于旧 head 强行 merge**。
- **凭据红线**：T2 红线（键名+8 位指纹，仅此两样）；测试必须含"明文零落库"断言（S-6）。
- **R-27 定调红线**：不引入任何跨进程机制。

---

## 4b. 已知限制与残留登记 🔒

| 编号 | 限制/残留 | 影响面 | 本期处置 | 后续归属 |
|---|---|---|---|---|
| R-40 | 归因四件套为单进程赋值（R-27 定调），多 worker 下各进程 actor/request_id 天然自洽（每个请求自带）但取消/热开关/预算仍进程内态 | 多 worker / 多副本部署 | 接受并登记（单进程定调是设计的一部分） | P12 前 D1 类统一外部化 |
| R-41 | credential ref 与 `.env` 实际值之间**无运行时强绑定**：指纹只在创建 ref 与 live 解析时比对，`.env` 半路换钥匙后旧 ref 指纹失配 → live 409（需重建 ref） | 运维操作序列 | 接受并登记（失配即 409 拒绝，属安全默认） | P12 OAuth 时重设计 |
| R-42 | upstream_request_ids_json 仅覆盖**成功拉取页**；被取消的进行中页、重试丢弃页不入数组 | 对账上游日志时取消 run 的页覆盖不全（留存目录仍可补证） | 接受并登记 | 观察期后决定是否补 |
| R-43 | live 验证为**一次性证据**（验收时点真实），不构成常开授权；`.env` 基线仍 false | — | 演练后即还原 | runbook §5.2 永久有效 |

---

## 5. live 验证（收口硬条件，非 pytest 用例）🔒

> 用户指令写死：live 四条件与配额纪律。**pytest 收口 ≠ 本片收口**，还差本节。

按 `docs/live-drill-runbook-20261028.md` §5.2 四条件执行（缺一不开）：
1. **事前裁定**：用户对本次 live 验证单独书面确认；
2. **基线不动**：乙路线运行时前缀开启（`SHOPIFY_LIVE_ENABLED=true SHOPIFY_SYNC_BUDGET_SECONDS=120`），
   收口回 false + capabilities/sync-409 双证据；
3. **开门复测 Gate 0**：中转轮次台账核对（55~60 轮止损条款延续）+ Shopify 配额探针
   ≥20（cost=1，失败不自动重试）；
4. **有人值守 + 急停可用**：E1→E2→E3 三连待命。

**配额纪律（写死）**：验证前报台账（探针余量 + 本片预估 ≤10 次）；实际只跑**一次**
orders-only 小同步（预算 120s）；取消/续跑**不重复**（P2-0 已实弹验证过，不重跑）；
全程逐次记账进 `docs/review-checklist-20261011.md` 台账。

**验证断言（人工核 + DB 查询留证）**：
- live run 行：`actor`==操作者 subject、`request_id`==响应头 X-Request-ID、
  `credential_ref_id` 非空且指向 `shopify-dev-store`、`upstream_request_ids_json`
  元素数 ≥1 且形如 `shopify-` 前缀；
- 留存目录照常产出；`.env` 还原 cmp 一致。

---

## 6. 用例（最小 14 条）🔒

新增 `tests/test_p2s2_source_batch.py`：

| # | 场景 | 期望 |
|---|---|---|
| S-1 | migration 0020 up/down | up 新列+新表在位；down 后逐列断言与 0019 基线相等（R6-4 同款 `_table_snapshot`） |
| S-2 | 带 principal 触发 sync（fixture） | run.actor == principal.subject；run.request_id == 响应头 X-Request-ID |
| S-3 | 无 principal 的系统路径（demo seed） | run.actor == `system:seed-demo`（写死常量，测试锁字面值） |
| S-4 | 多页 fixture 同步（<50 页） | `upstream_request_ids_json` 反解析为 `{ids, truncated, total}`：`ids` 长度==页数、`truncated is False`、`total`==页数、元素非空 |
| S-5 | credential ref 生命周期 | add→list→remove 全通过；ref_name 唯一约束生效；指纹=sha256 前 8 位 |
| S-6 | **明文零落库** | 构造含已知 token 的 ref 后：credential_refs 表、run 表全行、SQLite 文件 bytes 三层 grep 明文/全文 hash 零命中 |
| S-7 | live 触发但无匹配 ref | 409 SHOPIFY_LIVE_NOT_CONFIGURED（错误码不新增、语义不变）；**不写 run 行**（与既有前置失败行为一致） |
| S-8 | live 触发且 ref 指纹匹配（monkeypatch token+transport，不真连网） | run.credential_ref_id == 该 ref id；source_mode/simulated 既有值不变 |
| S-9 | resume：源 run 有归因，新 run 触发者不同 | 新 run attempt==源+1、retry_of_run_id 正确、actor==**当前**触发者（不继承源）、credential_ref_id==源 ref |
| S-10 | run 详情响应追加字段 | actor/request_id/upstream 计数出现在既有 run 端点响应；**既有键值逐字节不变**（响应 diff 只有新增键） |
| S-11 | 既有测试零回归 | preview/sync/dry-run/retention/cursor/resume 相关测试**零改动全绿** |
| S-12 | 归因赋值点唯一 | 代码审查项 + `grep -n "actor=" app/main.py app/external_sync.py` 仅新增的单点赋值；两 run 并发 fixture 场景 actor 互不串 |
| S-13 | **非 HTTP 路径 request_id 降级** | CLI/后台触发（无 request 对象）的 run：`request_id` 形如 `req-bg-` 前缀且唯一；同场景 actor 为 `system:*` 常量（与 S-3 同一场景双断言亦可） |
| S-14 | **上游 id 封顶截断** | 构造 51 页：`ids` 长度==50、`truncated is True`、`total`==51；**保留的是最近 50 个**（首元素==第 2 页 id，断言不丢最新页） |

---

## 7. 验证命令 🔒

```bash
# 定向
env -u PYTHONPATH PYTHONDONTWRITEBYTECODE=1 \
  /d/python/python/python.exe -m pytest -q tests/test_p2s2_source_batch.py -p no:cacheprovider

# 全量（收口必跑）
env -u PYTHONPATH PYTHONDONTWRITEBYTECODE=1 \
  /d/python/python/python.exe -m pytest -q -p no:cacheprovider

# migration 升降
env -u PYTHONPATH /d/python/python/python.exe -m alembic upgrade head
env -u PYTHONPATH /d/python/python/python.exe -m alembic downgrade -1
```

PowerShell 等价、flaky 规则照模板 §6。

---

## 8. 完成标准 🔒

- [ ] §6 十四条定向绿（贴条数）
- [ ] 全量绿：基线 **393**，收口 `393+新增 / 0 failed`
- [ ] migration 0020 up/down 成功且 down 逐列还原
- [ ] **§5 live 验证完成并留证**（用户事前裁定后执行；此项未完成不得宣布切片收口）
- [ ] R-40/41/42/43 已同步登记进 `docs/review-checklist-20261011.md`
- [ ] `.testtmp/delivery.p2s2-source-batch.md` 落盘（五节齐全）

---

## 9. 交付说明 🔒

照模板 §8 五节。**偏差声明必须覆盖**：① credential ref 管理面形态（CLI 子命令命名）；
② S-3 系统路径 actor 常量字面值；③ remove 语义（硬删/软删）；④ 若 SQLite batch 例外被
触发（预期不触发），按 R-33 先例说明；⑤ **`alembic heads` 实跑输出原文**（生成 revision 前）；
⑥ 非 HTTP 路径 `req-bg-` 的实际前缀字面值与非 HTTP 场景清单（CLI/后台/定时各覆盖到哪些）。

## 10. 提交要求 🔒

独立提交；格式 `p2s2: 来源批次生产化（归因四件套+授权引用实体化，单进程定调）`。
**R-34 分块**：建议 5 块（①migration+db 模型 ②credential ref 模块+CLI ③external_sync/main
归因接线 ④测试文件 ⑤自验与交付说明），每块 <10min、编辑即落盘、块说明
`.testtmp/p2s2-chunk<N>.md`；死亡先 `git status` 再判死。

## 11. 复核清单（复核方用）

| 检查 | 判据 |
|---|---|
| 范围 | diff 与 §4 一致；适配器文件/runtime_flags/identity 零改动 |
| 凭据红线 | S-6 三层断言亲自复跑；credential_refs 表结构无值类字段 |
| 等价性 | 既有测试零触碰；run 响应仅新增键 |
| migration | down 逐列断言亲自复跑 |
| live 证据 | §5 四条件证据链齐全才准收口；配额台账逐次核对 |
| 台账 | R-40..43 连续入库 |
