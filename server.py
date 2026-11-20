import os, stripe
from db import get_conn, q, init_db
from fastapi import FastAPI, HTTPException, BackgroundTasks, Request
from pydantic import BaseModel
from tasks import dispatch_autonomous_payout

app = FastAPI(title="Blaze Money Engine Ingestion Gateway")
WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")
init_db()

class PlaidOnboardRequest(BaseModel):
    agent_job_id: str
    public_token: str
    amount: float

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.get("/v1/ledger/balances")
async def get_ledger_balances():
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute(q("SELECT account_id, account_type, balance, updated_at FROM native_ledger;"))
    native = [{"account_id": r[0], "type": r[1], "balance": r[2], "updated_at": r[3]} for r in cursor.fetchall()]
    cursor.execute(q("SELECT job_id, amount, status, logged_at FROM track_ledger ORDER BY logged_at DESC LIMIT 20;"))
    track = [{"job_id": r[0], "amount": r[1], "status": r[2], "logged_at": r[3]} for r in cursor.fetchall()]
    conn.close()
    return {"native_ledger": native, "track_ledger": track}

@app.post("/v1/payments/onboard-and-payout")
async def onboard_and_payout(payload: PlaidOnboardRequest, background_tasks: BackgroundTasks):
    if payload.amount <= 0:
        raise HTTPException(status_code=400, detail="Transaction amount must be greater than $0.00.")
    permanent_access_token = f"access-sandbox-{payload.public_token.split('-')[-1]}"
    if os.getenv("CELERY_BROKER_URL"):
        task = dispatch_autonomous_payout.delay(
            agent_job_id=payload.agent_job_id,
            account_token=permanent_access_token,
            routing_token="rtg_stripe_rail_222",
            amount=payload.amount
        )
        return {"status": "QUEUED", "mode": "celery", "background_task_id": task.id}
    background_tasks.add_task(
        dispatch_autonomous_payout,
        agent_job_id=payload.agent_job_id,
        account_token=permanent_access_token,
        routing_token="rtg_stripe_rail_222",
        amount=payload.amount
    )
    return {"status": "QUEUED", "mode": "cloud_async", "agent_job_id": payload.agent_job_id}

@app.post("/v1/webhooks/stripe")
async def handle_stripe_webhook(request: Request):
    raw_body = await request.body()
    sig_header = request.headers.get("stripe-signature")
    if WEBHOOK_SECRET:
        if not sig_header:
            raise HTTPException(status_code=400, detail="Missing Stripe-Signature header.")
        try:
            event = stripe.Webhook.construct_event(raw_body, sig_header, WEBHOOK_SECRET)
        except Exception as err:
            raise HTTPException(status_code=400, detail=f"Invalid signature: {err}")
    else:
        event = await request.json()
    event_type = event.get("type")
    obj = event.get("data", {}).get("object", {})
    payout_id = obj.get("id", "")
    amount_dollars = obj.get("amount", 0) / 100.0
    failure_code = obj.get("failure_code", "UNKNOWN_ERROR")
    conn = get_conn()
    cursor = conn.cursor()
    try:
        if event_type == "payout.failed":
            cursor.execute(q("BEGIN TRANSACTION;"))
            cursor.execute(q("UPDATE native_ledger SET balance = balance + ?, updated_at = CURRENT_TIMESTAMP WHERE account_id = 'pool_01';"), (amount_dollars,))
            cursor.execute(q("UPDATE track_ledger SET status = ? WHERE job_id LIKE ?;"), (f"FAILED_BOUNCED ({failure_code})", f"%{payout_id}%"))
            conn.commit()
            return {"status": "REVERSED", "payout_id": payout_id, "refunded_to_pool": amount_dollars}
        elif event_type == "payout.paid":
            cursor.execute(q("UPDATE track_ledger SET status = 'FINAL_SETTLED' WHERE job_id LIKE ?;"), (f"%{payout_id}%",))
            conn.commit()
            return {"status": "FINAL_SETTLED", "payout_id": payout_id}
        return {"status": "IGNORED", "event_type": event_type}
    except Exception as err:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(err))
    finally:
        conn.close()

