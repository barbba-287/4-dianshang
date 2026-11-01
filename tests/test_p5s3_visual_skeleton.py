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
