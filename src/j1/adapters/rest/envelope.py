from typing import Any

from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class CamelModel(BaseModel):
    """Base model that serializes snake_case fields as camelCase."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )


class ApiError(CamelModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


def envelope(data: Any, request_id: str, *, meta: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build the standard success envelope."""
    return {
        "requestId": request_id,
        "data": data,
        "meta": meta or {},
    }


def error_envelope(
    *,
    code: str,
    message: str,
    request_id: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the standard error envelope."""
    return {
        "requestId": request_id,
        "error": {
            "code": code,
            "message": message,
            "details": details or {},
        },
    }


def error_response(
    *,
    status_code: int,
    code: str,
    message: str,
    request_id: str,
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=error_envelope(
            code=code,
            message=message,
            request_id=request_id,
            details=details,
        ),
    )
