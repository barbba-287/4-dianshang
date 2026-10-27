# P2-0b 任务文件：审计收口 + 测试解耦 + live 响应留存

> 创建：2026-10-27（WorkBuddy）。上位依据：`docs/p2-canary-readiness-gap-20261026.md`（P0 三项中
> 留存项未解）+ demo 审计遗留项 + 用户 2026-10-27 分项裁定（见 §1）。
> 前置状态：P2-0a 已收口并推送（`72653ca`），基线 **330 passed / 0 failed**。
> **全局约束（用户指令）：留存能力落地前，`.env` 的 `SHOPIFY_LIVE_ENABLED` 保持 `false`。**

---

## 1. 分项裁定（用户 2026-10-27，勿再议）

| 序 | 项 | 性质 | 门禁 |
|---|---|---|---|
| 1 | D-2 收口（含 contract test 翻转） | 文档 + 测试定版，**产品代码零改动** | 无 |
| 2 | D-3 测试与仓库外 `.env` 解耦 | 只动测试基建 | 无 |
| 3 | live 响应留存**设计** | 单独设计文档 | **用户评审通过才许开工项 4** |
| 4 | live 响应留存**实现** + 审计补全 | 代码 | 依赖项 3 评审 |

顺序执行；每项独立交付、独立复核、独立提交。D-1（DEMO_MODE 与 EMPLOYEE_AUTH 并存语义）**不在本轮**，
仍挂待拍板（`docs/demo-path-audit-20261026.md`），不要顺手处理。

---

## 2. 项 1：D-2 收口 + contract test 翻转

### 2.1 现状事实（每条附实测方法 —— R-20 教训）

- F1：`app/main.py:311` `if request.url.path.startswith("/api/"):` → 未登录访问 `/api/*` 返回
  **401 JSON**（`:316-333`，`{"detail":{"code":"UNAUTHORIZED",...}}`）；页面路由走 303（`:336-347`）。
  实测：`test_demo_path_contract.py` C1/C4/C7 已断言 401，全量绿。
- F2：原审计称"未登录访问 /api/* 返回 303"系采样窗口误读，D-2 已撤销
  （`docs/demo-path-audit-20261026.md` §5，`2643e2f`）。
- F3：`tests/test_demo_path_contract.py` 文件头 docstring 目前把 401 定性为"**现状锁定，不等于期望行为**"，
  并保留"拍板后修改产品代码时对应用例需同步更新"的待翻转框架（文件头第 1–15 行）。

### 2.2 要做什么

1. **审计文档定稿**：`docs/demo-path-audit-20261026.md` §5 的 D-2 撤销记录升格为**正式裁定**：
   "未登录访问 `/api/*` 返回 401 JSON、页面路由 303"即为规格语义（裁定依据：API 语义应 401 而非 302/303，
   浏览器跳转语义只适用页面路由）。注明裁定人与日期（2026-10-27 用户裁定 P2-0b 项 1）。
2. **contract test 翻转**：把 `test_demo_path_contract.py` 文件头从"现状锁定/待拍板"框架翻转为
   "**规格锁定**"——docstring 改写为引用上述裁定，删除"不等于期望行为""拍板后需同步更新"的临时性表述。
   **断言一律不改**（C1/C4/C7 的 401 断言与裁定一致）。
3. **补 1 条边界用例**：`/apifoo`（`/api` 前缀但无尾斜杠）不被 401 分支误伤——按 F1 的
   `startswith("/api/")` 实现语义，`/apifoo` 应走页面分支（303）。**先实测确认，再写断言**；
   若实测与预期不符，停下报告，不得迁就实现改断言。
4. 项 1 允许改动：`docs/demo-path-audit-20261026.md`、`tests/test_demo_path_contract.py`（仅 docstring
   与新增 1 用例）、任务文件本身。**产品代码零改动。**

### 2.3 完成标准

