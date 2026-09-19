"""Tamper-evident result certificates.

A solution certificate binds the exact request to the exact canonical result
and is signed with HMAC-SHA256 using a deployment secret.  Verification does
**not** trust the embedded answer: it re-runs the real solver on the embedded
inputs and compares the recomputed canonical outcome against the claimed one,
in addition to checking the signature.  Any modification to either the inputs
or the answer is therefore detected with a stable machine code.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from fractions import Fraction
from typing import Any

from .solver import Solution, SolveResult

DEFAULT_DEV_SECRET = "dev-only-cert-secret-change-me"


def _secret() -> bytes:
    return os.environ.get("CERT_SECRET", DEFAULT_DEV_SECRET).encode("utf-8")


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def _canonical(payload: Any) -> bytes:
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _solution_payload(sol: Solution) -> dict[str, Any]:
    return {
        "scale_factor": str(sol.scale_factor),
        "impurity_indices": list(sol.impurity_indices),
        "skipped_representable": sol.skipped_representable,
        "max_abs_residual": str(sol.max_abs_residual),
        "sum_abs_residual": str(sol.sum_abs_residual),
        "assignment": [
            {"peak_index": m.peak_index, "n": m.n, "hkl": list(m.hkl)}
            for m in sol.mappings
        ],
    }


def issue_certificate(
    peaks: list[Fraction],
    tolerance: Fraction,
    max_impurities: int,
    result: SolveResult,
) -> str:
    payload: dict[str, Any] = {
        "version": 1,
        "peaks": [str(p) for p in peaks],
        "tolerance": str(tolerance),
        "max_impurities": max_impurities,
        "status": result.status,
        "solutions": [_solution_payload(s) for s in result.solutions],
    }
    body = _b64(_canonical(payload))
    sig = hmac.new(_secret(), body.encode("ascii"), hashlib.sha256).digest()
    return f"{body}.{_b64(sig)}"


class CertificateError(ValueError):
    pass


def decode_certificate(certificate: str) -> dict[str, Any]:
    """Validate signature and return the embedded payload."""
    if not isinstance(certificate, str) or "." not in certificate:
        raise CertificateError("malformed certificate")
    body, _, sig_text = certificate.partition(".")
    try:
        body_bytes = body.encode("ascii")
        expected = hmac.new(_secret(), body_bytes, hashlib.sha256).digest()
        given = _unb64(sig_text)
    except Exception as exc:  # noqa: BLE001 - malformed base64 etc.
        raise CertificateError("malformed certificate") from exc
    if not hmac.compare_digest(expected, given):
        raise CertificateError("signature mismatch")
    try:
        return json.loads(_unb64(body).decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise CertificateError("malformed certificate payload") from exc
