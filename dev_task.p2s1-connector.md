# dev_task.p2s1-connector — P2-1 平台连接器生产化（注册表 + 能力真实化 + 协议收敛）

> 生成时间：2026-10-28
> 生成方式：WorkBuddy 只读调研（`app/platform_adapters/*`、`app/connectors.py`、`app/main.py` 构造点、`app/cli.py`、缺口矩阵 §6、入口就绪清单 B2 裁定）
> 执行角色：Claude Code（实施）；WorkBuddy（复核）
> 阶段归属：P2 主体 · 切片 1/4（P2-1 Connector）
> 上位依据：`docs/engineering-roadmap.md` P2 行；`docs/p2-entry-readiness-20261028.md` B2 裁定（四切片：P2-1 Connector → P2-2 source batch ∥ P2-3 质量 → P2-4 补偿）

---

## 1. 背景与目标

P2 主体四切片的第一片。现状：平台适配器**没有统一注册与构造入口**——`ShopifyReadOnlyAdapter`
在 `main.py` 4 处、`TaobaoAdapter` 1 处各自硬编码构造；对外能力声明
`connector_capabilities()` 是**写死的假数据**（`live_enabled:False / simulated:True` 对所有平台），
而演练已证明 shopify 可真实 live——能力端点在撒谎；`app/connectors.py` 里的
`PlatformConnector` 协议**零引用**（死代码），与 `ReadOnlyPlatformAdapter` 双协议并存。

本切片做完后：适配器的注册/发现/构造收敛到单一注册表；`GET /api/external/connectors`
与 CLI `capabilities` 返回**真实**能力状态；双协议收敛为一。
P2-2（source batch）与 P2-3（质量）都以注册表为依赖前提，本片是其地基。

**不做什么**（防止范围蔓延，必填）：
- **不做授权引用实体化**（凭证引用落库/external_platform_account 表）——涉及 db 模型 +
  migration + 安全语义，候选并入 P2-2（source batch 天然需要"哪个授权拉的"归因），
  待用户评审时裁定归属（见 §11 待定点 Q2）；
- **不给 taobao 接真实 transport**（保持 disabled-by-default 骨架语义不变）；
- **不动** sync 链路语义（R6 cursor / R4 resume / 预算 / 取消 / 留存全部原样）；
- **不做** migration、不加新 API 端点（只改既有 `/api/external/connectors` 的返回内容来源）；
- 不顺手重构 `app/connectors.py` 的 normalize 函数族。

---

## 2. 现状事实（Fact）🔒

> **R-20 强制**：每条事实附实测方法；实施前须整函数重读核实行号（下述行号为 2026-10-28 快照）。

| # | 事实 | 位置 / 实测方法 |
|---|---|---|
| F1 | `ShopifyReadOnlyAdapter(...)` 在 `main.py` 硬编码构造 **4 处**（preview live/fixture 2 处 + sync/dry-run 2 处） | `grep -n "ShopifyReadOnlyAdapter(" app/main.py` → `1774、1777、1935、2258`；实施前用 Read 整函数核实各处上下文与参数差异 |
| F2 | `TaobaoAdapter(enabled=True, simulated=True, transport=fixture_transport(...))` 硬编码 1 处（fixture 上传路径） | `app/main.py:3017`（grep `TaobaoAdapter(`） |
| F3 | `connector_capabilities()` 返回**写死常量**：对 PLATFORMS 六平台一律 `read_only:True, live_enabled:False, simulated:True`，与真实适配器状态脱节 | `app/connectors.py:480-482`（整函数已读）；`PLATFORMS = ("taobao","jd","pdd","douyin","amazon","shopify")` 在 `:14` |
| F4 | `GET /api/external/connectors`（权限 `inventory.read`）直接返回 F3 假数据 | `app/main.py:2992-2996`（整函数已读） |
| F5 | CLI `capabilities` 命令同样直接打印 F3 假数据 | `app/cli.py:396-398`（grep 已核） |
| F6 | `PlatformConnector` Protocol（含 `source_mode` 属性、`inventory_snapshots()/events()` 方法）**零引用**——仅定义处出现 | `app/connectors.py:180-189`；`grep -rn "PlatformConnector" app/ tests/` 仅命中定义行 |
| F7 | 真协议是 `ReadOnlyPlatformAdapter`（`base.py:28-35`），shopify/taobao 均按它实现 | `app/platform_adapters/base.py` 全文（35 行已读） |
| F8 | `tests/test_connectors.py:9` 断言 `all(item["read_only"] and not item["live_enabled"] and item["simulated"])`——**把假数据锁成了规格**，能力真实化后必须按"规格翻转"模式改写 | `tests/test_connectors.py:7-9`（grep 已核；与 P2-0b 项1 D-2 的"contract test 翻转为规格锁定"同型，教训见 §11 Q3） |
| F9 | 基线：全量测试绿（最近全绿 = R4 收口复核记录）；当前测试函数总数 **374** | `grep -c "^def test_" tests/*.py \| awk -F: '{s+=$2} END {print s}'`；精确 passed 数以开工当日 P3 前置校验实跑为准 |

