"""API: per-persona screens, 403 + PERMISSION_DENIED, CSR lookup, host check (TestClient, temporary state)."""
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.config import Config
from tests.conftest import DATA_DIR


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    app = create_app(DATA_DIR, state_root=str(tmp_path_factory.mktemp("state")),
                     cfg=Config(overrides={"agent.json": {"mode": "mock", "max_tool_calls": 12}}))
    with TestClient(app) as c:
        yield c, app


def H(user, sc="DISPUTE_REPLAY"):
    return {"X-User-Id": user, "X-Dataset-Id": "dataset_A_with_errors", "X-Scenario-Id": sc}


def test_two_personas_and_screens(client):
    c, app = client
    assert sorted(u["user_id"] for u in c.get("/api/users").json()["users"]) == ["u.csr", "u.dri"]
    expect = {"u.csr": {"lookup": "A", "backscan": "A", "cases": "V"}, "u.dri": {"lookup": "-", "backscan": "V", "cases": "A"}}
    for u, levels in expect.items():
        p = c.get("/api/me", headers=H(u)).json()["permissions"]
        assert "finance" not in p and "outputs" not in p
        for k, v in levels.items():
            assert p[k] == v, (u, k)


def test_forbidden_calls_return_403_and_are_logged(client):
    c, app = client
    eng = app.state.tb.engine
    before = sum(1 for e in eng.audit.events if e["action"] == "PERMISSION_DENIED")
    assert c.post("/api/lookup", json={"account_id": "8800010101"}, headers=H("u.dri")).status_code == 403
    assert c.post("/api/backscan/send", json={}, headers=H("u.dri", "BACKSCAN_2026-11-10")).status_code == 403
    assert c.get("/api/me", headers=H("u.finance")).status_code == 400          # Finance no longer exists
    after = sum(1 for e in eng.audit.events if e["action"] == "PERMISSION_DENIED")
    assert after - before == 2


def test_csr_lookup_endpoint(client):
    c, app = client
    r = c.post("/api/lookup", json={"account_id": "8800010455", "as_of": "2026-11-10"}, headers=H("u.csr"))
    assert r.status_code == 200 and r.json()["status"] == "STARTED" and r.json()["as_of"] == "2026-11-10 23:59"
    assert c.post("/api/lookup", json={"account_id": "nope"}, headers=H("u.csr")).status_code == 400
    assert c.post("/api/lookup", json={"account_id": "8800010101", "as_of": "2027-01-01"}, headers=H("u.csr")).status_code == 400
    items = c.get("/api/lookup", headers=H("u.csr")).json()["items"]
    assert items and items[0]["account_id"] == "8800010455"


def test_unknown_user_and_host(client):
    c, app = client
    assert c.get("/api/me").status_code == 400
    assert c.get("/api/users", headers={"host": "evil.example"}).status_code == 403


def test_dashboard_and_state(client):
    c, app = client
    r = c.get("/api/state", headers=H("u.dri")).json()
    assert r["extract_end"] == "2026-11-10 23:59" and "timeline" not in r and r["knowledge"]["mode"] in ("chromadb", "fallback")
    assert c.post("/api/timeline/play", json={}, headers=H("u.dri")).status_code == 404
    assert c.post("/api/demo/reset", json={}, headers=H("u.dri")).status_code == 404
    d = c.get("/api/dashboard", headers=H("u.dri")).json()
    assert "gate" in d["kpis"] and "exposure_addressed" in d["kpis"]


def test_offline_no_outbound():
    from app import guard
    assert guard.blocked == []
