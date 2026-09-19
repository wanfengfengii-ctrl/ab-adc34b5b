"""Application service layer: validation, solving, serialization, verification."""
from __future__ import annotations

from fractions import Fraction
from typing import Any

from . import errors as E
from .certificate import (
    CertificateError,
    decode_certificate,
    issue_certificate,
)
from .rational import RationalError, parse_rational
from .solver import Solution, SolveResult, SolverLimit, solve_indexing

MIN_PEAKS = 4
MAX_PEAKS = 32
MAX_IMPURITIES = 2


def validate_inputs(
    raw_peaks: Any,
    raw_tolerance: Any,
    raw_max_impurities: Any,
) -> tuple[list[Fraction], Fraction, int]:
    if not isinstance(raw_peaks, list):
        raise E.ApiError(
            E.INVALID_REQUEST_BODY, "field 'peaks' must be a list of rationals"
        )
    n = len(raw_peaks)
    if not (MIN_PEAKS <= n <= MAX_PEAKS):
        raise E.ApiError(
            E.PEAK_COUNT_OUT_OF_RANGE,
            f"expected between {MIN_PEAKS} and {MAX_PEAKS} peaks, got {n}",
            details={"min": MIN_PEAKS, "max": MAX_PEAKS, "given": n},
        )

    peaks: list[Fraction] = []
    for idx, raw in enumerate(raw_peaks):
        try:
            value = parse_rational(raw, name=f"peaks[{idx}]")
        except RationalError as exc:
            raise E.ApiError(E.INVALID_RATIONAL, str(exc), details={"index": idx}) from exc
        if value <= 0:
            raise E.ApiError(
                E.NON_POSITIVE_PEAK,
                f"peaks[{idx}] must be strictly positive",
                details={"index": idx},
            )
        if peaks and value <= peaks[-1]:
            raise E.ApiError(
                E.PEAKS_NOT_STRICTLY_INCREASING,
                f"peaks[{idx}] is not strictly greater than the previous peak",
                details={"index": idx},
            )
        peaks.append(value)

    try:
        tolerance = parse_rational(raw_tolerance, name="tolerance")
    except RationalError as exc:
        raise E.ApiError(E.INVALID_TOLERANCE, str(exc)) from exc
    if tolerance < 0:
        raise E.ApiError(E.INVALID_TOLERANCE, "tolerance must be non-negative")

    if isinstance(raw_max_impurities, bool) or not isinstance(raw_max_impurities, int):
        raise E.ApiError(
            E.IMPURITY_BUDGET_OUT_OF_RANGE,
            "max_impurities must be an integer",
        )
    if not (0 <= raw_max_impurities <= MAX_IMPURITIES):
        raise E.ApiError(
            E.IMPURITY_BUDGET_OUT_OF_RANGE,
            f"max_impurities must be between 0 and {MAX_IMPURITIES}",
            details={"max": MAX_IMPURITIES, "given": raw_max_impurities},
        )

    return peaks, tolerance, raw_max_impurities


def serialize_solution(sol: Solution, tolerance: Fraction) -> dict[str, Any]:
    return {
        "scale_factor": str(sol.scale_factor),
        "impurity_indices": list(sol.impurity_indices),
        "skipped_representable": sol.skipped_representable,
        "max_abs_residual": str(sol.max_abs_residual),
        "sum_abs_residual": str(sol.sum_abs_residual),
        "mappings": [
            {
                "peak_index": m.peak_index,
                "n": m.n,
                "hkl": list(m.hkl),
                "residual": str(m.residual),
                "within_tolerance": m.residual <= tolerance,
            }
            for m in sol.mappings
        ],
    }


