# dev_task.p6s3-data-export — P6-S3 通用数据导出与异常追溯（CSV Export）

> 生成时间：2026-11-03 17:20（v1.1 定稿，Q1–Q3 用户裁定完毕）
> 前置：P6-S1（`bef6003`/`749e463`）+ P6-S2（`9a0c2d1`），基线 **556 passed / 0 failed**

## 0. 裁定记录（2026-11-03 17:10，全部采纳并追加三条硬要求）

| # | 问题 | 裁定 |
|---|---|---|
| Q1 | 行级容错提交语义 | **opt-in**（默认关，勾选开启）。strict 严格模式一字不改；勾选后坏行剔除落库、好行照常入库 |
| Q2 | 新表 + migration | **批准** `external_import_rejections`，打破「零 migration」惯例仅此一次。字段至少含 `id / run_id / row_number / raw_data / error_reason / created_at`（实施再加 workspace_id、kind 以保隔离与分类） |
| Q3 | actor 归因 | 结构化 `logger.info`，**不建审计表** |

用户追加三条硬要求（实施必须满足）：

1. **异常明细 CSV 必须含「系统拦截原因」列**：商家原本上传的列全部保留，最后追加一列 `系统拦截原因`（原因文本带行号，如「第 3 行：数量必须是非负整数」）。
2. **容错提示必须显眼**：勾选后表单出现**黄底**提示——「将跳过错误行，错误数据可在批次历史中导出修改」。strict 默认下预览报错保持清晰。
3. **红线不变**：不改旧解析逻辑（`connectors.py` 零触碰）、不引重依赖（禁 pandas/openpyxl）。

## 1. 侦查结论（2026-11-04 只读核实）

### 1.1 原始事实层：数据齐全，直接可导

- `InventorySnapshot.sync_run_id`（`db.py:554`）与 `ExternalOrder.sync_run_id`（`db.py:755`）均有索引列，run → 行的反查零迁移。
- 行级富信息现成：`ExternalOrder.data_completeness` / `status_reason`（`db.py:757-758`）、`payload_json`、`payload_hash`；快照同构（`status` 默认 `observed`，`raw_ref`）。
- run 本身：`ExternalSyncRun`（`db.py:394`）含 `platform/sync_type/source_mode/status/total/inserted/no_op/conflict`，workspace 隔离约束 `uq_external_sync_run_workspace_id` 在（`db.py:397`）。
- 注意：`no_op/conflict` 行**不落库**（`external_sync.py:747` ingest 只对新建行 append snapshot_id），所以「入库状态」列对成功行是行上既有字段（status/完整度），不是 inserted/no_op 重放。

### 1.2 异常明细：脏行目前根本不落库（本片成立的前提改动）

- 现状全链路是**整文件全成全败**：`parse_upload` 任一行 normalize 失败 → ConnectorError → 整单 422（P6-S1 交付说明已声明「解析错误整单一条」，P6-S2 §30 残留「行级错误号留后」）。
- ingest 层同样没有 skipped 持久化：`ImportStats`（`external_sync.py:148`）只有计数，无脏行内容；全库 grep `skipped/rejected/dirty/error_rows` 零命中。
- **结论**：「导出被跳过的脏数据行」隐含两个前置改动——① `imports.py`（P6 自己的模块，非旧业务逻辑）解析改行级容错；② 脏行原文+原因落库（新表），否则批次提交后异常明细无处可查。这两点是本片的真实工作量所在，Q1/Q2 即为此设。

### 1.3 生态事实

- 建表走 Alembic（`db.py:1213` `init_db`：无 alembic_version 的空库 `upgrade head`，legacy 走 state-aware 初始化器）→ 新表**必须**配 migration，`Base.metadata.create_all` 只兜测试库。
- 库内无 AuditLog/审计表（grep `audit` 零命中）→ Q3。
- 导出纯标准库可达：`csv` + `io.StringIO`，BOM = 内容前缀 `\ufeff`（与前端模板下载同法，P6-S1 已验证国内 Excel 兼容）。**零新依赖**，pandas/openpyxl 禁入。
- 批次历史在 `/sources` 页既有 runs 表（`GET /api/external/sync/runs`），前端已按 `source_mode=csv` 过滤展示——按钮挂这里，天然只对 CSV 批次出现。