- 8+1 用例定向绿；全量 330+1=331 passed / 0 failed（或按 R-19 规则复跑）。
- docstring 无"待拍板/待翻转/现状不等于期望"残留字样（grep 验证）。

---

## 3. 项 2：D-3 测试与仓库外 `.env` 解耦

### 3.1 现状事实

- F4：`tests/conftest.py:20-24`（模块级）固定 5 个变量：`DATABASE_URL` / `ARTIFACTS_DIR` /
  `WORKER_LEASE_SECONDS` / `EMPLOYEE_AUTH_ENABLED` / `DEMO_MODE_ENABLED`；`:60-66` `_isolate_env`
  另设 4 个。**`SHOPIFY_*` 零覆盖**（grep 证实 conftest 无 SHOPIFY 字样）。
- F5：`Settings` 用 `env_file=".env"`（`app/config.py`），`case_sensitive=False` → 测试进程读到
  仓库外 `.env` 的 `SHOPIFY_LIVE_ENABLED/STORE_DOMAIN/ACCESS_TOKEN/API_VERSION/SYNC_BUDGET_SECONDS`
  等真值，**测试结果随本地 `.env` 漂移**。
- F6：唯一显式覆盖 `SHOPIFY_*` 的是 `tests/test_p2a_sync_guardrails.py:154-157`（harness 内 setenv）。

### 3.2 要做什么

1. 在 conftest 的两处（模块级 `:20-24` 与 `_isolate_env`）统一固定 Shopify 测试基线：
   `SHOPIFY_LIVE_ENABLED=false`、`SHOPIFY_STORE_DOMAIN=""`、`SHOPIFY_ACCESS_TOKEN=""`、
   `SHOPIFY_SYNC_BUDGET_SECONDS=300`、`SHOPIFY_MAX_PAGES=100`、`SHOPIFY_API_VERSION=2024-10`（以仓库
   `config.py` 默认值为准，勿发明）。需要非默认值的测试（如 p2a harness）自行覆盖——既有做法不变。
2. 新增 1 个防漂移用例（可放 `tests/test_demo_path_contract.py` 或新建 `tests/test_env_isolation.py`，
   二选一，新建文件需在交付说明声明理由）：构造 `Settings()`，断言
   `shopify_live_enabled is False` 且 `shopify_access_token == ""`（即与本地 `.env` 无关）。
3. **验收实验（必须做并留痕 stdout）**：临时把仓库 `.env` 的 `SHOPIFY_LIVE_ENABLED` 改为 `true`
   （先备份原值，实验后**字节级还原**并 `git status` 自查 `.env` 未被跟踪、内容还原），跑全量，
   结果必须与基线一致。这是"解耦"的唯一可信证据。
   **R-23 新规（2026-10-27 用户裁定，后续所有 `.env` 类实验强制）**：**优先用临时 `.env` 不改仓库 `.env`**
   ——做法：把仓库 `.env` 复制为 `.testtmp/env.variant`，实验时以 `env $(grep -v '^#' .testtmp/env.variant | xargs)`
   或显式环境变量注入干扰，仓库 `.env` 全程不被触碰；确需改仓库 `.env` 时才走"备份 → 改 → 还原 → cmp 核验"，
   且必须预期进程可能死在还原前（本轮 LEAK-PROBE 残留即此）。

### 3.3 边界

- 不改 `app/config.py`（生产语义不动）；只动 `tests/conftest.py` + 1 个测试文件。
- `.env` 实验还原失败 = 立即中止并报告，不得提交。

### 3.4 完成标准

- 全量 331（或 332，若防漂移用例独立成文件）passed / 0 failed。
- 防漂移实验 stdout 落盘交付说明；`.env` 还原核查通过。

---

## 4. 项 3：live 响应留存设计（单独文档，评审门禁）

### 4.1 现状锚点（缺口矩阵 P0-3）

- F7：`ShopifyLiveTransport.query`（`app/platform_adapters/shopify.py:48`）是 live GraphQL 的**唯一出口**，
  返回完整响应 dict —— 天然留存点。协议接口定义在 `:21`（Protocol）。
