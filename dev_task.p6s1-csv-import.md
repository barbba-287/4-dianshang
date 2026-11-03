# dev_task.p6s1-csv-import — P6-S1 多平台报表 CSV 导入通道

> 生成时间：2026-11-03（**v1.1 定稿**：用户 2026-11-03 14:57 指示"实施吧"，Q1–Q6 全部按推荐值裁定生效）
> 生成方式：WorkBuddy 只读调研（`app/connectors.py`、`app/external_sync.py`、`app/main.py`、
> `app/schemas.py`、`app/storage.py`、`app/templates/shopify.html`、`docs/v6.2-no-api-operations.md`）
> 执行角色：Claude Code（实施）；WorkBuddy（复核）
> 阶段归属：P6 主体 · 切片 1（零外部依赖，全程本地可验证）
> 上位依据：`docs/v6.2-no-api-operations.md`（"官方报表导出 = 无 API 商家首选降级方式"）、
> `docs/engineering-roadmap.md` P12（更多平台）、用户 2026-11-03 裁定"平台目的就是汇总"
> 测试基线：**528 passed / 0 failed**（P5-S7 收口 `b3bc6a2`）

## 0. 裁定记录（2026-11-03 14:57 用户"实施吧"= Q1–Q6 全按推荐值生效）

| # | 裁定 | 本片落位 |
|---|---|---|
| Q1 | /shopify 泛化为「数据源」，/shopify 保留 301 兜底 | 模板改名 `sources.html`；导航项 path `/sources` 文案「数据源」；三处空态链接同步改指 |
| Q2 | 允许 `platform=shopify` 走 CSV | 平台白名单 = `PLATFORMS` 全量 6 个 |
| Q3 | v1 只认标准英文列 + 前端模板下载 | 中文映射留 P6-S2 |
| Q4 | 只做 inventory + orders | `events` 后置 |
| Q5 | utf-8-sig → utf-8 → gb18030 自动探测 | 全失败 → 422 `EXTERNAL_INPUT_ENCODING` |
| Q6 | 强制两步：预览成功才可入库 | 前端提交按钮由预览成功解锁 |

**v1.1 修正一处 v1.0 设计**：`errors` 明细不逐行收集（orders 的 lines 预处理在 `load_orders` 内部，
逐行复刻会与既有实现漂移）。解析错误整单返回一条（含字段名/行号信息由错误消息自身承载），
响应字段仍为 `errors: [{message}]`。实施偏差在交付说明登记。

| # | 问题 | 推荐 | 影响面 |
|---|---|---|---|
| Q1 | **页面归属**：把 `/shopify` 泛化为「数据源」页，还是新开 `/sources`？ | **泛化 `/shopify`**：路由与导航文案改为「数据源」，`/shopify` 保留 301 → `/sources` 兜底旧链接。理由：用户已明确反对"每个源单开一页"，且 `/dashboard`、`/orders`、`/index` 的空态均硬链 `/shopify` | 导航、路由、模板、3 处空态链接 |
| Q2 | CSV 是否允许 `platform=shopify`？ | **允许**。Shopify 报表导出（`source_mode=csv`）与 live API（`source_mode=shopify_live`）在 `source_mode` 字段天然区分，不会污染事实层语义；`simulated` 亦按 mode 计算 | 平台白名单、质量报表展示 |
| Q3 | 列名映射策略 | **v1 只认标准英文列**（`external_sku`/`account_ref`/`as_of`/…），并提供模板 CSV 下载；中文列头映射（「商品编码」→`external_sku`）留 P6-S2 | 解析器、前端模板 |
| Q4 | 支持哪些 `kind` | **库存 + 订单**（`inventory`/`orders`）本片做；**事件 `events` 后置**（`ExternalEventIngestRequest` 现禁 csv，语义面更大） | 端点参数、解析分支 |
| Q5 | 编码支持 | **`utf-8-sig` → `utf-8` → `gb18030` 三档自动探测**，全失败报 `422 EXTERNAL_INPUT_ENCODING`（中文平台报表实际多为 GBK） | 解码函数、错误码 |
| Q6 | 是否强制"先预览再入库" | **是**。前端交互必须两步：预览（不写库）→ 用户确认 → 入库。后端也提供两个独立端点，不设隐式 dry_run | 端点数量、前端流程 |

