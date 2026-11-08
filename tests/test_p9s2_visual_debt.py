# -*- coding: utf-8 -*-
"""P9-S2 视觉债清理测试：M-1（ui-v2 叠色 token 化）。

对应 dev_task.p9s2-visual-debt.md §5。

本文件只放**不需要业务数据**的纯文件级断言（不造第二套登录/造数脚手架）；
需要业务数据的 F-52 / E-19 分别追加到 tests/test_p9s1_profit_enhance.py 与
tests/test_p6s3_data_export.py，复用各自的既有夹具。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UI_V2 = ROOT / "app" / "static" / "css" / "ui-v2.css"
TOKENS = ROOT / "app" / "static" / "css" / "tokens.css"

# 裸色值有边界正则（dev_task §1.3 坑 2）：
# 前置/后置断言保证 `#add-event`、`#toast-root` 这类 id 选择器不会被误判成色值。
# Python 的 re 原生支持 lookaround，不存在 grep -E 那种「静默返回零行」的假绿。
HARDCODED_HEX = re.compile(
    r"(?<![0-9A-Za-z_-])#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{3})(?![0-9A-Za-z_-])"
)

# 新增的 6 个 alpha token 及其**逐字节**期望值（含 8 位 hex 的最后两位 alpha）。
# 基线：ui-v2.css 改动前 7 处出现 / 6 种值，值必须一字不差地搬进 tokens.css。
EXPECTED_ALPHA_TOKENS = {
    "--shadow-tint-primary-15": "#2a78d626",
    "--shadow-tint-primary-40": "#2a78d666",
    "--shadow-tint-primary-50": "#2a78d680",
    "--sheen-on-primary-25": "#ffffff40",
    "--hairline-on-primary-06": "#ffffff0f",
    "--shadow-tint-danger-15": "#c53b4526",
}


def test_m1_no_hardcoded_color_in_ui_v2():
    """ui-v2.css 裸色值计数 == 0；6 个 alpha token 均已定义且值逐字节正确。

    正向校验 tokens.css 是必须的：只断言「ui-v2 里没有 hex」无法防止有人
    把 token 的值改掉、把视觉带偏（值等价这件事本身必须被锁进测试）。
    """
    css = UI_V2.read_text(encoding="utf-8")
    hits = HARDCODED_HEX.findall(css)
    assert not hits, f"ui-v2.css 仍残留裸色值 {sorted(set(hits))}"

    tokens = TOKENS.read_text(encoding="utf-8")
    for name, expected in EXPECTED_ALPHA_TOKENS.items():
        match = re.search(
            re.escape(name) + r"\s*:\s*(#[0-9a-fA-F]{6,8})\s*;", tokens
        )
        assert match, f"tokens.css 缺少 token {name}"
        assert match.group(1).lower() == expected, (
            f"{name} 的值应为 {expected}（与 ui-v2 原裸值逐字节一致），"
            f"实为 {match.group(1)}"
        )

    # 反向自证：token 定义必须真的被用到（否则「清了裸色值」只是把值删了）
    for name in EXPECTED_ALPHA_TOKENS:
        assert f"var({name})" in css, f"ui-v2.css 未引用 token {name}"