@app.post("/v1/payments/onboard-and-payout")
async def onboard_and_payout(payload: PlaidOnboardRequest, background_tasks: BackgroundTasks):
    if payload.amount <= 0:
        raise HTTPException(status_code=400, detail="Transaction amount must be greater than $0.00.")
    tok = f"access-sandbox-{payload.public_token.split('-')[-1]}"
    if os.getenv("CELERY_BROKER_URL"):
        t = dispatch_autonomous_payout.delay(agent_job_id=payload.agent_job_id, account_token=tok, routing_token="rtg_stripe_rail_222", amount=payload.amount)
        return {"status": "QUEUED", "mode": "celery", "background_task_id": t.id}
    background_tasks.add_task(dispatch_autonomous_payout, agent_job_id=payload.agent_job_id, account_token=tok, routing_token="rtg_stripe_rail_222", amount=payload.amount)
    return {"status": "QUEUED", "mode": "cloud_async", "agent_job_id": payload.agent_job_id}

@app.post("/v1/webhooks/stripe")
async def handle_stripe_webhook(request: Request):
    raw_body = await request.body()
    sig = request.headers.get("stripe-signature")
    event = stripe.Webhook.construct_event(raw_body, sig, WEBHOOK_SECRET) if WEBHOOK_SECRET else await request.json()
    etype = event.get("type")
    obj = event.get("data", {}).get("object", {})
    pid, amt, code = obj.get("id", ""), obj.get("amount", 0) / 100.0, obj.get("failure_code", "UNKNOWN")
    conn = get_conn()
    c = conn.cursor()
    try:
        if etype == "payout.failed":
            c.execute(q("BEGIN TRANSACTION;"))
            c.execute(q("UPDATE native_ledger SET balance = balance + ?, updated_at = CURRENT_TIMESTAMP WHERE account_id = 'pool_01';"), (amt,))
            c.execute(q("UPDATE track_ledger SET status = ? WHERE job_id LIKE ?;"), (f"FAILED_BOUNCED ({code})", f"%{pid}%"))
            conn.commit()
            return {"status": "REVERSED", "payout_id": pid, "refunded": amt}
        elif etype == "payout.paid":
            c.execute(q("UPDATE track_ledger SET status = 'FINAL_SETTLED' WHERE job_id LIKE ?;"), (f"%{pid}%",))
            conn.commit()
            return {"status": "FINAL_SETTLED", "payout_id": pid}
        return {"status": "IGNORED", "event_type": etype}
    finally:
        conn.close()

@app.get("/v1/plaid/accounts")
async def get_plaid_accounts():
    from tasks import plaid_client
    from plaid.model.accounts_get_request import AccountsGetRequest
    res = plaid_client.accounts_get(AccountsGetRequest(access_token=os.getenv("PLAID_ACCESS_TOKEN"))).to_dict()
    return [{"name": a["name"], "mask": a["mask"], "subtype": str(a["subtype"]), "PLAID_ACCOUNT_ID": a["account_id"]} for a in res["accounts"]]

@app.get("/v1/plaid/link")
async def create_plaid_link():
    try:
        from tasks import plaid_client
        from plaid.model.link_token_create_request import LinkTokenCreateRequest
        from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
        from plaid.model.products import Products
        from plaid.model.country_code import CountryCode
        res = plaid_client.link_token_create(LinkTokenCreateRequest(
            user=LinkTokenCreateRequestUser(client_user_id="blazepay_admin"),
            client_name="Blaze Payment Engine",
            products=[Products("auth")],
            country_codes=[CountryCode("US")],
            language="en"
        )).to_dict()
        return {"link_token": res["link_token"]}
    except Exception as e:
        return {"error": getattr(e, "body", None) or str(e)}

@app.post("/v1/plaid/exchange")
async def exchange_plaid_token(payload: dict):
    try:
        from tasks import plaid_client
        from plaid.model.item_public_token_exchange_request import ItemPublicTokenExchangeRequest
        from plaid.model.accounts_get_request import AccountsGetRequest
        ex = plaid_client.item_public_token_exchange(ItemPublicTokenExchangeRequest(public_token=payload["public_token"])).to_dict()
        at = ex["access_token"]
        accts = plaid_client.accounts_get(AccountsGetRequest(access_token=at)).to_dict()["accounts"]
        return {"PLAID_ACCESS_TOKEN": at, "accounts": [{"name": a["name"], "mask": a["mask"], "PLAID_ACCOUNT_ID": a["account_id"]} for a in accts]}
    except Exception as e:
        return {"error": getattr(e, "body", None) or str(e)}
