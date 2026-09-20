"""Application service layer: validation, solving and certificate review.

Everything here works with :class:`fractions.Fraction` values; the web
layer only translates between JSON tokens and these functions.
"""

from __future__ import annotations

from fractions import Fraction
from typing import Any, Optional

from . import certificate as cert_mod
from .errors import ErrorCode
from .rational import canonical_decimal, rational_from_json
from .solver import INDEX_LIMIT, Solution, SolverBudgetExceeded, solve

MIN_PEAKS = 4
MAX_PEAKS = 32
MAX_IMPURITY_QUOTA = 2
ALLOWED_FIELDS = frozenset({"peaks", "tolerance", "impurity_quota"})


class ServiceError(Exception):
    def __init__(self, code: ErrorCode, message: str, http_status: int = 422):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


# ---------------------------------------------------------------------------
# Request validation
# ---------------------------------------------------------------------------


def parse_index_payload(raw: Any) -> tuple[list[Fraction], Fraction, int]:
    if not isinstance(raw, dict):
        raise ServiceError(ErrorCode.INVALID_REQUEST, "request body must be a JSON object", 400)
    unknown = set(raw) - ALLOWED_FIELDS
    if unknown:
        raise ServiceError(
            ErrorCode.INVALID_REQUEST,
            f"unknown field(s): {', '.join(sorted(unknown))}",
            400,
        )

    if "peaks" not in raw or "tolerance" not in raw:
        raise ServiceError(
            ErrorCode.INVALID_REQUEST, "fields 'peaks' and 'tolerance' are required", 400
        )

    raw_peaks = raw["peaks"]
    if not isinstance(raw_peaks, list):
        raise ServiceError(ErrorCode.INVALID_REQUEST, "'peaks' must be an array", 400)
    count = len(raw_peaks)
    if not MIN_PEAKS <= count <= MAX_PEAKS:
        raise ServiceError(
            ErrorCode.PEAK_COUNT_OUT_OF_RANGE,
            f"peaks count must be between {MIN_PEAKS} and {MAX_PEAKS}, got {count}",
        )

    peaks: list[Fraction] = []
    for idx, item in enumerate(raw_peaks):
        try:
            value = rational_from_json(item, field=f"peaks[{idx}]")
        except Exception as exc:  # RationalParseError
            raise ServiceError(
                ErrorCode.INVALID_RATIONAL,
                f"peaks[{idx}] is not an exact rational: {exc}",
            ) from None
        if value <= 0:
            raise ServiceError(
                ErrorCode.PEAK_POSITION_NOT_POSITIVE,
                f"peaks[{idx}] must be strictly positive",
            )
        if peaks and value <= peaks[-1]:
            raise ServiceError(
                ErrorCode.PEAKS_NOT_STRICTLY_INCREASING,
                f"peaks[{idx}] must be strictly greater than peaks[{idx - 1}]",
            )
        peaks.append(value)

    try:
        tolerance = rational_from_json(raw["tolerance"], field="tolerance")
    except Exception:
        raise ServiceError(
            ErrorCode.INVALID_RATIONAL, "tolerance is not an exact rational"
        ) from None
    if tolerance <= 0:
        raise ServiceError(ErrorCode.TOLERANCE_NOT_POSITIVE, "tolerance must be positive")

    quota = raw.get("impurity_quota", 0)
    if isinstance(quota, bool) or not isinstance(quota, int):
        raise ServiceError(
            ErrorCode.IMPURITY_QUOTA_OUT_OF_RANGE,
            "impurity_quota must be an integer between 0 and 2",
        )
    if not 0 <= quota <= MAX_IMPURITY_QUOTA:
        raise ServiceError(
            ErrorCode.IMPURITY_QUOTA_OUT_OF_RANGE,
            f"impurity_quota must be between 0 and {MAX_IMPURITY_QUOTA}, got {quota}",
        )

    return peaks, tolerance, quota


# ---------------------------------------------------------------------------
# Result serialization
# ---------------------------------------------------------------------------


def _witness_document(sol: Solution) -> dict[str, Any]:
    return {
        "scale_factor": canonical_decimal(sol.scale_factor),
        "impurity_count": len(sol.impurity_indices),
        "impurities": list(sol.impurity_indices),
        "omitted_representable_count": sol.omitted_representables,
        "max_abs_residual": canonical_decimal(sol.max_abs_residual),
        "sum_abs_residual": canonical_decimal(sol.sum_abs_residual),
        "mappings": [
            {
                "peak_index": a.peak_index,
                "n": a.n,
                "hkl": list(a.hkl),
                "residual": canonical_decimal(a.residual),
            }
            for a in sol.assignments
        ],
    }


