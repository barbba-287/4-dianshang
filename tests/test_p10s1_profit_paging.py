# -*- coding: utf-8 -*-
"""P10-S1 利润明细分页 + SKU 筛选测试：Q1 ~ Q7。

覆盖三条主线：

- **Q1 ~ Q3 前端契约**：`app/templates/cockpit.html` 里分页/筛选的**结构**必须齐备
  （分页条元素、SKU 输入框、`offset` 真传、输入框回填走 `escapeHtml`），
  以及本片唯一的**功能性死锁点**——空结果时筛选行必须仍在。
- **Q4 ~ Q7 后端分页语义**：跨页**不重不漏**（本片最重要的用例）、SKU 过滤 + 分页组合、
  `page_profit` 是**本页**口径（D3 的口径锁）、`offset` 越界返回 200 而非 422/500。

夹具直接复用 `tests/test_p7s3_profit_engine.py` 的 `ctx`（含 sku_a/sku_b/sku_c、
`_order`/`_snapshot`/`_ingest`/`_profit_lines`），不另造一套造数脚手架
（沿用 P9-S1 已确立的跨文件复用范式）。
"""
from __future__ import annotations

import importlib
import pathlib
import sys

# 复用 P7-S3 的夹具模块：tests/ 不是包，同目录模块可直接 import。
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
p7s3 = importlib.import_module("test_p7s3_profit_engine")

ctx = p7s3.ctx          # pytest 夹具（沿用 P7-S3 的构造）
_order = p7s3._order
_snapshot = p7s3._snapshot
_ingest = p7s3._ingest
_profit_lines = p7s3._profit_lines   # 已封装 limit/offset/sku_code 透传

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
COCKPIT_TEMPLATE = REPO_ROOT / "app" / "templates" / "cockpit.html"


def _template() -> str:
    return COCKPIT_TEMPLATE.read_text(encoding="utf-8")


def _make_rows(count: int, sku: str = "EXT-SKU-A", *, gross_of=None):
    """造 count 笔订单，每笔 1 行；`gross_of(i)` 给出该行的实付金额。

    所有订单的 `created_at` 相同（SALE_TIME），因此排序完全依赖
    `ExternalOrderLine.id.desc()` 这个**兜底键**——这正是 Q4 要验证的东西。
    """
    rows = []
    for i in range(count):
        gross = gross_of(i) if gross_of else "50.00"
        rows.append(
            _order(
                order_no=f"ORD-{sku}-{i}",
                lines=[
                    {
                        "external_line_id": f"L-{sku}-{i}",
                        "external_sku": sku,
                        "qty": 1,
                        "gross": gross,
                    }
                ],
            )
        )
    return rows


# ---------------------------------------------------------------------------
# Q1 前端分页元素齐备
# ---------------------------------------------------------------------------


def test_q1_cockpit_template_has_pager_elements():
    """Q1：`cockpit.html` 必须含全部分页/筛选元素（结构缺失即静默失去翻页入口）。"""
    template = _template()
    for anchor in (
        'id="profit-line-sku"',
        'id="profit-line-size"',
        'id="profit-line-info"',
        'data-pager="prev"',
        'data-pager="next"',
        'data-pager="filter"',
        'data-pager="clear"',
        'class="profit-pager"',
    ):
        assert anchor in template, f"cockpit.html 缺少分页/筛选锚点：{anchor}"

    # 分页条必须真的被 renderProfitDetail 渲染（不是写在模板里当摆设）。
    assert "renderProfitDetail" in template
    assert "bindProfitPager" in template
    assert "loadProfitDetail" in template


# ---------------------------------------------------------------------------
# Q2 不再硬编码单页 50，且真的传 offset
# ---------------------------------------------------------------------------


def test_q2_no_hardcoded_limit_and_offset_is_sent():
    """Q2：旧的 `&limit=50` 写死必须消失；`offset` 与 `profitPage.limit` 必须真的被使用。"""
    template = _template()

    # 旧写法：`?days=...&limit=50`，用户实际只能看 50 行。
    assert "&limit=50" not in template, "cockpit.html 仍写死 limit=50，用户翻不到第 51 行"
    assert "limit=50" not in template, "cockpit.html 仍出现写死的 limit=50"

    # 新写法：每页行数来自状态，且真的传了 offset。
    assert "profitPage.limit" in template, "每页行数未走 profitPage.limit"
    assert "offset" in template, "模板未出现 offset"
    assert "profitPage.offset" in template, "模板未出现 profitPage.offset"
    assert "URLSearchParams" in template, "查询串必须用 URLSearchParams（SKU 可能含特殊字符）"

    # 打开明细时归零 offset（否则可能落在不存在的页上）。
    toggle = template.split("async function toggleProfitDetail()")[1].split("\n")[0]
    assert "profitPage.offset=0" in toggle, "打开明细时未重置 offset"


# ---------------------------------------------------------------------------
# Q3 SKU 输入框回填走 escapeHtml
# ---------------------------------------------------------------------------


