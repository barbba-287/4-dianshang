# MCP Demo Runbook

## 环境

- Python 3.11+
- 项目依赖：`python -m pip install -r requirements.txt`
- 当前 Demo 不需要真实 LLM Key；数据为本地 mock。
- 生产接 Webman 时使用环境变量/secret_ref 注入 URL 和 Token，不把凭证写入代码或日志。

## 启动

```powershell
cd D:\ai\dianshang
python -m app.mcp_server
```

## 排错

- `METHOD_NOT_FOUND`：确认方法是 `tools/list` 或 `tools/call`。
- `TOOL_NOT_FOUND`：检查工具名是否在 tools/list 返回值中。
- `WORKSPACE_PARAMETER_FORBIDDEN`：workspace 必须来自可信 session，不允许模型参数覆盖。
- `INVALID_SALES_WINDOW`：销售窗口限定为 2～90 天。
- `CAMPAIGN_NOT_FOUND`：广告数据缺失时返回 unknown，而不是零 ROI。
- 进程停止：输入 EOF 后服务退出；生产部署由进程管理器负责重启和日志轮换。

## 替换为 Webman

保留四个 Skill 的输入/输出 schema，只替换 `MockEcomDataSource`：

```text
MockEcomDataSource
        ↓
WebmanEcomDataSource（HTTP + Token 透传）
```

必须增加：连接超时、重试/熔断、HTTP 状态与业务状态分离、响应 schema 校验、request ID 透传、workspace/session scope、脱敏日志和接口版本映射。
