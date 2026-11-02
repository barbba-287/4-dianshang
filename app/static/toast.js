/* WorkBuddy 全局 Toast（无依赖，配合 ui-v2.css 段 13 样式）。
 * 用法：toast.success('已保存') / toast.error('失败') / toast.warning('注意')。
 * 行为：右下角堆叠（同屏最多 4 条）、3.2s 自动消失；动画在
 * prefers-reduced-motion 下被 ui-v2 既有全局块关闭。
 */
(function () {
  'use strict';
  var TYPES = {
    success: { cls: 'toast-success', icon: '✓' },
    error: { cls: 'toast-error', icon: '✕' },
    warning: { cls: 'toast-warning', icon: '!' }
  };
  function root() {
    var el = document.getElementById('toast-root');
    if (!el) {
      el = document.createElement('div');
      el.id = 'toast-root';
      el.setAttribute('aria-live', 'polite');
      document.body.appendChild(el);
    }
    return el;
  }
  function show(msg, type) {
    var conf = TYPES[type] || TYPES.success;
    var host = root();
    while (host.children.length >= 4) host.removeChild(host.firstChild);
    var item = document.createElement('div');
    item.className = 'toast ' + conf.cls;
    item.setAttribute('role', 'status');
    var mark = document.createElement('span');
    mark.className = 'toast-mark';
    mark.textContent = conf.icon;
    var text = document.createElement('span');
    text.className = 'toast-text';
    text.textContent = String(msg == null ? '' : msg);
    item.appendChild(mark);
    item.appendChild(text);
    host.appendChild(item);
    setTimeout(function () {
      item.classList.add('toast-leave');
      setTimeout(function () { item.remove(); }, 250);
    }, 3200);
  }
  window.toast = {
    show: show,
    success: function (m) { show(m, 'success'); },
    error: function (m) { show(m, 'error'); },
    warning: function (m) { show(m, 'warning'); }
  };
})();
