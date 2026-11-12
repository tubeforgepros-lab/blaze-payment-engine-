"""Smoke test against a deployed gateway.

Usage:
    BLAZE_URL=https://your-service.onrender.com BLAZE_API_KEY=... python test_production_cloud.py
"""
import os
import time

import requests

base = os.getenv("BLAZE_URL", "").rstrip("/")
if not base:
    raise SystemExit("Set BLAZE_URL to your deployed service URL (e.g. https://xxx.onrender.com).")

server_url = f"{base}/v1/payments/onboard-and-payout"
headers = {"Content-Type": "application/json"}
if os.getenv("BLAZE_API_KEY"):
    headers["X-API-Key"] = os.environ["BLAZE_API_KEY"]

unique_id = int(time.time())
payload = {
    "agent_job_id": f"agent_tx_blaze_production_test_{unique_id}",
    "public_token": f"public-sandbox-99887766-5544-aabb-ccdd-{unique_id}",
    "amount": 1.00,
}

print("--- Health check ---")
print(requests.get(f"{base}/health", timeout=10).json())

print(f"\n--- Firing pre-flight request to {server_url} ---")
try:
    response = requests.post(server_url, json=payload, headers=headers, timeout=15)
    print(f"Status Code: {response.status_code}")
    print(f"Reply: {response.text}")
except Exception as exc:  # noqa: BLE001
    print(f"Network handshake refused! Error: {exc}")
