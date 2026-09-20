import json

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def post(path, payload, *, raw=False):
    data = payload if raw else json.dumps(payload).encode()
    return client.post(path, content=data, headers={"Content-Type": "application/json"})


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_index_unique_end_to_end():
    r = post("/api/v1/index", {"peaks": ["2", "4", "6", "8"], "tolerance": "0.01"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "unique"
    assert body["witness_count"] == 1
    w = body["witnesses"][0]
    assert [m["n"] for m in w["mappings"]] == [1, 2, 3, 4]
    assert w["scale_factor"] == "2"
    assert len(body["certificate"]) == 64


def test_index_ambiguous_end_to_end():
    r = post("/api/v1/index", {"peaks": ["152", "153", "154", "157"], "tolerance": "1e-9"})
    body = r.json()
    assert body["status"] == "ambiguous"
    assert body["witness_count"] == 2
    assert body["witnesses"][0] != body["witnesses"][1]


def test_no_solution_has_no_partial_payload():
    r = post(
        "/api/v1/index",
        {"peaks": ["1", "1000", "2000", "3000"], "tolerance": "1e-9"},
    )
    assert r.status_code == 422
    body = r.json()
    assert body["error"]["code"] == "E2001_NO_SOLUTION"
    assert "witnesses" not in body and "scale_factor" not in body


def test_float_token_rejected_over_http():
    r = post("/api/v1/index", {"peaks": [1.5, "2", "3", "4"], "tolerance": "0.01"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "E1002_INVALID_RATIONAL"


def test_verify_accepts_intact_certificate():
    req = {"peaks": ["2", "4", "6", "8"], "tolerance": "0.01", "impurity_quota": 0}
    result = post("/api/v1/index", req).json()
    r = post("/api/v1/verify", {"request": req, "result": result})
    assert r.status_code == 200, r.text
    assert r.json()["valid"] is True


def test_verify_rejects_tampered_mapping():
    req = {"peaks": ["2", "4", "6", "8"], "tolerance": "0.01", "impurity_quota": 0}
    result = post("/api/v1/index", req).json()
    result["witnesses"][0]["mappings"][0]["n"] = 999
    r = post("/api/v1/verify", {"request": req, "result": result})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "E3002_CERTIFICATE_TAMPERED"


def test_verify_rejects_forged_certificate():
    req = {"peaks": ["2", "4", "6", "8"], "tolerance": "0.01", "impurity_quota": 0}
    result = post("/api/v1/index", req).json()
    result["certificate"] = "0" * 64
    r = post("/api/v1/verify", {"request": req, "result": result})
    assert r.status_code == 409


def test_verify_rejects_result_for_other_request():
    req_a = {"peaks": ["2", "4", "6", "8"], "tolerance": "0.01", "impurity_quota": 0}
    req_b = {"peaks": ["3", "6", "9", "12"], "tolerance": "0.01", "impurity_quota": 0}
    result = post("/api/v1/index", req_a).json()
    r = post("/api/v1/verify", {"request": req_b, "result": result})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "E3003_CERTIFICATE_REQUEST_MISMATCH"


def test_malformed_json_and_content_type():
    r = client.post("/api/v1/index", content=b"{not json", headers={"Content-Type": "application/json"})
    assert r.status_code == 400
    r = client.post("/api/v1/index", content=b"{}", headers={"Content-Type": "text/plain"})
    assert r.status_code == 415


def test_tampering_scale_factor_detected():
    req = {"peaks": ["2", "4", "6", "8"], "tolerance": "0.01", "impurity_quota": 0}
    result = post("/api/v1/index", req).json()
    result["witnesses"][0]["scale_factor"] = "3"
    r = post("/api/v1/verify", {"request": req, "result": result})
    assert r.json()["error"]["code"] == "E3002_CERTIFICATE_TAMPERED"
