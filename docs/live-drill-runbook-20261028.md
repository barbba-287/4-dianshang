# 受控 live 演练手册（drill-runbook v1.0 草案，2026-10-28）

> **性质声明**：本文件是**操作手册**，不是实施任务文件——不含任何写代码动作，
> 执行人不调实施方、不改仓库。演练通过**不等于** live 默认放开；
> `SHOPIFY_LIVE_ENABLED` 是否常开由用户单独裁定。
>
> **Gate 0（执行前置，用户确认）**：中转站 pro 档配额余量 **≥15 次**（本手册执行本身
> 不耗 CLI 配额——全部是手工操作；配额留给演练中暴露缺陷时的应急修复）。
> **未确认前不得执行 §2 起的任何步骤。**

## 0. 演练目标与范围

| 项 | 内容 |
|---|---|
| 目标 | 在 Dev Store 上首次实弹验证 P2-0 全部护栏：热开关、双预算、可取消、留存、续跑（R4 首次实弹） |
| 对象 | `dianshang-dev.myshopify.com`（已证实为开发店，2026-10-26 `shop.json` 只读核实） |
| 不做 | 任何 Shopify mutation；不写 InventoryBalance/Transaction；不放开 live 常开 |
| 前置已具备 | S4 闸门 / R6 游标持久化 / R4 续跑 / 留存（P2-0b）/ 全量 381 passed |

## 1. 证据归档框架（先建后用，§2 起每步产出一并归档）

```bash
mkdir -p .testtmp/drill-evidence-20261028
EV=.testtmp/drill-evidence-20261028
```

- 每步的命令与输出 → `$EV/step<N>-<名称>.txt`（`tee` 落盘）；
- 每步结尾一句话结论（通过/异常+原因）追加到 `$EV/CONCLUSION.md`；
- 演练结束把 `$EV/` 整体留存，摘要登记进 `docs/review-checklist-20261011.md`（新 §15）。

## 2. 九步流程

### 步骤 1：环境预检与备份

```bash
cd /d/ai/dianshang
git status --short                    # 必须干净
ps -W | grep -Ei "pytest|uvicorn|claude|codex|cursor|code\b"   # R-29，必须无输出
cp .env "$EV/env.backup.pre-drill"    # R-23：.env 备份（演练会改 .env）
cmp .env "$EV/env.backup.pre-drill" && echo backup-ok
grep -c "SHOPIFY_LIVE_ENABLED=false" .env   # 确认当前基线 false
```

启动服务（另一个终端）：`uvicorn app.main:app --port 8000`；浏览器登录 admin，
F12 复制 `dianshang_csrf` cookie 值备用（runbook「live 热开关」节推荐取法）。
**止损**：git 不干净或有并行进程 → 停，不清场不硬跑。

### 步骤 2：关闭态基线回归（live=false 下的两个硬预期）

```bash
curl -s http://127.0.0.1:8000/api/external/shopify/capabilities | tee "$EV/step2-capabilities.json"
# 预期：live_enabled: false
curl -s -X POST "http://127.0.0.1:8000/api/external/shopify/sync?resources=orders" \
  -b <登录cookie> -H "X-CSRF-Token: $CSRF" | tee "$EV/step2-sync-denied.json"
# 预期：409 SHOPIFY_LIVE_NOT_CONFIGURED
```

**止损**：任一预期不符（关闭态能同步 = 前置门失效）→ **立即停**，登记新编号，不进步骤 3。

### 步骤 3：.env 切 live + 重启（R-23 全流程）

```bash
# 只改 SHOPIFY_LIVE_ENABLED 一行 false→true；其余逐字节不动
# 改完：grep -c "SHOPIFY_LIVE_ENABLED=true" .env 确认恰好 1 处
# 然后重启 uvicorn（杀掉旧进程再起）
curl -s http://127.0.0.1:8000/api/external/shopify/capabilities | tee "$EV/step3-capabilities.json"
# 预期：live_enabled: true
```

**止损**：进程起不来 / capabilities 仍 false → 还原 .env（`cp "$EV/env.backup.pre-drill" .env`
+ `cmp` 核对）再排查；**不允许带病继续**。

### 步骤 4：开热开关（运行期，不落盘）

```bash
curl -s -X POST http://127.0.0.1:8000/api/admin/live-switch \
  -b <cookie> -H "X-CSRF-Token: $CSRF" -H "Content-Type: application/json" \
  -d '{"live_enabled": true}' | tee "$EV/step4-switch-on.json"
# 预期：{"live_enabled": true, "source": "runtime_flag"}
```

