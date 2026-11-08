"""P9-S3 订单行指纹与行 id 稳定化。

对应 `dev_task.p9s3-order-upsert.md` §2 D1/D2 与 §4 A-2。

设计要点（**不要在别处另写一份**）：

- D1：行指纹 = `(external_sku, 该 SKU 在订单内的出现序号)`，**不是** `(sku)`、
  也不是 `(位置)`。锚定五件套全是 SKU 级/订单级事实，与行的数量金额无关，
  所以只要 SKU 对得上锚定就是对的。
- D2：合成 id（`external_line_id.startswith("line:")`）改写为
  `csv:{sku_token}|{seq}`；**平台真实 id 一律不动**（Shopify 的 id 不以 `line:` 开头）。
- `legacy_seq_map` 是**旧行 seq 的唯一解析口径**：A-3 的锚定继承与 §5.2 第 5 条的
  探针归一化都必须调用它。两套实现会让「对拍通过」失去意义。

本模块是**纯函数层**：不 import fastapi、不访问数据库、不做 I/O。
"""
from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import replace
from typing import Any, Iterable

# db.py:835 的 `external_line_id` 是 `String(128)`，指纹 id 必须留足余量。
MAX_LINE_ID_LEN = 128
# `sku_token` 原样保留的最大长度；超过则走 sha1 截断。
MAX_SKU_TOKEN_LEN = 100

# 已写成新规则的 id：csv:{token}|{seq}
_FINGERPRINT_RE = re.compile(r"^csv:(?P<token>[^|]*)\|(?P<seq>\d+)$")
# 旧规则 id：line:{N}
_LEGACY_LINE_RE = re.compile(r"^line:(?P<n>\d+)$")


def sku_token(sku: str) -> str:
    """指纹 id 里的 SKU 片段。

    - 长度 ≤ 100 且不含 `|` → 原样保留（可读，便于人工排查）；
    - 否则 → `sha1(sku)[:16]`（长度与 `|` 两个坑一次解决）。

    `|` 会破坏 `csv:{token}|{seq}` 的解析（用 `split("|")` 取 seq 会错位），
    所以含 `|` 的 SKU **必须**走 hash 分支，不能原样保留。
    """
    text = str(sku)
    if len(text) <= MAX_SKU_TOKEN_LEN and "|" not in text:
        return text
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def line_fingerprint(sku: str, seq: int) -> str:
    """指纹 id = `csv:{sku_token}|{seq}`。长度恒 < 128（见 `MAX_LINE_ID_LEN`）。"""
    token = sku_token(sku)
    return f"csv:{token}|{seq}"


def normalize_csv_line_ids(records: Iterable[Any]) -> list[Any]:
    """把 csv 记录里的**合成 id**（`line:N`）改写为稳定指纹 id。

    适用范围（红线 3）：只处理 `source_mode == "csv"` 的记录；
    其余（`json` / `mock` / `shopify_live`）**一个字符都不动**。

    只处理 `external_line_id.startswith("line:")` 的行 —— 平台真实 id 原样保留。

    `seq` = 该 SKU 在**本订单内**第几次出现（0-based），按记录内行的先后顺序累加。

    返回**新 list**（`ExternalOrderRecord` 是 frozen dataclass，用 `replace`），
    调用方不得假设入参被就地修改。
    """
    normalized: list[Any] = []
    for record in records:
        if getattr(record, "source_mode", None) != "csv":
            normalized.append(record)
            continue
        counter: Counter[str] = Counter()
        new_lines = []
        for line in record.lines:
            sku = line.external_sku
            seq = counter[sku]
            counter[sku] += 1
            if str(line.external_line_id).startswith("line:"):
                new_lines.append(
                    replace(line, external_line_id=line_fingerprint(sku, seq))
                )
            else:
                new_lines.append(line)
        normalized.append(replace(record, lines=tuple(new_lines)))
    return normalized


def legacy_seq_map(lines: Iterable[Any]) -> dict[str, tuple[str, int]]:
    """把库里已有的行映射成 `{旧 external_line_id: (external_sku, seq)}`。

    **旧行的 seq 必须从 id 解析，不能靠查询返回顺序**——
    `app/external_orders.py` 里取旧行的 select **没有 `ORDER BY`**，
    返回顺序不受保证，靠位置推断 seq 会让行为随 SQLite 返回顺序漂移。

    解析规则（D2）：

    - 旧 id 形如 `csv:{token}|{seq}` → **直接解析** seq（确定性）；
    - 旧 id 形如 `line:{N}` → **先按 N 数值升序排序**，再对同 SKU 依次累加 seq（确定性）；
    - 两种都不匹配（理论上不存在）→ **不进表**，只能靠 `inherited_by_id` 兜底。

    `lines` 里的对象只需有 `external_line_id` 与 `external_sku` 两个属性
    （ORM 行与探针快照都满足）。
    """
    parsed: dict[str, tuple[str, int]] = {}
    numbered: list[tuple[int, str, str]] = []
    for line in lines:
        line_id = str(line.external_line_id)
        sku = line.external_sku
        match = _FINGERPRINT_RE.match(line_id)
        if match is not None:
            parsed[line_id] = (sku, int(match.group("seq")))
            continue
        match = _LEGACY_LINE_RE.match(line_id)
        if match is not None:
            numbered.append((int(match.group("n")), line_id, sku))
    # 旧 id 的 N 数值升序 —— 决定「同 SKU 的第几次出现」。
    numbered.sort(key=lambda item: item[0])
    counter: Counter[str] = Counter()
    for _, line_id, sku in numbered:
        parsed[line_id] = (sku, counter[sku])
        counter[sku] += 1
    return parsed
