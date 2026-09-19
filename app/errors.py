"""Stable machine-readable error codes.

Error responses never embed partial solver output.
"""
from __future__ import annotations

from typing import Any

from fastapi import status as http_status


class ApiError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        http_status: int = http_status.HTTP_422_UNPROCESSABLE_ENTITY,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.details = details or {}


# Input validation
INVALID_RATIONAL = "INVALID_RATIONAL"
PEAK_COUNT_OUT_OF_RANGE = "PEAK_COUNT_OUT_OF_RANGE"
PEAKS_NOT_STRICTLY_INCREASING = "PEAKS_NOT_STRICTLY_INCREASING"
NON_POSITIVE_PEAK = "NON_POSITIVE_PEAK"
INVALID_TOLERANCE = "INVALID_TOLERANCE"
IMPURITY_BUDGET_OUT_OF_RANGE = "IMPURITY_BUDGET_OUT_OF_RANGE"
INVALID_REQUEST_BODY = "INVALID_REQUEST_BODY"

# Outcomes
NO_SOLUTION = "NO_SOLUTION"
ENGINE_LIMIT_REACHED = "ENGINE_LIMIT_REACHED"

# Certificates
CERTIFICATE_MALFORMED = "CERTIFICATE_MALFORMED"
CERTIFICATE_SIGNATURE_INVALID = "CERTIFICATE_SIGNATURE_INVALID"
CERTIFICATE_RECOMPUTATION_MISMATCH = "CERTIFICATE_RECOMPUTATION_MISMATCH"


def error_body(code: str, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "details": details or {}}}
