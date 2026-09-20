"""One-shot acceptance entry point.

Runs a fixed battery of end-to-end scenarios — successful unique and
ambiguous indexing, certificate review, tamper detection, infeasibility
and out-of-range inputs — against either

* the real service functions in-process (default), or
* a running server over HTTP (``--base-url`` / ``ACCEPTANCE_BASE_URL``),

so the acceptance check always exercises the real solving and
certificate-verification implementations.  Exits with status 0 only when
every scenario behaves exactly as specified.

Examples
--------
    python -m app.acceptance
    python -m app.acceptance --base-url http://localhost:8080
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from typing import Any, Callable, Optional

from .errors import ErrorCode
from .service import index_raw, verify_raw

# A clean cubic sequence: peaks = 2 * N for N = 1, 2, 3, 4.
CLEAN_REQUEST: dict[str, Any] = {
    "peaks": ["2", "4", "6", "8"],
    "tolerance": "0.01",
    "impurity_quota": 0,
}

# Same four clean lines plus a trailing peak beyond lambda*Nmax; it can
# only be accounted for by spending the one impurity allowance.
IMPURE_REQUEST: dict[str, Any] = {
    "peaks": ["2", "4", "6", "8", "900"],
    "tolerance": "0.01",
    "impurity_quota": 1,
}

# Peak ratio exceeds Nmax/Nmin = 432, so no positive factor can exist.
IMPOSSIBLE_REQUEST: dict[str, Any] = {
    "peaks": ["1", "1000", "2000", "3000"],
    "tolerance": "0.000000001",
    "impurity_quota": 0,
}

# Genuinely ambiguous: the same four peaks index both with lambda=1 to
# N={152,153,154,157} and with lambda=1/2 to N={304,306,308,314}; all
# four ranking criteria are identical.
AMBIGUOUS_REQUEST: dict[str, Any] = {
    "peaks": ["152", "153", "154", "157"],
    "tolerance": "0.000000001",
    "impurity_quota": 0,
}

BAD_COUNT_REQUEST: dict[str, Any] = {
    "peaks": ["1", "2", "3"],
    "tolerance": "0.01",
}

BAD_QUOTA_REQUEST: dict[str, Any] = {
    "peaks": ["2", "4", "6", "8"],
    "tolerance": "0.01",
    "impurity_quota": 3,
}


class AcceptanceFailure(Exception):
    pass


class Checker:
    def __init__(self, base_url: Optional[str]) -> None:
        self.base_url = base_url.rstrip("/") if base_url else None
        self.passed = 0

    def index(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.base_url:
            return self._http("POST", "/api/v1/index", payload)
        return index_raw(payload)

    def verify(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.base_url:
            return self._http("POST", "/api/v1/verify", payload)
        return verify_raw(payload)

    def health(self) -> dict[str, Any]:
        if self.base_url:
            return self._http("GET", "/health", None)
        return {"status": "ok"}

    def _http(self, method: str, path: str, payload: Optional[dict[str, Any]]) -> Any:
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base_url + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = json.loads(exc.read().decode("utf-8"))
            raise HttpExpect(exc.code, body) from None

    def check(self, name: str, condition: bool, detail: str = "") -> None:
        if not condition:
            raise AcceptanceFailure(f"{name}: {detail}")
        self.passed += 1
        print(f"  PASS  {name}")


class HttpExpect(Exception):
    def __init__(self, status: int, body: Any) -> None:
        self.status = status
        self.body = body


def expect_error(checker: Checker, name: str, fn: Callable[[], Any], code: ErrorCode) -> None:
    try:
        fn()
    except HttpExpect as exc:
        body = exc.body
        got = body.get("error", {}).get("code") if isinstance(body, dict) else None
        checker.check(name, got == code.value, f"expected {code.value}, got {got!r}")
        return
    except Exception as exc:  # noqa: BLE001 - ServiceError in local mode
        got = getattr(getattr(exc, "code", None), "value", None)
        checker.check(name, got == code.value, f"expected {code.value}, got {got!r}")
        return
    raise AcceptanceFailure(f"{name}: expected error {code.value}, call succeeded")


def run(base_url: Optional[str]) -> int:
    checker = Checker(base_url)
    print("Acceptance suite (" + ("HTTP " + base_url if base_url else "in-process") + ")")

    # 0. Health.
    health = checker.health()
    checker.check("health reports ok", health.get("status") == "ok", str(health))

    # 1. Unique clean solution.
    result = checker.index(CLEAN_REQUEST)
    checker.check("clean: unique status", result.get("status") == "unique", str(result.get("status")))
    checker.check("clean: one witness", result.get("witness_count") == 1)
    witness = result["witnesses"][0]
    ns = [m["n"] for m in witness["mappings"]]
    checker.check("clean: N = 1,2,3,4", ns == [1, 2, 3, 4], str(ns))
    checker.check("clean: factor 2", witness["scale_factor"] == "2", witness["scale_factor"])
    checker.check("clean: zero residual", witness["max_abs_residual"] == "0")
    checker.check("clean: no impurities", witness["impurity_count"] == 0)
    checker.check("clean: hkl normalized", all(m["hkl"] == list(sorted(m["hkl"])) for m in witness["mappings"]))
    checker.check("clean: certificate present", isinstance(result.get("certificate"), str))

    # 2. Certificate review of the real result.
    review = checker.verify({"request": CLEAN_REQUEST, "result": result})
    checker.check("verify: valid", review.get("valid") is True, str(review))
    checker.check("verify: status echoed", review.get("status") == "unique")

    # 3. Tampered mapping must be rejected without leaking a solution.
    tampered = json.loads(json.dumps(result))
    tampered["witnesses"][0]["mappings"][0]["n"] = 999
    expect_error(
        checker,
        "verify: tampered mapping rejected",
        lambda: checker.verify({"request": CLEAN_REQUEST, "result": tampered}),
        ErrorCode.CERTIFICATE_TAMPERED,
    )

    # 4. Valid signature but foreign request must be rejected.
    expect_error(
        checker,
        "verify: foreign request rejected",
        lambda: checker.verify({"request": IMPURE_REQUEST, "result": result}),
        ErrorCode.CERTIFICATE_REQUEST_MISMATCH,
    )

    # 5. Impurity is identified exactly.
    impure = checker.index(IMPURE_REQUEST)
    iw = impure["witnesses"][0]
    checker.check("impure: unique", impure.get("status") == "unique")
    checker.check("impure: one impurity at index 4", iw["impurities"] == [4], str(iw["impurities"]))
    ins = [m["n"] for m in iw["mappings"]]
    checker.check("impure: retained N = 1,2,3,4", ins == [1, 2, 3, 4], str(ins))
    checker.check("impure: factor 2", iw["scale_factor"] == "2")

    # 5b. A true ambiguity is flagged and returns two canonical witnesses.
    amb = checker.index(AMBIGUOUS_REQUEST)
    checker.check("ambiguous: status", amb.get("status") == "ambiguous", str(amb.get("status")))
    checker.check("ambiguous: exactly two witnesses", amb.get("witness_count") == 2)
    maps = [tuple(m["n"] for m in w["mappings"]) for w in amb["witnesses"]]
    checker.check(
        "ambiguous: witnesses are the two tied mappings",
        sorted(maps) == [(152, 153, 154, 157), (304, 306, 308, 314)],
        str(maps),
    )
    # The two witnesses tie on every ranking criterion.
    crit = [
        (w["impurity_count"], w["omitted_representable_count"], w["max_abs_residual"], w["sum_abs_residual"])
        for w in amb["witnesses"]
    ]
    checker.check("ambiguous: criteria identical", crit[0] == crit[1], str(crit))
    amb_review = checker.verify({"request": AMBIGUOUS_REQUEST, "result": amb})
    checker.check("ambiguous: certificate reviews as valid", amb_review.get("valid") is True)

    # 6. Infeasible input gives the stable no-solution code and no payload.
    expect_error(checker, "no-solution code", lambda: checker.index(IMPOSSIBLE_REQUEST), ErrorCode.NO_SOLUTION)

    # 7. Out-of-range inputs.
    expect_error(checker, "peak count out of range", lambda: checker.index(BAD_COUNT_REQUEST), ErrorCode.PEAK_COUNT_OUT_OF_RANGE)
    expect_error(checker, "quota out of range", lambda: checker.index(BAD_QUOTA_REQUEST), ErrorCode.IMPURITY_QUOTA_OUT_OF_RANGE)

    # 8. Non-strictly-increasing and non-rational inputs are rejected.
    expect_error(
        checker,
        "duplicate peak rejected",
        lambda: checker.index({"peaks": ["1", "2", "2", "3"], "tolerance": "0.01"}),
        ErrorCode.PEAKS_NOT_STRICTLY_INCREASING,
    )
    expect_error(
        checker,
        "float token rejected",
        lambda: checker.index({"peaks": [1.5, "2", "3", "4"], "tolerance": "0.01"}),
        ErrorCode.INVALID_RATIONAL,
    )

    print(f"\nAll {checker.passed} acceptance checks passed.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the acceptance suite")
    parser.add_argument("--base-url", default=None, help="HTTP base URL of a running server")
    args = parser.parse_args()
    base_url = args.base_url
    import os

    base_url = base_url or os.environ.get("ACCEPTANCE_BASE_URL")
    try:
        return run(base_url)
    except AcceptanceFailure as exc:
        print(f"\nACCEPTANCE FAILURE: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
