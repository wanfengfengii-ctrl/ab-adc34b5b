"""FastAPI web layer for the cell-indexing service.

Request bodies are consumed as raw JSON and decoded by the service layer
so that no framework float coercion can bypass the exact-rational rules.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__
from .errors import ErrorCode
from .service import ServiceError, index_raw, verify_raw

logger = logging.getLogger("indexer")

app = FastAPI(
    title="Synchrotron Powder Cell Indexing Service",
    version=__version__,
    description=(
        "Backend-only cubic cell indexing over exact rational peak "
        "positions. No user interface is served; interactive docs are "
        "disabled and only the machine-readable OpenAPI document exists."
    ),
    docs_url=None,
    redoc_url=None,
    openapi_url="/api/v1/openapi.json",
)


def _error_body(code: ErrorCode, message: str) -> dict[str, Any]:
    return {
        "error": {
            "code": code.value,
            "message": message,
        }
    }


@app.exception_handler(ServiceError)
async def service_error_handler(_request: Request, exc: ServiceError) -> JSONResponse:
    return JSONResponse(status_code=exc.http_status, content=_error_body(exc.code, exc.message))


@app.exception_handler(StarletteHTTPException)
async def http_error_handler(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
    if exc.status_code == 404:
        code, message = ErrorCode.NOT_FOUND, "unknown endpoint"
    elif exc.status_code == 405:
        code, message = ErrorCode.NOT_FOUND, "method not allowed for this endpoint"
    else:
        code, message = ErrorCode.INVALID_REQUEST, str(exc.detail)
    return JSONResponse(status_code=exc.status_code, content=_error_body(code, message))


@app.exception_handler(Exception)
async def unhandled_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    ref = uuid.uuid4().hex
    logger.exception("unhandled error %s", ref)
    return JSONResponse(
        status_code=500,
        content=_error_body(
            ErrorCode.INTERNAL_ERROR,
            f"internal error (reference {ref})",
        ),
    )


async def _read_json(request: Request) -> Any:
    if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
        raise ServiceError(
            ErrorCode.INVALID_REQUEST,
            "Content-Type must be application/json",
            415,
        )
    raw = await request.body()
    if not raw:
        raise ServiceError(ErrorCode.INVALID_REQUEST, "request body is empty", 400)
    try:
        return json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ServiceError(
            ErrorCode.INVALID_REQUEST, "request body is not valid JSON", 400
        ) from None


def _reject_constant(value: str) -> None:
    raise ServiceError(ErrorCode.INVALID_REQUEST, f"forbidden JSON constant: {value}", 400)


@app.get("/health", tags=["health"])
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "cell-indexer", "version": __version__}


@app.get("/api/v1", tags=["meta"])
async def api_meta() -> dict[str, Any]:
    return {
        "api_version": "v1",
        "service": "cell-indexer",
        "version": __version__,
        "endpoints": {
            "index": "POST /api/v1/index",
            "verify": "POST /api/v1/verify",
        },
    }


@app.post("/api/v1/index", tags=["indexing"])
async def index(request: Request) -> JSONResponse:
    payload = await _read_json(request)
    # CPU-bound exhaustive solving runs off the event loop.
    result = await run_in_threadpool(index_raw, payload)
    return JSONResponse(status_code=200, content=result)


@app.post("/api/v1/verify", tags=["indexing"])
async def verify(request: Request) -> JSONResponse:
    payload = await _read_json(request)
    # Verification re-runs the real (CPU-bound) solver off the event loop.
    result = await run_in_threadpool(verify_raw, payload)
    return JSONResponse(status_code=200, content=result)
