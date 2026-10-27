# P2-0b 项 3：live 响应原样留存设计（评审中，未实现）

> 状态：**评审中** —— 依 `dev_task.p2b-audit-closure-and-retention.md` §4.3 门禁，本文件获用户评审
> 通过前不写任何实现代码。
> 作者：WorkBuddy，2026-10-27。上位依据：缺口矩阵 R3（P0-3）"live 响应无原样留存，出问题无法复现同一份数据"。

## 0. 一句话方案

在 `ShopifyLiveTransport.query` 成功返回前，把**原始响应字节**原样落盘到
`ARTIFACTS_DIR/shopify_sync/<run_id>/<request_id>.json`，`resource_status_json` 资源条目记引用与状态；
留存失败降级为告警，不影响同步成功。

## 1. 现状锚点（实测，行号对应 `aa76d91`）

| 事实 | 位置 | 含义 |
|---|---|---|
| live GraphQL 唯一出口 | `app/platform_adapters/shopify.py:48` `ShopifyLiveTransport.query` | 留存单点，天然覆盖 orders/inventory 全部页 |
| Protocol 定义 | 同文件 `:21` | fixture transport（`:83`）与 live transport 同签名，留存在 live 实现内不影响 fixture |
| 响应原样可得 | `:77` `response.json()` 前 `response.text` 即原始字节 | 当前只返回解析后 dict，`request_id` 已并入返回值 |
| `raw_ref` 合成串 | `:217` | 无正文，本设计为其补"可回放的正文" |
| 台账结构 | `app/external_sync.py:34-88` `_resource_entry` / `_save_resource_status` | P2-0a 已有 `pages_used/budget_seconds` 留痕先例，续用同通道 |
| artifacts 目录先例 | `app/config.py:18` + `app/main.py:2795`（`resolve_path(artifacts_dir)/content`） | 落盘布局有现成模式可循 |

## 2. 设计决策（按任务文件 §4.2 九问逐条）

### 2.1 留存点：transport 层（query 返回前）

- **理由**：单点覆盖所有资源与所有页；重试逻辑已在 transport 内，留存的是**最终成功响应**，
  同时记录 `attempt`（第几次尝试成功）与总耗时，诊断价值最大。
- **备选否决**：适配器/服务层留存 → 需要在 `_pages()` 循环内插桩，且拿不到原始 HTTP 层信息
  （状态码、耗时），退化为"留存解析后数据"，违背"原样"目标。

### 2.2 存储介质与布局：落盘（不改 DB schema）

```
{ARTIFACTS_DIR}/shopify_sync/{run_id}/{request_id}.json          # 原样响应体（字节级）
{ARTIFACTS_DIR}/shopify_sync/{run_id}/{request_id}.meta.json     # 元数据
```

meta 内容：`run_id`、`resource`、`request_id`、`attempt`、`http_status`、`elapsed_ms`、
`query`（GraphQL 文本）、`variables`、`captured_at`（UTC ISO8601）。

- **理由**：P2-0a 起的既定约束"不动 `db.py`"；大响应入 `resource_status_json` 会撑爆台账字段；
  artifacts 目录已有 `resolve_path` 先例。
- **文件名即索引**：`request_id` 贯穿（请求头 `X-Request-ID` 与台账/日志同名），按 run_id 目录聚合，
  无需额外索引表。查询某 run 全部留存 = 列目录。
- **备选否决**：入库新表 → 违反本切片 schema 约束，且留存的诊断数据没有业务查询需求，落盘足够。

### 2.3 留存内容

- **响应体**：`response.text` **字节级原样**写入（不重新序列化——重新序列化会破坏"原样"承诺）。
- 请求侧：GraphQL query 全文 + variables（variables 只含分页参数，无敏感值）。
- 传输侧：attempt、http_status、elapsed_ms、captured_at。

### 2.4 脱敏

- `access_token` 只存在于**请求头**，响应体与其无关，不落盘即自然满足；留存文件不记录任何请求头。
- 响应体**不做字段级脱敏**：对象是 Dev Store（`dianshang-dev.myshopify.com`，Partner 开发店），
  "原样"是本能力的核心价值，字段级脱敏会破坏回放保真。若未来接生产店，需重审本节（登记为前置条件）。
- 台账（`resource_status_json`）与日志中只出现文件引用，不出现响应内容。

### 2.5 体积与保留策略

- 单文件保护上限 **10 MB**：`page_size=50` 的实际响应远小于此，超限视为异常（store 协议变化），
  写入截断版本并在 meta 标 `truncated: true` + 台账 `retention_status="truncated"`，同步继续。
- run 级上限 = `max_pages`（既有）× 两资源，天然受控，不另设配额。
- 清理：**本期只定策略不实现**——按 `run_id` 目录整目录删除，保留期建议 30 天，后续运维切片落地。

### 2.6 失败语义：降级，不 fail run

- 磁盘写失败（空间/权限/路径）→ `logger.warning` + 该资源台账 `retention_status="failed"` +
  meta 不落盘，**同步照常成功**。
- **理由**：同步的可用性是业务主链路；留存是诊断能力，"留不成"不能反过来打断业务。
  台账可见性保证"留没留住"永远可审计。
- 反向问题（回放依赖留存而留存缺失）由 §2.7 的回放端显式报"无留存"覆盖。

### 2.7 关联与回放（本期最小形态）

- **关联**：`_resource_entry` 新增 `retention` 子对象：
  `{"status": "ok|failed|truncated", "files": <int>, "dir": "shopify_sync/<run_id>/"}`。
- **回放**：本期只做**读取**，推荐 CLI（`python -m app.cli retention list|get --run-id <id>`），
  不新增 HTTP 路由面（新路由要过鉴权/权限评审，本期收益不成比例）。
  "自动化重放"（把留存响应喂回适配器）**明确不在本期**，本设计保证的是"人在环上可取回同一份数据"。

### 2.8 测试策略（最小 4 例，进项 4 的测试文件）

1. live sync 成功 → run 目录下文件数 = 实际请求数，且文件内容字节级等于 fake transport 返回体。
2. 台账引用正确：`retention.status=ok`、`files` 计数、`dir` 指向存在目录。
3. 留存写失败（monkeypatch 写路径为不可写）→ run 仍成功，台账 `retention_status=failed`，日志有 warning。
4. fixture 路径**零留存**（文件目录不产生）——防止留存误入非 live 链路。

### 2.9 明确不做

定时清理实现、对象存储/远程归档、多平台留存抽象（只做 Shopify live）、自动化重放、
HTTP 回放路由、生产店字段级脱敏。

## 3. 影响面与回归

- 允许改动：`app/platform_adapters/shopify.py`（live transport 留存）、`app/external_sync.py`
  （台账 retention 字段）、`app/config.py`（如需 `retention_max_bytes` 常量——倾向硬编码 10MB 不进配置，
  待评审定）、新测试文件、CLI 回放读取。
- 禁改：`db.py`/`schemas.py`（无 schema 变更）；fixture transport 行为零变化（§2.8 用例 4 兜底）。
- 全量预期：项 2 完成后基线 +4（≥335 passed / 0 failed）。

## 4. 评审问题清单（请用户逐条给意见）

1. §2.2 落盘 + 不动 schema，是否接受？
2. §2.4 Dev Store 响应**不脱敏**（原样优先），是否接受？
3. §2.6 留存失败降级不 fail run，是否接受？
4. §2.7 回放本期只做 CLI 读取（不加 HTTP 路由），是否接受？
5. 体积上限 10MB 硬编码 vs 进配置（§3 影响面），倾向哪个？
6. 保留期 30 天（只定策略不实现），是否认可？
