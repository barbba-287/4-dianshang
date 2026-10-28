# 受控 live 演练手册（drill-runbook v1.0 草案，2026-10-28）

> **性质声明**：本文件是**操作手册**，不是实施任务文件——不含任何写代码动作，
> 执行人不调实施方、不改仓库。演练通过**不等于** live 默认放开；
> `SHOPIFY_LIVE_ENABLED` 是否常开由用户单独裁定。
>
> **Gate 0（执行前置，用户逐项确认后截图/记录留证）**：
> - G0-1 中转站 pro 档配额余量 **≥50 轮**（单位=轮，按次结算的"轮"；本手册执行本身
>   不耗 CLI 配额，配额全部留给缺陷应急修复——每次修复开工前重报余量）；
> - G0-2 **Shopify API 配额余量 ≥20 次调用**（演练全程含重试预计消耗 10~15 次
>   GraphQL 调用：同步分页 + capabilities + 一次重试余量；Dev Store 桶按分钟恢复，
>   低于 20 不开工）。**任何 API 调用失败（含 429 节流）不自动重试**——记录响应原文、
>   等桶恢复（Dev Store 每 分钟回补）后人工重发；禁止循环/退避脚本自动重试
>   （防止节流放大，消耗失控）。

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

### 步骤 3：切 live + 重启（**路线二选一**，任选其一并记录选择理由）

**路线甲（改 `.env`，R-23 全流程）**——推荐，状态持久、重启不丢：

```bash
# 只改 SHOPIFY_LIVE_ENABLED 一行 false→true；其余逐字节不动
grep -c "SHOPIFY_LIVE_ENABLED=true" .env   # 确认恰好 1 处
# 重启 uvicorn（杀掉旧进程再起）
```

**路线乙（仅环境变量，不碰 `.env`）**——更安全，`.env` 全程零改动、无需还原。
**启动命令逐字照抄**（shell 语法写死，两个 shell 二选一，与实际使用的终端一致）：

```bash
# Git Bash（本机默认终端）——单行，前缀=环境变量赋值：
SHOPIFY_LIVE_ENABLED=true "D:/python/python/python.exe" -m uvicorn app.main:app --port 8000
```

```powershell
# PowerShell（若用 PS 启动）——两行，$env: 前缀只对当前会话生效：
$env:SHOPIFY_LIVE_ENABLED = "true"
D:\python\python\python.exe -m uvicorn app.main:app --port 8000
```

> 注意：乙路线必须**先杀旧进程**再以上述命令起新进程；`&&` 链式写法在 PS 5.1 不可用
> （PS 里分开两条执行）。`.env` 保持 false 不动（步骤 9 无需还原 `.env`，只重启回基线）。
>
> **乙路线演练期间禁止重启**：热开关与运行态都是进程内存，重启即回落 false——
> 演练步骤 3~8 之间**任何情况下不得重启进程**（包括"顺手重启清状态"）。
> 若进程崩溃：**立即止损回步骤②**（关闭态基线回归确认 false 态完好），
> 再从头走 3~8；崩溃现场照常归档（崩溃本身可能就是登记项）。

**选择约束**：若走甲，步骤 9 必须做 `.env` 逐字节还原；若走乙，步骤 9 只需重启
（不带前缀）即可回 false。**两条路线禁止混用**（半改半不改 = 排查地狱）。

验证（两路线相同）：

```bash
curl -s http://127.0.0.1:8000/api/external/shopify/capabilities | tee "$EV/step3-capabilities.json"
# 预期：live_enabled: true
```

**止损**：进程起不来 / capabilities 仍 false → 若走甲立即还原 `.env`（`cp` + `cmp`），
若走乙直接不带前缀重启；**不允许带病继续**。

### 步骤 4：热开关状态**确认或跳过**（运行期，不落盘）

运行期开关（`app/runtime_flags.py`）**默认就是放行态**——步骤 3 后 capabilities 已返回
`live_enabled: true` 即含开关语义。本步只做**确认**：

