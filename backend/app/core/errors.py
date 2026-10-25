"""统一异常与错误响应。"""
from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse


class AppError(Exception):
    """业务异常基类。"""

    status_code = 400
    code = "bad_request"

    def __init__(self, message: str, *, code: str | None = None, detail: Any = None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        self.detail = detail


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"


class PermissionDeniedError(AppError):
    status_code = 403
    code = "permission_denied"


class AuthError(AppError):
    status_code = 401
    code = "unauthorized"


class ConflictError(AppError):
    status_code = 409
    code = "conflict"


class StaleWriteError(ConflictError):
    """并发写冲突：记录在本次读取之后已被他人修改（乐观锁 row_version 不匹配）。

    前端应提示「内容已被他人修改」并让用户刷新后再改，而不是静默覆盖。
    """

    code = "stale_write"


class QuotaExceededError(AppError):
    """配额/容量超限（存储、文档数、分块数）。"""

    status_code = 507
    code = "quota_exceeded"


class RateLimitError(AppError):
    """请求过于频繁 / 账号被锁定（防爆破）。"""

    status_code = 429
    code = "rate_limited"


class ValidationError(AppError):
    status_code = 422
    code = "validation_error"


class ServiceUnavailableError(AppError):
    """依赖资源暂时不可用（连接池排队超时、数据库暂不可达等）。

    区别于 500：这类失败是「现在太忙」，重试或扩容就能恢复，
    负载均衡/前端应当按可重试处理；500 表示代码里出了问题。
    """

    status_code = 503
    code = "service_unavailable"


class UpstreamError(AppError):
    """上游模型/服务调用失败，携带结构化诊断信息（供前端展示与修复建议）。"""

    status_code = 502
    code = "upstream_error"

    def __init__(
        self, message: str, *, provider: str | None = None, base_url: str | None = None,
        model: str | None = None, status_code: int | None = None, raw: str | None = None,
        purpose: str | None = None,
    ):
        super().__init__(message)
        self.provider = provider
        self.base_url = base_url
        self.model = model
        self.upstream_status = status_code
        self.raw = raw
        self.purpose = purpose

    def __str__(self) -> str:
        # 完整信息，不截断（供 error_detail 保存与日志）
        parts = [self.message]
        if self.upstream_status:
            parts.append(f"HTTP {self.upstream_status}")
        if self.base_url:
            parts.append(f"@ {self.base_url}")
        if self.model:
            parts.append(f"model={self.model}")
        if self.raw:
            parts.append(f"上游响应: {self.raw}")
        return " | ".join(parts)


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "code": exc.code,
            "message": exc.message,
            "detail": exc.detail,
        },
    )


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    from app.core.logging import get_logger

    get_logger("error").exception("unhandled_error", path=str(request.url))
    return JSONResponse(
        status_code=500,
        content={"code": "internal_error", "message": "服务器内部错误"},
    )


async def db_timeout_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """连接池排队超时 → 503（可重试），而不是 500（代码有问题）。

    SQLAlchemy 在 ``pool_timeout`` 内拿不到空闲连接时抛 ``sqlalchemy.exc.TimeoutError``。
    它继承自 SQLAlchemyError → 会落进 unhandled handler 变成 500，
    于是「机器忙」和「代码炸了」在监控上无法区分，前端也不会重试。
    SQLite 池现在只有 1~3 个连接，这个转换尤其关键。
    """
    from app.core.logging import get_logger

    log = get_logger("error")
    log.warning("db_pool_timeout", path=str(request.url), err=str(exc)[:200])
    # 池配置信息对排查有用，且不含连接串（不泄露口令）
    from app.core.db import async_engine

    try:
        pool = async_engine.pool.status()
    except Exception:  # noqa: BLE001
        pool = ""
    return JSONResponse(
        status_code=503,
        content={
            "code": "service_unavailable",
            "message": "数据库繁忙，请稍后重试",
            "detail": {"reason": "db_pool_timeout", "pool": pool},
        },
        headers={"Retry-After": "1"},
    )
