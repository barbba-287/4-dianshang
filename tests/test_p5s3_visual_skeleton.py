# -*- coding: utf-8 -*-
"""P5-S3 视觉骨架覆盖层 —— 契约用例（A 段：T-1 .. T-7 + T-12）。

对应 dev_task.p5s3-visual-overhaul.md §8。
本文件只做静态契约校验，不启动服务、不触碰数据库。

A 段（只增不删）覆盖 T-1..T-7 与 T-12；
T-8 / T-9 / T-10 / T-11 属 B 段（删除页内副本 + 图表尺寸锚点），A 段不写。
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOKENS = ROOT / 'app' / 'static' / 'css' / 'tokens.css'
SHELL = ROOT / 'app' / 'static' / 'workspace-shell.css'
UI_V2 = ROOT / 'app' / 'static' / 'css' / 'ui-v2.css'
TPL_DIR = ROOT / 'app' / 'templates'

TEMPLATES = sorted(TPL_DIR.glob('*.html'))

TOKENS_LINK = '/static/css/tokens.css'
SHELL_LINK = '/static/workspace-shell.css'
UI_V2_LINK = '/static/css/ui-v2.css'

# SVG 命名空间是 data-URI 的必需项，不算外链
XMLNS_RE = re.compile(r"""xmlns\s*=\s*['"]http://www\.w3\.org/2000/svg['"]""")

NAV_ICON_RE = re.compile(r"""--nav-icon\s*:\s*url\(\s*["']data:image/svg\+xml;utf8,""")
HREF_BIND_RE = re.compile(r"""\[href="(/[^"]+)"\]""")


def _read(path: Path) -> str:
    return path.read_text(encoding='utf-8')


def _read_css(path: Path) -> str:
    """读取 CSS 并剥离注释。

    注释里会写「严禁 #kpis」「不得出现 @import / position:fixed」这类禁令说明，
    若不剥离，禁令本身会被当成违规命中。所有语义断言都走这里。
    """
    return re.sub(r'/\*.*?\*/', '', _read(path), flags=re.S)


def _rule_body(css: str, selector_fragment: str):
    """返回所有「选择器里含 selector_fragment」的规则体（{...} 内文本）。"""
    out = []
    for m in re.finditer(r'([^{}]+)\{([^{}]*)\}', css):
        if selector_fragment in m.group(1):
            out.append(m.group(2))
    return out


# ---------------------------------------------------------------- T-1

def test_t1_all_templates_link_ui_v2_in_order():
    """T-1：10 个模板都含 ui-v2.css 的 <link>，且索引顺序 tokens < shell < ui-v2 < <style>。"""
    files = TEMPLATES
    assert len(files) == 10, f'模板数量应为 10，实测 {len(files)}'

    for f in files:
        text = _read(f)
        assert UI_V2_LINK in text, f'{f.name} 缺少 ui-v2.css 引用'
        assert text.count(UI_V2_LINK) == 1, f'{f.name} 里 ui-v2.css 出现 {text.count(UI_V2_LINK)} 次，应为 1'

        i_tok = text.index(TOKENS_LINK)
        i_shell = text.index(SHELL_LINK)
        i_ui = text.index(UI_V2_LINK)
        assert i_tok < i_shell, f'{f.name}: tokens.css 应在 workspace-shell.css 之前'
        assert i_shell < i_ui, f'{f.name}: ui-v2.css 应在 workspace-shell.css 之后'

        if '<style>' in text:
            assert i_ui < text.index('<style>'), f'{f.name}: ui-v2.css 必须在页内 <style> 之前'


# ---------------------------------------------------------------- T-2

def test_t2_ui_v2_exists_and_covers_five_layers():
    """T-2：ui-v2.css 存在且 ≥ 200 行；含 .sidebar / .topbar / .panel / button / input 五类规则。"""
    assert UI_V2.exists(), 'app/static/css/ui-v2.css 不存在'
    raw = _read(UI_V2)
    css = _read_css(UI_V2)

    lines = raw.count('\n') + 1
    assert lines >= 200, f'ui-v2.css 仅 {lines} 行，应 ≥ 200'

    for sel in ('.sidebar', '.topbar', '.panel', 'button', 'input'):
        assert re.search(r'[^{}]*' + re.escape(sel) + r'[^{}]*\{', css), f'ui-v2.css 缺少 {sel} 规则'