```bash
curl -s http://127.0.0.1:8000/api/external/shopify/capabilities | tee "$EV/step4-confirm.json"
# capabilities live_enabled:true → 开关已确认放行，**跳过 POST**（少耗一次 Shopify 之外的调用）；
# 仅当此前被显式关过（capabilities false）才执行显式开启：
# curl -s -X POST http://127.0.0.1:8000/api/admin/live-switch \
#   -b <cookie> -H "X-CSRF-Token: $CSRF" -H "Content-Type: application/json" \
#   -d '{"live_enabled": true}' | tee "$EV/step4-switch-on.json"
```

（把显式 POST 留给步骤 8 的反向验证与急停 E1——避免"为开而开"多打一次接口。）

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

### 步骤 6：演练 B——中途取消（**三态拆 6a/6b/6c**，各留独立证据）

**6a 取消运行中 run（主场景）**：

```bash
# 后台发起 orders,inventory 同步（inventory 页数多，给取消留窗口），
# 立即对新 run_id 发取消：
curl -s -X POST "http://127.0.0.1:8000/api/external/sync/runs/<run_id>/cancel" \
  -b <cookie> -H "X-CSRF-Token: $CSRF" | tee "$EV/step6a-cancel-202.json"
# 预期：202 effective:true；随后查询 run：status=failed + SHOPIFY_SYNC_CANCELLED，
# 且 cursor_json 中已完成页的游标已落库（DB 查询或 cli）
```

**6b 取消不存在的 run**：

```bash
curl -s -o "$EV/step6b-cancel-404.json" -w "%{http_code}" \
  -X POST "http://127.0.0.1:8000/api/external/sync/runs/nonexistent000/cancel" \
  -b <cookie> -H "X-CSRF-Token: $CSRF"
# 预期：404 SYNC_RUN_NOT_FOUND
```

**6c 取消已终态 run**（用步骤 5 已 succeeded 的 run_id）：

```bash
curl -s -o "$EV/step6c-cancel-409.json" -w "%{http_code}" \
  -X POST "http://127.0.0.1:8000/api/external/sync/runs/<步骤5的run_id>/cancel" \
  -b <cookie> -H "X-CSRF-Token: $CSRF"
# 预期：409 SYNC_RUN_NOT_CANCELLABLE
```

**止损**：6a 返回 202 但 run 永远 running（>2 分钟无检查点生效）→ 停，登记（护栏未生效）；
6b/6c 状态码与预期不符 → 停，登记。

### 步骤 7：演练 C——断点续跑（R4 首次实弹，**cursor 空/非空两分支**）

先查 6a 新 run 的 `cursor_json` 判定走哪条分支：

```bash
python -m app.cli retention list --run-id <6a新run_id>   # 辅助；cursor 以 DB 查询为准
# DB：SELECT cursor_json FROM external_sync_runs WHERE run_id='<6a新run_id>';
```

**分支 7-A（cursor 非空：至少一个资源已有已取页记录）**——主路径：

```bash
curl -s -X POST "http://127.0.0.1:8000/api/external/sync/runs/<6a新run_id>/resume" \
  -b <cookie> -H "X-CSRF-Token: $CSRF" | tee "$EV/step7-resume-branchA.json"
# 预期：新 run attempt==2、retry_of_run_id==6a新run、resumed_from==6a新run；
# 有已取页的资源从落点 cursor 续（不重拉已取页）；
# 已取完（complete=true）的资源走 SKIP（响应体 resumed:"skip" + source_pages/source_fetched）。
```

**分支 7-B（cursor 空：`"{}"` 或无任何资源条目——取消发生在首个检查点之前）**：

若 6a 实际留下的 cursor 非空，7-B 用**手工构造**（真实 DB 插一条探针 run）：

