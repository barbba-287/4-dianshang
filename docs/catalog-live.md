# 淘宝公开竞品 Canary 说明

## 目标

`app/catalog_live.py` 提供淘宝公开搜索页的受控 Canary 采集能力。它使用关键词构造搜索 URL，只读取无需登录即可看到的公开商品标题、明确单一价格和商品详情链接；不打开详情页，不登录，不写数据库。

当前首次测试关键词：

```text
白色波点半身裙女夏秋季2026新款高腰a字包臀半裙miu里miu气短裙
```

对应搜索 URL：

```text
https://s.taobao.com/search?q=%E7%99%BD%E8%89%B2%E6%B3%A2%E7%82%B9%E5%8D%8A%E8%BA%AB%E8%A3%99%E5%A5%B3%E5%A4%8F%E7%A7%8B%E5%AD%A32026%E6%96%B0%E6%AC%BE%E9%AB%98%E8%85%B0a%E5%AD%97%E5%8C%85%E8%87%80%E5%8D%8A%E8%A3%99miu%E9%87%8Cmiu%E6%B0%94%E7­è£
```

用户手动打开该 URL 时已经反馈页面出现拦截。因此真实采集成功与否仍是未知；不能通过任何手段绕过拦截。

## 双重门禁

真实网络默认关闭。只有同时满足以下条件，CLI 才会启动浏览器：

1. 环境变量 `TAOBAO_CATALOG_LIVE_ENABLED=true`；
2. 命令中显式提供 `--confirm-public-read-only`。

PowerShell 完整单行命令：

```powershell
$env:TAOBAO_CATALOG_LIVE_ENABLED="true"; python -m app.cli collect-taobao-catalog --keyword "白色波点半身裙女夏秋季2026新款高腰a字包臀半裙miu里miu气短裙" --confirm-public-read-only --max-pages 1 --delay-ms 3000 --timeout-seconds 20 --total-timeout-seconds 60 --artifacts-dir artifacts
```

这是一次单页人工 Canary，不是定时任务，不接后台队列，不落库，不自动重试。执行前应确认平台规则、企业业务边界和适用法律；命令运行期间不得提供 Cookie、Token、storage state 或代理凭证。

## 结果语义

- `LIVE_NETWORK_DISABLED`：没有显式打开真实网络开关；不会启动浏览器；
- `LIVE_CONFIRMATION_REQUIRED`：缺少人工只读确认；不会启动浏览器；
- `BLOCKED_PAGE` / `ACCESS_DENIED`：登录、验证码、人机验证、风控或平台拒绝；立即停止；
- `RATE_LIMITED`：收到 429；立即停止，不刷新、不重试；
- `REDIRECT_NOT_ALLOWED`：重定向到登录、安全域或其他不允许地址；
- `DOM_SCHEMA_MISMATCH`：页面结构无法按 `taobao_search_v1` 明确解析；不猜测、不静默丢数据；
- `PAGE_TIMEOUT` / `REMOTE_HTTP_ERROR`：页面或平台错误；本次 CLI 不自动重试；
- 成功时返回公开商品记录的数量、页数和脱敏 artifact refs，不写 `products` 或库存/订单数据。

## 访问边界

- 页面导航只允许 `https://s.taobao.com/search`；
- 商品链接只接受 HTTPS 的 `item.taobao.com/item.htm` 或 `detail.tmall.com/item.htm`，且必须有纯数字 `id`；
- 不访问详情页、不读取隐藏接口或内嵌状态 JSON；
- 不使用登录态、Cookie、Token、Authorization、代理池、stealth、指纹伪装或验证码处理；
- 单页、串行、限速、限商品数；遇阻断或页面改版立即停止；
- 真实竞品数据不进入自有店铺商品、SKU、库存、订单、销量或补货账本。

页面被平台拦截不是需要“修复”的代码异常，而是应尊重的停止信号。替代方案是合规第三方数据服务或人工记录/导入。