**取证红线**：复制模板 v1.1 §2 原文（禁 `sed` 中段采样、控制流断言读整函数或跑探针、推断必须标注）。

---

## 3. 目标（Target）

**方案选择**：注册表形态二选一——
- **A（推荐）：轻量工厂注册表**。新增 `app/platform_adapters/registry.py`：
  `register_platform(platform, factory)` + `get_adapter(platform, **kwargs)` +
  `registered_platforms()`。shopify/taobao 的 factory 内聚各自构造参数（shopify 的
  live/fixture 分支逻辑从 main.py:1770-1778 原样搬入）。
  理由：最小增量、无新依赖、sync 链路零语义变化；B 方案（类注册表+插件式发现）
  对六平台规模过度设计。
- B：不做注册表，仅抽公共构造函数。被否决原因：P2-2/P2-3 需要"按平台发现与能力查询"
  的统一入口，散函数不满足。

**T1 注册表（方案 A）**：新建 `app/platform_adapters/registry.py`；
`main.py` 5 处构造（F1×4 + F2）改为经 registry；**构造参数逐字节等价搬移**，
sync/preview/dry-run 行为不变。

**T2 能力真实化**：`connector_capabilities()` 改为从 registry 读取真实状态：
- `shopify`：`live_enabled = settings.shopify_live_enabled and runtime_live_enabled()`、
  `simulated` 按传输形态、`read_only=True`；
- `taobao`：如实报 `live_enabled:False, simulated:True`（骨架未变）；
- jd/pdd/douyin/amazon：如实报"未实现"——**字段定死 `implemented:False`**（Q4 裁定，
  四平台条目一律携带）；
- **`test_connectors.py:9` 按规格翻转改写**（锁"真实反映状态"而非"全是 mock"），
  并在交付说明偏差节登记翻转记录。

**T3 协议收敛（Q1 裁定：删除）**：删除 `app/connectors.py:180-189` 的死协议
`PlatformConnector`（零引用，删除无行为影响）；`ReadOnlyPlatformAdapter` 为唯一适配器契约。

**方案不变式**：对外 API 路径、权限门槛、响应结构（除 capabilities 条目内字段真实化外）
逐字节不变；`AdapterPage` / `AdapterError` / R6 / R4 语义不动。

---

## 3b. 前置校验 🔒（开工前逐条跑，全绿才允许写代码）

