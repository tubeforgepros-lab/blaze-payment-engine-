"""Manual end-to-end check against a running server with signature verification."""
import hashlib
import hmac
import json
import sqlite3
import time

import requests

BASE = "http://127.0.0.1:8004"
SECRET = "whsec_test"


def sign(payload: bytes, secret: str) -> str:
    ts = int(time.time())
    signed = f"{ts}.".encode() + payload
    sig = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


def balance():
    return requests.get(f"{BASE}/v1/ledger/balances", timeout=10).json()["native_ledger"][0]["balance"]


conn = sqlite3.connect("blaze_ledgers.db")
conn.execute(
    "INSERT INTO track_ledger(job_id,amount,status,bank_tx_id) VALUES('job_x',10.0,'SETTLED','po_123')"
)
conn.execute("UPDATE native_ledger SET balance=990 WHERE account_id='pool_01'")
conn.commit()
conn.close()
print("seeded balance:", balance())

body = json.dumps(
    {
        "id": "evt_signed_1",
        "type": "payout.failed",
        "data": {"object": {"id": "po_123", "amount": 1000, "failure_code": "R01"}},
    }
).encode()

# 1. unsigned -> rejected
r = requests.post(f"{BASE}/v1/webhooks/stripe", data=body, timeout=10)
print("unsigned:", r.status_code, r.json())

# 2. signed -> reversed
r = requests.post(
    f"{BASE}/v1/webhooks/stripe",
    data=body,
    headers={"stripe-signature": sign(body, SECRET), "Content-Type": "application/json"},
    timeout=10,
)
print("signed #1:", r.status_code, r.json())

# 3. signed replay -> duplicate ignored
r = requests.post(
    f"{BASE}/v1/webhooks/stripe",
    data=body,
    headers={"stripe-signature": sign(body, SECRET), "Content-Type": "application/json"},
    timeout=10,
)
print("signed #2 (replay):", r.status_code, r.json())

# 4. bad signature -> rejected
r = requests.post(
    f"{BASE}/v1/webhooks/stripe",
    data=body,
    headers={"stripe-signature": sign(body, "wrong_secret"), "Content-Type": "application/json"},
    timeout=10,
)
print("bad sig:", r.status_code, r.json())

print("final balance (expect 1000.0):", balance())