## 1. 现状事实（已亲验，实施方无需重新调研）

### 1.1 解析层已完整支持 CSV —— 这是本片的最大利好

| 事实 | 位置 |
|---|---|
| 六平台常量 | `app/connectors.py:14` `PLATFORMS = ("taobao","jd","pdd","douyin","amazon","shopify")` |
| 模式含 csv | `app/connectors.py:15` `MODES = ("json","csv","mock")`；`:16` `NORMALIZED_SOURCE_MODES = MODES + ("shopify_live",)` |
| CSV 解码实现 | `app/connectors.py:417-426`（`csv.DictReader` + 首行兜底 platform/account_ref/store_ref/warehouse_ref/as_of） |
| 库存行标准化必填列 | `app/connectors.py:225-260`：`external_sku`、`account_ref`、`as_of`（或 `captured_at`）必填；`available_qty`/`reserved_qty`/`inbound_qty` 非负整数 |
| 订单 CSV 支持 | `app/connectors.py:341-355`：`lines` 列为 JSON 数组，或提供 `external_sku` 等扁平列自动组装单行 |
| 对外入口 | `app/connectors.py:455-466` `load_records` / `load_events` / `load_orders`，签名 `(content, *, platform, source_mode)` |

### 1.2 入库层已具备批次 + 幂等，无需新建

| 事实 | 位置 |
|---|---|
| 建批次 | `app/external_sync.py:622-673` `begin_sync_run(..., platform, sync_type, account_ref, store_ref, source_mode, simulated, actor, request_id)` |
| 批次范围校验 | `:720` `validate_inventory_batch_scope`；`:738` `validate_event_batch_scope` |
| 库存入库幂等 | `:747-812` `ingest_inventory`：同 `idempotency_key` + 同 `payload_hash` → `no_op`；不同 hash → `conflict`；**永不覆盖** |
| 事件入库 | `:813+` `ingest_events` |

### 1.3 既有 ingest 端点已是完整范式（照抄结构，勿重新设计）

| 端点 | 位置 | 可复用点 |
|---|---|---|
| `POST /api/external/orders/ingest` | `app/main.py:3033-3062` | 最完整范式：`load_orders` → `begin_sync_run` → `ingest_orders` → `complete_sync_run`；`ValueError` → 422 + `fail_sync_run`；其他异常 → 500 `EXTERNAL_SYNC_FAILED` + `fail_sync_run` |
| `POST /api/external/inventory/preview` | `app/main.py:3759-3777` | 预览端点范式（不写库） |
| `POST /api/external/inventory/ingest` | `app/main.py:3779-3796` | 走 `SyncApplicationService`（注意：**此路径与 orders 不同**，本片统一照 orders 范式手写，不用 Service，见 §3.3） |
| `POST /api/external/events/ingest` | `app/main.py:3797-3818` | 幂等 + fail 兜底写法 |

**端点现有局限（本片必须解决）**：请求体 `content: str`，`max_length=2_000_000`（`app/schemas.py:800`）；
`platform` 正则不含 shopify（`:798`）；事件 `source_mode` 正则禁 csv（`:834`）。**这就是为什么必须新增 multipart 端点，而不是纯前端直连。**

### 1.4 文件大小闸已有现成实现

- `app/storage.py:66-73` `enforce_size_limit(content_length)` → 抛 `StorageError("FILE_TOO_LARGE", …)`
- `app/config.py:28` `upload_max_bytes: int = 20 * 1024 * 1024`
- 结论：CSV 上限**复用 20MB**，不得自造常量。

### 1.5 前端现状

