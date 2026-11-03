"""P6-S1/P6-S2 多平台报表导入：上传解码 + 列头映射 + 解析分发。

零网络、零数据库副作用、零存储路径参与（文件内容不落盘）。
编码探测按 Q5 裁定：BOM 显式探测 → utf-8 → gb18030，全失败抛 ConnectorError。

P6-S2 列头映射：发生在 rows 进入 connectors 归一化**之前**——
映射后的行经 csv.DictWriter 重序列化回文本，再走既有 load_* 全链路
（首行兜底、orders lines 预处理、normalize 全部原样保留），connectors.py 零触碰。
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
class ImportOutcome:
    """解析结果；records 为 None 表示列头层缺必填（preview 返回 missing，commit 422）。"""

    records: list[Any] | None
    encoding: str
    mapping: dict[str, str] = field(default_factory=dict)   # {field: 原始列头}（对外展示方向）
    unmapped: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    csv_headers: list[str] = field(default_factory=list)


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
    None 时走自动建议。解析错误（值非法/缺列）以 ConnectorError 抛出，由调用方映射 422。
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
    if kind == "orders":
        records: list[Any] = load_orders(remapped_text, platform=platform, source_mode="csv", defaults=defaults)
    else:
        records = load_records(remapped_text, platform=platform, source_mode="csv", defaults=defaults)
    return ImportOutcome(
        records=records, encoding=encoding, mapping=mapping,
        unmapped=unmapped, missing=[], csv_headers=headers,
    )
