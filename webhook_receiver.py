import os, sqlite3, stripe
from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException

load_dotenv()
app = FastAPI(title="Blaze Stripe Webhook & Reversal Receiver")
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "blaze_ledgers.db")
WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")

@app.post("/v1/webhooks/stripe")
async def handle_stripe_webhook(request: Request):
    raw_body = await request.body()
    sig_header = request.headers.get("stripe-signature")
    if WEBHOOK_SECRET:
        if not sig_header:
            raise HTTPException(status_code=400, detail="Missing Stripe-Signature header.")
        try:
            event = stripe.Webhook.construct_event(raw_body, sig_header, WEBHOOK_SECRET)
            event = event.to_dict() if hasattr(event, "to_dict") else dict(event)
        except Exception as err:
            raise HTTPException(status_code=400, detail="Invalid signature: " + str(err))
    else:
        event = await request.json()

    event_type = event.get("type")
    obj = event.get("data", {}).get("object", {})
    payout_id = obj.get("id", "")
    amount_dollars = obj.get("amount", 0) / 100.0
    failure_code = obj.get("failure_code", "UNKNOWN_ERROR")

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    try:
        if event_type == "payout.failed":
            cursor.execute("BEGIN TRANSACTION;")
            cursor.execute("UPDATE native_ledger SET balance = balance + ?, updated_at = CURRENT_TIMESTAMP WHERE account_id = 'pool_01';", (amount_dollars,))
            cursor.execute("UPDATE track_ledger SET status = ? WHERE job_id LIKE ?;", (f"FAILED_BOUNCED ({failure_code})", f"%{payout_id}%"))
            rows_updated = cursor.rowcount
            conn.commit()
            return {"status": "REVERSED", "payout_id": payout_id, "refunded_to_pool": amount_dollars, "ledger_rows_matched": rows_updated}
        elif event_type == "payout.paid":
            cursor.execute("UPDATE track_ledger SET status = 'FINAL_SETTLED' WHERE job_id LIKE ?;", (f"%{payout_id}%",))
            rows_updated = cursor.rowcount
            conn.commit()
            return {"status": "FINAL_SETTLED", "payout_id": payout_id, "ledger_rows_matched": rows_updated}
        return {"status": "IGNORED", "event_type": event_type}
    except Exception as err:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(err))
    finally:
        conn.close()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("webhook_receiver:app", host="127.0.0.1", port=8050)