# ---------------------------------------------------------------- T-3

def test_t3_no_external_resources():
    """T-3：ui-v2.css 内无 @import、无任何外部 URL（SVG 命名空间除外）。"""
    assert UI_V2.exists()
    css = _read_css(UI_V2)

    assert '@import' not in css, 'ui-v2.css 不得使用 @import'

    # 剥离 SVG 命名空间后再查外链（xmlns 是 data-URI 的强制项，不是网络请求）
    stripped = XMLNS_RE.sub('', css)
    assert 'http://' not in stripped, 'ui-v2.css 出现 http:// 外链'
    assert 'https://' not in stripped, 'ui-v2.css 出现 https:// 外链'


# ---------------------------------------------------------------- T-4

def test_t4_topbar_sticky_not_fixed():
    """T-4：ui-v2.css 内 position:fixed 为 0；.topbar 规则用 position:sticky。"""
    assert UI_V2.exists()
    css = _read_css(UI_V2)

    assert 'position:fixed' not in css.replace(' ', ''), 'ui-v2.css 不得出现 position:fixed'

    bodies = _rule_body(css, '.topbar')
    assert bodies, 'ui-v2.css 缺少 .topbar 规则'
    assert any('position:sticky' in b.replace(' ', '') for b in bodies), '.topbar 必须用 position:sticky'


# ---------------------------------------------------------------- T-5

def test_t5_transitions_use_duration_tokens():
    """T-5：transition 出现 ≥ 6 次，且用到 var(--dur-fast) 或 var(--dur-base)。"""
    assert UI_V2.exists()
    css = _read_css(UI_V2)

    n = css.count('transition')
    assert n >= 6, f'transition 仅 {n} 次，应 ≥ 6'

    assert 'var(--dur-fast)' in css or 'var(--dur-base)' in css, \
        'transition 时长必须走 --dur-fast / --dur-base token'

    # token 必须真实存在（防止引用悬空）
    tokens = _read(TOKENS)
    for name in ('--dur-fast', '--dur-base', '--ease-out'):
        assert re.search(re.escape(name) + r'\s*:', tokens), f'tokens.css 缺少 {name}'


# ---------------------------------------------------------------- T-6

def test_t6_focus_ring_implemented():
    """T-6：focus-visible 出现 ≥ 2 次；input:focus 规则含 box-shadow（焦点环）。"""
    assert UI_V2.exists()
    css = _read_css(UI_V2)

    n = css.count('focus-visible')
    assert n >= 2, f'focus-visible 仅 {n} 次，应 ≥ 2'

    bodies = [b for b in _rule_body(css, 'input:focus')]
    assert bodies, 'ui-v2.css 缺少 input:focus 规则'
    assert any('box-shadow' in b for b in bodies), 'input:focus 必须带 box-shadow 焦点环'

    tokens = _read(TOKENS)
    assert re.search(r'--color-focus-ring\s*:', tokens), 'tokens.css 缺少 --color-focus-ring'


# ---------------------------------------------------------------- T-7

def test_t7_nav_icons_bound_by_href():
    """T-7：图标用 [href="…"] 属性选择器绑定 ≥ 10 条，每条都赋 --nav-icon，且 data-URI 前缀合法。"""
    assert UI_V2.exists()
    css = _read_css(UI_V2)

    # 只统计「绑定到 .nav-icon 的 href 规则」
    bindings = []
    for m in re.finditer(r'([^{}]+)\{([^{}]*)\}', css):
        sel, body = m.group(1), m.group(2)
        if '.nav-icon' in sel and HREF_BIND_RE.search(sel):
            bindings.append((sel.strip(), body))

    assert len(bindings) >= 10, f'图标绑定仅 {len(bindings)} 条，应 ≥ 10'

    for sel, body in bindings:
        assert '--nav-icon' in body, f'{sel} 未定义 --nav-icon'
        assert NAV_ICON_RE.search(body), f'{sel} 的 data-URI 前缀不合法'

    # 应用侧必须消费 --nav-icon（否则绑定是死代码）
    assert 'mask-image:var(--nav-icon)' in css.replace(' ', ''), \
        '.nav-icon 必须通过 mask-image 消费 --nav-icon'


# ---------------------------------------------------------------- T-12

