"""Tamper-evident certificates for indexing results.

Every successful response carries two HMAC-SHA256 values computed over a
canonical (sorted-key, whitespace-free) JSON encoding:

* ``request_fingerprint`` binds the exact rational input values, the
  tolerance, the impurity quota and the interface version;
* ``certificate`` signs the whole result payload (including the
  fingerprint), so any modification of a returned peak mapping, factor,
  score or witness is detected.

Verification additionally re-runs the real solver; a signature that
validates but whose claims no longer match the recomputed optimum is
reported as a mismatch rather than trusted.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from fractions import Fraction
from typing import Any

from .rational import canonical_decimal, dumps_canonical

API_VERSION = "v1"
INDEX_LIMIT = 12


def _secret() -> bytes:
    return os.environ.get(
        "APP_CERTIFICATE_SECRET", "development-only-secret-change-me"
    ).encode("utf-8")


def _hmac(payload: bytes) -> str:
    return hmac.new(_secret(), payload, hashlib.sha256).hexdigest()


def normalize_request(request: dict[str, Any]) -> dict[str, Any]:
    """Project a request to the exact-value form that gets signed."""

    peaks = [canonical_decimal(p) if isinstance(p, Fraction) else p for p in request["peaks"]]
    tolerance = request["tolerance"]
    if isinstance(tolerance, Fraction):
        tolerance = canonical_decimal(tolerance)
    return {
        "api_version": API_VERSION,
        "peaks": peaks,
        "tolerance": tolerance,
        "impurity_quota": int(request["impurity_quota"]),
        "index_limit": INDEX_LIMIT,
    }


def request_fingerprint(request: dict[str, Any]) -> str:
    return _hmac(dumps_canonical(normalize_request(request)))


def sign_result(result: dict[str, Any]) -> str:
    """Sign a result document that already carries ``request_fingerprint``."""

    if "certificate" in result:
        raise ValueError("result to sign must not already contain a certificate")
    return _hmac(dumps_canonical(result))


def verify_mac(payload: dict[str, Any], signature: str) -> bool:
    """Constant-time check of ``signature`` against ``payload``."""

    if not isinstance(signature, str):
        return False
    try:
        expected = _hmac(dumps_canonical(payload))
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(expected, signature)