def test_q3_sku_input_value_is_escaped():
    """Q3：SKU 输入框回填必须走 escapeHtml（筛选词会回显到 HTML，防 XSS）。"""
    template = _template()
    assert 'value="${escapeHtml(profitPage.sku)}"' in template, \
        "SKU 输入框回填未走 escapeHtml（筛选词会原样注入 HTML）"


# ---------------------------------------------------------------------------
# Q4 跨页不重不漏（本片最重要的用例）
# ---------------------------------------------------------------------------


def test_q4_paging_across_pages_has_no_duplicate_or_missing(ctx):
    """Q4：120 行、limit=50 翻三页 → 50/50/20，total 恒 120，external_line_id 并集 == 120 且无重复。

    这是「页面上找不到、导出里却有」的正解：排序键 `external_created_at` 相同，
    靠 `ExternalOrderLine.id.desc()` 兜底才能跨页不重不漏——排序若少了这个兜底键，
    三页之间会重复或漏行，`set` 长度立刻 ≠ 120。
    """
    _snapshot(ctx, ctx.sku_a, "10.00")
    _ingest(ctx, *_make_rows(120))

    seen: list[str] = []
    lengths: list[int] = []
    totals: list[int] = []
    for offset in (0, 50, 100):
        response = _profit_lines(ctx, limit=50, offset=offset)
        assert response.status_code == 200, response.text
        body = response.json()
        lengths.append(len(body["items"]))
        totals.append(int(body["total"]))
        seen.extend(item["external_line_id"] for item in body["items"])

    assert lengths == [50, 50, 20], f"三页条数应为 [50,50,20]，实际 {lengths}"
    assert totals == [120, 120, 120], f"total 三次都应为 120，实际 {totals}"
    assert len(seen) == 120, f"三页取回的条数应为 120，实际 {len(seen)}"
    assert len(set(seen)) == 120, \
        f"三页并集去重后应为 120，实际 {len(set(seen))}（排序不稳定会重复或漏行）"


# ---------------------------------------------------------------------------
# Q5 SKU 过滤 + 分页组合
# ---------------------------------------------------------------------------


def test_q5_sku_filter_combined_with_paging(ctx):
    """Q5：A 80 行 / B 40 行 → `sku_code=SKU-A&limit=50&offset=50` 得 30 行且全为 A，total==80。"""
    _snapshot(ctx, ctx.sku_a, "10.00")
    _snapshot(ctx, ctx.sku_b, "10.00")
    _ingest(ctx, *_make_rows(80, "EXT-SKU-A"))
    _ingest(ctx, *_make_rows(40, "EXT-SKU-B"))

    response = _profit_lines(ctx, sku_code="SKU-A", limit=50, offset=50)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 80, f"SKU 过滤后的 total 应为 80，实际 {body['total']}"
    assert len(body["items"]) == 30, f"第 2 页应返回 30 行，实际 {len(body['items'])}"
    assert {item["sku_code"] for item in body["items"]} == {"SKU-A"}, \
        "SKU 过滤后混进了别的 SKU"
    assert body["offset"] == 50 and body["limit"] == 50


# ---------------------------------------------------------------------------
# Q6 page_profit 是本页口径（D3 的口径锁）
# ---------------------------------------------------------------------------


def test_q6_page_profit_is_page_scoped_not_window_total(ctx):
    """Q6：`page_profit` 是**本页**口径（limit=50 与 limit=200 不相等）。

    这条钉死「翻页会变本页合计」这个事实，防未来有人把它当总额用——
    前端因此必须保留「全窗口合计见上方利润卡，切换页码不会改变它」这句限定。
    """
    _snapshot(ctx, ctx.sku_a, "10.00")
    # 逐行金额各不相同（利润各不相等），排除「恰好相等」的巧合通过。
    _ingest(ctx, *_make_rows(120, gross_of=lambda i: f"{20 + i}.00"))

    first = _profit_lines(ctx, limit=50, offset=0).json()
    wide = _profit_lines(ctx, limit=200, offset=0).json()

    assert first["total"] == wide["total"] == 120
    assert first["page_profit"] != wide["page_profit"], \
        "page_profit 必须随每页行数变化（它是本页口径，不是窗口总额）"
    assert first["page_computed"] == 50
    assert wide["page_computed"] == 120


# ---------------------------------------------------------------------------
# Q7 offset 越界
# ---------------------------------------------------------------------------


def test_q7_offset_out_of_range_returns_200_and_empty(ctx):
    """Q7：`offset=10000` → items == []、total 仍是真实值、HTTP 200（不许 422/500）。"""
    _snapshot(ctx, ctx.sku_a, "10.00")
    _ingest(ctx, *_make_rows(120))

    response = _profit_lines(ctx, limit=50, offset=10000)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["items"] == [], f"越界时应返回空列表，实际 {len(body['items'])} 行"
    assert body["total"] == 120, f"越界时 total 仍应是真实值，实际 {body['total']}"