- `/shopify` 路由：`app/main.py:601-604` → `_render_workspace_page("shopify.html", principal, "/shopify")`
- 导航项：`app/main.py:264` `("/shopify", "Shopify 接入", "◇", (ROLE_ADMIN, ROLE_OPERATIONS), "inventory.read")`
- 分组归属：`app/main.py:320` 的「运营分析」元组
- 模板 `app/templates/shopify.html` 共 **5985 字节**，长行内联（样式全在第 2 行 `<style>`，JS 全在末行 `<script>`）
- 现有能力：`#sync` 调 `POST /api/external/shopify/sync`，`#refresh` 拉真实预览，`#cards` 四个计数卡
- **无任何文件上传 UI、无批次历史**

### 1.6 空态硬链 `/shopify` 的三处（Q1 若改路由必须同步）

- `app/templates/dashboard.html:39`（`去 Shopify 同步到外部事实层`）
- `app/templates/index.html:525`（同上）
- `app/templates/orders.html:7`（`去 Shopify 同步订单`）

## 2. 目标与非目标

### 目标

商家在「数据源」页上传淘宝/京东/拼多多/抖音/亚马逊的报表 CSV → 系统按标准列解析 → 建来源批次 → 写入外部事实层 → 驾驶舱汇总视图里与 Shopify 数据并列出现，且**显式标注来源与数据延迟**。

### 非目标（本片明确不做）

- 不做中文列头自动映射（Q3）
- 不做 Excel `.xlsx` 解析（只收 `.csv`；`openpyxl` 已在依赖里但不在本片范围）
- 不做事件流导入（Q4）
- 不做定时/文件夹监听导入
- 不做平台写回、不做 OAuth
- 不改 `Simulated`/`data_completeness` 既有语义
- **不碰 Shopify live 通道**（`platform_adapters/`、`catalog_live.py` 零改动）

## 3. 后端设计

### 3.1 新模块 `app/imports.py`（约 90 行）

```python
def decode_upload(raw: bytes) -> str:
    """utf-8-sig → utf-8 → gb18030 依次尝试。全失败抛 ConnectorError('EXTERNAL_INPUT_ENCODING')。"""

def parse_upload(*, raw: bytes, platform: str, kind: str, defaults: dict[str, str | None]) -> list:
    """kind=inventory → load_records；kind=orders → load_orders。
    defaults 中的非空值在归一化前逐行 setdefault（不覆盖 CSV 已有值）。"""
```

`defaults` 注入方式：**给 `load_records` / `load_orders` 增加可选关键字参数 `defaults: dict | None = None`**
（`app/connectors.py:455-466`），在 `_decode_input` 返回 rows 后、`normalize_*` 之前逐行 `setdefault`。
**只增参数、默认 None = 现状行为逐字节不变**（既有 528 条测试必须零改动通过）。

### 3.2 两个新端点（`app/main.py`，追加在 `/api/external/inventory/preview` 段之后）

```
POST /api/external/imports/preview   multipart: file, platform, kind, account_ref?, store_ref?, warehouse_ref?
POST /api/external/imports           multipart: 同上
```

- 权限：`catalog.write`（与既有 ingest 一致，`require_principal(request, permission="catalog.write")`）
- `kind` 白名单：`inventory` | `orders`；其他 → 422
- `platform` 白名单：`PLATFORMS`（Q2 若通过则含 shopify）
- 大小：`enforce_size_limit(len(raw))`，`StorageError.code == "FILE_TOO_LARGE"` → **413**
- 文件名：仅用于错误提示；**绝不参与任何存储路径**（与文档上传同红线）
- preview 响应：`{platform, kind, total, normalized_rows: [...前 20 行], errors: [{row, field, message}], detected_encoding}`
- ingest 响应：`{platform, kind, total, inserted, updated, no_op, conflict, sync_run_id, sync_status, detected_encoding}`
- 入库路径**逐段照抄 `main.py:3033-3062`**：`load → begin_sync_run → ingest_* → complete_sync_run`，
  `except ValueError` → `fail_sync_run` + 422；`except Exception` → `fail_sync_run` + 500 `EXTERNAL_SYNC_FAILED`
