"""P6-S1/P6-S2/P6-S3 多平台报表导入：上传解码 + 列头映射 + 解析分发 + 行级容错。

零网络、零数据库副作用、零存储路径参与（文件内容不落盘）。
编码探测按 Q5 裁定：BOM 显式探测 → utf-8 → gb18030，全失败抛 ConnectorError。

P6-S2 列头映射：发生在 rows 进入 connectors 归一化**之前**——
映射后的行经 csv.DictWriter 重序列化回文本，再走既有 load_* 全链路
（首行兜底、orders lines 预处理、normalize 全部原样保留），connectors.py 零触碰。

P6-S3 行级容错（Q1 裁定 opt-in 的底座）：整文件解析失败时**不立刻放弃**，
改用二分隔离定位坏行——好行照常产出记录、坏行连同原始行原文进 rejected 列表。
二分只对失败分支付出代价（O(k·log n) 次子解析，k = 坏行数），全绿路径零额外开销。
strict / allow_skipped 的取舍由调用方（commit 端点）决定，本模块只如实报告。
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from typing import Any

from app.column_mappings import (
    apply_column_mapping,
    invert_mapping,
    missing_required,
    suggest_mapping,
)
from app.connectors import PLATFORMS, ConnectorError, load_orders, load_records

IMPORT_KINDS = ("inventory", "orders")
ENCODING_CANDIDATES = ("utf-8-sig", "utf-8", "gb18030")
ENCODING_ERROR_CODE = "EXTERNAL_INPUT_ENCODING"
MAPPING_CONFLICT_CODE = "MAPPING_CONFLICT"
ROW_ERROR_CODE = "EXTERNAL_ROW_INVALID"
# 单次上传最多收集的脏行条数（防坏文件把预览响应撑爆）；总数另计，不因截断失真。
MAX_REJECTED_ROWS = 500


def decode_upload(raw: bytes) -> tuple[str, str]:
    """按候选编码依次尝试解码；返回 (文本, 命中编码名)。

    utf-8-sig 是 utf-8 的超集（无 BOM 时等同），直接参与循环会让
    detected_encoding 恒为 utf-8-sig；因此先显式探测 BOM 前缀。
    """
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig"), "utf-8-sig"
    for encoding in ("utf-8", "gb18030"):
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    raise ConnectorError(ENCODING_ERROR_CODE)


@dataclass
class RejectedRow:
    """被跳过的一行：序号 + 拦截原因 + 上传原文（键序 = 上传列序）。"""

    row_number: int
    reason_code: str
    reason_message: str
    raw_data: dict[str, str]


@dataclass
class ImportOutcome:
    """解析结果；records 为 None 表示列头层缺必填（preview 返回 missing，commit 422）。"""

    records: list[Any] | None
    encoding: str
    mapping: dict[str, str] = field(default_factory=dict)   # {field: 原始列头}（对外展示方向）
    unmapped: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    csv_headers: list[str] = field(default_factory=list)
    rejected: list[RejectedRow] = field(default_factory=list)
    rejected_total: int = 0


def _csv_rows(text: str) -> tuple[list[str], list[dict[str, Any]]]:
    rows = list(csv.DictReader(io.StringIO(text)))
    headers = list(rows[0].keys()) if rows else []
    return headers, rows


def _serialize_csv(rows: list[dict[str, Any]], fieldnames: list[str]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, restval="", extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


def _load(kind: str, text: str, *, platform: str, defaults: dict[str, str | None]) -> list[Any]:
    if kind == "orders":
        return load_orders(text, platform=platform, source_mode="csv", defaults=defaults)
    return load_records(text, platform=platform, source_mode="csv", defaults=defaults)


def _isolate(
    mapped_rows: list[dict[str, Any]],
    fieldnames: list[str],
    *,
    kind: str,
    platform: str,
    defaults: dict[str, str | None],
    start: int,
    end: int,
    bad: list[tuple[int, Exception]],
) -> list[Any]:
    """对 [start, end) 区间做二分隔离；返回该区间内的有效记录，坏行索引进 bad。"""
    text = _serialize_csv(mapped_rows[start:end], fieldnames)
    try:
        return _load(kind, text, platform=platform, defaults=defaults)
    except ConnectorError as exc:
        if end - start == 1:
            bad.append((start, exc))
            return []
        mid = (start + end) // 2
        left = _isolate(mapped_rows, fieldnames, kind=kind, platform=platform,
                        defaults=defaults, start=start, end=mid, bad=bad)
        right = _isolate(mapped_rows, fieldnames, kind=kind, platform=platform,
                         defaults=defaults, start=mid, end=end, bad=bad)
        return left + right


def _raw_of(row: dict[str, Any]) -> dict[str, str]:
    """原始行 → 纯字符串字典；None 值补空串，多余列（DictReader 的 None 键）丢弃。"""
    return {
        str(key): "" if value is None else str(value)
        for key, value in row.items()
        if key is not None
    }


def parse_upload(
    raw: bytes,
    *,
    platform: str,
    kind: str,
    defaults: dict[str, str | None],
    column_mapping: dict[str, str] | None = None,
) -> ImportOutcome:
    """解码 + 列头映射 + 标准化；kind 仅支持 inventory / orders（P6-S1 Q4 裁定）。

    column_mapping：用户回传的 {真实CSV列名: 系统字段名}（v1.1 钦定方向）；
    None 时走自动建议。列头级错误（缺必填列/编码不可识别）照旧抛 ConnectorError；
    行级错误（值非法）不再整单失败——好行产出记录，坏行进 rejected（P6-S3）。
    """
    if platform not in PLATFORMS:
        raise ConnectorError(f"不支持的平台: {platform}")
    if kind not in IMPORT_KINDS:
        raise ConnectorError(f"不支持的导入类型: {kind}")
    text, encoding = decode_upload(raw)
    headers, rows = _csv_rows(text)
    if column_mapping is None:
        mapping, unmapped = suggest_mapping(headers, kind=kind)  # {field: 原始列头}
    else:
        mapping = invert_mapping(column_mapping)  # 同 field 两列 → ValueError MAPPING_CONFLICT
        unmapped = [header for header in headers if header not in column_mapping]
    missing = missing_required(mapping, kind=kind)
    if missing:
        return ImportOutcome(
            records=None, encoding=encoding, mapping=mapping,
            unmapped=unmapped, missing=missing, csv_headers=headers,
        )
    # 应用映射（列名 → field），再重序列化回 CSV 文本走既有 load_* 全链路。
    user_mapping = {column: f for f, column in mapping.items()}
    mapped_rows = apply_column_mapping(rows, user_mapping)
    fieldnames = list(mapping.keys()) + unmapped
    remapped_text = _serialize_csv(mapped_rows, fieldnames)
    rejected: list[RejectedRow] = []
    rejected_total = 0
    if not mapped_rows:
        # 只有表头没有数据行：不进 load_*（避免空区间递归），交由调用方报空文件。
        records: list[Any] = []
    else:
        try:
            records = _load(kind, remapped_text, platform=platform, defaults=defaults)
        except ConnectorError:
            # 整文件失败 → 二分隔离，区分「哪几行坏」与「其余行可用」。
            bad: list[tuple[int, Exception]] = []
            records = _isolate(
                mapped_rows, fieldnames, kind=kind, platform=platform, defaults=defaults,
                start=0, end=len(mapped_rows), bad=bad,
            )
            bad.sort(key=lambda item: item[0])
            rejected_total = len(bad)
            for index, exc in bad:
                if len(rejected) >= MAX_REJECTED_ROWS:
                    break
                rejected.append(
                    RejectedRow(
                        row_number=index + 1,
                        reason_code=str(getattr(exc, "code", ROW_ERROR_CODE)),
                        reason_message=str(exc)[:500],
                        raw_data=_raw_of(rows[index]),
                    )
                )
    return ImportOutcome(
        records=records, encoding=encoding, mapping=mapping,
        unmapped=unmapped, missing=[], csv_headers=headers,
        rejected=rejected, rejected_total=rejected_total,
    )
