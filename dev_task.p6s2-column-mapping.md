# dev_task.p6s2-column-mapping — P6-S2 智能中文列头映射（CSV Header Mapping）

> 生成时间：2026-11-03（**v1.1 定稿**：16:05 用户评审通过，三处调整全部采纳；
> **映射方向按用户开工提示修正**：`column_mapping` = `{真实CSV列名: 系统字段名}`，
> 前端 select 的 option value 即 CSV 原始列头原文，规避归一化/全角/空格导致的回传失配）
> 生成方式：WorkBuddy 只读调研（`app/imports.py`、`app/connectors.py:225-330`、
> `app/main.py` P6-S1 两端点、`app/templates/sources.html`）
> 执行角色：Claude Code（实施）；WorkBuddy（复核）——若 Claude Code 额度未恢复则 WorkBuddy 实施
> 阶段归属：P6 主体 · 切片 2（依赖 P6-S1 `bef6003`+`749e463`，零外部依赖）
> 上位依据：用户 2026-11-03 15:25 四点要求（映射字典 / 预览扩展 / 前端确认区 / 守卫红线）
> 测试基线：**543 passed / 0 failed**（P6-S1 收口 `749e463`）

## 0. 评审裁定记录（2026-11-03 16:05，全部采纳）

**目标一句话**：商家把平台后台导出的中文报表**原样拖进来**就能入库——系统能认的列自动认（绿勾），
认不出的列在预览区手动下拉指定；必填字段没着落就禁止入库。

1. preview 缺列 422 → **200 + `missing_required_fields`**（用户："预览返回 200 才能把控制权交给前端"）；commit 仍 422。
2. `account_ref` 特例维持（表单兜底优先）。
3. 预览 20 行维持；中文显示名放前端常量维持。
4. 加载期别名冲突自检维持。
5. **v1.1 修正（用户开工提示）**：`column_mapping` 方向 = `{真实CSV列名: 系统字段名}`；
   应用映射时同一 field 被两列声明 → 422 `MAPPING_CONFLICT`。
6. **v1.1 实施补充**：preview 响应增补 `csv_headers`（CSV 原始列头，按文件顺序）——
   映射确认区的下拉选项数据源，v1.0 漏列。

## 1. 现状事实（已亲验）

| 事实 | 位置 |
|---|---|
| CSV 上传 → `parse_upload` → `load_*` 全链路已通 | `app/imports.py`（P6-S1） |
| 归一化必填字段 | 库存 `app/connectors.py:230-232`：`external_sku`、`account_ref`、`as_of`；订单 `:294-314`：`account_ref`、`external_order_no`、`external_created_at`、非空 `lines`（CSV 扁平列时行内 `external_sku`+`ordered_qty` 必填，`cancelled/refunded_qty` 默认 0） |
| 表单兜底机制 | `load_*` 的 `defaults` 参数（行缺列时 setdefault）——`account_ref` 可由表单满足 |
| 预览响应 | `main.py` `/api/external/imports/preview`：`normalized_rows` 前 20 行、`errors`、`detected_encoding` |
| 缺必填列的现行行为 | **commit 端点 422**（C-7）；**preview 端点同样 422**——本片要改：预览阶段缺列不再 422，改为返回 `missing_required_fields` 让用户补映射 |
| 前端导入区 | `sources.html` `#import-form`：预览成功才解锁 `#import-commit`（Q6 两步） |
| 编码探测 | `imports.py decode_upload`：BOM→utf-8→gb18030 |

## 2. 方案设计

### 2.1 新模块 `app/column_mappings.py`（映射字典唯一真源，硬性要求）

```python
FIELD_ALIASES: dict[str, dict[str, tuple[str, ...]]]
# 结构：{kind: {field: (alias, ...)}}；kind ∈ {"inventory", "orders"}

REQUIRED_FIELDS: dict[str, tuple[str, ...]]
# inventory: ("external_sku", "as_of")   # account_ref 可由表单兜底，见 §2.4
# orders:    ("external_order_no", "external_created_at")

def normalize_header(text: str) -> str:
    """unicodedata NFKC（全角→半角）→ casefold → 去除全部空白字符。"""

def suggest_mapping(headers: list[str], *, kind: str) -> tuple[dict[str, str], list[str]]:
    """返回 (mapping, unmapped_columns)。mapping: field → 原始列头；
    同一字段多个列命中时取**第一个**，其余列进 unmapped。"""

def missing_required(mapping: dict[str, str], *, kind: str) -> list[str]:
    """REQUIRED_FIELDS[kind] 中未出现在 mapping 的字段，按定义顺序返回。"""
```

**别名清单（初版，实施时可增补）**——每条均须在测试中至少出现一次：

