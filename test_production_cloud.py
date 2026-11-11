import time
import requests

# ⚠️ REPLACE THIS URL with your actual public Web Service URL from your Render Dashboard!
# Example format: "https://onrender.com"
server_url = "https://YOUR_REAL_RENDER_URL.onrender.com/v1/payments/onboard-and-payout"

unique_id = int(time.time())
payload = {
    "agent_job_id": f"agent_tx_blaze_production_test_{unique_id}",
    "public_token": f"public-sandbox-99887766-5544-aabb-ccdd-{unique_id}", 
    "amount": 1.00  # Safe $1.00 micro-transaction baseline validation
}

print(f"--- Firing Production Pre-flight Request to Cloud Gateway ---")
print(f"Targeting: {server_url}\n")

try:
    response = requests.post(server_url, json=payload, timeout=10)
    print(f"Cloud Server Status Code: {response.status_code}")
    print(f"Cloud Server Ingestion Reply: {response.json()}")
except Exception as e:
    print(f"Network Handshake Refused! Error: {e}")
    print("Hint: Make sure you replaced the dummy URL string inside test_production_cloud.py with your real Render link.")
