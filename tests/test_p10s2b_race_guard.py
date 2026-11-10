# -*- coding: utf-8 -*-
"""P10-S2b 利润趋势请求竞态守卫测试：T-R77-1 ~ T-R77-4。

背景（R-77，已实测复现）：P10-S2 的 `loadProfitTrend()` 是三个请求函数里
**唯一没有请求序号守卫**的。慢的旧请求（days=7 / 200ms）返回后会覆盖快的
新请求（days=30 / 10ms），导致页面控件写着「近 30 天」而趋势图画的是 7 天一拍。

本片是**补丁级**：后端零改动，改动全部落在 `app/templates/cockpit.html` 的
`loadProfitTrend()`。仓库没有 JS 单测框架且本片**不引入**（不新增 node 依赖、
不改 pytest 配置），因此测试走**结构断言**——但每一条都必须能抓到一种具体的
改坏方式，不做"字符串存在性"空壳断言。

切片说明：模板是长行内联，用「按函数名切片 + 大括号配对」取函数体。
锚点必须写 `"function " + name + "("`（**带左括号**）——不带括号会先命中
`loadProfitDetail` 之类的同名前缀函数，切到错误的函数体。
"""
from __future__ import annotations

import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
COCKPIT_TEMPLATE = REPO_ROOT / "app" / "templates" / "cockpit.html"


def _template() -> str:
    return COCKPIT_TEMPLATE.read_text(encoding="utf-8")


def _fn_body(html: str, name: str) -> str:
    """取顶层 async/sync 函数体（含 async 前缀），按大括号配对。"""
    start = html.index("function " + name + "(")   # ★ 必须带左括号
    if html[start - 6:start] == "async ":
        start -= 6
    brace = html.index("{", start)
    depth = 0
    for i in range(brace, len(html)):
        if html[i] == "{":
            depth += 1
        elif html[i] == "}":
            depth -= 1
            if depth == 0:
                return html[start:i + 1]
    raise AssertionError(f"{name} 函数体未闭合")


# ---------------------------------------------------------------------------
# T-R77-1 守卫序号必须声明在函数之外
# ---------------------------------------------------------------------------


def test_r77_1_trend_sequence_declared_at_module_scope():
    """T-R77-1：`profitTrendSeq=0` 必须声明在全局（函数体之外）。

    抓的坏法：把 `let profitTrendSeq` 写进函数体内 → 每次调用重置为 0，
    守卫恒真、等于没有守卫。
    """
    tpl = _template()
    body = _fn_body(tpl, "loadProfitTrend")
    decl = tpl[:tpl.index("function loadProfitTrend")]
    assert "profitTrendSeq=0" in decl, \
        "profitTrendSeq 未在函数之外声明（写进函数体内每次调用会重置为 0，守卫恒真）"
    # 反向：函数体内不得出现声明（`let`/`const`/`var` + profitTrendSeq）。
    assert "let profitTrendSeq" not in body, \
        "函数体内出现 let profitTrendSeq 声明（每次调用重置序号）"
    assert "const profitTrendSeq" not in body, \
        "函数体内出现 const profitTrendSeq 声明（每次调用重置序号）"
    assert "var profitTrendSeq" not in body, \
        "函数体内出现 var profitTrendSeq 声明（每次调用重置序号）"


# ---------------------------------------------------------------------------
# T-R77-2 递增必须早于第一个 await
# ---------------------------------------------------------------------------


def test_r77_2_sequence_increment_precedes_first_await():
    """T-R77-2：`++profitTrendSeq` 恰好 1 处，且位于第一个 `await` 之前。

    抓的坏法：把 `const seq=++profitTrendSeq;` 放在 `await fetch(...)` 之后
    → 两个并发请求都会在各自 resolve 时才递增，序号顺序被打乱，守卫失效。
    """
    tpl = _template()
    body = _fn_body(tpl, "loadProfitTrend")
    assert "++profitTrendSeq" in body, "loadProfitTrend 缺少序号递增"
    inc = body.index("++profitTrendSeq")
    assert body.count("++profitTrendSeq") == 1, "递增点必须唯一"
    assert inc < body.index("await "), "递增必须在第一个 await 之前"


# ---------------------------------------------------------------------------
# T-R77-3 成功路径与失败路径各有一处守卫，且在渲染之前
# ---------------------------------------------------------------------------


def test_r77_3_guard_on_both_success_and_failure_paths_before_render():
    """T-R77-3：守卫恰好 2 处（成功 + catch），且都在渲染/清空之前。

    抓的坏法：
    ① 只守成功路径、漏 catch；
    ② 守卫写在 `renderProfitTrend(...)` 之后（渲染完才 return，等于没守）；
    ③ catch 里先 `dispose` 再守（图已经被清掉了）。
    """
    tpl = _template()
    body = _fn_body(tpl, "loadProfitTrend")

    pos = [i for i in range(len(body)) if body.startswith("seq!==profitTrendSeq", i)]
    assert len(pos) == 2, f"守卫应恰好 2 处（成功 + catch），实际 {len(pos)}"

    render_at = body.index("renderProfitTrend(")
    empty_at = body.index("chartEmpty(")
    assert pos[0] < render_at, "成功路径的守卫必须在 renderProfitTrend 之前"
    assert pos[1] < empty_at, "catch 里的守卫必须在 chartEmpty 之前"
    assert body.index("await response.json()") < pos[0], "守卫应在 json() 之后（json 也是 await 点）"
    assert body.index("catch(error)") < pos[1], "第 2 处守卫必须在 catch 块内"


# ---------------------------------------------------------------------------
# T-R77-4 既有守卫不许被"顺手统一"改坏
# ---------------------------------------------------------------------------


def test_r77_4_existing_guards_untouched():
    """T-R77-4：`load()` / `loadProfitDetail()` 的既有守卫保持原样。

    抓的坏法：把三个函数的守卫合并重构成一个公共 `guard()`，或误删
    `load()` 的 catch 守卫。
    """
    tpl = _template()
    assert _fn_body(tpl, "load").count("sequence!==requestSequence") == 2
    assert _fn_body(tpl, "loadProfitDetail").count("seq!==profitSeq") == 2
    assert tpl.count("void loadProfitTrend()") == 1