- F8：`raw_ref` 是合成串（`shopify://inventory/<id>/<loc>`，`shopify.py:217`），无 payload 正文，
  **出问题无法复现同一份数据**（缺口矩阵 R3，P0）。

### 4.2 设计文档（`docs/p2-retention-design-20261027.md`）必须逐条回答

1. **留存点**：transport 层（query 返回前）还是适配器/服务层？为什么？（倾向 transport 层单点，
   需论证 fixture transport 不受影响）
2. **存储介质与布局**：落盘（`ARTIFACTS_DIR` 下按 run 分目录？）还是入库（新表？——本切片倾向不改
   schema，落盘 + `resource_status_json` 存引用）？文件命名（request_id 关联）与索引方式。
3. **留存内容**：原始响应体（JSON）、请求 query/variables（脱敏后）、时间戳、request_id。
4. **脱敏**：响应中是否可能含敏感字段？`access_token` 绝不落盘（它在请求头，本就不在响应体）。
5. **体积与保留策略**：单文件上限、run 级上限、超限行为（截断告警 vs 拒绝留存 vs 放弃留存）；
   清理机制（本期可只定策略不实现清理）。
6. **失败语义**：留存写盘失败是否影响同步成功？（倾向：留存失败降级为 warning + 台账标记，不 fail run；
   需论证"可回放"与"可用性"的取舍）
7. **关联与回放**：`resource_status_json` 如何引用留存文件；回放接口本期只要求"能按 run/资源取回原文件"，
   不要求自动化重放。
8. **测试策略**：留存/脱敏/超限/失败降级各一条用例的最小集。
9. **明确不做**：清约定时任务、对象存储、跨平台抽象（先只做 Shopify live）。

### 4.3 门禁

- 设计文档提交后**停下**，等用户评审意见；未获通过不得写任何实现代码。
- 文档本身可先行提交（标注"评审中"）。

---

## 5. 项 4：留存实现 + 审计补全（待项 3 评审通过后另立细目）

- 按评审通过的设计实现；允许文件以设计文档评审稿为准。
- 审计补全 = 关闭缺口矩阵 P0-3（更新 `docs/p2-canary-readiness-gap-20261026.md` R3 行为"已具备"）
  + 留存引用进 `resource_status_json`（可审计链路闭环）。
- 全量预期 ≥331 passed / 0 failed（以项 2 后基线为准）。

---

## 6. 全局验证命令（各项通用）

```bash
cd /d/ai/dianshang && \
env -u PYTHONPATH -u PYTHONHOME PYTHONDONTWRITEBYTECODE=1 \
/d/python/python/python.exe -m pytest -q tests/test_demo_path_contract.py -p no:cacheprovider

cd /d/ai/dianshang && \
env -u PYTHONPATH -u PYTHONHOME PYTHONDONTWRITEBYTECODE=1 \
/d/python/python/python.exe -m pytest -q -p no:cacheprovider
```

全量基线：**330 passed**（P2-0a 后）。⚠️ R-19 flaky 规则不变（单跑过 + 复跑绿 + 登记）。

---

## 7. 实施方式与完成标准（各分项通用）

1. **CLI 分块执行**：每项单独调起，每项完成即落盘 `.testtmp/delivery.p2b-item<N>.md`
   （变更清单 / 关键决策 / 验证原始 stdout / 真实-模拟-未知 / 已知限制）。R-22（偶发 SIGTERM）仍在，
   落盘文件是复核唯一入口，不要依赖进程 stdout 存活。
2. 每项独立提交；提交信息不含 Windows 反斜杠。
3. 复核方（WorkBuddy）逐项独立复核后再报用户；项 3 后强制停机等评审。
4. 全局红线：`.env` 的 `SHOPIFY_LIVE_ENABLED` 在项 4 落地并用户确认前**保持 false**。