def test_t12_global_reset_and_panel_card_declared():
    """T-12：ui-v2.css 含 body{margin:0}；且含 body .panel 与 body .card 两条等效声明。"""
    assert UI_V2.exists()
    css = _read_css(UI_V2)

    assert re.search(r'body\s*\{\s*margin\s*:\s*0\s*\}', css), \
        'ui-v2.css 必须含 body{margin:0}（修 6 个页面的 8px 白边）'

    for sel in ('body .panel', 'body .card'):
        assert re.search(re.escape(sel) + r'[\s,{]', css), f'ui-v2.css 缺少 {sel} 声明'

    # 卡片化白名单：必须覆盖 index 的两个裸 section，且不得误伤网格容器
    for w in ('section[id$="-panel"]', 'section#warehouse', 'section#purchase-workflow'):
        assert w in css, f'ui-v2.css 缺少卡片化选择器 {w}'
    for banned in ('#kpis', '#kpi-grid', '#knowledge'):
        assert banned not in css, f'ui-v2.css 误把网格容器 {banned} 纳入卡片化白名单'


# ================================================================ B 段（T-8..T-11）

def _style_of(name: str) -> str:
    """取模板全部 <style> 内容拼接。"""
    text = _read(TPL_DIR / name)
    return '\n'.join(re.findall(r'<style[^>]*>(.*?)</style>', text, re.S))


def test_t8_b_segment_deleted_skeleton_copies():
    """T-8（B 段）：页内骨架副本已删 —— dashboard/cockpit 顶层不再有
    .crumb/.panel-subtitle/.table-wrap/button 的等价副本；
    orders/content 的 button 副本已删。

    ⚠️ 按判定器（§6.4.1）实测，含锚点属性（height/width/position 等）的重名规则
    与全部 @media 响应式规则**有意保留** —— 这是「宁可重复、不可白屏」的裁定，
    所以本用例只断言已删清单，不断言 .sidebar/.topbar 不存在。
    """
    for name in ('dashboard.html', 'cockpit.html'):
        style = _style_of(name)
        for gone in ('.crumb{', '.panel-subtitle{', '.table-wrap{'):
            assert gone not in style, f'{name} 顶层 {gone} 副本未删'
        assert not re.search(r'[}]button\{', style), f'{name} 顶层 button{{ 副本未删'

    assert '.table-wrap{' not in _style_of('orders.html')
    assert not re.search(r'[}]button\{', _style_of('orders.html'))
    assert not re.search(r'[}]button\{', _style_of('content.html'))


def test_t9_media_and_anchor_whitelist_intact():
    """T-9（B 段）：@media 响应式块与尺寸锚点**必须仍在页内**（删除禁区）。

    dashboard 6 个 @media / cockpit 3 个；600px 断点的图表降高、
    .chart-refreshing 定位锚点、.cards 列数声明一个都不能少。
    """
    dash = _style_of('dashboard.html')
    ckpt = _style_of('dashboard.html')  # noqa 占位避免误用
    ckpt = _style_of('cockpit.html')

    assert len(re.findall(r'@media', dash)) == 6, \
        f'dashboard @media 数量异常：{len(re.findall(r"@media", dash))}（应为 6）'
    assert len(re.findall(r'@media', ckpt)) == 3, \
        f'cockpit @media 数量异常：{len(re.findall(r"@media", ckpt))}（应为 3）'

    # 600px 断点的响应式图表高度（误删会塌成 0 高）
    assert '.chart{height:260px' in dash, 'dashboard 600px 断点的 .chart 降高丢失'
    assert '.chart{height:260px' in ckpt, 'cockpit 600px 断点的 .chart 降高丢失'

    # 列数声明（shell 是 auto-fit，删了会变列数）
    assert 'grid-template-columns:repeat(4' in dash
    assert 'grid-template-columns:repeat(3' in ckpt

    # 刷新徽标定位锚点（删了徽标飘到页面角落）
    assert '.chart-refreshing{position:absolute' in ckpt


def test_t10_shell_and_main_regression_lock():
    """T-10（B 段）：回归锁 —— workspace-shell.css 仍含 P5-S1 的 5 个字面串；
    app/main.py 的 _WORKSPACE_NAV_ITEMS 图标字符未被修改。
    """
    shell = _read(SHELL)
    for lit in ('position:fixed', 'height:100vh', 'overflow-y:auto',
                'margin-left:232px', 'margin-left:76px'):
        assert lit in shell, f'workspace-shell.css 缺少字面串 {lit}'

    main_py = _read(ROOT / 'app' / 'main.py')
    for icon in ('◎', '▦', '▣', '◫', '✦', '◇', '▤', '♙'):
        assert icon in main_py, f'app/main.py 的导航图标 {icon} 被改动'


