# P0-0 发布候选审查记录

- 审查日期：2026-10-04
- 分支：`feat/warehouse-mvp`
- 当前状态：候选文件已识别，尚未 `git add`、commit 或 push
- 证据级别：E3（当前工作区自动化测试与隔离副本/临时 SQLite 迁移验证）

## 已验证事实

| 项目 | 结果 | 证据 |
|---|---|---|
| Alembic 唯一 head | 通过 | `python -m alembic heads` → `0018_repair_service_ticket_schema` (head) |
| 空库升级 | 通过 | 临时 SQLite `upgrade head` |
| 重复升级 | 通过 | 同一临时 SQLite 第二次 `upgrade head` |
| current | 通过 | `0018_repair_service_ticket_schema` (head) |
| legacy 修复 | 通过 | 临时 legacy 库执行 `python -m app.cli init-db`，升级至 head |
| 应用模块导入 | 通过 | `app.main`、assistant、business_dates、Shopify、MCP、内容生产等模块 import smoke |
| 隔离副本核心验证 | 通过 | 排除 `.git`、缓存、运行数据和 `.env` 后，核心导入通过；迁移重复升级通过；候选核心测试 15 passed |
| P0-0 定向回归 | 通过 | 59 passed |
| 全量回归 | 通过 | 205 passed，命令为 `PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider` |
| diff 格式检查 | 通过 | `git diff --check` |

## 运行时发布闭包（需用户审查后再暂存）

已跟踪代码直接依赖以下未跟踪运行文件：

- `app/analytics.py`、`app/dashboard.py`、`app/replenishment.py` → `app/business_dates.py`
- `app/main.py` → `app/assistant.py`、`app/platform_adapters/shopify.py`、`app/visual_workflows.py`、`app/workflow_schema.py`、`app/content_production.py`
- `app/background.py` → `app/content_production.py`
- `app/cli.py` → `app/catalog_live.py`、`app/catalog_replay.py`
- `tests/test_assistant.py` → `app/assistant.py`

上述模块继续依赖：

- `app/assistant.py` → `app/skills_ecommerce.py`、`app/skills_ecommerce_platform.py`
- `app/content_production.py` → `app/quality_gate.py`
- `app/quality_gate.py`、`app/visual_workflows.py` → `app/workflow_schema.py`
- `app/mcp_server*.py` → `app/skills_ecommerce*.py`

因此不能只纳入 `app/assistant.py` 或 `0016`；必须以完整闭包作为候选。

## 建议候选范围

### 运行时源码与资源

```text
app/assistant.py
app/business_dates.py
app/catalog_live.py
app/catalog_replay.py
app/content_production.py
app/mcp_server.py
app/mcp_server_official.py
app/platform_adapters/shopify.py
app/quality_gate.py
app/skills_ecommerce.py
app/skills_ecommerce_fixture.py
app/skills_ecommerce_platform.py
app/visual_workflows.py
app/workflow_schema.py
app/templates/content.html
app/templates/shopify.html
migrations/versions/0016_content_production.py
```

### 对应测试、fixture 和技术文档

```text
tests/test_assistant.py
tests/test_catalog_live.py
tests/test_catalog_replay.py
tests/test_ecommerce_analysis.py
tests/test_mcp_official.py
tests/test_mcp_server.py
tests/test_quality_gate.py
tests/fixtures/catalog_replay/
docs/catalog-live.md
docs/catalog-replay.md
docs/mcp-platform-integration.md
docs/mcp-server-runbook.md
docs/mcp-tools.md
docs/shopify-canary-runbook.md
docs/skills-ecommerce-analysis.md
```

`docs/interview-demo-script.md` 属于演示材料，不是运行时依赖；是否纳入版本由用户决定。

## 不应自动进入运行时发布候选

```text
app/schemas.recovered.py
面试/
问题解决资产库/
CLAUDE.md
```

这些文件可能有个人工作、方法论或恢复过程价值，但不是当前运行时闭包。尤其不能因为它们存在于工作区，就把它们当作已审查、已发布能力。

## 未完成门禁

- 已在隔离副本中完成核心导入、空库重复迁移和候选核心测试验证；完整 clean clone 安装/发布流程仍未执行。
- 候选文件尚未由用户确认，因此没有执行 `git add`。
- 未验证 MySQL、offline SQL、真实 Shopify 生产店铺、多租户 OAuth 或任何平台写操作。
- `migrations.env` 不能作为普通 Python 模块直接导入；它必须在 Alembic 运行上下文中加载，正常 Alembic 命令已验证。
- 全量测试虽通过，但存在约 3118 条弃用警告，不作为 P0-0 失败条件；后续应治理 `datetime.utcnow()` 和 Starlette/httpx 兼容性。

## 下一步

用户确认候选范围后，才执行候选文件暂存或建立隔离副本验证；在此之前保持当前工作区改动原样，不删除、重置或覆盖任何文件。
