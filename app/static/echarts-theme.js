/* WorkBuddy ECharts 主题（registerTheme 'wb'）
 * 色值必须与 app/static/css/tokens.css 同步，改 token 时同步改这里：
 *   主色 #2a78d6 = --color-primary；成功 #0ca30c = --color-success；
 *   警示 #d58b00 = --color-warning；危险 #d03b3b = --color-danger；
 *   紫 #8b5cf6 = 图表辅助色（cockpit 现值）；文本 #142033 = --color-text；
 *   次级文本 #69778b = --color-text-muted；网格/轴线 #e5e7eb = gray-200 量级。
 * 用法：模板引入本文件后，echarts.init(el, 'wb')。
 */
(function () {
  if (typeof echarts === 'undefined') return;
  echarts.registerTheme('wb', {
    color: ['#2a78d6', '#0ca30c', '#d58b00', '#d03b3b', '#8b5cf6'],
    textStyle: { color: '#142033' },
    categoryAxis: {
      axisLine: { lineStyle: { color: '#e5e7eb' } },
      axisTick: { lineStyle: { color: '#e5e7eb' } },
      axisLabel: { color: '#69778b' },
      splitLine: { show: false }
    },
    valueAxis: {
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: { color: '#69778b' },
      splitLine: { lineStyle: { color: '#e5e7eb' } }
    },
    legend: { textStyle: { color: '#69778b' } },
    tooltip: {
      backgroundColor: '#ffffff',
      borderColor: '#e5e7eb',
      borderWidth: 1,
      borderRadius: 8,
      textStyle: { color: '#142033' },
      extraCssText: 'box-shadow:0 4px 12px rgba(20,32,51,.08);'
    }
  });
})();