def run(
    raw_peaks: Any,
    raw_tolerance: Any,
    raw_max_impurities: Any,
) -> dict[str, Any]:
    peaks, tolerance, max_imp = validate_inputs(
        raw_peaks, raw_tolerance, raw_max_impurities
    )
    try:
        result = solve_indexing(peaks, tolerance, max_imp)
    except SolverLimit as exc:
        raise E.ApiError(
            E.ENGINE_LIMIT_REACHED,
            "the indexing search exceeded its combinatorial safety limit",
            http_status=422,
        ) from exc

    if result.status == "no_solution":
        raise E.ApiError(
            E.NO_SOLUTION,
            "no assignment of peaks to representable values satisfies the "
            "tolerance and impurity budget",
            http_status=422,
        )

    # Deterministic witness order for ambiguous results.
    ordered = sorted(result.solutions, key=lambda s: s.assignment_key())
    if result.status == "ambiguous":
        ordered = ordered[:2]
    canonical_result = SolveResult(status=result.status, solutions=ordered)

    certificate = issue_certificate(peaks, tolerance, max_imp, canonical_result)
    return {
        "status": result.status,
        "peaks": [str(p) for p in peaks],
        "tolerance": str(tolerance),
        "max_impurities": max_imp,
        "solutions": [serialize_solution(s, tolerance) for s in ordered],
        "certificate": certificate,
    }


def verify(certificate: str) -> dict[str, Any]:
    """Verify a certificate by recomputing the answer with the real solver."""
    try:
        payload = decode_certificate(certificate)
    except CertificateError as exc:
        code = (
            E.CERTIFICATE_SIGNATURE_INVALID
            if "signature" in str(exc)
            else E.CERTIFICATE_MALFORMED
        )
        raise E.ApiError(code, str(exc), http_status=400) from exc

    try:
        peaks = [parse_rational(p, name="peaks") for p in payload["peaks"]]
        tolerance = parse_rational(payload["tolerance"], name="tolerance")
        max_imp = int(payload["max_impurities"])
        claimed_status = payload["status"]
        claimed_solutions = payload["solutions"]
    except (KeyError, TypeError, ValueError, RationalError) as exc:
        raise E.ApiError(E.CERTIFICATE_MALFORMED, "certificate payload is incomplete", http_status=400) from exc

    try:
        result = solve_indexing(peaks, tolerance, max_imp)
    except SolverLimit as exc:
        raise E.ApiError(E.ENGINE_LIMIT_REACHED, "recomputation exceeded the safety limit", http_status=422) from exc

    ordered = sorted(result.solutions, key=lambda s: s.assignment_key())
    if result.status == "ambiguous":
        ordered = ordered[:2]
    recomputed = SolveResult(status=result.status, solutions=ordered)

    # Re-issue over the exact embedded inputs and compare the canonical
    # outcome; this proves the embedded answer is what the real engine returns.
    fresh_certificate = issue_certificate(peaks, tolerance, max_imp, recomputed)
    fresh_payload = decode_certificate(fresh_certificate)

    if result.status == "no_solution" or claimed_status != fresh_payload["status"]:
        raise E.ApiError(
            E.CERTIFICATE_RECOMPUTATION_MISMATCH,
            "certificate does not match a fresh recomputation",
            http_status=409,
        )

    if not _same_solutions(claimed_solutions, fresh_payload["solutions"]):
        raise E.ApiError(
            E.CERTIFICATE_RECOMPUTATION_MISMATCH,
            "certificate answer was altered or is stale",
            http_status=409,
        )

    return {
        "valid": True,
        "status": fresh_payload["status"],
        "solutions": [serialize_solution(s, tolerance) for s in ordered],
        "tolerance": str(tolerance),
    }


def _same_solutions(claimed: list[dict[str, Any]], fresh: list[dict[str, Any]]) -> bool:
    if len(claimed) != len(fresh):
        return False
    keys = (
        "scale_factor",
        "impurity_indices",
        "skipped_representable",
        "max_abs_residual",
        "sum_abs_residual",
        "assignment",
    )
    for a, b in zip(claimed, fresh):
        for key in keys:
            if a.get(key) != b.get(key):
                return False
    return True
