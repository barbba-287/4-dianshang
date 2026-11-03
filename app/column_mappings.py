"""P6-S2 智能中文列头映射——字典唯一真源。

红线（任务文件 dev_task.p6s2-column-mapping.md §3）：
- 别名硬编码只准住在本模块；main.py / imports.py / sources.html 零别名。
- 解析器 connectors.py 零触碰：映射发生在 rows 进入 normalize 之前。
- 加载期自检：同一 kind 内一个归一化别名命中两个 field → 拒绝启动
  （防字典腐化导致数据张冠李戴）。

column_mapping 方向（用户 2026-11-03 开工提示钦定）：
    {真实CSV列名: 系统字段名}
前端 select 的 option value 即 CSV 原始列头原文，规避归一化/全角/空格导致的回传失配。
"""
from __future__ import annotations

import unicodedata
from typing import Any

# kind → field → 别名元组。field 自身的英文名也参与匹配，无需重复写进别名。
FIELD_ALIASES: dict[str, dict[str, tuple[str, ...]]] = {
    "inventory": {
        "external_sku": ("商品编码", "sku", "商家编码", "规格代码", "货号", "商品编号", "商品代码"),
        "account_ref": ("账号", "店铺账号", "店铺", "account", "账号标识"),
        "store_ref": ("店铺名称", "门店", "store", "分店"),
        "warehouse_ref": ("仓库", "仓库名称", "warehouse", "仓库/库位"),
        "available_qty": ("可售库存", "可用库存", "库存数量", "现货数量", "可售数量", "available"),
        "reserved_qty": ("占用库存", "锁定库存", "预留库存", "已占用", "reserved"),
        "inbound_qty": ("在途库存", "入库在途", "在途数量", "in_transit"),
        "as_of": ("快照时间", "统计时间", "盘点时间", "数据时间", "库存时间", "as_of", "截止时间"),
    },
    "orders": {
        "external_order_no": ("订单号", "订单编号", "单号", "订单ID", "order_no", "order_id", "external_order_no"),
        "account_ref": ("账号", "店铺账号", "店铺", "account", "账号标识"),
        "store_ref": ("店铺名称", "门店", "store", "分店"),
        "external_created_at": ("下单时间", "创建时间", "订单时间", "下单日期", "created_at", "付款下单时间"),
        "external_updated_at": ("更新时间", "修改时间", "最后更新", "updated_at"),
        "order_status": ("订单状态", "状态", "status"),
        "paid_at": ("付款时间", "支付时间", "paid_at", "付款日期"),
        "external_sku": ("商品编码", "sku", "商家编码", "规格代码", "货号", "商品编号", "商品代码"),
        "ordered_qty": ("数量", "购买数量", "商品数量", "下单数量", "qty", "quantity", "件数"),
        "gross_amount": ("实付金额", "订单金额", "总价", "买家实付", "应付金额", "金额", "amount", "price", "订单实付"),
        "refund_amount": ("退款金额", "退款", "refund_amount", "已退款"),
        "currency": ("币种", "货币", "currency", "币别"),
    },
}

# 必填（列头层判定）。account_ref 可由表单兜底（P6-S1 defaults 机制），不在此列。
REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "inventory": ("external_sku", "as_of"),
    "orders": ("external_order_no", "external_created_at"),
}


def normalize_header(text: str) -> str:
    """全角→半角（NFKC）→ casefold → 去除全部空白字符（用户钦定匹配规则）。"""
    normalized = unicodedata.normalize("NFKC", str(text)).casefold()
    return "".join(normalized.split())


def _build_reverse_index(kind: str) -> dict[str, str]:
    index: dict[str, str] = {}
    for field, aliases in FIELD_ALIASES[kind].items():
        for alias in (field, *aliases):
            key = normalize_header(alias)
            if key in index and index[key] != field:
                raise RuntimeError(
                    f"列头映射别名冲突（kind={kind}）: '{alias}' 同时命中 {index[key]} 与 {field}"
                )
            index[key] = field
    return index


# 模块加载期自检：别名冲突直接让进程起不来（防御性编程，评审裁定第 4 条）。
_REVERSE_INDEX: dict[str, dict[str, str]] = {kind: _build_reverse_index(kind) for kind in FIELD_ALIASES}


def suggest_mapping(headers: list[str], *, kind: str) -> tuple[dict[str, str], list[str]]:
    """返回 (mapping, unmapped_columns)。mapping: field → 原始列头（v1.0 语义，内部用）。

    同一字段多个列命中时取**第一个**，其余列进 unmapped（歧义取首列，D-8）。
    """
    index = _REVERSE_INDEX[kind]
    mapping: dict[str, str] = {}
    unmapped: list[str] = []
    for header in headers:
        field = index.get(normalize_header(header))
        if field is None or field in mapping:
            unmapped.append(header)
        else:
            mapping[field] = header
    return mapping, unmapped


def missing_required(mapping: dict[str, str], *, kind: str) -> list[str]:
    """必填字段未命中清单，按定义顺序返回。"""
    return [field for field in REQUIRED_FIELDS[kind] if field not in mapping]


def invert_mapping(mapping: dict[str, str]) -> dict[str, str]:
    """{真实CSV列名: field} → {field: 真实CSV列名}；同一 field 被两列声明 → ValueError。"""
    out: dict[str, str] = {}
    for column, field in mapping.items():
        if field in out:
            raise ValueError("MAPPING_CONFLICT")
        out[field] = column
    return out


def apply_column_mapping(rows: list[dict[str, Any]], mapping: dict[str, str]) -> list[dict[str, Any]]:
    """按用户回传的 {真实CSV列名: 系统字段名} 重命名行 key；返回新列表（不改入参）。

    同一 field 被两列声明 → ValueError("MAPPING_CONFLICT")（由调用方映射 422）。
    """
    seen: dict[str, str] = {}
    for column, field in mapping.items():
        if field in seen:
            raise ValueError("MAPPING_CONFLICT")
        seen[field] = column
    out: list[dict[str, Any]] = []
    for row in rows:
        new_row: dict[str, Any] = {}
        for key, value in row.items():
            field = mapping.get(key, key)
            # 原列名与目标字段同名（英文列直传）时不重复写入
            if field in new_row and not new_row.get(field):
                new_row[field] = value
            elif field not in new_row:
                new_row[field] = value
            else:
                new_row[key] = value
        out.append(new_row)
    return out