| field | 中文别名（示例） |
|---|---|
| `external_order_no` | 订单号 / 订单编号 / 单号 / 订单ID |
| `account_ref` | 账号 / 店铺账号 / 店铺 |
| `store_ref` | 店铺名称 / 门店 |
| `external_created_at` | 下单时间 / 创建时间 / 订单时间 / 下单日期 |
| `external_updated_at` | 更新时间 / 修改时间 |
| `order_status` | 订单状态 / 状态 |
| `paid_at` | 付款时间 / 支付时间 |
| `external_sku` | 商品编码 / SKU / 商家编码 / 规格代码 / 货号 / 商品编号 |
| `ordered_qty` | 数量 / 购买数量 / 商品数量 / 下单数量 |
| `gross_amount` | 实付金额 / 订单金额 / 总价 / 买家实付 / 应付金额 / 金额 |
| `refund_amount` | 退款金额 / 退款 |
| `currency` | 币种 / 货币 |
| `available_qty` | 可售库存 / 可用库存 / 库存数量 / 现货数量 |
| `reserved_qty` | 占用库存 / 锁定库存 / 预留库存 |
| `inbound_qty` | 在途库存 / 入库在途 |
| `warehouse_ref` | 仓库 / 仓库 / 仓库名称 |
| `as_of` | 快照时间 / 统计时间 / 盘点时间 / 数据时间 / 库存时间 |

英文别名（`sku`/`amount`/`qty`/`order_id` 等）同表配置——用户示例中 `amount`、`sku` 即此类。

### 2.2 匹配规则（用户钦定）

1. `normalize_header`：**全角→半角（NFKC）→ casefold → 去所有空白**；
2. 别名表在模块加载时构建 `normalize(alias) → field` 反向索引；
3. **加载期自检**：同一 kind 内一个归一化别名不得命中两个 field（重复 → 模块导入即报错，
   防止字典腐化）；
4. CSV 列头按原始文本保留（映射值用于重命名行 key），建议结果给**原始列头**。

### 2.3 预览接口扩展（`/api/external/imports/preview`）

请求新增表单字段：

- `column_mapping: str | None = Form(default=None)` —— JSON 对象 `{"field": "CSV原始列头"}`；
  由前端确认区回传。**仅在 `source_mode=csv` 路径生效**。

响应新增三键（向后兼容，只增）：

```json
{
  "mapping_suggestion": {"external_sku": "商品编码", "as_of": "统计时间"},
  "unmapped_columns": ["备注", "快递单号"],
  "missing_required_fields": ["as_of"]
}
```

**行为变化（关键）**：预览的列头检查提前到解析之前——

1. 先解码 CSV 取列头 → `suggest_mapping`（或采用用户回传的 `column_mapping`）；
2. `missing_required` 非空 → **不进 normalize**，直接 200 返回三键 +
   `normalized_rows: []`（这就是"让用户手动映射"的入口；P6-S1 的 422 行为仅剩 commit 端点保留）；
3. 列头齐全 → 应用映射（`row[new_field] = row.pop(csv_col)`）→ 走既有
   `parse_upload` → 200 返回解析结果；
4. 解析期错误（值非法）仍 422（与 P6-S1 一致）。

commit 端点同步接受 `column_mapping`：先应用映射再入库；缺必填仍 422（C-7 语义不变）。

### 2.4 `account_ref` 特例

`account_ref` 可由表单兜底（P6-S1 `defaults` 机制）。因此：

- `missing_required` 的判定排除 `account_ref`（它不在 `REQUIRED_FIELDS` 里）；
- 但 `mapping_suggestion` 仍会给出 `account_ref` 的建议命中（如果 CSV 里有该列）；
- 前端在 `#import-account` 输入框旁加提示：CSV 无账号列时必须填这里。

### 2.5 前端交互升级（`sources.html`）

预览成功后，在 `#import-result` 上方渲染**映射确认区** `#import-mapping`：

- 每个已知 field 一行：字段中文名 + 状态徽标 + `<select>`；
  - 自动命中 → 绿勾徽标（复用 `.badge-success`），select 默认选中建议列；
  - 未命中 → 中性徽标，select 默认「不映射」；
  - select 选项 = 全部 CSV 原始列头 + 「不映射」；
  - 非必填字段可不映射；**必填字段**（`REQUIRED_FIELDS`）未选中任何列时整行标警示；
- 区块底部「应用映射并重新预览」按钮：收集所有 select → `column_mapping` JSON → 重调 preview；
- `missing_required_fields` 非空（或必填 select 处于「不映射」）→ **禁用 `#import-commit`**；
- 「确认入库」提交时携带当前 `column_mapping`；
- 映射区在每次预览后重建；入库成功后清空。

字段中文名映射表放前端常量（`FIELD_LABELS`），与后端字典**无耦合**（前端不引后端模块）。

## 3. 守卫与红线（用户四点要求的第 4 点）