```bash
"D:/python/python/python.exe" - <<'PY' | tee "$EV/step7B-seed.txt"
from sqlalchemy import text
from app.db import SessionLocal, ExternalSyncRun
from datetime import datetime
with SessionLocal() as db:
    ws = db.execute(text("SELECT id FROM workspaces LIMIT 1")).scalar_one()
    run = ExternalSyncRun(
        workspace_id=ws, run_id="drillprobe7b0000000000000000", platform="shopify",
        sync_type="shopify_live", account_ref="probe.myshopify.com",
        store_ref="dianshang-dev.myshopify.com", source_mode="shopify_live",
        simulated=False, status="failed", attempt=1,
        resource_status_json='{"schema_version":1}',
        retryable_resources_json="[]", cursor_json="{}", heartbeat_at=datetime.utcnow(),
    )
    db.add(run); db.commit()
    print("seeded:", run.run_id)
    # 复核断言：NOT_LIVE_RUN 闸门字段必须满足（main.py 前置判定：
    # platform=="shopify" 且 source_mode=="shopify_live"；模型无 is_live 字段，
    # source_mode 即 live 语义的唯一载体——此行若被改，resume 必 409 NOT_LIVE_RUN）。
    db.expire(run)
    assert run.platform == "shopify" and run.source_mode == "shopify_live", "seed 未过 live 闸门字段校验"
    print("live-gate fields OK:", run.platform, run.source_mode)
PY
```

```bash
curl -s -X POST "http://127.0.0.1:8000/api/external/sync/runs/drillprobe7b0000000000000000/resume" \
  -b <cookie> -H "X-CSRF-Token: $CSRF" | tee "$EV/step7-resume-branchB.json"
# 预期：**不是 409**（R4-6b 语义："{}" = 未开始，允许续跑 = 全量重拉 RESTART）；
# 新 run attempt==2、全量从第 1 页开始。
```

**7-B 构造件还原**（演练完立即执行，顺序不可颠倒）：

```bash
"D:/python/python/python.exe" - <<'PY' | tee "$EV/step7B-cleanup.txt"
from app.db import SessionLocal
from sqlalchemy import text
with SessionLocal() as db:
    # 先删子 run（有 retry_of_run_id 指向探针的），再删探针本体——FK 顺序。
    db.execute(text("DELETE FROM external_sync_runs WHERE retry_of_run_id='drillprobe7b0000000000000000'"))
    db.execute(text("DELETE FROM external_sync_runs WHERE run_id='drillprobe7b0000000000000000'"))
    db.commit()
    left = db.execute(text("SELECT count(*) FROM external_sync_runs WHERE run_id LIKE 'drillprobe7b%'")).scalar()
    print("remaining:", left)   # 预期 0
PY
```

> 还原失败（FK 残留）→ 不硬删，把残留 run_id 登记进 §15，留待统一清理。
> 7-B 的 resume 会真实拉取 Dev Store 数据（只读、幂等去重兜底），消耗计入 G0-2。
**止损**：409 reason 与实际状态矛盾 / 续跑把 pages 归零（分支 A 中 cursor 非空却从第 1 页重拉）→ 停，登记。

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

## 3. 全局急停三连（**E1→E2→E3 严格按序执行，每步带独立验证，禁止跳序**）

**E1：热开关关闭**（最快、进程不重启、立即生效）：

```bash
curl -s -X POST http://127.0.0.1:8000/api/admin/live-switch \
  -b <cookie> -H "X-CSRF-Token: $CSRF" -H "Content-Type: application/json" \
  -d '{"live_enabled": false}' | tee "$EV/estop-E1-switch-off.json"
# E1 验证 1a：上一步返回 live_enabled:false；
# E1 验证 1b：sync 立即被拒 → 预期 409 SHOPIFY_LIVE_NOT_CONFIGURED（存 $EV/estop-E1-denied.json）
```

**E2：`.env`/环境基线还原 + 重启**（E1 不够时执行）：

```bash
# 走甲：cp "$EV/env.backup.pre-drill" .env && cmp .env "$EV/env.backup.pre-drill" && echo restored
# 走乙：无需改 .env，直接不带前缀重启
# 重启后 E2 验证：
curl -s http://127.0.0.1:8000/api/external/shopify/capabilities | tee "$EV/estop-E2-capabilities.json"
# 预期：live_enabled: false（.env/无前缀基线回落）
```

**E3：杀 uvicorn 进程**（最后手段：进程失控 / E1、E2 无法执行时）：

```bash
ps -W | grep -i uvicorn          # 定位 PID
taskkill //PID <pid> //F         # 杀
ps -W | grep -i uvicorn          # E3 验证 a：无输出 = 进程已死
# 热开关是内存态：重启后必然回落 false。重启后 E3 验证 b 同 E2 验证。
```

> 顺序依据：E1 零副作用即时生效 → E2 消持久化状态 → E3 兜底。
> 禁止跳序（先杀进程会丢失"取消是否落库"等现场证据）。