## 2. 方案

### 2.1 后端：新表 `external_import_rejections`（Q2）

```
id / workspace_id(FK, index) / run_id(String64, index) / kind(String16)
row_number(Integer)         # 1-based 数据行序号（表头不计）
raw_data(Text)              # 原始行 JSON：{原始列头: 原文}，列序即上传列序（商家改完重传的凭据）
error_code(String64) / error_reason(String500)
created_at
幂等：uq (workspace_id, run_id, row_number) —— 同文件重传 no_op 时 rejected 不重复膨胀（E-9）
```

- migration：`alembic/versions/` 新增一个 revision（仅 add this table）。
- `db.py` 加模型类；**不改任何既有模型/表**。

### 2.2 后端：`imports.py` 行级容错（Q1 语义落位）

- `parse_upload` 增加 `row_errors` 收集：单行 normalize 抛错 → 记 `(row_no, reason_code, reason_message, raw_row)`，**继续**处理后续行；全坏才抛错。
- 预览响应新增 `rejected_rows: [{row_no, reason_code, reason_message, raw_row}]`（只读，不落库）——商家在预览阶段就能看到坏行。
- commit 分叉：
  - **strict（默认）**：存在 `rejected_rows` → 422 `EXTERNAL_ROWS_REJECTED`（message 含坏行数与首条原因），不建 run、不落 rejected 表。既有语义不变，C 系列全绿。
  - **allow_skipped=true（表单勾选）**：坏行剔除，好行照常走既有 ingest 建批；rejected 行随 run 落 `external_import_rejections`；`ExternalSyncRun` 复用既有 `error_code/error_message`？否——加**响应/schema 字段** `rejected_count`（`GET /api/external/sync/runs` 响应模型加字段，additive）；run 仍 `succeeded`（有好行）或 `failed`（全坏）。
- 缺必填**列**（Q6-S2 语义）仍走 `missing_required_fields` / `EXTERNAL_MISSING_FIELDS`，与本条（缺字段值的**行**）不同层，互不干扰。

### 2.3 后端：导出端点（用户钦定形态）

`POST /api/external/imports/{run_id}/export?view=raw|rejected`

- 权限 `catalog.read`；run 不存在或**不属于 principal 的 workspace** → 404（不泄露存在性）。
- `run.source_mode != "csv"` → 422 `EXPORT_NOT_CSV_IMPORT`（Shopify live 批次走既有页面看板，不入本端点范畴）。
- `view=raw`：按 `run.sync_type` 取对应表（inventory→snapshots / orders→orders），`sync_run_id == run.run_id`，按 id 排序。列头 = 该 kind 的标准导出列（显式列清单，不用 `vars()` 全倒）+ `入库状态`（status）、`完整度`（data_completeness）、`备注`（status_reason）。
- `view=rejected`：查 `external_import_rejections`，列 = `row_no/reason_code/reason_message` + `raw_row_json` 展开为列。空明细 → 仍返回仅含列头的合法 CSV（前端按 0 字节行处理提示「该批次无异常行」）。
- 生成：`io.StringIO` + `csv.writer`；内容前缀 `\ufeff`（UTF-8 BOM，防国内 Excel 乱码，用户钦定）。
- 响应：`Response(content, media_type="text/csv; charset=utf-8")` + `Content-Disposition: attachment; filename="wb-export-<run_id>-<raw|rejected>.csv"`（run_id 已是安全字符集，仍做白名单校验）。
- **actor 归因（Q3）**：出文件前 `logger.info("export.csv", extra={actor, workspace_id, run_id, view, rows})`；复用现有 logging 配置，不新建审计表。

### 2.4 前端：`/sources` 批次历史加两按钮