### 步骤 5：演练 A——单资源小同步（orders only + 短预算）

先在 .env 预置 `SHOPIFY_SYNC_BUDGET_SECONDS=120`（随步骤 3 一起改，或此处重启一次）：

```bash
curl -s -X POST "http://127.0.0.1:8000/api/external/shopify/sync?resources=orders" \
  -b <cookie> -H "X-CSRF-Token: $CSRF" | tee "$EV/step5-sync-orders.json"
```

核对响应与台账：`run.status=="succeeded"`、`pages_used` 有值且 ≤ max_pages、
资源级 inserted/no-op 数字合理（Dev Store 现状订单 3 条）；
`python -m app.cli retention list --run-id <run_id>` 有留存放记录（runbook「响应留存」节）。
**止损**：status=failed → 看 error_code：BUDGET_EXCEEDED→预算内正常中止（记录后可调预算重试一次）；
其它 code → 停，登记。**遇 SHOPIFY_SYNC_IN_PROGRESS → 说明闸门误锁，停（新编号）。**

### 步骤 6：演练 B——中途取消

```bash
# 后台发起 orders,inventory 同步（inventory 页数多，给取消留窗口），
# 立即对新 run_id 发取消：
curl -s -X POST "http://127.0.0.1:8000/api/external/sync/runs/<run_id>/cancel" \
  -b <cookie> -H "X-CSRF-Token: $CSRF" | tee "$EV/step6-cancel.json"
# 预期：202 effective:true；随后查询 run：status=failed + SHOPIFY_SYNC_CANCELLED，
# 且 cursor_json 中已完成页的游标已落库（DB 查询或 cli）
```

对重复取消与终态 run 再各发一次：404（不存在 id）/ 409（已终态）各留证据。
**止损**：cancel 返回 202 但 run 永远 running（>2 分钟无检查点生效）→ 停，登记（护栏未生效）。

### 步骤 7：演练 C——断点续跑（R4 首次实弹）

```bash
curl -s -X POST "http://127.0.0.1:8000/api/external/sync/runs/<步骤6的新run_id>/resume" \
  -b <cookie> -H "X-CSRF-Token: $CSRF" | tee "$EV/step7-resume.json"
```

核对：新 run `attempt==2`、`retry_of_run_id==源`、`resumed_from==源`；
inventory 未取完则从落点继续（不重拉已取页）；已取完资源走 SKIP（响应体 `resumed:"skip"`）。
**止损**：409 reason 与实际状态矛盾 / 续跑把 pages 归零 → 停，登记。

### 步骤 8：关闭态回归（反向）

```bash
curl -s -X POST http://127.0.0.1:8000/api/admin/live-switch \
  -b <cookie> -H "X-CSRF-Token: $CSRF" -H "Content-Type: application/json" \
  -d '{"live_enabled": false}' | tee "$EV/step8-switch-off.json"
curl -s -X POST "http://127.0.0.1:8000/api/external/shopify/sync?resources=orders" \
  -b <cookie> -H "X-CSRF-Token: $CSRF" | tee "$EV/step8-sync-denied.json"
# 预期：409 SHOPIFY_LIVE_NOT_CONFIGURED（热开关关闭 = 立即拒绝，无需重启）
```

### 步骤 9：还原与归档

```bash
cp "$EV/env.backup.pre-drill" .env && cmp .env "$EV/env.backup.pre-drill" && echo env-restored
# 重启服务；capabilities 应回 live_enabled:false
```

归档：`$EV/CONCLUSION.md` 写九步逐条结论；`docs/review-checklist-20261011.md` 新增 §15
「受控 live 演练记录」（日期/九步结果/异常编号/留存目录路径）。

## 3. 全局急停（任一时刻可执行）

1. `POST /api/admin/live-switch {"live_enabled": false}`（即时生效，不重启）；
2. `cp "$EV/env.backup.pre-drill" .env` + 重启（回 .env 基线）；
3. 极端情况杀 uvicorn 进程（热开关是内存态，重启即回落 false）。

## 4. 边界与裁定留白

- 演练全程 `.env` 的 `SHOPIFY_LIVE_ENABLED` 最终**必须还原为 false**；
- 演练通过 → 只登记「演练通过、具备放开条件」，**不自行放开**；
- 放开 = 用户单独裁定（届时才有"常开 + 监控"议题，含 R-25/R-26 多 worker 限制的处置）。