- 批次归因：`begin_sync_run` 已从请求上下文取 `actor`/`request_id`，**不得显式覆盖**

### 3.3 为什么不用 `SyncApplicationService`

`main.py:3789` 的 inventory ingest 走 Service，但 orders ingest（`:3041`）是手写范式。
本片要同时支持两种 kind 并复用同一错误映射，**统一照 orders 手写范式**，避免 Service 层二次抽象。
不得修改 `sync_application.py`。

### 3.4 错误码表（不得自造）

| 场景 | 码 | HTTP |
|---|---|---|
| 缺文件 | `NO_FILE` | 400 |
| 文件超 20MB | `FILE_TOO_LARGE` | 413 |
| 编码三档全失败 | `EXTERNAL_INPUT_ENCODING` | 422 |
| 列缺失 / 类型非法 / 平台不支持 | 沿用 `ConnectorError` 文案，码 = `str(exc)` | 422 |
| 入库期非预期异常 | `EXTERNAL_SYNC_FAILED` | 500 |

## 4. 前端设计（`app/templates/shopify.html` → 数据源页）

保持单文件长行内联写法（本项目无 Jinja2，`_render_workspace_page` 只做 sidebar 正则替换，见 `main.py:328-338`）。

1. **页头**：标题「数据源」；副标题说明"Shopify 走实时接口，其他平台走报表导入，都进入同一外部事实层"
2. **源卡片区**（`#cards` 保留并扩到 5 项）：店铺 / 连接 / 商品 / 库存 / 订单，追加「最近导入批次」
3. **区块 A：Shopify 实时接入**（现有 `#sync`/`#refresh` 与三张预览表**原样保留**，仅挪进 `<section>`）
4. **区块 B：报表导入（CSV）**
   - 平台下拉（5 个非 Shopify 平台 + Shopify 报表 = 6 项，Q2 决定是否含 Shopify）
   - 类型下拉（库存 / 订单）
   - 文件选择（`accept=".csv,text/csv"`）+ 拖拽区（`dragover` 高亮）
   - 「预览」按钮 → `POST /api/external/imports/preview` → 展示前 20 行 + 错误明细 + 识别到的编码
   - 「确认入库」按钮 → **仅在预览成功后启用**（Q6）→ `POST /api/external/imports` → 成功后 `toast.success` 并刷新批次表
   - 「下载模板」：前端 `Blob` 生成标准列头 CSV，**不新增后端端点**
5. **区块 C：导入批次历史**：`GET /api/external/sync/runs`（既有）过滤 `source_mode == "csv"`，
   列：时间 / 平台 / 类型 / 总数 / 新增 / 跳过 / 冲突 / 运行 ID
6. 空态用既有 `.empty` 类（P5-S7 段 14 已提供圆柱图标）
7. 全部文案走中文；不写解释性长句（P5-S5 裁定：UI 不放口径说明）
8. 样式**只加页面级 `<style>`**，不改 `tokens.css` / `workspace-shell.css` / `ui-v2.css`

## 5. 铁律与禁区

1. **不动** `app/platform_adapters/`、`app/catalog_live.py`、`app/external_orders.py`、`sync_application.py`、`quality_gate.py`
2. **零 migration**（本片不建表、不改列）
3. **零 `DELETE`**
4. 既有 528 条测试 **零改动通过**；`load_*` 的 `defaults` 参数必须默认 None 且不改变既有调用结果
5. `CSV` 解析不得引入新依赖（标准库 `csv` 已够）
6. 文件内容**不落盘**（本片不做 raw 留存；`raw_ref` 沿用 `mock://{platform}/inventory/{index}` 格式）
7. 不得把 `#2563eb` 等裸色值写入模板（P5-S1 已收敛到 token；登录页为唯一历史例外）
8. 不改 `_PAGE_ANCHORS`（数据源页无锚点需求）