## 4. 缺陷处置、分工、耗时、证据落库

### 4.1 缺陷处置

| 级别 | 判定 | 处置 |
|---|---|---|
| 阻断 | 前置门失效（关闭态能同步）、取消永不生效、续跑把 pages 归零、**任一权限异常（P-1/P-2/P-3，见下）** | **立即执行 E1→E2→E3**；登记新编号 R-3x；当天报告用户；未修复前不重跑 |
| 非阻断 | 数字偏差（inserted 与预期差 ≤1）、留存 meta 字段缺失、告警噪音 | 记入 CONCLUSION.md + 编号；演练可继续；汇总后统一裁定 |
| 观察 | 性能/时延、Dev Store 数据量级变化 | 只记录不处理 |

**三类权限异常（写死判定，出现任一 = 阻断级）**：

| 编号 | 异常 | 判定 |
|---|---|---|
| **P-1 权限提升** | 非 admin 会话触达 admin 接口 | operations 角色会话调 `POST /api/admin/live-switch` 返回**非 403** |
| **P-2 跨租户** | 越过 workspace 隔离触达他人 run | 以 workspace A 的登录会话对 workspace B 的 run 调 `cancel`/`resume` 返回**非 404**（正确行为 = 按"不存在"处理） |
| **P-3 写权限缺失** | 无 `inventory.write` 权限也能写外部事实 | 仅持 `inventory.read` 的会话成功发起 `sync`/`resume`（非 4xx） |

> 三类在演练中不主动构造（超出本手册范围），但**任何步骤观察到疑似行为即升级阻断**；
> 专项验证留待安全审计任务。

- 缺陷编号从 **R-35** 起（R-34 已被模板占用）；
- 修复属**实施任务**，走 dev_task 流程（R-34 分块），不在演练现场改代码；
- 修复消耗中转配额——**每次修复开工前向用户报当前余量**。

### 4.2 执行人分工

| 角色 | 职责 |
|---|---|
| **用户** | 执行人：所有手工操作（curl/CLI/`.env`/服务启停/急停）；Gate 0 配额确认与放行 |
| **WorkBuddy（复核方）** | 陪跑：逐步核对响应与台账数字、证据文件完整性、异常初步定位；不碰 `.env`、不发请求 |
| **实施方（claude CLI）** | 仅在缺陷裁定后按 dev_task 流程修复；不在演练中现场待命 |

### 4.3 预计耗时（单次完整通过）

| 段 | 步骤 | 估时 |
|---|---|---|
| 准备 | Gate 0 + 步骤 1-2 | 10 min |
| 切换 | 步骤 3-4 | 5-10 min（含重启） |
| 演练 | 步骤 5-7（A/B/C 三场景） | 20-30 min（含等待取消检查点与两分支复现） |
| 收尾 | 步骤 8-9 + 归档 | 10-15 min |
| **合计** | | **约 45-65 min**；含一次阻断缺陷返工按 +30 min 预留 |

### 4.4 证据落库（区分"入库摘要"与"本地原始件"）

- **入库（随 docs 提交）**：`docs/review-checklist-20261011.md` §15 演练记录——
  日期、九步逐条结果、run_id 清单、资源级数字（inserted/no-op/pages_used）、
  缺陷编号、结论（通过/不通过）；
- **本地留存（不提交、`.testtmp` 已被 gitignore 覆盖）**：`$EV/` 全部原始输出
  与 `artifacts/shopify_sync/<run_id>/` 留存文件——**live 响应原文与 cookie/CSRF
  证据一律不入库**（防凭证与业务数据泄漏）；
- 演练目录与台账 SQL 输出由 WorkBuddy 复核后写入 §15，用户过目后随阶段推送。

## 5. 边界与裁定留白

- 演练全程 `.env` 的 `SHOPIFY_LIVE_ENABLED` 最终**必须还原为 false**（走乙路线则验证基线回落）；
- 演练通过 → 只登记「演练通过、具备放开条件」，**不自行放开**；
- **演练通过 ≠ live 放开**：放开 = 用户单独裁定（届时才有"常开 + 监控"议题，
  含 R-25/R-26 多 worker 限制的处置）。
