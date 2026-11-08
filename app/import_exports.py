"""P6-S3/P6-S4 报表批次导出规格：列清单 + xlsx/csv 生成。

展示层标签不散落在路由函数里，故独立成模块（与 column_mappings.py 同源思路）：

- `INVENTORY_EXPORT_COLUMNS` / `ORDER_EXPORT_COLUMNS`：原始事实层的显式列清单
  （不用 `vars()` 全倒，防模型演进导致列漂移）；
- `rejected_table`：异常明细，列头 = 商家原表列序 + 末尾「系统拦截原因」；
- `xlsx_download`：默认格式（用户裁定 P6-S4：异常单要能在 Excel 里排版编辑）——
  表头加粗+浅底、全表居中、按内容自适应列宽、拦截原因列加宽并自动换行、首行冻结；
- `csv_download`：保留为 `format=csv` 的机器可读通道（UTF-8 BOM，国内 Excel 直开不乱码）。

P6-S4 口径变更：P6-S3 的红线是「导出链路零重型依赖」，用户已明确改为复用
项目既有的 openpyxl（app/exports.py 同款依赖，requirements 早已存在）。
pandas 仍禁止（E-11 断言）。
"""
from __future__ import annotations

import csv
import io
import json
import unicodedata
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Iterable

from fastapi import Response
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

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

# 列宽策略：按内容自适应，夹在 [MIN_WIDTH, MAX_WIDTH]；宽列单独放宽。
MIN_WIDTH = 10
MAX_WIDTH = 40
WIDE_WIDTH = 60
WIDE_COLUMNS = frozenset({REASON_COLUMN, "备注"})

XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

_HEADER_FILL = PatternFill("solid", fgColor="F2F5F9")
_CENTER = Alignment(horizontal="center", vertical="center")
_WRAP_CENTER = Alignment(horizontal="center", vertical="center", wrap_text=True)


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


def _display_len(value: str) -> int:
    """显示宽度：中日韩全角字符按 2 计（openpyxl 列宽单位≈半角字符数）。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in value)


def _column_width(header: str, values: list[str]) -> int:
    if header in WIDE_COLUMNS:
        return WIDE_WIDTH
    longest = max([_display_len(header)] + [_display_len(value) for value in values])
    return min(max(longest + 4, MIN_WIDTH), MAX_WIDTH)


def xlsx_download(
    filename: str,
    headers: list[str],
    rows: list[list[str]],
    *,
    sheet_title: str = "导出结果",
) -> Response:
    """xlsx 附件响应：表头加粗+浅底、全表居中、自适应列宽、宽列换行、首行冻结。"""
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = sheet_title[:31]  # Excel 工作表名上限 31 字符

    sheet.append(headers)
    for name in sheet[1]:
        name.font = Font(bold=True)
        name.fill = _HEADER_FILL
        name.alignment = _CENTER
    for row in rows:
        sheet.append(row)

    # 全表内容居中；宽列（拦截原因/备注）额外自动换行
    for row_cells in sheet.iter_rows(min_row=2, max_row=max(sheet.max_row, 1)):
        for name in row_cells:
            header = sheet.cell(1, name.column).value
            name.alignment = _WRAP_CENTER if header in WIDE_COLUMNS else _CENTER

    for index, header in enumerate(headers, start=1):
        values = [str(row[index - 1]) if index - 1 < len(row) else "" for row in rows]
        sheet.column_dimensions[get_column_letter(index)].width = _column_width(str(header), values)

    sheet.freeze_panes = "A2"

    buffer = io.BytesIO()
    workbook.save(buffer)
    return Response(
        content=buffer.getvalue(),
        media_type=XLSX_MEDIA_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"},
    )


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


def build_download(
    *,
    filename_base: str,
    view: str,
    headers: list[str],
    rows: list[list[str]],
    export_format: str = "xlsx",
    sheet_title: str | None = None,
) -> Response:
    """统一出口：xlsx（默认，Excel 友好）/ csv（机器可读）。

    ``sheet_title``：None = 沿用原判据（「异常明细」/「批次结果」）；
    显式传入则用传入值（P9-S2：利润导出的表名是「利润明细」，原先落到 else
    分支显示成「批次结果」）。默认值必须是 None——若给成 "导出结果"，
    批次导出的表名会从「批次结果」变成「导出结果」，属于未经裁定的行为变更。
    """
    if export_format == "csv":
        return csv_download(f"{filename_base}.csv", headers, rows)
    resolved_title = sheet_title
    if resolved_title is None:
        resolved_title = "异常明细" if view == "rejected" else "批次结果"
    return xlsx_download(
        f"{filename_base}.xlsx", headers, rows, sheet_title=resolved_title,
    )
