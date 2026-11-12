"""Tests for the real Plaid + Stripe integration layer.

No network calls: plaid_service / stripe_service are monkeypatched so we can
assert the API layer performs the correct sequence (link token -> public token
exchange -> stored access token -> payout dispatch) and surfaces actionable
errors when credentials are missing.
"""
import sqlite3

import pytest

import plaid_service
import server
import stripe_service
import token_vault
from init_ledgers import DB_PATH


@pytest.fixture(autouse=True)
def _reset_plaid_client():
    plaid_service.reset_client()
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM plaid_items;")
    conn.commit()
    conn.close()
    yield
    plaid_service.reset_client()
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM plaid_items;")
    conn.commit()
    conn.close()


def _item_rows():
    conn = sqlite3.connect(DB_PATH)
    try:
        return conn.execute(
            "SELECT item_id, access_token, institution_name, account_mask FROM plaid_items;"
        ).fetchall()
    finally:
        conn.close()


def _fake_balances(total=2500.0):
    return {
        "available_total": total,
        "accounts": [
            {
                "account_id": "acct_123",
                "name": "Plaid Checking",
                "mask": "0000",
                "type": "depository",
                "subtype": "checking",
                "available": total,
                "currency": "USD",
            }
        ],
    }


# ---------------------------------------------------------------- diagnostics
def test_diagnostics_reports_unconfigured(monkeypatch):
    monkeypatch.delenv("PLAID_CLIENT_ID", raising=False)
    monkeypatch.delenv("PLAID_SECRET", raising=False)
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)

    from fastapi.testclient import TestClient

    with TestClient(server.app) as c:
        r = c.get("/v1/diagnostics/integrations")
    assert r.status_code == 200
    body = r.json()
    assert body["ready"] is False
    assert body["plaid"]["code"] == "PLAID_NOT_CONFIGURED"
    assert body["stripe"]["code"] == "STRIPE_NOT_CONFIGURED"
    assert body["config"]["plaid_configured"] is False
    assert body["config"]["stripe_configured"] is False


def test_diagnostics_reports_ready(monkeypatch):
    monkeypatch.setattr(
        server.plaid_service,
        "healthcheck",
        lambda: {"ok": True, "code": "OK", "env": "sandbox", "latency_ms": 12.0},
    )
    monkeypatch.setattr(
        server.stripe_service,
        "healthcheck",
        lambda: {"ok": True, "code": "OK", "mode": "test", "latency_ms": 8.0},
    )

    from fastapi.testclient import TestClient

    with TestClient(server.app) as c:
        body = c.get("/v1/diagnostics/integrations").json()
    assert body["ready"] is True
    assert body["plaid"]["code"] == "OK"
    assert body["stripe"]["code"] == "OK"


# ------------------------------------------------------------------ link token
def test_link_token_returns_503_without_credentials(monkeypatch):
    monkeypatch.delenv("PLAID_CLIENT_ID", raising=False)
    monkeypatch.delenv("PLAID_SECRET", raising=False)

    from fastapi.testclient import TestClient

    with TestClient(server.app) as c:
        r = c.post("/v1/plaid/link-token", json={"agent_job_id": "job-link"})
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "PLAID_NOT_CONFIGURED"


def test_link_token_success(monkeypatch):
    monkeypatch.setattr(
        server.plaid_service,
        "create_link_token",
        lambda agent_job_id, client_user_id=None: {
            "link_token": "link-sandbox-abc",
            "expiration": "2025-01-01T00:00:00Z",
            "request_id": "req-1",
        },
    )

    from fastapi.testclient import TestClient

    with TestClient(server.app) as c:
        r = c.post("/v1/plaid/link-token", json={"agent_job_id": "job-link"})
    assert r.status_code == 200
    assert r.json()["link_token"] == "link-sandbox-abc"


