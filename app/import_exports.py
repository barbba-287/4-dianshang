"""P6-S3 报表批次导出规格：列清单 + CSV 生成（UTF-8 BOM）。

独立成模块的理由与 column_mappings.py 同源——**展示层标签不散落在路由函数里**：

- `INVENTORY_EXPORT_COLUMNS` / `ORDER_EXPORT_COLUMNS`：原始事实层的显式列清单
  （不用 `vars()` 全倒，防模型演进导致列漂移）；
- `rejected_table`：异常明细，列头 = 商家原表列序 + 末尾「系统拦截原因」；
- `csv_download`：统一 UTF-8 BOM 与附件响应头，写法对齐既有 `app/exports.py`
  （`utf-8-sig` + CRLF + 公式注入转义），但**不引用该模块**——它依赖 openpyxl，
  本片红线是导出链路零重型依赖，故公式转义在此本地实现（镜像 app/exports.py:77）。

零第三方依赖：只用标准库 csv / io。
"""
from __future__ import annotations

import csv
import io
import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Iterable

from fastapi import Response

INVENTORY_EXPORT_COLUMNS = (
    ("平台", "platform"), ("账号", "account_ref"), ("店铺", "store_ref"), ("仓库", "warehouse_ref"),
    ("商品编码", "external_sku"), ("可售库存", "available_qty"), ("占用库存", "reserved_qty"),
    ("在途库存", "inbound_qty"), ("快照时间", "as_of"), ("入库状态", "status"),
    ("来源模式", "source_mode"), ("接收时间", "received_at"),
)
ORDER_EXPORT_COLUMNS = (
    ("平台", "platform"), ("账号", "account_ref"), ("店铺", "store_ref"), ("订单号", "external_order_no"),
    ("订单状态", "order_status"), ("下单时间", "external_created_at"), ("付款时间", "paid_at"),
    ("实付金额", "gross_amount"), ("退款金额", "refund_amount"), ("币种", "currency"),
    ("数据完整度", "data_completeness"), ("备注", "status_reason"),
    ("来源模式", "source_mode"), ("接收时间", "received_at"),
)
REASON_COLUMN = "系统拦截原因"


def export_columns(kind: str) -> tuple[tuple[str, str], ...]:
    """kind → 列清单（label, 属性名）。"""
    return ORDER_EXPORT_COLUMNS if kind == "orders" else INVENTORY_EXPORT_COLUMNS


def cell(value: Any) -> str:
    """单元格序列化：None → 空串，时间到秒，Decimal/dict 走 str。"""
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if isinstance(value, Decimal):
        return str(value)
    return str(value)


def safe_cell(value: Any) -> str:
    """防电子表格公式注入（镜像 app/exports.py 的 _safe_csv_cell）。

    比原版多一条豁免：负数（`-5`）不转义——库存/退款金额的合法负值若被加前缀，
    商家改完重传会解析失败，违背异常明细"改完可重传"的目的。
    """
    text = cell(value)
    if text.startswith(("=", "+", "@")):
        return "'" + text
    if text.startswith("-") and not text[1:2].isdigit():
        return "'" + text
    return text


def raw_table(records: Iterable[Any], kind: str) -> tuple[list[str], list[list[str]]]:
    columns = export_columns(kind)
    headers = [label for label, _ in columns]
    rows = [[safe_cell(getattr(record, attr, None)) for _, attr in columns] for record in records]
    return headers, rows


def rejected_table(rejections: Iterable[Any]) -> tuple[list[str], list[list[str]]]:
    """异常明细：列头取原始行键序（= 上传列序），末尾追加拦截原因（含行号）。"""
    headers: list[str] = []
    parsed: list[tuple[dict[str, str], Any]] = []
    for item in rejections:
        try:
            raw = json.loads(item.raw_data)
        except (json.JSONDecodeError, TypeError):
            raw = {}
        entry = {str(k): "" if v is None else str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}
        for key in entry:
            if key not in headers:
                headers.append(key)
        parsed.append((entry, item))
    rows = [
        [safe_cell(entry.get(header, "")) for header in headers] + [f"第 {item.row_number} 行：{item.error_reason}"]
        for entry, item in parsed
    ]
    return headers + [REASON_COLUMN], rows


def csv_download(filename: str, headers: list[str], rows: list[list[str]]) -> Response:
    """CSV 附件响应：UTF-8 BOM（国内 Excel 直开不乱码）+ no-store。"""
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(headers)
    writer.writerows(rows)
    body = buf.getvalue().encode("utf-8-sig")
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"},
    )
