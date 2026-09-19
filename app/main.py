"""FastAPI application: versioned JSON API for cubic-cell powder indexing.

Endpoints
---------
GET  /healthz                       liveness/readiness probe
POST /api/v1/index                  solve an indexing request
POST /api/v1/verify                 recompute and verify a result certificate
POST /api/v1/acceptance             one-shot self-contained acceptance check
"""
from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from . import errors as E
from . import service

app = FastAPI(
    title="Powder Diffraction Cell Indexing Service",
    version="1.0.0",
    description=(
        "Backend-only cubic-cell powder indexing with exact rational "
        "arithmetic and tamper-evident certificates."
    ),
)


class IndexRequest(BaseModel):
    # Accept ints, strings, and {"num","den"} objects; the service layer
    # performs the strict rational parsing.  Floats are rejected there.
    model_config = ConfigDict(extra="ignore")

    peaks: list[Any]
    tolerance: Any
    max_impurities: int = 0


class VerifyRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    certificate: str


@app.exception_handler(E.ApiError)
async def api_error_handler(_: Request, exc: E.ApiError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.http_status,
        content=E.error_body(exc.code, exc.message, exc.details),
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    # Structural request errors use a single stable code; the first problem
    # location is included as a detail, never any solver output.
    details: dict[str, Any] = {}
    if exc.errors():
        first = exc.errors()[0]
        loc = [str(p) for p in first.get("loc", []) if p != "body"]
        details = {"field": ".".join(loc), "type": first.get("type", "")}
    return JSONResponse(
        status_code=422,
        content=E.error_body(
            E.INVALID_REQUEST_BODY, "request body is malformed or missing fields", details
        ),
    )


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/v1/index")
async def index(req: IndexRequest) -> dict[str, Any]:
    return service.run(req.peaks, req.tolerance, req.max_impurities)


@app.post("/api/v1/verify")
async def verify(req: VerifyRequest) -> dict[str, Any]:
    return service.verify(req.certificate)


@app.post("/api/v1/acceptance")
async def acceptance() -> dict[str, Any]:
    """One-shot acceptance entrypoint exercising solve and real verification."""
    from .acceptance import run_acceptance

    return run_acceptance()