def test_t11_chart_size_anchors_survive():
    """T-11（B 段，G17 的机器实现）：图表尺寸锚点未丢 ——
    .chart 的 height 是 ECharts 容器唯一高度来源，删掉 = 空白图且不报错。
    """
    dash = _style_of('dashboard.html')
    ckpt = _style_of('cockpit.html')

    for lit in ('.chart{height:300px', '.quadrant-chart{height:360px',
                '.chart-grid{grid-template-columns', '.chart-empty'):
        assert lit in dash, f'dashboard 缺少图表锚点 {lit}'
    for lit in ('.chart{height:320px', '.chart-refreshing{position:absolute',
                '.chart-empty'):
        assert lit in ckpt, f'cockpit 缺少图表锚点 {lit}'


def test_t13_spacing_hardening_block():
    """T-13（段 10）：呼吸感强化块 —— !important 只允许出现在段 10 的 6 条规则里；
    段 10 的选择器与值必须逐条在位（用户 2026-11-02 硬性视觉要求）。
    """
    raw = _read(UI_V2)      # 原文（含注释），用于找段 10 标记
    css = _read_css(UI_V2)  # 剥注释版，用于数 !important（注释里的文字不计数）
    # 剥注释后，!important 只允许两类：prefers-reduced-motion 块的 2 处
    # + 段 10 的 6 条规则 = 8
    bangs = [m.start() for m in re.finditer(r'!important', css)]
    assert len(bangs) == 8, f'!important 出现 {len(bangs)} 次，应为 8（reduced-motion 2 + 段 10 的 6）'
    # 分界 = 段 10 第一条规则的起点（其后全部是授权区）
    first_rule = css.index('body h2{margin-bottom:var(--space-4) !important}')
    rm = css.index('prefers-reduced-motion')
    # 块内两处（transition-duration / animation-duration），终点取第二处之后
    rm_end = css.index('duration:.01ms !important',
                       css.index('duration:.01ms !important', rm) + 1) \
        + len('duration:.01ms !important')
    for b in bangs:
        assert b > first_rule or (rm < b < rm_end), \
            f'!important 出现在非授权区（offset {b}）'

    for lit in (
        'body h2{margin-bottom:var(--space-4) !important}',
        'body .status{margin-bottom:var(--space-4) !important}',
        'body .table-wrap{margin-top:var(--space-4) !important}',
        'body .field{gap:var(--space-2) !important}',
        'body .row{row-gap:var(--space-5) !important}',
        'body .actions{row-gap:var(--space-5) !important}',
    ):
        assert lit in css, f'段 10 缺少规则 {lit}'

    # token 刻度：--space-5 必须存在且为 20px
    tokens = _read(ROOT / 'app' / 'static' / 'css' / 'tokens.css')
    assert '--space-5: 20px' in tokens


def test_t14_bare_section_padding():
    """T-14（段 5 补丁）：/ops 裸 section 区块必须有内边距 —— 卡片化时只给了
    背景/边框/圆角，漏了 padding，导致商品资料/跨境平台事实/仓储协同/补货建议
    四个区块内容全部贴边（2026-11-02 用户复核）。
    今日待办的 16px 来自 index 页内 #operations-today-panel (1,0,0)——注意
    [id$=…] 是属性选择器（class 级），本规则 (0,1,2) 特意保持 16px 与其天然一致。
    """
    css = re.sub(r'\s+', ' ', _read_css(UI_V2))  # 多行规则归一成单行再断言
    # padding 规则在位（三行选择器组，16px 与用户认可的今日待办一致）
    assert 'body section[id$="-panel"], body section#warehouse, body section#purchase-workflow{padding:var(--space-4)}' in css, \
        '缺少裸 section padding 规则'
    # 首个 h2 顶部归零（否则 padding 16 + margin 24 = 40px 巨空）
    assert 'body section#purchase-workflow > h2:first-child{margin-top:0}' in css
    # 不得误用 24px（会与今日待办的 16px 分裂）
    assert 'purchase-workflow{padding:var(--space-6)}' not in css


