"""P6-S1 多平台报表导入：上传解码 + 解析分发。

零网络、零数据库副作用、零存储路径参与（文件内容不落盘）。
编码探测按 Q5 裁定：utf-8-sig → utf-8 → gb18030 依次尝试，
全失败抛 ConnectorError（码 EXTERNAL_INPUT_ENCODING）。
"""
from __future__ import annotations

from typing import Any

from app.connectors import PLATFORMS, ConnectorError, load_orders, load_records

IMPORT_KINDS = ("inventory", "orders")
ENCODING_CANDIDATES = ("utf-8-sig", "utf-8", "gb18030")
ENCODING_ERROR_CODE = "EXTERNAL_INPUT_ENCODING"


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


def parse_upload(
    raw: bytes, *, platform: str, kind: str, defaults: dict[str, str | None]
) -> tuple[list[Any], str]:
    """解码并标准化上传的报表；kind 仅支持 inventory / orders（Q4 裁定）。

    返回 (records, detected_encoding)；解析错误（缺列/类型非法）以
    ConnectorError 抛出，由调用方映射 422。
    """
    if platform not in PLATFORMS:
        raise ConnectorError(f"不支持的平台: {platform}")
    if kind not in IMPORT_KINDS:
        raise ConnectorError(f"不支持的导入类型: {kind}")
    text, encoding = decode_upload(raw)
    if kind == "orders":
        records: list[Any] = load_orders(text, platform=platform, source_mode="csv", defaults=defaults)
    else:
        records = load_records(text, platform=platform, source_mode="csv", defaults=defaults)
    return records, encoding
