"""统一业务错误，返回 4xx 并携带中文说明。"""

from __future__ import annotations

from typing import Any


class ApiError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        # 结构化细节（如已确认时的原确认号、预览过期时的差异），便于调用方区分处理
        self.details = details


class ConflictError(ApiError):
    def __init__(self, message: str, details: dict[str, Any] | None = None):
        super().__init__(409, "conflict", message, details)


class NotFoundError(ApiError):
    def __init__(self, message: str, details: dict[str, Any] | None = None):
        super().__init__(404, "not_found", message, details)


class BadRequestError(ApiError):
    def __init__(self, message: str, details: dict[str, Any] | None = None):
        super().__init__(400, "bad_request", message, details)