| # | 前置条件 | 校验命令 | 期望 | 实测结果 |
|---|---|---|---|---|
| P1 | 工作区干净 | `git status --porcelain` | 空 | 待贴 |
| P2 | 无并行 AI CLI 进程 | `ls .git/*.lock 2>/dev/null; ps -W \| grep -Ei "claude\|codex\|cursor" \| grep -v grep` | 无 | 待贴 |
| P3 | 基线全量绿 | `env -u PYTHONPATH PYTHONDONTWRITEBYTECODE=1 /d/python/python/python.exe -m pytest -q -p no:cacheprovider` | 全绿（passed 数记录进 §7 基线栏） | 待贴 |
| P4 | F1–F6 事实成立 | 逐条跑 §2「实测方法」列命令 | 与 §2 一致 | 待贴 |
| P5 | `runtime_live_enabled` 存在且为热开关判定入口 | `grep -n "def runtime_live_enabled" app/runtime_flags.py` | 命中定义行 | 待贴 |
| P6 | 改动规模与预期一致（提交前跑） | `git diff --numstat`（暂存后 `--cached`） | 新增 ≈ registry(+~60) + tests(+~120)；修改 main.py/connectors.py/cli.py/test_connectors.py 少量行；**删除数非 0 必须解释**（仅 PlatformConnector 块 + test_connectors 断言改写允许有删除） | 待贴 |

红线照模板：任一不满足即停下报告；P1 失败不得 `git stash`。

---

## 4. 边界与红线 🔒

- **允许改**：
  - 新增 `app/platform_adapters/registry.py`；
  - `app/main.py`（仅 5 处构造点 + import，行号见 F1/F2；**不动路由与业务函数其余部分**）；
  - `app/connectors.py`（仅 `connector_capabilities()` + 删 `PlatformConnector` 块）；
  - `app/cli.py`（仅 :396-398 capabilities 命令的取数来源）；
  - `tests/test_connectors.py`（断言翻转）+ 新增 `tests/test_p2s1_connector.py`。
- **禁止改**：`app/external_sync.py`、`app/platform_adapters/shopify.py`、`taobao.py`、`base.py`
  （除非 import 调整的必要最小行）、`app/db.py`、`migrations/`、`app/identity.py`、
  `app/runtime_flags.py`、sync/resume/cancel/budget/retention 相关代码与测试——**不得顺手重构**。
- **数据/契约**：无 migration、无 db schema 变化；`GET /api/external/connectors` 响应
  item 内允许字段变化（真实化 + 可能新增 `implemented`），**数组长度与 platform 键值不变**；
  CLI `capabilities` 输出随之变化（属预期行为变更，交付说明须声明）。
- **残留登记**：见 §4b。

---

## 4b. 已知限制与残留登记 🔒

| 编号 | 限制/残留 | 影响面 | 本期处置 | 后续归属 |
|---|---|---|---|---|
| R-37 | `jd/pdd/douyin/amazon` 四平台在 PLATFORMS 常量中存在但无适配器实现，capabilities 中以未实现状态如实暴露 | 仅能力展示层；无运行时影响 | 接受并登记 | 有真实授权需求时再立项 |
| R-38 | 授权引用（凭证引用）仍未实体化：凭证仍在 `.env`/Settings 单文件态，无法回答"这次拉取用哪个授权" | P2-2 source batch 归因能力 | 接受并登记 | 候选并入 P2-2（用户裁定，Q2） |
| R-39 | 能力真实化的 `live_enabled` 判定含进程内热开关（`runtime_live_enabled()`），多 worker 部署下各进程状态可能不一致 | 多 worker / 多副本部署 | 接受并登记（单进程假设既有形态的延伸，非新增风险源） | P12 前随技术债"并发与多 worker"类统一外部化 |

**登记红线**：编号接现有序列（当前最大 R-36，本片从 R-37 起，不跳号不复用），同步写进
`docs/review-checklist-20261011.md` 缺陷台账。

---

## 5. 技术债盘点（B4 裁定：五类框架，本片内盘点，不单独排期）🔒

> 来源：`docs/p2-entry-readiness-20261028.md` B4（2026-10-28 用户裁定）。
> 每类只陈述现状证据与归属建议，**本片不实现任何一项**。