# -------------------------------------------------------------------- exchange
def test_exchange_stores_real_item(monkeypatch):
    monkeypatch.setattr(
        server.plaid_service,
        "exchange_public_token",
        lambda public_token: {
            "access_token": "access-sandbox-REAL-TOKEN",
            "item_id": "item-real-1",
            "request_id": "req-2",
        },
    )
    monkeypatch.setattr(server.plaid_service, "get_balances", lambda token: _fake_balances())
    monkeypatch.setattr(
        server.plaid_service, "get_institution_name", lambda iid: "First Platypus Bank"
    )

    from fastapi.testclient import TestClient

    with TestClient(server.app) as c:
        r = c.post("/v1/plaid/exchange", json={"public_token": "public-sandbox-xyz"})
    assert r.status_code == 200
    body = r.json()
    assert body["item_id"] == "item-real-1"
    assert body["available_total"] == 2500.0

    rows = _item_rows()
    assert any(row[0] == "item-real-1" and row[1] == "access-sandbox-REAL-TOKEN" for row in rows)


def test_exchange_surfaces_plaid_error(monkeypatch):
    def boom(public_token):
        raise plaid_service.PlaidServiceError(
            "PLAID_INVALID_PUBLIC_TOKEN", "The public_token is invalid or already used.", status=400
        )

    monkeypatch.setattr(server.plaid_service, "exchange_public_token", boom)

    from fastapi.testclient import TestClient

    with TestClient(server.app) as c:
        r = c.post("/v1/plaid/exchange", json={"public_token": "bad"})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "PLAID_INVALID_PUBLIC_TOKEN"


# -------------------------------------------------------------- onboard/payout
def test_onboard_requires_plaid_reference(client):
    r = client.post(
        "/v1/payments/onboard-and-payout",
        json={"agent_job_id": "job-no-ref", "amount": 5.0},
    )
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "MISSING_PLAID_REFERENCE"


def test_onboard_with_public_token_exchanges_and_queues(client, monkeypatch):
    monkeypatch.setattr(
        server.plaid_service,
        "exchange_public_token",
        lambda public_token: {
            "access_token": "access-sandbox-FRESH",
            "item_id": "item-fresh",
            "request_id": "req-3",
        },
    )
    monkeypatch.setattr(server.plaid_service, "get_balances", lambda token: _fake_balances())
    monkeypatch.setattr(server.plaid_service, "get_institution_name", lambda iid: "Bank")

    # Prevent the background task from touching the network.
    monkeypatch.setattr(server, "dispatch_autonomous_payout", lambda **kwargs: None)

    r = client.post(
        "/v1/payments/onboard-and-payout",
        json={"agent_job_id": "job-fresh", "public_token": "public-fresh", "amount": 10.0},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "QUEUED"
    assert body["item_id"] == "item-fresh"

    rows = _item_rows()
    assert any(row[0] == "item-fresh" and row[1] == "access-sandbox-FRESH" for row in rows)


def test_onboard_with_item_id_uses_stored_token(client, monkeypatch):
    # Seed a linked item directly.
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT OR REPLACE INTO plaid_items (item_id, access_token) VALUES (?, ?);",
        ("item-stored", "access-sandbox-STORED"),
    )
    conn.commit()
    conn.close()

    captured = {}

    def fake_dispatch(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(server, "dispatch_autonomous_payout", fake_dispatch)

    r = client.post(
        "/v1/payments/onboard-and-payout",
        json={"agent_job_id": "job-stored", "item_id": "item-stored", "amount": 7.5},
    )
    assert r.status_code == 200
    assert r.json()["item_id"] == "item-stored"
    assert captured["account_token"] == "access-sandbox-STORED"
    assert captured["amount"] == 7.5


# ------------------------------------------------------------------ token vault
def test_token_vault_roundtrip(monkeypatch):
    key = token_vault.generate_key()
    monkeypatch.setenv("BLAZE_TOKEN_ENCRYPTION_KEY", key)

    secret = "access-sandbox-super-secret"
    stored = token_vault.encrypt(secret)
    assert stored.startswith("enc:v1:")
    assert secret not in stored
    assert token_vault.decrypt(stored) == secret


def test_token_vault_passthrough_without_key(monkeypatch):
    monkeypatch.delenv("BLAZE_TOKEN_ENCRYPTION_KEY", raising=False)
    assert token_vault.encrypt("plain-token") == "plain-token"
    assert token_vault.decrypt("plain-token") == "plain-token"
