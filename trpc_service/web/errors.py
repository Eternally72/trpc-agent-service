"""Stable public error models and FastAPI exception handlers."""

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException


class ErrorDetail(BaseModel):
    """Machine-readable error code with a safe human-readable message."""

    code: str
    message: str
    details: list[dict[str, Any]] | None = None


class ErrorResponse(BaseModel):
    """Top-level error envelope shared by all API failures."""

    error: ErrorDetail


def _error_response(status_code: int, detail: ErrorDetail) -> JSONResponse:
    """Serialize one error using the service-wide public contract."""

    payload = ErrorResponse(error=detail).model_dump(mode="json", exclude_none=True)
    return JSONResponse(status_code=status_code, content=payload)


def install_exception_handlers(app: FastAPI) -> None:
    """Install handlers that prevent framework-specific error payloads from leaking."""

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(_: Request, error: StarletteHTTPException) -> JSONResponse:
        """Map expected HTTP failures to stable public error codes."""

        codes = {
            404: "not_found",
            409: "conflict",
        }
        message = error.detail if isinstance(error.detail, str) else "request failed"
        return _error_response(
            error.status_code,
            ErrorDetail(code=codes.get(error.status_code, "http_error"), message=message),
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(_: Request, error: RequestValidationError) -> JSONResponse:
        """Return field locations and types without echoing sensitive input values."""

        # Pydantic includes the rejected input by default. Only location and
        # error type are exposed so credentials cannot be reflected to clients.
        details = [{
            "location": [str(part) for part in item["loc"]],
            "type": item["type"],
        } for item in error.errors()]
        return _error_response(
            422,
            ErrorDetail(
                code="validation_error",
                message="request validation failed",
                details=details,
            ),
        )