| # | 类别 | 现状证据 | 归属建议 |
|---|---|---|---|
| D1 | **并发与多 worker** | 热开关（`app/runtime_flags.py` Event）、cancel token dict、预算计时均为进程内存态；R-25/R-26 已登记 | P12 受控开放前外部化（DB/共享存储）；P2 内不排期 |
| D2 | **中断粒度** | 取消仅在页间/资源间检查点生效；当前页 transport 等待不可中断（`timeout×retries` + 退避上限 8s 兜底） | P2-2 live 验证时评估"够不够用"，够用即永久接受 |
| D3 | **可观察性** | 无 `/metrics`、无告警通道（缺口矩阵 A6/A7，P2 级）；R-36 取消 409 消歧（P3 观察挂起） | R-36 依赖评估是 P2-4 任务文件必答项；metrics/告警 P2 内不排期 |
| D4 | **审计与归因** | sync run 无 `actor`/`request_id` 落库（A2）；上游 request-id 未落库（A3） | **候选并入 P2-2**（source batch 天然需要 actor/来源归因）——待 Q2 一并裁定 |
| D5 | **进程与环境稳定性** | R-22 CLI 收割、R-19 flaky 判据、R-23 .env 纪律、R-29 并行写者检测 | 流程纪律已固化模板 v1.1，无工程排期项 |

---

## 6. 用例（最小 N 条）🔒

新增 `tests/test_p2s1_connector.py`，编号 C-1..C-10：

| # | 场景 | 期望 |
|---|---|---|
| C-1 | registry 注册并获取 shopify（fixture transport） | 返回适配器 `platform=="shopify"`、`read_only=True`、可调用 `fetch_inventory()` |
| C-2 | registry 获取未注册平台 | 抛 `AdapterError` 且 code=`CONNECTOR_UNKNOWN_PLATFORM`（不得返回 None） |
| C-3 | registry 对同平台重复注册 | **报错**（Q5 裁定：注册只在 import 期发生，覆盖是隐患），code=`CONNECTOR_DUPLICATE_PLATFORM`，测试锁死 |
| C-4 | capabilities 关闭态（默认 settings） | shopify 条目 `live_enabled=False`；taobao 条目 `live_enabled=False, simulated=True`（如实反映骨架） |
| C-5 | capabilities live 判定（monkeypatch settings + runtime flag，**不真连网**） | shopify 条目 `live_enabled=True` |
| C-6 | capabilities 未实现平台 | jd/pdd/douyin/amazon 条目带未实现标记（字段与 T2 定死的名字一致） |
| C-7 | `GET /api/external/connectors` 端到端 | 未登录 401、无 `inventory.read` 权限 403、登录后 items 与 registry 直查一致（含 shopify 真实状态） |
| C-8 | CLI `capabilities` 命令 | 输出 JSON 与 registry 直查一致（子进程或直接调函数断言） |
| C-9 | 5 处构造点改经 registry 后行为不变 | 既有 preview/sync/dry-run 相关测试**零改动全绿**（这是"逐字节等价搬移"的证明；若发现需改旧测试才能过，即构造不等价，停下报告） |
| C-10 | `PlatformConnector` 删除后 | `grep -rn "PlatformConnector" app/ tests/` 零命中；全量 import 无损 |

要求：每条可自动断言；C-5 不得发起真实网络请求。

---

## 7. 验证命令 🔒（R-15 强制前缀）

```bash
# 定向
env -u PYTHONPATH PYTHONDONTWRITEBYTECODE=1 \
  /d/python/python/python.exe -m pytest -q tests/test_p2s1_connector.py tests/test_connectors.py -p no:cacheprovider

# 全量（收口必跑）
env -u PYTHONPATH PYTHONDONTWRITEBYTECODE=1 \
  /d/python/python/python.exe -m pytest -q -p no:cacheprovider

# 死协议清除证明
grep -rn "PlatformConnector" app/ tests/ || echo "clean"

# 路由仍在
env -u PYTHONPATH /d/python/python/python.exe -c "import app.main as m; print(any(getattr(r,'path','')=='/api/external/connectors' for r in m.app.routes))"
```

