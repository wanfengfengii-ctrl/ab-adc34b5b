"""One-shot acceptance suite.

Invoked through ``POST /api/v1/acceptance`` (or ``python -m app.acceptance``
on the CLI), it runs a fixed battery of scenarios **through the real
service layer** (solver + certificate issue/verify) and returns a strict
report.  A non-zero exit / ``"all_passed": false`` means the build must be
rejected; no result is returned when an internal step fails.
"""
from __future__ import annotations

import sys
from typing import Any

from . import errors as E
from . import service
from .certificate import decode_certificate


def _expect_error(fn, code: str) -> tuple[bool, str]:
    try:
        fn()
    except E.ApiError as exc:
        return exc.code == code, f"got {exc.code}, wanted {code}"
    return False, "call unexpectedly succeeded"


def _check(name: str, ok: bool, detail: str = "") -> dict[str, Any]:
    return {"name": name, "passed": bool(ok), "detail": detail}


def run_acceptance() -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    # 1. Unique solution with a non-trivial rational scale factor (c = 3/2).
    r1 = service.run(["3/2", "3", "9/2", "6"], "0", 0)
    s1 = r1["solutions"][0]
    checks.append(_check(
        "unique_rational_scale",
        r1["status"] == "unique"
        and [m["n"] for m in s1["mappings"]] == [1, 2, 3, 4]
        and s1["scale_factor"] == "3/2"
        and all(m["within_tolerance"] for m in s1["mappings"]),
        f"status={r1['status']} assignment={[m['n'] for m in s1['mappings']]} "
        f"c={s1['scale_factor']}",
    ))

    # 2. Non-zero absolute tolerance; residuals are exact rationals.
    r2 = service.run(["1", "2", "3", "41/10"], "1/10", 0)
    s2 = r2["solutions"][0]
    checks.append(_check(
        "tolerance_exact_residuals",
        r2["status"] == "unique"
        and [m["n"] for m in s2["mappings"]] == [1, 2, 3, 4]
        and s2["max_abs_residual"] == "3/70"
        and s2["sum_abs_residual"] == "9/70",
        f"max={s2['max_abs_residual']} sum={s2['sum_abs_residual']}",
    ))

    # 3. Exactly one impurity is required and preferred over huge skips.
    r3 = service.run(["1", "2", "3", "382/127", "4"], "0", 1)
    s3 = r3["solutions"][0]
    checks.append(_check(
        "single_impurity",
        r3["status"] == "unique"
        and list(s3["impurity_indices"]) == [3]
        and [m["n"] for m in s3["mappings"]] == [1, 2, 3, 4]
        and s3["skipped_representable"] == 0,
        f"imp={s3['impurity_indices']} skip={s3['skipped_representable']}",
    ))

    # 4. Same data with no impurity budget must be reported unsolvable.
    ok, detail = _expect_error(
        lambda: service.run(["1", "2", "3", "382/127", "4"], "0", 0),
        E.NO_SOLUTION,
    )
    checks.append(_check("no_solution_code", ok, detail))

    # 5. Ambiguous mapping returns exactly two canonical witnesses, identical
    #    on all four optimization criteria.
    r5 = service.run(["272", "274", "278", "280", "288"], "0", 0)
    sols5 = r5["solutions"]
    criteria = {
        (s["skipped_representable"], s["max_abs_residual"],
         s["sum_abs_residual"], len(s["impurity_indices"]))
        for s in sols5
    }
    assignments5 = {tuple(m["n"] for m in s["mappings"]) for s in sols5}
    checks.append(_check(
        "ambiguous_two_witnesses",
        r5["status"] == "ambiguous"
        and len(sols5) == 2
        and len(criteria) == 1
        and assignments5 == {(136, 137, 139, 140, 144), (272, 274, 278, 280, 288)},
        f"status={r5['status']} witnesses={sorted(assignments5)}",
    ))

    # 6. Indices are canonical h <= k <= l within 0..12.
    canonical = all(
        0 <= m["hkl"][0] <= m["hkl"][1] <= m["hkl"][2] <= 12
        for s in sols5
        for m in s["mappings"]
    )
    checks.append(_check("canonical_indices", canonical))

    # 7. Certificate round-trip via real recomputation.
    v7 = service.verify(r1["certificate"])
    checks.append(_check(
        "certificate_roundtrip",
        v7["valid"] is True
        and v7["solutions"][0]["scale_factor"] == "3/2",
    ))

    # 8. A tampered answer inside an otherwise well-formed envelope is caught
    #    by recomputation (re-sign with one digit changed).  Detected either
    #    as signature failure or recomputation mismatch.
    import base64
    import hashlib
    import hmac
    import json as _json
    import os

    from .certificate import DEFAULT_DEV_SECRET

    body_b64, sig_b64 = r1["certificate"].split(".")
    pad = "=" * (-len(body_b64) % 4)
    payload = _json.loads(base64.urlsafe_b64decode(body_b64 + pad))
    payload["solutions"][0]["scale_factor"] = "999/1"
    canon = _json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    tampered_body = base64.urlsafe_b64encode(canon).rstrip(b"=").decode()
    secret = os.environ.get("CERT_SECRET", DEFAULT_DEV_SECRET).encode()
    tampered_sig = base64.urlsafe_b64encode(
        hmac.new(secret, tampered_body.encode(), hashlib.sha256).digest()
    ).rstrip(b"=").decode()
    tampered = f"{tampered_body}.{tampered_sig}"
    ok8, detail8 = _expect_error(
        lambda: service.verify(tampered), E.CERTIFICATE_RECOMPUTATION_MISMATCH
    )
    checks.append(_check("certificate_tamper_detected", ok8, detail8))

    # 9. A certificate with a broken signature is rejected.
    broken = r1["certificate"][:-2] + ("aa" if not r1["certificate"].endswith("aa") else "bb")
    ok9, detail9 = _expect_error(lambda: service.verify(broken), E.CERTIFICATE_SIGNATURE_INVALID)
    checks.append(_check("certificate_bad_signature", ok9, detail9))

    # 10. Input validation: count, ordering, positivity, float rejection, budget.
    ok, detail = _expect_error(
        lambda: service.run(["1", "2", "3"], "0", 0), E.PEAK_COUNT_OUT_OF_RANGE
    )
    checks.append(_check("reject_too_few_peaks", ok, detail))

    ok, detail = _expect_error(
        lambda: service.run(["1", "2", "2", "3"], "0", 0),
        E.PEAKS_NOT_STRICTLY_INCREASING,
    )
    checks.append(_check("reject_non_increasing", ok, detail))

    ok, detail = _expect_error(
        lambda: service.run(["1", "2", "-3", "4"], "0", 0), E.NON_POSITIVE_PEAK
    )
    checks.append(_check("reject_non_positive", ok, detail))

    ok, detail = _expect_error(
        lambda: service.run([1.5, "3", "9/2", "6"], "0", 0), E.INVALID_RATIONAL
    )
    checks.append(_check("reject_float_literal", ok, detail))

    ok, detail = _expect_error(
        lambda: service.run(["1", "2", "3", "4"], "0", 3),
        E.IMPURITY_BUDGET_OUT_OF_RANGE,
    )
    checks.append(_check("reject_budget_over_two", ok, detail))

    ok, detail = _expect_error(
        lambda: service.run(["1", "2", "3", "4"], "-1/10", 0), E.INVALID_TOLERANCE
    )
    checks.append(_check("reject_negative_tolerance", ok, detail))

    # 11. Explicit {num, den} objects and decimal strings are accepted.
    r11 = service.run(
        [{"num": 1, "den": 2}, "1", "3/2", "2"], "0", 0
    )
    checks.append(_check(
        "mixed_rational_forms",
        r11["status"] == "unique"
        and [m["n"] for m in r11["solutions"][0]["mappings"]] == [1, 2, 3, 4]
        and r11["solutions"][0]["scale_factor"] == "1/2",
    ))

    # 12. Skipped-representable minimization: the chosen mapping skips the
    #     least representable values between the first and last assignment.
    r12 = service.run(["2", "4", "6", "8"], "0", 0)
    checks.append(_check(
        "skip_minimization",
        r12["status"] == "unique"
        and [m["n"] for m in r12["solutions"][0]["mappings"]] == [1, 2, 3, 4]
        and r12["solutions"][0]["skipped_representable"] == 0
        and r12["solutions"][0]["scale_factor"] == "2",
    ))

    all_passed = all(c["passed"] for c in checks)
    return {
        "all_passed": all_passed,
        "passed": sum(1 for c in checks if c["passed"]),
        "total": len(checks),
        "checks": checks,
    }


def main() -> int:
    report = run_acceptance()
    import json

    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["all_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
