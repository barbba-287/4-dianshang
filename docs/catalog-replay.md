# 竞品公开类目回放采集说明

## 当前交付范围

当前实现的是 `local_html_replay`：使用 Playwright 读取 manifest 明确列出的本地 HTML 页面，验证商品卡片选择器、分页、去重、阻断检测、限页、产物和错误语义。

```bash
python -m app.cli replay-catalog \
  --manifest tests/fixtures/catalog_replay/manifest.json \
  --max-pages 3 \
  --delay-ms 1000 \
  --artifacts-dir artifacts
```

命令只输出 JSON 摘要，不访问网络、不写数据库、不调用淘宝/天猫 API。当前结果可以证明本地回放链路成立，不能证明真实平台竞品页面已经接入。

## Manifest 约定

```json
{
  "mode": "local_html_replay",
  "root": ".",
  "entrypoint": "page-1.html"
}
```

页面使用项目约定的商品卡片结构：

- `[data-product-card]`
- `data-product-id`
- `[data-field="title"]`
- `a[data-field="url"]`
- `[data-field="price"]`
- 可选 `data-category`、`data-rating`、`data-currency`
- 可选 `a[data-next-page]` 指向 root 内的下一页 HTML

所有页面必须位于 manifest 的 root 目录内。HTTP/HTTPS、路径越界、绝对路径和外部分页链接都会被拒绝。页数达到上限但仍有下一页时，结果标记为 `partial` 并返回 `PAGE_LIMIT_EXCEEDED`，不能把部分数据当成完整数据。

## 产物

每次运行会在 `artifacts/catalog-replay/<run_id>/` 生成：

- `run.json`：模式、页数、完整度、错误码和限制参数；
- `raw-products.json`：标准化后的商品回放快照；
- `page-*.png` / `failure-page-*.png`：页面诊断截图；
- `trace.zip`：Playwright 诊断 Trace（浏览器可用时）。

产物目录已被 Git 忽略，不应提交真实页面中的 Cookie、Token、个人信息或完整敏感响应。

## 与真实竞品页面的边界

未来如要读取淘宝/天猫其他店铺的竞品商品页面，只能另行评估真实公开页面 transport：

1. 只读取无需登录即可访问的公开商品信息；
2. 不登录竞品店铺后台，不使用 Cookie、Token 或其他登录态；
3. 不读取订单、库存、销售额、买家信息等非公开经营数据；
4. 不调用隐藏接口、破解签名、绕过验证码、人机验证、MFA、风控或访问限制；
5. 人工触发、串行、限页、限速，遇到登录墙、验证码、429、风控或异常页面立即停止；
6. 先确认平台规则、访问限制和适用的法律合规要求；必要时改用合规第三方数据服务或人工录入。

“RPA 不需要 API”不等于“RPA 可以绕过平台权限”。模拟浏览器只是一种技术实现方式，不能保证平台允许访问，也不能将公开页面理解为可以无限量或商业化批量抓取。

自有店铺的后台报表/库存/订单自动化属于另一条明确授权的 RPA 方案，应优先使用官方导出和最小权限子账号，不能与竞品公开页面采集混用。
