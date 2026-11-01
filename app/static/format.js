/* ==========================================================================
   P5-S2 展示层格式化工具
   --------------------------------------------------------------------------
   用途：金额固定两位小数、数量规范化。**只做显示格式化**：
     - 不做币种换算、不做汇率折算、不做任何跨行聚合；
     - 不修改接口返回的原值；
     - 仅在渲染时把数值转成可扫读的字符串。
   依据：dev_task.p5s2-ui-tables-and-empty.md §D1.2（裁定 Q-P5S2-2）
   ========================================================================== */

(function (global) {
  'use strict';

  /** 宽松转数字：null / undefined / 空串 / 非有限数 → null */
  function toNumber(value) {
    if (value === null || value === undefined || value === '') return null;
    if (typeof value === 'number') return Number.isFinite(value) ? value : null;
    if (typeof value === 'boolean') return null;
    var text = String(value).trim();
    if (text === '') return null;
    var parsed = Number(text);
    return Number.isFinite(parsed) ? parsed : null;
  }

  /**
   * 金额：固定两位小数；非数值 → '—'。
   * currency 仅作为原样后缀展示，不做任何折算。
   */
  function dianshangAmount(value, currency) {
    var number = toNumber(value);
    if (number === null) return '—';
    var text = number.toFixed(2);
    var unit = currency === null || currency === undefined ? '' : String(currency).trim();
    return unit ? text + ' ' + unit : text;
  }

  /**
   * 数量：整数直接显示；非整数保留两位小数；非数值 → '—'。
   */
  function dianshangQty(value) {
    var number = toNumber(value);
    if (number === null) return '—';
    if (Number.isInteger(number)) return String(number);
    return number.toFixed(2);
  }

  global.dianshangAmount = dianshangAmount;
  global.dianshangQty = dianshangQty;
})(typeof window !== 'undefined' ? window : this);
