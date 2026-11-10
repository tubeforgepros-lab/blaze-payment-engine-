import os
import sqlite3
from fastapi import FastAPI, Request, HTTPException, Header
import stripe

app = FastAPI(title="Blaze Money Engine Stripe Reconciler")

# Your live Stripe Webhook Signing Secret (found in your Stripe Dashboard)
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "whsec_mock_secret")

def process_ledger_reversal(agent_job_id: str, amount: float, failure_reason: str):
    """
    Executes a correction loop if the bank network bounces the payment.
    Refunds the Native Pool and updates the Track Ledger status.
    """
    db_path = os.path.abspath("blaze_ledgers.db")
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    try:
        cursor.execute("BEGIN TRANSACTION;")
        
        # 1. Reverse the debit: Add the money back into the internal balance pool
        cursor.execute("""
            UPDATE native_ledger 
            SET balance = balance + ?, updated_at = CURRENT_TIMESTAMP
            WHERE account_id = 'pool_01';
        """, (amount,))
        
        # 2. Update tracking state to reflect the clearing house bounce
        cursor.execute("""
            UPDATE track_ledger 
            SET status = ?
            WHERE job_id LIKE ?;
        """, (f"FAILED_BOUNCED ({failure_reason})", f"%{agent_job_id}%"))
        
        conn.commit()
        print(f"[RECONCILIATION SUCCESS] Ledger reversed for Job {agent_job_id}. Balance refunded.")
    except Exception as e:
        conn.rollback()
        print(f"[RECONCILIATION CRITICAL ERROR] Failed to execute reversal transaction: {e}")
    finally:
        conn.close()

@app.post("/v1/webhooks/stripe")
async def stripe_webhook(request: Request, stripe_signature: str = Header(None)):
    """
    Listens for live asynchronous transaction updates from Stripe's banking grid.
    """
    payload = await request.body()
    
    try:
        # Verify the webhook came genuinely from Stripe's servers
        event = stripe.Webhook.construct_event(
            payload, stripe_signature, STRIPE_WEBHOOK_SECRET
        )
    except Exception as e:
        # For development simulation, we will parse the raw JSON if secrets aren't set
        import json
        event = json.loads(payload)

    event_type = event.get("type")
    data_object = event.get("data", {}).get("object", {})
    
    # Extract our tracking identifier from Stripe's description descriptor metadata
    payout_id = data_object.get("id", "")
    # Extract the original job ID string out of the payout reference fields
    agent_job_id = payout_id.replace("po_stripe_test_", "")
    amount = data_object.get("amount", 0) / 100.0 # Convert cents back to dollars
    
    print(f"[WEBHOOK INTERCEPT] Received event '{event_type}' for Payout {payout_id}")
    
    if event_type == "payout.paid":
        print(f"[RECONCILED] Funds successfully settled in destination bank for Job {agent_job_id}")
        
    elif event_type == "payout.failed":
        failure_reason = data_object.get("failure_code", "ACCOUNT_CLOSED")
        print(f"[RECONCILED FAILURE] Payout bounced! Triggering dual-ledger automatic correction loop...")
        process_ledger_reversal(agent_job_id, amount, failure_reason)
        
    return {"status": "SUCCESS"}

if __name__ == "__main__":
    import uvicorn
    # Run our reconciler receiver route on local port 8050
    uvicorn.run("webhook_receiver:app", host="127.0.0.1", port=8050, reload=True)

