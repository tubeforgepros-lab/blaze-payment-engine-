import time
import requests

# Stacking the endpoint URLs safely to clear loopback ports
ip_part = "127.0.0.1"
port_part = "8000"
route_part = "v1/payments/onboard-and-payout"
server_url = f"http://{ip_part}:{port_part}/{route_part}"

unique_id = int(time.time())
payload = {
    "agent_job_id": f"agent_tx_blaze_2026_auth_{unique_id}",
    "public_token": f"public-sandbox-99887766-5544-aabb-ccdd-{unique_id}", 
    "amount": 1.00  # ◄ Updated explicitly to $1.00 for micro-payout validation
}

print(f"--- Sending Autonomous Ingestion Request for Job: {payload['agent_job_id']} ---")
try:
    response = requests.post(server_url, json=payload)
    print(f"Server Status Code: {response.status_code}")
    print(f"Server Ingestion Reply: {response.json()}\n")
except Exception as e:
    print(f"Connection Failed! Error: {e}")
