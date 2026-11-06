"""R-66 展平报表自动预聚合：同一订单号的多行 CSV → 一个订单 + 多条明细。

背景（用户 2026-11-04 裁定）
--------------------------
真实电商后台导出的订单报表是**一行一个 SKU**，而本系统的订单契约是「一行一个订单，
多商品放 `lines` 列」。P7-S3 的 R-65 把这种情况从「500 报错」修成了「422 + 让商家手工合并」——
能报错，但业务仍然走不通（让商家手工合并几十行是反人类的）。

本模块把它变成「系统自动合并」。三条硬约束（写死在代码里，不靠调用方自觉）：

1. **只对 `source_mode == "csv"` 生效**：Shopify live 走适配器直接构造
   `ExternalOrderRecord(source_mode="shopify_live")`，物理上碰不到这条分支；
   JSON 模式的记录本身已带 `lines` 数组，也不该被"合并"。
2. **纯内存、无 DB 访问、绝不抛异常**：聚合是尽力而为的增强。任何判定不通过 →
   原样返回交给 R-65 判重（内容冲突 → 422；内容相同 → 幂等 no_op）。
   把增强失败升级成整批失败，等于用新功能扩大故障面。
3. **合并后必须重算 `payload_hash`**：它决定 `ingest_orders` 的 `no_op` 判定。
   沿用首行 hash → 第二行内容变了也判 no_op（静默丢更新）；每次算得不同 → 幂等失效、
   成本锚定反复重建。

为什么不在 `load_orders` 里做
----------------------------
`app/imports.py::_isolate` 的判定是「记录数 == 行数」（P6-S3 修「静默丢行」时立的规矩），
且 `parse_upload` 末尾有 `len(records) + len(rejected) == len(mapped_rows)` 的不变量。
在 `load_orders` 里聚合会让 3 行 → 1 记录，被 `_isolate` 判成 3 行全坏、整单失败。
所以聚合只能发生在**行级隔离完成之后**，即 `ingest_orders` 入口。
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import ROUND_HALF_UP, Decimal
import logging
from typing import Any, Iterable

from app.connectors import ExternalOrderLineRecord, ExternalOrderRecord, _payload_hash

logger = logging.getLogger(__name__)

# 只有这个数据源的记录参与聚合（用户裁定第 1 条）。
MERGE_SOURCE_MODE = "csv"

# M3 金额三态（用户裁定第 2 条的细化，见 dev_task.p8-order-merge.md §2.3）。
MERGE_RULE_SAME = "same"
MERGE_RULE_SINGLE_NONZERO = "single_nonzero"
MERGE_RULE_SUM = "sum"
# R-67 分摊（用户 2026-11-06 裁定方案②）：SAME 态且各行金额 == 单头金额时按数量分摊。
MERGE_RULE_ALLOCATED = "same_allocated"

# M2：「订单级事实」字段。组内不一致 → 放弃合并（不猜、不抛错），交给 R-65 的 422。
# 这四个字段一旦猜错，影响的是钱：status 决定这单算不算销售、currency 决定能不能求和、
# created_at 是成本 as-of 的时间锚（P7-S2 §5.3）。
STRICT_FIELDS: tuple[str, ...] = (
    "order_status",
    "currency",
    "external_created_at",
    "external_updated_at",
)

# M4：取第一行、空则向后找第一个非空（不参与金额与成本锚定的字段）。
FIRST_NON_NULL_FIELDS: tuple[str, ...] = (
    "paid_at",
    "event_version",
    "status_reason",
    "raw_ref",
    "schema_version",
)

_ZERO = Decimal("0")
_CENT = Decimal("0.01")


@dataclass(frozen=True)
class MergeReport:
    """一次合并的可观测记录（M6 打点 + 测试断言用）。"""

    order_no: str
    row_count: int
    line_count: int
    gross_rule: str
    refund_rule: str
    main_gross: Decimal
    main_refund: Decimal
    lines_gross_sum: Decimal
    lines_refund_sum: Decimal
    delta: Decimal
    refund_delta: Decimal

    def as_log_fields(self) -> dict[str, Any]:
        return {
            "order_no": self.order_no,
            "rows": self.row_count,
            "lines": self.line_count,
            "gross_rule": self.gross_rule,
            "refund_rule": self.refund_rule,
            "main_gross": str(self.main_gross),
            "lines_sum": str(self.lines_gross_sum),
            "delta": str(self.delta),
            "main_refund": str(self.main_refund),
            "lines_refund_sum": str(self.lines_refund_sum),
            "refund_delta": str(self.refund_delta),
        }


def order_group_key(record: ExternalOrderRecord) -> tuple[str, str, str, str]:
    """判重键与数据库唯一约束 `uq_external_order_workspace_account_no` 同构。"""
    return (record.platform, record.account_ref, record.store_ref or "default", record.external_order_no)


def resolve_amount(values: list[Decimal]) -> tuple[Decimal, str] | None:
    """M3 金额三态判定：返回 (主表取值, 规则名)；无法判定返回 None。

    三态互斥且覆盖全部情况，**不存在"取第一行"把后两种吞掉的分支**：

    - `SAME`：组内取值去重后只有 1 个（含全为 0）→ 取该值（此时取第一行与取唯一值等价）；
    - `SINGLE_NONZERO`：恰有 1 个非零值、其余全为 0 → 取那个非零值（订单级金额挂在首行的形态）；
    - `SUM`：≥2 个不同的非零值 → 取各行之和（每行填的是本行金额，求和才与明细逐行之和自洽）。
    """
    distinct = list(dict.fromkeys(values))  # 保序去重，便于日志复现
    if len(distinct) == 1:
        return distinct[0], MERGE_RULE_SAME
    non_zero = [value for value in distinct if value != _ZERO]
    if len(non_zero) == 1:
        return non_zero[0], MERGE_RULE_SINGLE_NONZERO
    if len(non_zero) >= 2:
        return sum(values, _ZERO), MERGE_RULE_SUM
    # 去重后多于 1 个取值但全为 0 —— 只可能是 Decimal 的 +0/-0 之类，
    # 统一收敛为 0，不让它把整组打成"不可合并"。
    return _ZERO, MERGE_RULE_SAME


def _line_qty(line: ExternalOrderLineRecord) -> int:
    """分摊权重用的数量：订购数，负数按 0 处理（脏数据不该把分摊算成负数比例）。"""
    return max(0, int(getattr(line, "ordered_qty", 0) or 0))


def should_allocate(
    gross_rule: str,
    gross_amount: Decimal,
    lines: list[ExternalOrderLineRecord],
) -> bool:
    """R-67 分摊触发判据（用户 2026-11-06 裁定方案②，四条全部写死在这里）。

    四个条件**同时**成立才触发，任一不成立一律返回 False（"其他地方一律不走分摊"）：

    1. M3 判成 `SAME` —— 组内各行取值唯一（不是求和、不是单行非零形态）；
    2. 每行金额都等于单头金额 —— 这正是"商家把订单总额复制到每一行"的指纹；
    3. 明细逐行之和 != 单头金额 —— 真的对不上才需要动，对得上就别碰；
    4. 至少两行 —— 单行订单没有"分摊"概念（也碰不到合并分支）。

    特别注意条件 2 与 3 是**并列**的：只判 `delta != 0` 会把
    "首行填总额、其余填 0"（`SINGLE_NONZERO`）也吞进来，那种形态本来就是对的。
    """
    if gross_rule != MERGE_RULE_SAME:
        return False
    if len(lines) < 2:
        return False
    if any(line.gross_amount != gross_amount for line in lines):
        return False
    return sum((line.gross_amount for line in lines), _ZERO) != gross_amount


def allocate_gross(
    gross_amount: Decimal,
    lines: list[ExternalOrderLineRecord],
) -> list[ExternalOrderLineRecord] | None:
    """把单头金额按数量分摊到各行：`单行 = 单头 × 单行数量 / 总数量`。

    **末行用 `单头 − 前面各行之和` 补齐**，保证分摊后逐行之和分毫不差（不留四舍五入残差）。
    总数量为 0（全部行数量都是 0）时无法分摊 → 返回 None，调用方保持原样、
    交给 M6 打 warning，绝不猜。
    """
    quantities = [_line_qty(line) for line in lines]
    total_qty = sum(quantities)
    if total_qty <= 0:
        return None
    allocated: list[ExternalOrderLineRecord] = []
    running = _ZERO
    last = len(lines) - 1
    for index, (line, quantity) in enumerate(zip(lines, quantities)):
        if index == last:
            # 末行补齐：差额可能是正也可能是负（极端小额多行时），但总和一定等于单头。
            share = gross_amount - running
        else:
            share = (gross_amount * quantity / total_qty).quantize(_CENT, rounding=ROUND_HALF_UP)
            running += share
        allocated.append(replace(line, gross_amount=share))
    return allocated


def _first_non_null(group: list[ExternalOrderRecord], field: str) -> Any:
    for record in group:
        value = getattr(record, field)
        if value is not None:
            return value
    return getattr(group[0], field)


def _conflicting_fields(group: list[ExternalOrderRecord]) -> list[str]:
    """M2：列出组内取值不一致的订单级事实字段。"""
    return [
        field
        for field in STRICT_FIELDS
        if any(getattr(record, field) != getattr(group[0], field) for record in group[1:])
    ]


def merge_lines(group: list[ExternalOrderRecord]) -> list[ExternalOrderLineRecord]:
    """M5：按原顺序拼接明细，并保证 `external_line_id` 组内唯一。

    坑：订单的列映射表里**没有 `external_line_id`**（`app/column_mappings.py`），所以
    `normalize_order_row` 会给每条 CSV 行都造 `line:0`。三条 CSV 行合并后就是三个
    `line:0` → 直接撞 `uq_external_order_line_identity` → IntegrityError → 500。

    处置：**只在出现重复时整组统一重编号** `line:0..n-1`（按拼接位置）。统一重编号而不是
    "只改重复项"，是为了让结果确定——同一份文件重传必须得到同样的 id、同样的 hash，
    否则 `no_op` 判定失效、成本锚定会被反复重建。
    """
    lines = [line for record in group for line in record.lines]
    ids = [line.external_line_id for line in lines]
    if len(set(ids)) == len(ids):
        return lines
    return [
        replace(line, external_line_id=f"line:{index}")
        for index, line in enumerate(lines)
    ]


def merge_group(
    group: list[ExternalOrderRecord],
) -> tuple[ExternalOrderRecord, MergeReport] | None:
    """把一组同名记录合并成一条；不可合并返回 None（**绝不抛异常**，M7）。"""
    if len(group) < 2:
        return None
    if any(record.source_mode != MERGE_SOURCE_MODE for record in group):
        return None
    conflicts = _conflicting_fields(group)
    if conflicts:
        # 不抛错：原样返回后由 R-65 拦（422 文案已可操作）。
        # 这里替商家"取第一行"等于编造事实——状态/时间不一致说明文件本身自相矛盾。
        logger.info(
            "R-66 跳过合并（订单级字段不一致）order=%s rows=%d fields=%s",
            group[0].external_order_no, len(group), ",".join(conflicts),
        )
        return None
    gross_amount, gross_rule = resolve_amount([record.gross_amount for record in group])
    refund_amount, refund_rule = resolve_amount([record.refund_amount for record in group])
    lines = merge_lines(group)
    # R-67 方案②（用户 2026-11-06 裁定）：只有「商家把订单总额复制到每一行」这一种形态
    # 才按数量分摊。分摊后逐行之和 == 单头，M6 的 delta 校验才允许通过、利润引擎才按对的口径算。
    # 其余形态（SUM / SINGLE_NONZERO / 本来就填对）一律不动，维持现状。
    if should_allocate(gross_rule, gross_amount, lines):
        allocated = allocate_gross(gross_amount, lines)
        if allocated is not None:
            logger.info(
                "R-67 金额分摊 order=%s rows=%d main_gross=%s total_qty=%d allocated=%s",
                group[0].external_order_no,
                len(group),
                gross_amount,
                sum(_line_qty(line) for line in lines),
                ",".join(str(line.gross_amount) for line in allocated),
            )
            lines = allocated
            gross_rule = MERGE_RULE_ALLOCATED
    merged = replace(
        group[0],
        gross_amount=gross_amount,
        refund_amount=refund_amount,
        lines=tuple(lines),
        # M4：三个"取第一行"的字段（含 paid_at 首个非空）。
        paid_at=_first_non_null(group, "paid_at"),
        event_version=_first_non_null(group, "event_version"),
        status_reason=_first_non_null(group, "status_reason"),
        raw_ref=_first_non_null(group, "raw_ref"),
        # 数据完整性取最差，不取首行——部分字段缺失不该被首行的 complete 掩盖。
        data_completeness=(
            "complete"
            if all(record.data_completeness == "complete" for record in group)
            else "partial"
        ),
        # idempotency_key 取首行原值：这样"先传单行文件、后传完整文件"会命中
        # uq_external_order_workspace_account_key 的同一行，走 update 分支而不是插重复。
        idempotency_key=group[0].idempotency_key,
    )
    # ★ 必须重算：as_dict() 是确定性的（固定键序 + sort_keys），同文件重传得到同样的 hash。
    # 注意 as_dict() 的键集与 normalize_order_row 内部的 canonical 不同，所以**只有合并路径**
    # 能用它——单行记录一律原样保留，否则会打挂既有幂等测试。
    merged = replace(merged, payload_hash=_payload_hash(merged.as_dict()))
    lines_gross_sum = sum((line.gross_amount for line in lines), _ZERO)
    lines_refund_sum = sum((line.refund_amount for line in lines), _ZERO)
    report = MergeReport(
        order_no=merged.external_order_no,
        row_count=len(group),
        line_count=len(lines),
        gross_rule=gross_rule,
        refund_rule=refund_rule,
        main_gross=gross_amount,
        main_refund=refund_amount,
        lines_gross_sum=lines_gross_sum,
        lines_refund_sum=lines_refund_sum,
        delta=gross_amount - lines_gross_sum,
        refund_delta=refund_amount - lines_refund_sum,
    )
    return merged, report


def aggregate_order_records(
    records: list[ExternalOrderRecord],
) -> tuple[list[ExternalOrderRecord], list[MergeReport]]:
    """返回 (聚合后的记录, 合并报告列表)；记录顺序按各订单首次出现的位置排列。

    单条组、非 CSV 组、判定不通过的组一律**原样原序**返回，不丢记录、不改内容。
    """
    groups: dict[tuple[str, str, str, str], list[ExternalOrderRecord]] = {}
    order: list[tuple[str, str, str, str]] = []
    for record in records:
        key = order_group_key(record)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(record)

    merged_records: list[ExternalOrderRecord] = []
    reports: list[MergeReport] = []
    for key in order:
        group = groups[key]
        if len(group) < 2:
            merged_records.extend(group)
            continue
        outcome = merge_group(group)
        if outcome is None:
            merged_records.extend(group)
            continue
        merged, report = outcome
        merged_records.append(merged)
        reports.append(report)
    return merged_records, reports


def log_merge_reports(reports: Iterable[MergeReport]) -> None:
    """M6 安全底线：合并结果**无条件**打点，金额不平升级为 warning。

    `delta != 0` 意味着「主表金额」与「明细逐行之和」对不上。R-67 分摊（用户 2026-11-06
    裁定方案②）会把「订单总额复制到每一行」的形态按数量摊平，所以**正常路径下
    分摊成功后 delta 恒为 0**；仍出现 `delta != 0` 只剩两种可能，都值得警惕：

    - 全部行数量为 0（无法按数量分摊）；
    - 形态不满足分摊判据（`should_allocate` 为 False），例如 `SUM` 态却因精度问题对不上。

    `refund_delta != 0` 是**同构的退款风险**：商家若把订单退款总额复制到每一行，利润引擎
    按行的退款冲减会重复扣。本轮只做观测不自动分摊（登记 R-71，待裁定），绝不"安静地"发生。
    """
    for report in reports:
        fields = report.as_log_fields()
        if report.delta == _ZERO:
            logger.info(
                "R-66 预聚合 order=%(order_no)s rows=%(rows)d lines=%(lines)d "
                "gross_rule=%(gross_rule)s refund_rule=%(refund_rule)s "
                "main_gross=%(main_gross)s lines_sum=%(lines_sum)s delta=%(delta)s "
                "main_refund=%(main_refund)s lines_refund_sum=%(lines_refund_sum)s "
                "refund_delta=%(refund_delta)s",
                fields,
            )
        else:
            logger.warning(
                "R-66 预聚合金额不平（明细之和 != 主表）order=%(order_no)s rows=%(rows)d lines=%(lines)d "
                "gross_rule=%(gross_rule)s refund_rule=%(refund_rule)s "
                "main_gross=%(main_gross)s lines_sum=%(lines_sum)s delta=%(delta)s "
                "main_refund=%(main_refund)s lines_refund_sum=%(lines_refund_sum)s "
                "refund_delta=%(refund_delta)s",
                fields,
            )
        if report.refund_delta != _ZERO:
            logger.warning(
                "R-71 退款金额可能被复制到每行（未自动分摊）order=%(order_no)s "
                "gross_rule=%(gross_rule)s refund_rule=%(refund_rule)s "
                "main_refund=%(main_refund)s lines_refund_sum=%(lines_refund_sum)s "
                "refund_delta=%(refund_delta)s",
                fields,
            )