- 仅 `source_mode=csv` 的 run 行显示「导出结果」「导出异常」。
- 下载方式：**fetch + Blob**（推荐并采纳为定案）——复用页面既有 `api()` 封装（带鉴权、能解析 4xx JSON 错误并 toast 出来）；form 表单提交无法优雅处理鉴权失败与错误响应。流程：fetch → 404/422 时读 JSON 报错 → 200 时 `response.blob()` + `URL.createObjectURL` + `<a download>` 点击后 revoke。
- 文件名取响应 `Content-Disposition`，取不到则回退 `wb-export-<run_id>-<view>.csv`。
- rejected 视图 0 行：toast 提示「该批次无异常行」不触发下载。

## 3. 红线（守卫）

1. **不碰旧业务逻辑**：`connectors.py`、`external_sync.py` 的 ingest/begin/complete/fail 一律零改动；容错只在 `imports.py`（P6 自己的模块）。「仅做查询导出」对端点成立——导出路径只有 SELECT。
2. **严禁 pandas/openpyxl**：E-11 测试断言 `app/` 内不出现这两个 import。
3. 既有 556 条测试零改动预期达成（strict 默认 = 现状语义）；若核定中发现既有断言与本设计冲突，先登记再改，禁止静默改测。
4. live 红线不涉：`.env` `SHOPIFY_LIVE_ENABLED` 不动；导出端点不触碰任何 live 通道代码。
5. workspace 隔离与既有范式一致：一切查询带 `workspace_id = principal.workspace_id`。

## 4. 测试计划（E-1..E-12，基线 556 → 预期 ~568）

- **E-1** raw 导出：CSV 批次入 2 行 → 导出含 BOM 头、标准列头、2 数据行、入库状态列。
- **E-2** rejected 导出：allow_skipped 批次混 1 坏行 → 异常视图含 row_no/reason/raw 列展开。
- **E-3** BOM：两视图响应体均以 `\ufeff` 开头（字节级断言）。
- **E-4** 权限与隔离：无 `catalog.read` → 403；他 workspace 的 run_id → 404。
- **E-5** 非 CSV run（mock/shopify_live 来源）→ 422 `EXPORT_NOT_CSV_IMPORT`。
- **E-6** actor 日志：caplog 捕获 export.csv 记录，含 actor 与 view。
- **E-7** strict commit 有坏行 → 422 `EXTERNAL_ROWS_REJECTED`，rejected 表零落库、run 未创建。
- **E-8** allow_skipped → rejected 落库、run succeeded、响应含 rejected_count。
- **E-9** 同文件重传（幂等 no_op）→ rejected 不重复膨胀（uq 约束生效）。
- **E-10** 预览返回 rejected_rows（strict 下也能看到坏行明细）。
- **E-11** 依赖守卫：`app/**/*.py` grep 不到 `import pandas|openpyxl`。
- **E-12** migration 存在性：alembic/versions 新 revision 含该表；测试库 create_all 建表成功。
- 前端契约并入 E-10 后追加断言：`sources.html` 含「导出结果/导出异常」字样与 fetch+blob 下载函数。

## 5. 交接五要素（实施者阅读）

- **目标文件**：`app/db.py`（新模型，加在 ExternalSyncRun 之后）、`alembic/versions/`（新 revision）、`app/imports.py`（行级容错 + rejected 收集）、`app/main.py`（导出端点 + commit 分叉 + runs 响应加 rejected_count）、`app/schemas.py`（Preview/Commit/Run 响应 additive 字段）、`app/templates/sources.html`（两按钮 + 下载 JS）、`tests/test_p6s3_data_export.py`（新文件）。
- **改什么**：仅上列；**不改什么**：`connectors.py`、`external_sync.py`、既有表结构、既有端点行为（runs 响应只 additive）。
- **验收**：E-1..E-12 全绿 + 全量 ≥568/0（标准前缀跑法，见 P6-S1 交付说明）。
- **已排除**：审计落库表（Q3）、GET 导出（用 POST 防浏览器直开 + URL 泄露 run_id 于日志）、前端 form 提交下载（无法处理鉴权错误）、按 `vars()` 全字段导出（列漂移失控）。
