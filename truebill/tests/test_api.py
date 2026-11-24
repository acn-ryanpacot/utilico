"""API: per-persona screens, 403 + PERMISSION_DENIED, host check, SSE endpoint (TestClient, temporary state)."""
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import DATA_DIR


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    app = create_app(DATA_DIR, state_root=str(tmp_path_factory.mktemp("state")))
    with TestClient(app) as c:
        yield c, app


def H(user, sc="DISPUTE_REPLAY"):
    return {"X-User-Id": user, "X-Dataset-Id": "dataset_A_with_errors", "X-Scenario-Id": sc}


def test_screens_per_persona(client):
    c, app = client
    expect = {"u.csr": {"finance": "-", "outputs": "-", "backscan": "A", "cases": "V"},
              "u.dri": {"finance": "V", "outputs": "A", "backscan": "V", "cases": "A"},
              "u.finance": {"finance": "A", "outputs": "-", "backscan": "V", "cases": "V"}}
    for u, levels in expect.items():
        p = c.get("/api/me", headers=H(u)).json()["permissions"]
        for k, v in levels.items():
            assert p[k] == v, (u, k)


def test_forbidden_calls_return_403_and_are_logged(client):
    c, app = client
    eng = app.state.tb.engine
    before = sum(1 for e in eng.audit.events if e["action"] == "PERMISSION_DENIED")
    assert c.get("/api/finance", headers=H("u.csr")).status_code == 403
    assert c.get("/api/outputs", headers=H("u.finance")).status_code == 403
    assert c.post("/api/backscan/send", json={}, headers=H("u.dri", "BACKSCAN_2026-11-10")).status_code == 403
    after = sum(1 for e in eng.audit.events if e["action"] == "PERMISSION_DENIED")
    assert after - before == 3


def test_unknown_user_and_host(client):
    c, app = client
    assert c.get("/api/me").status_code == 400
    assert c.get("/api/users", headers={"host": "evil.example"}).status_code == 403


def test_dashboard_and_state(client):
    c, app = client
    r = c.get("/api/state", headers=H("u.dri")).json()
    assert r["timeline"]["clock"] == "2026-11-02 00:00" and r["knowledge"]["mode"] in ("chromadb", "fallback")
    d = c.get("/api/dashboard", headers=H("u.dri")).json()
    assert "gate" in d["kpis"]


def test_offline_no_outbound():
    from app import guard
    assert guard.blocked == []
