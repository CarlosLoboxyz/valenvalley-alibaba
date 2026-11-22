"""Consistent error payloads: ``{"detail": ..., "code": ...}``."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

_STATUS_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    422: "validation_error",
    503: "service_unavailable",
}


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, detail: Any):
        self.status_code = status_code
        self.code = code
        self.detail = detail
        super().__init__(str(detail))


def _payload(status: int, code: str, detail: Any, headers: dict | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content=jsonable_encoder({"detail": detail, "code": code}),
        headers=headers,
    )


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError):
        headers = {"WWW-Authenticate": "Bearer"} if exc.status_code == 401 else None
        return _payload(exc.status_code, exc.code, exc.detail, headers)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException):
        return _payload(exc.status_code, _STATUS_CODES.get(exc.status_code, "error"), exc.detail)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError):
        # Strip raw input values: they may echo secrets or large uploads
        errors = [{k: v for k, v in e.items() if k not in {"input", "ctx", "url"}} for e in exc.errors()]
        return _payload(422, "validation_error", errors)
