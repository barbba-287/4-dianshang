# -*- coding: utf-8 -*-
"""P5-S1 设计 Token 与全局样式收敛 —— 契约用例（T-1 .. T-8）。

对应 dev_task.p5s1-ui-tokens.md §6。
本文件只做静态契约校验，不启动服务、不触碰数据库。
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOKENS = ROOT / 'app' / 'static' / 'css' / 'tokens.css'
SHELL = ROOT / 'app' / 'static' / 'workspace-shell.css'
TPL_DIR = ROOT / 'app' / 'templates'

TEMPLATES = sorted(TPL_DIR.glob('*.html'))

TOKENS_LINK = '<link rel="stylesheet" href="/static/css/tokens.css">'
SHELL_LINK = '<link rel="stylesheet" href="/static/workspace-shell.css">'

HEX_RE = re.compile(r'#[0-9a-fA-F]{3,8}\b')
RGBA_RE = re.compile(r'rgba?\(')
VAR_RE = re.compile(r'var\(\s*(--[A-Za-z0-9_-]+)')


def _read(path: Path) -> str:
    return path.read_text(encoding='utf-8')


def _style_blocks(text: str) -> str:
    """抽取所有 <style> 块内容拼接。"""
    return '\n'.join(re.findall(r'<style[^>]*>(.*?)</style>', text, re.S))


def _script_blocks(text: str) -> str:
    """抽取所有 <script> 块内容拼接。"""
    return '\n'.join(re.findall(r'<script[^>]*>(.*?)</script>', text, re.S))


def _defined_vars(css: str):
    """返回 {变量名: 值} 的定义表（后定义覆盖先定义）。"""
    out = {}
    for m in re.finditer(r'(--[A-Za-z0-9_-]+)\s*:\s*([^;{}]+)', css):
        out[m.group(1)] = m.group(2).strip()
    return out


# ---------------------------------------------------------------- T-1

def test_t1_token_completeness():
    """T-1：tokens.css 存在且含语义色 13 / 中性 11 / 字号 7 / 行高 2 /
    间距 6 / 圆角 4 / 阴影 2 / 字体 2。"""
    assert TOKENS.exists(), 'app/static/css/tokens.css 不存在'
    css = _read(TOKENS)
    defined = set(_defined_vars(css))

    semantic = {
        '--color-primary', '--color-primary-hover', '--color-primary-soft',
        '--color-success', '--color-warning', '--color-danger', '--color-info',
        '--color-bg', '--color-surface', '--color-border', '--color-text',
        '--color-text-muted', '--color-nav-bg',
    }
    assert semantic <= defined, '语义色缺失：%s' % sorted(semantic - defined)

    neutral = {'--neutral-%d' % i for i in range(0, 1000, 100)}
    assert neutral <= defined, '中性色阶缺失：%s' % sorted(neutral - defined)

    sizes = {'--text-xs', '--text-sm', '--text-base', '--text-md',
             '--text-lg', '--text-xl', '--text-2xl'}
    assert sizes <= defined, '字号 token 缺失：%s' % sorted(sizes - defined)

    assert {'--leading-tight', '--leading-normal'} <= defined, '行高 token 缺失'

    spaces = {'--space-%s' % s for s in ('1', '2', '3', '4', '6', '8')}
    assert spaces <= defined, '间距 token 缺失：%s' % sorted(spaces - defined)

    radii = {'--radius-sm', '--radius-md', '--radius-lg', '--radius-pill'}
    assert radii <= defined, '圆角 token 缺失：%s' % sorted(radii - defined)

    assert {'--shadow-card', '--shadow-overlay'} <= defined, '阴影 token 缺失'
    assert {'--font-sans', '--font-mono'} <= defined, '字体 token 缺失'

    # 兼容别名层必须在位（§4.1 强制要求）
    aliases = {'--page', '--panel', '--ink', '--muted', '--line', '--brand',
               '--brand-soft', '--good', '--warning', '--critical', '--danger',
               '--shadow'}
    assert aliases <= defined, '兼容别名缺失：%s' % sorted(aliases - defined)
    assert 'deprecated' in css, '兼容别名层缺少 deprecated 标注'


# ---------------------------------------------------------------- T-2

def test_t2_no_dangling_css_variable_reference():
    """T-2（核心）：tokens.css + workspace-shell.css + 10 个模板中
    所有 var(--x) 引用的变量都必须有定义。"""
    defined = set(_defined_vars(_read(TOKENS)))
    defined |= set(_defined_vars(_read(SHELL)))

    dangling = {}
    sources = [('tokens.css', _read(TOKENS)), ('workspace-shell.css', _read(SHELL))]
    for path in TEMPLATES:
        sources.append((path.name, _read(path)))

    for name, text in sources:
        missing = sorted({v for v in VAR_RE.findall(text) if v not in defined})
        if missing:
            dangling[name] = missing

    assert not dangling, '存在悬空 CSS 变量引用：%s' % dangling


# ---------------------------------------------------------------- T-3

def test_t3_single_source_of_truth():
    """T-3：模板内无 :root 残留；--color-primary 在 tokens.css 中恰好定义一次。"""
    for path in TEMPLATES:
        text = _read(path)
        assert ':root' not in text, '%s 仍残留 :root 定义' % path.name

    css = _read(TOKENS)
    assert len(re.findall(r'--color-primary\s*:', css)) == 1, \
        '--color-primary 必须只定义一次'


# ---------------------------------------------------------------- T-4

def test_t4_tokens_link_injected_and_ordered():
    """T-4：10 个模板都注入 tokens.css 的 <link>，且早于 workspace-shell.css。"""
    assert len(TEMPLATES) == 10, '模板数量异常：%d' % len(TEMPLATES)
    for path in TEMPLATES:
        text = _read(path)
        assert TOKENS_LINK in text, '%s 缺少 tokens.css 的 link' % path.name
        assert SHELL_LINK in text, '%s 缺少 workspace-shell.css 的 link' % path.name
        assert text.index(TOKENS_LINK) < text.index(SHELL_LINK), \
            '%s 的 tokens.css link 必须早于 workspace-shell.css' % path.name


# ---------------------------------------------------------------- T-5

def test_t5_shell_literal_strings_intact():
    """T-5：workspace-shell.css 的 5 个关键字面串仍在（防格式化守卫）。"""
    css = _read(SHELL)
    for literal in ('position:fixed', 'height:100vh', 'overflow-y:auto',
                     'margin-left:232px', 'margin-left:76px'):
        assert literal in css, 'workspace-shell.css 丢失字面串：%s' % literal


# ---------------------------------------------------------------- T-6

def test_t6_no_hardcoded_color_in_template_styles():
    """T-6：10 个模板 <style> 内 hex = 0，rgba( = 0。"""
    for path in TEMPLATES:
        style = _style_blocks(_read(path))
        hexes = HEX_RE.findall(style)
        assert not hexes, '%s <style> 残留 hex 色值：%s' % (path.name, hexes[:5])
        rgbas = RGBA_RE.findall(style)
        assert not rgbas, '%s <style> 残留 rgba() 色值：%s' % (path.name, rgbas[:5])


# ---------------------------------------------------------------- T-7

def test_t7_script_blocks_unchanged():
    """T-7：10 个模板 <script> 内色值合计 = 21（P5-S6 后）。

    迁移说明：chart 轴/legend 颜色已迁至 app/static/echarts-theme.js（registerTheme 'wb'）；
    剩余 21 处 = series 语义色（cockpit 7 + dashboard 13）+ 1 处 customer_service 的
    jQuery 选择器 $('#add') 误计入。"""
    total = 0
    for path in TEMPLATES:
        script = _script_blocks(_read(path))
        total += len(HEX_RE.findall(script))
        total += len(RGBA_RE.findall(script))
    assert total == 21, '<script> 内色值合计应为 21，实际 %d' % total


# ---------------------------------------------------------------- T-8

def test_t8_semantic_alias_equivalence():
    """T-8：--brand/--shell-brand → --color-primary；--danger = --color-danger；
    --line/--shell-line = --color-border。"""
    tokens = _defined_vars(_read(TOKENS))
    shell = _defined_vars(_read(SHELL))

    assert tokens['--brand'] == 'var(--color-primary)'
    assert shell['--shell-brand'] == 'var(--color-primary)'
    assert tokens['--danger'] == 'var(--color-danger)'
    assert tokens['--critical'] == 'var(--color-danger)'
    assert tokens['--line'] == 'var(--color-border)'
    assert shell['--shell-line'] == 'var(--color-border)'
