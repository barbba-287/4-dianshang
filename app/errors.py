"""领域异常定义。

用途：承载业务层对外暴露的稳定错误契约（错误码 / 文案 / HTTP 状态码），
由 `app/main.py` 注册的唯一异常处理器映射为统一响应体。

约束：
- 本模块**不得** import fastapi（保持领域层与 Web 框架解耦）；
- 本模块只做"错误事实"的承载，不做日志、重试、i18n、序列化等附加能力。
"""


class DomainError(Exception):
    """业务异常。

    语义与迁移前逐字对应的 `HTTPException(status_code=..., detail={"code", "message"})` 一致：
    `code` 是稳定错误码，`message` 是对外文案，`http_status` 是 HTTP 状态码。
    """

    def __init__(self, code: str, message: str, *, http_status: int = 400) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.http_status = http_status
