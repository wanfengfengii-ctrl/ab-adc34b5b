"""HTTP-layer tests for the versioned JSON API."""
from __future__ import annotations

import base64
import json

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_healthz():
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_index_unique():
    r = client.post(
        "/api/v1/index",
        json={"peaks": ["3/2", "3", "9/2", "6"], "tolerance": "0", "max_impurities": 0},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "unique"
    assert body["solutions"][0]["scale_factor"] == "3/2"
    assert [m["n"] for m in body["solutions"][0]["mappings"]] == [1, 2, 3, 4]
    assert "certificate" in body and "." in body["certificate"]


def test_index_ambiguous_two_witnesses():
    r = client.post(
        "/api/v1/index",
        json={"peaks": ["272", "274", "278", "280", "288"], "tolerance": "0"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ambiguous"
    assert len(body["solutions"]) == 2


def test_no_solution_stable_code():
    r = client.post(
        "/api/v1/index",
        json={"peaks": ["1", "2", "3", "335"], "tolerance": "0", "max_impurities": 0},
    )
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "NO_SOLUTION"
    # No partial result leaks in an error envelope.
    assert "solutions" not in err and "certificate" not in err


@pytest.mark.parametrize(
    "payload,code",
    [
        ({"peaks": ["1", "2", "3"], "tolerance": "0"}, "PEAK_COUNT_OUT_OF_RANGE"),
        ({"peaks": ["1", "2", "2", "3"], "tolerance": "0"}, "PEAKS_NOT_STRICTLY_INCREASING"),
        ({"peaks": ["1", "2", "0", "3"], "tolerance": "0"}, "NON_POSITIVE_PEAK"),
        ({"peaks": [1.5, "3", 4.5, 6], "tolerance": "0"}, "INVALID_RATIONAL"),
        ({"peaks": ["1", "2", "3", "4"], "tolerance": "0", "max_impurities": 3},
         "IMPURITY_BUDGET_OUT_OF_RANGE"),
        ({"peaks": ["1", "2", "3", "4"], "tolerance": "-1/10"}, "INVALID_TOLERANCE"),
        ({"peaks": ["1", "2", "3", "4/0"]}, "INVALID_RATIONAL"),
    ],
)
def test_validation_codes(payload, code):
    payload.setdefault("tolerance", "0")
    r = client.post("/api/v1/index", json=payload)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == code


def test_malformed_json_body():
    r = client.post("/api/v1/index", content="{not json", headers={"content-type": "application/json"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "INVALID_REQUEST_BODY"


def test_missing_field_code():
    r = client.post("/api/v1/index", json={"tolerance": "0"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "INVALID_REQUEST_BODY"


def test_certificate_verify_roundtrip():
    made = client.post(
        "/api/v1/index",
        json={"peaks": ["3/2", "3", "9/2", "6"], "tolerance": "0"},
    ).json()
    r = client.post("/api/v1/verify", json={"certificate": made["certificate"]})
    assert r.status_code == 200
    body = r.json()
    assert body["valid"] is True
    assert body["solutions"][0]["scale_factor"] == "3/2"


def test_certificate_bad_signature():
    made = client.post(
        "/api/v1/index",
        json={"peaks": ["1", "2", "3", "4"], "tolerance": "0"},
    ).json()
    bad = made["certificate"][:-1] + ("0" if made["certificate"][-1] != "0" else "1")
    r = client.post("/api/v1/verify", json={"certificate": bad})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "CERTIFICATE_SIGNATURE_INVALID"


def test_certificate_tampered_payload_recomputed():
    # Re-sign a modified payload; signature is valid but the answer no longer
    # matches the real recomputation.
    import hashlib
    import hmac
    import os

    from app.certificate import DEFAULT_DEV_SECRET

    made = client.post(
        "/api/v1/index",
        json={"peaks": ["1", "2", "3", "4"], "tolerance": "0"},
    ).json()
    body_b64, _ = made["certificate"].split(".")
    pad = "=" * (-len(body_b64) % 4)
    payload = json.loads(base64.urlsafe_b64decode(body_b64 + pad))
    payload["solutions"][0]["skipped_representable"] = 99
    canon = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    nb = base64.urlsafe_b64encode(canon).rstrip(b"=").decode()
    secret = os.environ.get("CERT_SECRET", DEFAULT_DEV_SECRET).encode()
    ns = base64.urlsafe_b64encode(
        hmac.new(secret, nb.encode(), hashlib.sha256).digest()
    ).rstrip(b"=").decode()
    r = client.post("/api/v1/verify", json={"certificate": f"{nb}.{ns}"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "CERTIFICATE_RECOMPUTATION_MISMATCH"


def test_certificate_tampered_input_recomputed():
    # Re-sign a certificate whose INPUT peaks were altered: the signature is
    # cryptographically valid, but the embedded answer no longer matches a
    # fresh recomputation over the changed inputs.
    import hashlib
    import hmac
    import os

    from app.certificate import DEFAULT_DEV_SECRET

    made = client.post(
        "/api/v1/index",
        json={"peaks": ["3/2", "3", "9/2", "6"], "tolerance": "0"},
    ).json()
    body_b64, _ = made["certificate"].split(".")
    pad = "=" * (-len(body_b64) % 4)
    payload = json.loads(base64.urlsafe_b64decode(body_b64 + pad))
    payload["peaks"][0] = "2"  # change an input peak
    canon = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    nb = base64.urlsafe_b64encode(canon).rstrip(b"=").decode()
    secret = os.environ.get("CERT_SECRET", DEFAULT_DEV_SECRET).encode()
    ns = base64.urlsafe_b64encode(
        hmac.new(secret, nb.encode(), hashlib.sha256).digest()
    ).rstrip(b"=").decode()
    r = client.post("/api/v1/verify", json={"certificate": f"{nb}.{ns}"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "CERTIFICATE_RECOMPUTATION_MISMATCH"


def test_acceptance_endpoint():
    r = client.post("/api/v1/acceptance")
    assert r.status_code == 200
    body = r.json()
    assert body["all_passed"] is True
    assert body["passed"] == body["total"]