## 6. 测试要求（用例以基线 528 递增）

新增 `tests/test_p6s1_csv_import.py`：

| # | 用例 | 断言要点 |
|---|---|---|
| C-1 | 库存 CSV 正常入库 | 200；`inserted==3`；`sync_run_id` 非空；库内 3 条 `source_mode=="csv"` |
| C-2 | 同文件重复导入 | 第二次 `no_op==3` / `inserted==0`（幂等） |
| C-3 | 同 key 改数量 | `conflict==1`，且**库内值未被覆盖** |
| C-4 | GBK 编码 CSV | 解析成功，`detected_encoding=="gb18030"` |
| C-5 | UTF-8 BOM | 首列名不带 `\ufeff` |
| C-6 | 三档全失败（如 `utf-16` 二进制） | 422 `EXTERNAL_INPUT_ENCODING` |
| C-7 | 缺 `external_sku` 列 | 422，错误信息含字段名 |
| C-8 | 超 20MB | 413 `FILE_TOO_LARGE` |
| C-9 | 无 `catalog.write` 权限 | 403，且**零写入**（表计数不变） |
| C-10 | `kind` 非法 | 422，零建批次（`external_sync_runs` 计数不变） |
| C-11 | 订单 CSV 扁平列（`external_sku`） | 自动组装单行 `lines`，入库成功 |
| C-12 | 表单 `account_ref` 兜底 | CSV 无该列时用表单值，入库成功 |
| C-13 | preview 不写库 | 响应 200 且四表计数全不变 |
| C-14 | 批次可查询 | `GET /api/external/sync/runs` 返回该 run，`source_mode=="csv"` |
| C-15 | 页面契约 | `/sources` 200 且含 `#import-csv`、`#import-preview`、`#import-commit`、`#import-runs`；`/shopify` 301/302 → `/sources`（Q1 通过则测） |

实施方须在交付说明写明**实测通过数**；全量用项目标准前缀（剥 `PYTHONPATH` 垫片）。**禁止只跑定向不跑全量。**

## 7. 已排除选项（防止实施方重新论证）

| 选项 | 为什么不做 |
|---|---|
| 纯前端 FileReader + 直连既有 `/ingest` | 2MB 上限、无 GBK 探测、事件禁 csv、无错误行号 → 真实报表必挂 |
| 新建 `import_batches` 表 | `ExternalSyncRun` 已是批次概念，新建表等于造两套账 |
| 用 `SyncApplicationService` 统一 | 两种 kind 需同一错误映射；Service 层为库存专用 |
| 引入 `pandas` | 依赖过重，标准库 `csv` 足够 |
| 支持 xlsx | 需新解析 + 合并单元格语义，另立切片 |
| 中文列头自动映射 | 映射表需要与 5 个平台逐一核对，工作量超本片；v1 用模板约束列名 |
| 让 `/shopify` 与 `/sources` 双页面并存 | 用户已明确反对"每源一页"；两页会导致状态重复展示 |

## 8. 切片划分建议

| 切片 | 内容 | 预估 |
|---|---|---|
| **P6-S1a** | 后端：`app/imports.py` + `load_*` 加 `defaults` + 两个端点 + C-1..C-14 | 1 段 |
| **P6-S1b** | 前端：`/sources` 路由与导航、数据源页改造、模板下载、批次表、C-15 + 3 处空态链接改指 + 浏览器探针 | 1 段 |

**顺序强制**：a 收口后才起 b（与 P2-2/P2-3 同一纪律：共用 `main.py` 与解析层，禁并行）。

## 9. 交付清单（实施方必交）

1. `.testtmp/delivery.p6s1a.md` / `p6s1b.md`：改动文件 + 行号、未做项、偏差
2. 实测用例数（定向 + 全量），全量须写耗时
3. 浏览器探针截图落 `.testtmp/p6s1_shots/`
4. 提交到 `feat/warehouse-mvp`，**不 push**