PowerShell 等价写法照模板 §6。flaky 规则照模板（R-19：单跑通过+复跑全绿可登记，不得掩盖新失败）。

---

## 8. 完成标准 🔒

- [ ] §6 十条用例定向绿（贴条数）
- [ ] 全量绿：基线 = 开工当日 P3 实跑值 `__ passed / 0 failed`，收口应为 `基线+新增-翻转改写净变化 / 0 failed`（翻转只改断言不减覆盖，净变化须为正）
- [ ] `git diff --numstat` 与 §3b P6 预期一致；删除行全部可解释
- [ ] 无 migration、无 schema 变化（`git status` 不得出现 `migrations/`）
- [ ] R-37/38/39 已同步登记进 `docs/review-checklist-20261011.md`
- [ ] **`.testtmp/delivery.p2s1-connector.md` 已落盘**（§9 五节齐全）

---

## 9. 交付说明 🔒（R-17 强制，落盘式）

照模板 §8 原文执行：`# 交付说明：p2s1-connector`，五节（改动面/用例与结果/**偏差声明**/
残留与已知限制/证据路径）。**偏差声明必须覆盖**：① test_connectors 断言翻转记录（Q3 裁定授权）；
② 声明 `implemented:False`（Q4）与重复注册报错（Q5）实现与裁定一致。

---

## 10. 提交要求 🔒

- 独立提交，不混其他改动；信息格式：`p2s1: 连接器生产化（注册表+能力真实化+协议收敛，构造逐字节等价搬移）`。
- **R-34 分块条款**：按块执行（建议 4 块：①registry 实现+落盘 ②main.py 5 处改接+落盘
  ③capabilities 真实化+CLI+断言翻转+落盘 ④新测试文件+落盘），每块 <10 分钟、
  编辑即落盘、块完成写 `.testtmp/p2s1-chunk<N>.md`；疑似死亡先 `git status` 再判死；
  长验证（全量）留给复核方自跑。

---

## 11. 评审裁定记录（2026-10-28 16:15 用户五问全答，任务文件定稿）

| # | 问题 | 裁定 |
|---|---|---|
| Q1 | `PlatformConnector` 死协议处置 | **删除**（零引用，免双协议误导） |
| Q2 | 授权引用实体化 + 审计归因（D4）归属 | **并入 P2-2**（source batch 天然要"哪个授权/谁触发"）；P2-1 侧以 R-38 留痕 |
| Q3 | `test_connectors.py:9` 翻转授权 | **授权直接翻转并留痕**（按 D-2 教训写收敛后规格 + 交付说明登记翻转记录） |
| Q4 | 未实现平台表达字段 | **`implemented:False`**（四平台条目一律携带） |
| Q5 | registry 重复注册语义 | **报错**（`CONNECTOR_DUPLICATE_PLATFORM`） |

---

## 12. 复核清单（复核方用）

| 检查 | 判据 |
|---|---|
| 范围 | diff 与 §4 完全一致：`migrations/`/`db.py`/`external_sync.py`/适配器实现文件零改动 |
| 等价搬移 | 5 处构造点参数与原逐字节一致；C-9 旧测试零改动全绿 |
| 能力真实化 | C-4/C-5/C-6 语义正确；test_connectors 翻转有留痕；`/api/external/connectors` 契约外字段无意外变化 |
| 死代码 | C-10 grep clean |
| 独立验证 | 复核方自跑 §7 全部命令；全量绿数字与交付说明一致 |
| 台账 | R-37/38/39 已进复核文档；编号连续 |

---

## 附：模板固化条款索引

R-15（§7 前缀）、R-17（§9 落盘交付）、R-20（§2 实测红线）、R-27（§3b 前置校验）、
R-28（§4b 编号登记）、R-29（§3b P2）、R-34（§10 分块）——全文见 `dev_task.template.md` v1.1。
