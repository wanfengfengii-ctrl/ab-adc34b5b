from fractions import Fraction

from fastapi.testclient import TestClient

from app.main import app
from app.rational import canonical_decimal, rational_from_decimal

client = TestClient(app)


def post(path, payload):
    import json

    return client.post(
        path, content=json.dumps(payload), headers={"Content-Type": "application/json"}
    )


def test_exponent_and_fraction_literals_exact():
    assert rational_from_decimal("1.2e3") == Fraction(1200)
    assert rational_from_decimal("2e-2") == Fraction(2, 100)
    assert rational_from_decimal("7/4") == Fraction(7, 4)
    assert canonical_decimal(Fraction(1, 7)) == "0.(142857)"


def test_equivalent_request_formats_review_identically():
    req_a = {"peaks": ["2", "4", "6", "8"], "tolerance": "0.01", "impurity_quota": 0}
    req_b = {"peaks": ["4/2", "4.0", "6", "8"], "tolerance": "1e-2", "impurity_quota": 0}
    result = post("/api/v1/index", req_a).json()
    # Equivalent exact rational spellings carry the same request fingerprint.
    r = post("/api/v1/verify", {"request": req_b, "result": result})
    assert r.status_code == 200, r.text
    assert r.json()["valid"] is True


def test_tolerance_boundary_is_inclusive():
    # First and last peaks sit at exactly +-tolerance from the line; the
    # optimum has max residual exactly equal to the tolerance and must be
    # accepted (the bound is inclusive, compared exactly).
    body = {"peaks": ["1.99", "4", "6", "8.01"], "tolerance": "0.01"}
    equal = post("/api/v1/index", body)
    assert equal.status_code == 200, equal.text
    w = equal.json()["witnesses"][0]
    assert [m["n"] for m in w["mappings"]] == [1, 2, 3, 4]
    assert w["max_abs_residual"] == "0.01"
    assert w["mappings"][0]["residual"] == "0.01"
    assert w["mappings"][3]["residual"] == "0.01"

    # One rational notch below the tolerance makes that mapping infeasible.
    below = post("/api/v1/index", {**body, "tolerance": "999/100000"})
    # Either no solution at all, or the N=[1,2,3,4] boundary mapping is
    # gone (it required the full 0.01 tolerance).
    if below.status_code == 200:
        assert [m["n"] for m in below.json()["witnesses"][0]["mappings"]] != [1, 2, 3, 4]
    else:
        assert below.json()["error"]["code"] == "E2001_NO_SOLUTION"


def test_non_positive_tolerance_rejected():
    r = post("/api/v1/index", {"peaks": ["1", "2", "3", "4"], "tolerance": "0"})
    assert r.json()["error"]["code"] == "E1006_TOLERANCE_NOT_POSITIVE"


def test_integer_json_numbers_accepted_but_floats_rejected():
    r = post("/api/v1/index", {"peaks": [2, 4, 6, 8], "tolerance": "0.01"})
    assert r.status_code == 200, r.text
    r2 = post("/api/v1/index", {"peaks": [2, 4, 6, 8.0], "tolerance": "0.01"})
    assert r2.json()["error"]["code"] == "E1002_INVALID_RATIONAL"


def test_unknown_endpoint_and_method():
    r = client.get("/api/v1/index")
    assert r.status_code == 405
    r = client.get("/nope")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "E4004_NOT_FOUND"


def test_malformed_verification_body():
    r = post("/api/v1/verify", {"request": {}})
    assert r.json()["error"]["code"] == "E3001_CERTIFICATE_MALFORMED"
