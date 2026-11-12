import sqlite3

import pytest

from init_ledgers import DB_PATH
from tasks import dispatch_autonomous_payout


def pool_balance():
    conn = sqlite3.connect(DB_PATH)
    try:
        return conn.execute(
            "SELECT balance FROM native_ledger WHERE account_id = 'pool_01';"
        ).fetchone()[0]
    finally:
        conn.close()


def rows():
    conn = sqlite3.connect(DB_PATH)
    try:
        return conn.execute(
            "SELECT job_id, amount, status, bank_tx_id FROM track_ledger;"
        ).fetchall()
    finally:
        conn.close()


def seed_txn(job_id, amount, status, bank_tx_id=None):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO track_ledger (job_id, amount, status, bank_tx_id) VALUES (?,?,?,?);",
        (job_id, amount, status, bank_tx_id),
    )
    conn.commit()
    conn.close()


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_single_route_definitions():
    import server

    paths = [r.path for r in server.app.routes if hasattr(r, "methods")]
    assert paths.count("/v1/payments/onboard-and-payout") == 1
    assert paths.count("/v1/webhooks/stripe") == 1


def test_rejects_non_positive_amount(client):
    for amount in (0, -5):
        r = client.post(
            "/v1/payments/onboard-and-payout",
            json={"agent_job_id": "j1", "public_token": "p", "amount": amount},
        )
        assert r.status_code == 422


def test_webhook_requires_payout_id(client):
    r = client.post(
        "/v1/webhooks/stripe",
        json={"id": "evt_1", "type": "payout.paid", "data": {"object": {"amount": 100}}},
    )
    assert r.status_code == 400


def test_duplicate_webhook_does_not_double_credit(client):
    seed_txn("job_a", 10.0, "SETTLED", "po_123")
    before = pool_balance()

    payload = {
        "id": "evt_dup",
        "type": "payout.failed",
        "data": {"object": {"id": "po_123", "amount": 1000, "failure_code": "R01"}},
    }
    first = client.post("/v1/webhooks/stripe", json=payload)
    second = client.post("/v1/webhooks/stripe", json=payload)

    assert first.json()["status"] == "REVERSED"
    assert second.json()["status"] == "DUPLICATE_IGNORED"
    assert pool_balance() == pytest.approx(before + 10.0)
    assert rows()[0][2].startswith("FAILED_BOUNCED")


def test_reversal_uses_stored_amount_not_payload(client):
    seed_txn("job_b", 25.0, "SETTLED", "po_999")
    before = pool_balance()
    client.post(
        "/v1/webhooks/stripe",
        json={
            "id": "evt_amt",
            "type": "payout.failed",
            "data": {"object": {"id": "po_999", "amount": 999999}},
        },
    )
    assert pool_balance() == pytest.approx(before + 25.0)


def test_unknown_payout_never_credits(client):
    before = pool_balance()
    r = client.post(
        "/v1/webhooks/stripe",
        json={
            "id": "evt_unknown",
            "type": "payout.failed",
            "data": {"object": {"id": "po_zzz", "amount": 500}},
        },
    )
    assert r.json()["status"] == "NO_MATCH"
    assert pool_balance() == before


def test_paid_does_not_settle_unrelated_rows(client):
    seed_txn("job_c", 5.0, "SETTLED", "po_aaa")
    seed_txn("job_d", 7.0, "PENDING_BANK_CLEARING", None)
    r = client.post(
        "/v1/webhooks/stripe",
        json={
            "id": "evt_paid",
            "type": "payout.paid",
            "data": {"object": {"id": "po_aaa", "amount": 500}},
        },
    )
    assert r.status_code == 200
    statuses = {row[0]: row[2] for row in rows()}
    assert statuses["job_c"] == "FINAL_SETTLED"
    assert statuses["job_d"] == "PENDING_BANK_CLEARING"


def test_signed_webhook_with_stripe_object(client, monkeypatch):
    """construct_event returns a StripeObject; the handler must not 500."""
    import stripe

    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test")
    seed_txn("job_sig", 10.0, "SETTLED", "po_sig")
    before = pool_balance()

    event = stripe.Event.construct_from(
        {
            "id": "evt_sig",
            "type": "payout.failed",
            "data": {"object": {"id": "po_sig", "amount": 1000, "failure_code": "R01"}},
        },
        "sk_test",
    )
    monkeypatch.setattr(stripe.Webhook, "construct_event", lambda *a, **k: event)

    r = client.post(
        "/v1/webhooks/stripe",
        json={"unused": True},
        headers={"stripe-signature": "t=1,v1=deadbeef"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "REVERSED"
    assert pool_balance() == pytest.approx(before + 10.0)


def test_duplicate_job_id_rejected(client):
    first = dispatch_autonomous_payout(
        agent_job_id="job_dup", account_token="t", routing_token="r", amount=5.0
    )
    assert first["status"] == "FAILED"  # Plaid verification fails closed
    seed_txn("job_dup2", 5.0, "PENDING_BANK_CLEARING")
    second = dispatch_autonomous_payout(
        agent_job_id="job_dup2", account_token="t", routing_token="r", amount=5.0
    )
    assert second["status"] == "REJECTED"


def test_failed_stripe_call_rolls_back(client, monkeypatch):
    import stripe

    monkeypatch.setenv("BLAZE_ALLOW_UNVERIFIED_PLAID", "true")
    before = pool_balance()

    def boom(**kwargs):
        raise RuntimeError("card_declined")

    monkeypatch.setattr(stripe.Payout, "create", boom)
    res = dispatch_autonomous_payout(
        agent_job_id="job_rb", account_token="t", routing_token="r", amount=40.0
    )
    assert res["status"] == "FAILED"
    assert pool_balance() == pytest.approx(before)
    assert "FAILED_ROLLED_BACK" in rows()[0][2]


def test_insufficient_funds_blocks_payout(client, monkeypatch):
    monkeypatch.setenv("BLAZE_ALLOW_UNVERIFIED_PLAID", "true")
    before = pool_balance()
    res = dispatch_autonomous_payout(
        agent_job_id="job_big", account_token="t", routing_token="r", amount=before + 1
    )
    assert res["status"] == "FAILED"
    assert pool_balance() == pytest.approx(before)
