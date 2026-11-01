# -*- coding: utf-8 -*-
"""P5-S2 表格可读性 / 空态与加载态 —— 契约用例（T-1 .. T-8）。

对应 dev_task.p5s2-ui-tables-and-empty.md §7。
本文件只做静态契约校验，不启动服务、不触碰数据库。

约定：
  - 实施页 = index / dashboard / orders / warehouse / customer_service / content / assistant；
    禁区页 admin_users / shopify / cockpit 不在 T-2 / T-6 的扫描范围内。
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TPL_DIR = ROOT / 'app' / 'templates'
SHELL = ROOT / 'app' / 'static' / 'workspace-shell.css'
TOKENS = ROOT / 'app' / 'static' / 'css' / 'tokens.css'
FORMAT_JS = ROOT / 'app' / 'static' / 'format.js'

IMPLEMENT_PAGES = ('index', 'dashboard', 'orders', 'warehouse',
                   'customer_service', 'content', 'assistant')

HEX_RE = re.compile(r'#[0-9a-fA-F]{3,8}\b')
RGBA_RE = re.compile(r'rgba?\(')


def _read(path: Path) -> str:
    return path.read_text(encoding='utf-8')


def _shell() -> str:
    return _read(SHELL)


def _tpl(name: str) -> str:
    return _read(TPL_DIR / ('%s.html' % name))


# ---------------------------------------------------------------- T-1

def test_t1_numeric_column_class_is_global_and_unique():
    """T-1：`.num` 数值列类在全站只有一处定义（shell），且右对齐 + 等宽。"""
    shell = _shell()
    assert '.num{' in shell, 'workspace-shell.css 缺少 .num 数值列类'

    rule = re.search(r'\.num\{([^}]*)\}', shell)
    assert rule, '无法解析 .num 规则'
    body = rule.group(1)
    assert 'text-align:right' in body, '.num 必须右对齐，实际：%s' % body
    assert 'tabular-nums' in body, '.num 必须等宽数字，实际：%s' % body

    # 全站 .num{ 定义恰好 1 处（cockpit 页内那份已删除；.quadrant-num 不算）
    definitions = 0
    for path in sorted(TPL_DIR.glob('*.html')):
        text = _read(path)
        # 只统计选择器起始的 `.num{`（避免命中 .quadrant-num{）
        definitions += len(re.findall(r'(?<![\w-])\.num\{', text))
    definitions += len(re.findall(r'(?<![\w-])\.num\{', shell))
    assert definitions == 1, '全站 .num{ 定义应为 1 处，实际 %d 处' % definitions


# ---------------------------------------------------------------- T-2

def test_t2_numeric_columns_covered_per_page():
    """T-2：实施页的数值列都标注了 class="num"。"""
    thresholds = {'orders': 4, 'index': 3, 'dashboard': 10, 'warehouse': 2}
    for page, minimum in thresholds.items():
        text = _tpl(page)
        count = text.count('class="num"')
        assert count >= minimum, \
            '%s 的 class="num" 数量应为 >= %d，实际 %d' % (page, minimum, count)


# ---------------------------------------------------------------- T-3

def test_t3_amount_formatting_is_display_only():
    """T-3：金额两位小数由 format.js 提供，且不含任何币种换算。"""
    assert FORMAT_JS.exists(), 'app/static/format.js 不存在'
    source = _read(FORMAT_JS)
    assert 'toFixed(2)' in source, 'format.js 必须固定两位小数'

    # 不允许出现汇率 / 币种换算表达式
    assert not re.search(r'\*\s*rate|exchange_rate|toCurrency|convertCurrency',
                         source, re.I), 'format.js 不得包含币种换算逻辑'

    orders = _tpl('orders')
    assert 'dianshangAmount' in orders, 'orders 未调用 dianshangAmount'
    # 主表与详情表两处金额渲染路径都要走 format.js
    assert orders.count('dianshangAmount') >= 2, \
        'orders 主表与详情表的金额都应调用 dianshangAmount'


# ---------------------------------------------------------------- T-4

def test_t4_badge_component_and_five_levels():
    """T-4：shell 提供 .badge 与五档语义，色值全部来自 tokens.css。"""
    shell = _shell()
    assert '.badge{' in shell, 'shell 缺少 .badge 组件'
    for level in ('success', 'warning', 'danger', 'info', 'neutral'):
        assert '.badge-%s{' % level in shell, 'shell 缺少 .badge-%s' % level

    # 色值必须来自 tokens.css 中已定义的变量，不得出现裸 hex / rgba
    tokens = _read(TOKENS)
    defined = set(re.findall(r'(--[A-Za-z0-9_-]+)\s*:', tokens))
    for level in ('success', 'warning', 'danger', 'info', 'neutral'):
        rule = re.search(r'\.badge-%s\{([^}]*)\}' % level, shell)
        assert rule, '无法解析 .badge-%s 规则' % level
        body = rule.group(1)
        assert not HEX_RE.search(body), \
            '.badge-%s 出现裸 hex 色值：%s' % (level, body)
        assert not RGBA_RE.search(body), \
            '.badge-%s 出现裸 rgba 色值：%s' % (level, body)
        for var_name in re.findall(r'var\(\s*(--[A-Za-z0-9_-]+)', body):
            assert var_name in defined, \
                '.badge-%s 引用了 tokens.css 未定义的变量 %s' % (level, var_name)

    # 实施页的状态单元格确实使用 .badge
    for page in IMPLEMENT_PAGES:
        text = _tpl(page)
        if '<table' not in text and 'makeTable(' not in text:
            continue
        if page == 'assistant':
            continue  # Q-P5S2-5：assistant 只做空态，不做 badge 迁移
        assert 'badge' in text, '%s 未使用 .badge' % page


# ---------------------------------------------------------------- T-5

def test_t5_zebra_striping_and_border_unchanged():
    """T-5：shell 有斑马纹且背景取自 token；th,td 的 border-bottom 未加粗。"""
    shell = _shell()
    assert 'tbody tr:nth-child(even)' in shell, 'shell 缺少表格斑马纹规则'

    rule = re.search(r'tbody tr:nth-child\(even\)\{([^}]*)\}', shell)
    assert rule, '无法解析斑马纹规则'
    body = rule.group(1)
    assert 'var(--' in body, '斑马纹背景必须取自 token，实际：%s' % body
    assert '--color-bg-faint' not in body, \
        '斑马纹禁止使用 --color-bg-faint（与斑马纹几乎同色）'

    # border-bottom 保持 1px，未加粗
    assert not re.search(r'border-bottom:\s*(2px|3px|medium|thick)', shell), \
        'th,td 的 border-bottom 不得加粗'


# ---------------------------------------------------------------- T-6

def test_t6_empty_state_component_unified():
    """T-6：shell 提供空态三层结构；7 个实施页不再出现 <td colspan class="muted">。"""
    shell = _shell()
    for cls in ('.empty-title', '.empty-text', '.empty-action'):
        assert cls in shell, 'shell 缺少 %s' % cls

    # 实施页（排除禁区页 admin_users / shopify）
    for page in IMPLEMENT_PAGES:
        text = _tpl(page)
        offenders = re.findall(r'<td colspan="\d+" class="muted"', text)
        assert not offenders, \
            '%s 仍存在 <td colspan class="muted"> 形态空态：%d 处' % (page, len(offenders))

    # 禁区页保持原样（零改动守护）
    for page in ('admin_users', 'shopify'):
        text = _tpl(page)
        assert 'class="muted"' in text, '%s 是禁区页，不应有改动' % page

    # G5 附加：模板内不得出现悬空 var() 引用
    tokens = _read(TOKENS)
    defined = set(re.findall(r'(--[A-Za-z0-9_-]+)\s*:', tokens))
    defined |= set(re.findall(r'(--[A-Za-z0-9_-]+)\s*:', _shell()))
    var_re = re.compile(r'var\(\s*(--[A-Za-z0-9_-]+)')
    for path in sorted(TPL_DIR.glob('*.html')):
        missing = sorted({v for v in var_re.findall(_read(path)) if v not in defined})
        assert not missing, '%s 存在悬空 var() 引用：%s' % (path.name, missing)


# ---------------------------------------------------------------- T-7

def test_t7_loading_placeholder_and_no_fullscreen_overlay():
    """T-7：首屏空白表格有加载占位行；无新增 position:fixed 全屏遮罩。"""
    # 6 处首屏空白表格的加载占位
    loading_pages = {
        'index': 2,       # 库存表 + 补货/采购表
        'orders': 1,      # 订单主表
        'customer_service': 2,  # 工单表 + 知识文档表
    }
    for page, minimum in loading_pages.items():
        text = _tpl(page)
        count = text.count('class="loading-cell"')
        assert count >= minimum, \
            '%s 的加载占位行应为 >= %d，实际 %d' % (page, minimum, count)

    # 不得出现新的 position:fixed 全屏遮罩
    # 既有侧栏（.sidebar）在 cockpit / dashboard 页内定义了 position:fixed，属既有实现，不算遮罩。
    # 因此只允许 position:fixed 出现在 .sidebar 规则中，其他任何选择器出现即视为新增遮罩。
    for path in sorted(TPL_DIR.glob('*.html')):
        text = _read(path)
        for match in re.finditer(r'([^{}]*)\{([^}]*)\}', text):
            selector, body = match.group(1), match.group(2)
            if 'position:fixed' not in body:
                continue
            assert selector.strip().endswith('.sidebar'), \
                '%s 出现非侧栏的 position:fixed 全屏遮罩：%s' % (path.name, selector.strip())

    # .chart-refreshing 不得用于整块替换
    shell = _shell()
    assert '.chart-refreshing' not in shell, 'shell 不得引入 .chart-refreshing'


# ---------------------------------------------------------------- T-8

def test_t8_format_js_loaded_synchronously_before_inline_script():
    """T-8（G17）：使用 format.js 的页面必须同步引入，且早于调用它的内联 <script>。"""
    pages = [p for p in sorted(TPL_DIR.glob('*.html'))
             if 'dianshangAmount' in _read(p) or 'dianshangQty' in _read(p)]
    assert pages, '没有页面使用 format.js'

    for path in pages:
        text = _read(path)
        tag = re.search(r'<script src="/static/format\.js[^"]*"[^>]*>', text)
        assert tag, '%s 缺少 format.js 的 <script src>' % path.name
        tag_text = tag.group(0)
        assert 'defer' not in tag_text, '%s 的 format.js 禁止 defer' % path.name
        assert 'async' not in tag_text, '%s 的 format.js 禁止 async' % path.name

        # 必须早于第一个内联 <script>（不带 src 的）
        inline = re.search(r'<script(?![^>]*\bsrc=)[^>]*>', text)
        assert inline, '%s 没有内联 script' % path.name
        assert text.index(tag_text) < text.index(inline.group(0)), \
            '%s 的 format.js 必须早于内联 <script>' % path.name
