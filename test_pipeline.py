import time
from tasks import dispatch_autonomous_payout

# Automatically append a timestamp so every script execution uses a fresh, unique transaction ID
unique_suffix = int(time.time())
shared_job_id = f"agent_tx_blaze_2026_{unique_suffix}"

print(f"--- Generated Fresh Execution Token for this Run: {shared_job_id} ---")
print("--- Launching Step 1: Dispatching the initial autonomous transfer request ---")
first_attempt = dispatch_autonomous_payout.delay(
    agent_job_id=shared_job_id,
    account_token="act_plaid_test_998",
    routing_token="rtg_fed_test_111",
    amount=2500.00
)

# Give the background worker 1 full second to completely lock and process Step 1
time.sleep(1.0)

print("\n--- Launching Step 2: Agent experiences a timeout and retries the exact same job ID ---")
second_attempt = dispatch_autonomous_payout.delay(
    agent_job_id=shared_job_id,
    account_token="act_plaid_test_998",
    routing_token="rtg_fed_test_111",
    amount=2500.00
)

# Fetch results from the Celery backend queue
print("\n--- Reviewing System Settlement Status ---")
time.sleep(2.0)  # Give the worker a moment to process everything
print(f"First Dispatch Result: {first_attempt.get()}")
print(f"Second Dispatch Result: {second_attempt.get()}")