1. **字典独立模块**：`FIELD_ALIASES`/`REQUIRED_FIELDS` 只准住在 `app/column_mappings.py`；
   `main.py`/`imports.py`/`sources.html` **零别名硬编码**（测试 grep 验证：main.py 与
   imports.py 内不得出现「商品编码」「实付金额」等别名字面量）；
2. **既有 543 条测试零改动通过**；唯一行为变化是 preview 的缺列分支
   （422 → 200+missing），P6-S1 无此分支的测试，实证无冲突；
3. **不碰**：`app/connectors.py`（解析器零改动——映射发生在 rows 进入 normalize 之前）、
   `app/external_sync.py`、`app/external_orders.py`、`platform_adapters/`、`catalog_live.py`；
4. **零 migration、零新依赖**（`unicodedata` 标准库）；
5. 映射仅作用于 `source_mode=csv` 的导入端点；`json`/`mock`/`shopify_live` 路径零影响；
6. `normalize_header` 不得改变 CSV 原始列头对外展示（建议/确认区显示原文）；
7. 样式只加页面级 `<style>`；不动 `tokens.css`/`workspace-shell.css`/`ui-v2.css`（字节锁）。

## 4. 测试要求（新增 `tests/test_p6s2_column_mapping.py`，基线 543 → 预期 ~554）

| # | 用例 | 断言要点 |
|---|---|---|
| D-1 | 字典模块独立性 | `column_mappings.py` 存在且含 `FIELD_ALIASES`/`REQUIRED_FIELDS`；`main.py`/`imports.py`/`sources.html` 无别名硬编码字面量 |
| D-2 | 反向索引自检 | 同 kind 别名跨字段重复 → 导入报错（monkeypatch 字典构造冲突样例） |
| D-3 | 归一化（函数级） | 全角「ＳＫＵ」/含空格「商 品 编 码」/大小写「Sku」均命中 `external_sku` |
| D-4 | 自动建议 | 中文库存报表 → `mapping_suggestion` 全命中、`missing_required_fields==[]`、未知列进 `unmapped_columns` |
| D-5 | 缺必填列 → 预览 200 + missing | preview 缺 `external_sku` 列：200、`normalized_rows==[]`、`missing_required_fields==["external_sku"]`；**commit 同输入仍 422** |
| D-6 | 用户覆盖映射 | `column_mapping` 把「货号」指定给 `external_sku` → 解析成功、入库 `inserted==1` |
| D-7 | 中文全流程 | 中文列头 CSV：预览（建议）→ commit（带建议 mapping）→ 200、库内 `source_mode=="csv"` |
| D-8 | 歧义取首列 | CSV 两列都命中 `gross_amount`（如「总价」「金额」并存）→ 取先出现者，另一列进 unmapped |
| D-9 | 表单兜底互动 | CSV 无账号列 + 表单 `account_ref` → missing 不含 `account_ref`、入库成功 |
| D-10 | commit 带映射幂等 | 同一中文文件带 mapping 连传两次 → 第二次 `no_op` 全额（幂等不因映射破坏） |
| D-11 | 前端契约 | `/sources` 页含 `#import-mapping`、`FIELD_LABELS`、禁用钩子；映射区含必填字段行 |

**交付方须实测定向 + 全量并写实测数；全量用剥垫片标准前缀；禁止只跑定向。**

## 5. 已排除选项

| 选项 | 为什么不做 |
|---|---|
| 把映射做进 `connectors.py` 解析器 | 污染既有归一化层；live/json 路径也会被牵连；P6-S1 刚定的「解析器零改动」 |
| 别名放 JSON 文件 | 模块级 Python 常量自带加载期自检（D-2 可断言）与 IDE 可导航性；JSON 需自写校验器 |
| 后端强制"必须映射成功才能预览" | 与两步预览初衷相反——预览正是给用户补映射的地方 |
| 模糊/拼音匹配 | 误映射风险高；v1 只做精确归一化匹配，人工下拉兜底 |
| mapping 持久化（记住商家的映射习惯） | 引入新表 = migration + 隐私面；v2 候选 |
| 前端从后端拉字典接口 | 字典在前端只需 field→中文名 13 行常量；全量字典下发展示无价值 |

## 6. 交付清单（实施方必交）

1. `.testtmp/delivery.p6s2.md`：改动文件 + 行号、未做项、偏差
2. 定向 + 全量实测数与耗时；基线 543 → 预期 ~554（以实测为准）
3. 浏览器探针：中文报表截图落 `.testtmp/p6s1_shots/`（复用 P6-S1 目录）；
   若无法登录探针（P6-S1 先例），以 TestClient 契约替代并登记
4. 提交 `feat/warehouse-mvp`，**不 push**（用户裁定：P6-S1 的 3 笔先行留档）

## 7. 实施顺序建议（单切片，无需拆 a/b）

后端（column_mappings.py → imports.py 应用函数 → preview/commit 扩展 → D-1..D-10）
→ 前端（映射确认区 → D-11）→ 全量 → 提交。预计一段会话内完成。
