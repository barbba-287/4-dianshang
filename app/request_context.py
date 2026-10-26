from __future__ import annotations

"""轻量请求上下文：统一承载 request_id 与当前身份。

`request` 参数采用鸭子类型（不 import fastapi 的 Request），保持本模块与
HTTP 框架解耦；调用方传入任意带 `.state` 属性的对象即可。
"""

from dataclasses import dataclass

from app.errors import DomainError
from app.identity import Principal


@dataclass(frozen=True)
class RequestContext:
    request_id: str
    principal: Principal | None


def get_request_context(request) -> RequestContext:
    ctx = getattr(request.state, "ctx", None)
    if ctx is None:
        raise DomainError(code="UNAUTHORIZED", message="请求上下文缺失", http_status=401)
    return ctx