def build_result(
    peaks: list[Fraction],
    tolerance: Fraction,
    quota: int,
    solutions: Optional[list[Solution]],
) -> dict[str, Any]:
    """Assemble the unsigned result document.

    ``None`` solutions means infeasibility; the caller raises
    :class:`ServiceError` E2001 without producing any partial payload.
    """

    if solutions is None:
        raise ServiceError(
            ErrorCode.NO_SOLUTION,
            "no indexing exists within the given tolerance and impurity quota",
        )

    request_view = {
        "peaks": [canonical_decimal(p) for p in peaks],
        "tolerance": canonical_decimal(tolerance),
        "impurity_quota": quota,
    }
    fingerprint = cert_mod.request_fingerprint(request_view)
    status = "unique" if len(solutions) == 1 else "ambiguous"
    result: dict[str, Any] = {
        "api_version": cert_mod.API_VERSION,
        "status": status,
        "index_limit": INDEX_LIMIT,
        "request": {
            "peaks": request_view["peaks"],
            "tolerance": request_view["tolerance"],
            "impurity_quota": quota,
        },
        "request_fingerprint": fingerprint,
        "witness_count": len(solutions),
        "witnesses": [_witness_document(s) for s in solutions],
    }
    result["certificate"] = cert_mod.sign_result(result)
    return result


def _run_solve(
    peaks: list[Fraction], tolerance: Fraction, quota: int
) -> Optional[list[Solution]]:
    try:
        return solve(peaks, tolerance, quota)
    except SolverBudgetExceeded:
        raise ServiceError(
            ErrorCode.COMPUTATION_LIMIT_EXCEEDED,
            "the exhaustive indexing search exceeded its computation limit; "
            "narrow the tolerance or reduce the peak count",
            422,
        ) from None


def index_raw(raw: Any) -> dict[str, Any]:
    peaks, tolerance, quota = parse_index_payload(raw)
    solutions = _run_solve(peaks, tolerance, quota)
    return build_result(peaks, tolerance, quota, solutions)


# ---------------------------------------------------------------------------
# Certificate review (re-runs the real solver)
# ---------------------------------------------------------------------------

_VERIFY_FIELDS = frozenset({"request", "result"})


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ServiceError(ErrorCode.CERTIFICATE_MALFORMED, message, 400)


def verify_raw(raw: Any) -> dict[str, Any]:
    _require(isinstance(raw, dict), "verification body must be a JSON object")
    _require(not (set(raw) - _VERIFY_FIELDS), "unknown field(s) in verification body")
    _require("request" in raw and "result" in raw, "'request' and 'result' are required")

    submitted_result = raw["result"]
    _require(isinstance(submitted_result, dict), "'result' must be a JSON object")
    _require(isinstance(raw["request"], dict), "'request' must be a JSON object")

    signature = submitted_result.get("certificate")
    _require(isinstance(signature, str) and signature, "missing 'certificate'")
    unsigned = {k: v for k, v in submitted_result.items() if k != "certificate"}
    if not cert_mod.verify_mac(unsigned, signature):
        # Any byte-level change lands here; no internal/partial data leaks.
        raise ServiceError(
            ErrorCode.CERTIFICATE_TAMPERED,
            "certificate signature does not match the result document",
            409,
        )

    peaks, tolerance, quota = parse_index_payload(raw["request"])

    expected_fp = cert_mod.request_fingerprint(
        {
            "peaks": [canonical_decimal(p) for p in peaks],
            "tolerance": canonical_decimal(tolerance),
            "impurity_quota": quota,
        }
    )
    if submitted_result.get("request_fingerprint") != expected_fp:
        raise ServiceError(
            ErrorCode.CERTIFICATE_REQUEST_MISMATCH,
            "result was not issued for the supplied request",
            422,
        )

    # Re-run the real implementation and compare every claim exactly.
    solutions = _run_solve(peaks, tolerance, quota)
    expected = build_result(peaks, tolerance, quota, solutions)
    expected_unsigned = {k: v for k, v in expected.items() if k != "certificate"}

    if unsigned != expected_unsigned:
        raise ServiceError(
            ErrorCode.CERTIFICATE_REQUEST_MISMATCH,
            "certified result does not match the recomputed optimum",
            422,
        )

    return {
        "valid": True,
        "status": expected["status"],
        "request_fingerprint": expected_fp,
        "certificate": signature,
    }