# ================================================================ 段 11（P5-S4 细节精修）

def test_t15_p5s4_button_demotions():
    """T-15（段 11）：退出登录与 assistant 快捷提问降级 ghost ——
    #logout (1,0,1) / button[data-question] (0,1,2) 必须存在于 ui-v2.css，
    hover 分别走危险色与主色 soft token。
    """
    css = re.sub(r'\s+', ' ', _read_css(UI_V2))

    for sel in ('body #logout{', 'body #logout:hover:not(:disabled){',
                'body button[data-question]{', 'body button[data-question]:hover:not(:disabled){'):
        assert sel in css, f'段 11 缺少降级规则 {sel}'

    # 提取 body #logout 的 hover 规则体，断言危险色 token
    logout_hover = css[css.index('body #logout:hover:not(:disabled){'):]
    logout_hover = logout_hover[:logout_hover.index('}') + 1]
    for tok in ('--color-danger-muted', '--color-danger-strong', '--color-danger-border'):
        assert tok in logout_hover, f'#logout hover 缺少 {tok}'

    # chip hover 走主色 soft
    chip_hover = css[css.index('body button[data-question]:hover:not(:disabled){'):]
    chip_hover = chip_hover[:chip_hover.index('}') + 1]
    for tok in ('--color-primary-soft', '--color-primary-deep', '--color-primary-border'):
        assert tok in chip_hover, f'data-question chip hover 缺少 {tok}'


def test_t16_p5s4_today_panel_and_chart_warning_alignment():
    """T-16（段 11）：今日待办白卡化（米黄底 → 白底 + 左侧 3px 警示色条）；
    dashboard/cockpit 图表橙色已对齐 --color-warning 的 #d58b00，亮橙 #eb6834 清零。
    """
    css = re.sub(r'\s+', ' ', _read_css(UI_V2))

    rule = css[css.index('body section#operations-today-panel{'):]
    rule = rule[:rule.index('}') + 1]
    assert 'background:var(--color-surface)' in rule, '今日待办必须白卡化'
    assert 'border-left:3px solid var(--color-warning)' in rule, '缺少左侧警示色条'
    assert '--color-bg-warm' not in rule, '不得再引用米黄底 token'

    for name in ('dashboard.html', 'cockpit.html'):
        text = _read(TPL_DIR / name)
        assert '#eb6834' not in text, f'{name} 仍含亮橙 #eb6834'
        assert 'rgba(235,104,52' not in text, f'{name} 仍含亮橙 rgba'
        assert '#d58b00' in text, f'{name} 缺少对齐后的 #d58b00'


def test_t17_p5s4_section11_hygiene():
    """T-17（段 11 卫生）：段 11 分界之后无 !important、无裸 hex 色值（G14 延续）；
    全文件 ！important 总数保持 8（T-13 授权区不因追加而失效）。
    """
    raw = _read(UI_V2)
    css = _read_css(UI_V2)

    assert len(re.findall(r'!important', css)) == 8, \
        '!important 总数应为 8（reduced-motion 2 + 段 10 的 6），段 11 不得新增'

    marker = '段 11：细节精修'
    assert marker in raw, 'ui-v2.css 缺少段 11 注释分界'
    # marker 在注释文字内部，分界必须回退到包含它的注释块的起点
    tail = raw[raw.rfind('/*', 0, raw.index(marker)):]
    tail_no_comment = re.sub(r'/\*.*?\*/', '', tail, flags=re.S)

    bangs_after = len(re.findall(r'!important', tail_no_comment))
    assert bangs_after == 0, f'段 11 出现 {bangs_after} 处 !important（应为 0）'

    hexes = re.findall(r'#[0-9a-fA-F]{3,8}\b', tail_no_comment)
    assert not hexes, f'段 11 出现裸色值 {hexes}（G14：必须走 token）'

    # 控件圆角统一 8px（就地改的两处声明）
    flat = re.sub(r'\s+', ' ', css)
    assert 'border:1px solid var(--color-border-input); border-radius:var(--radius-md);' in flat, \
        '输入控件圆角应为 var(--radius-md)'
    assert 'border:0;border-radius:var(--radius-md);' in flat, \
        '按钮圆角应为 var(--radius-md)'