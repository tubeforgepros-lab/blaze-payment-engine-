import os
import sqlite3

import stripe
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from init_ledgers import DB_PATH, setup_database
from tasks import dispatch_autonomous_payout

setup_database(DB_PATH)

app = FastAPI(title="Blaze Money Engine Ingestion Gateway", version="1.0.0")

_origins = [o.strip() for o in os.getenv("BLAZE_ALLOWED_ORIGINS", "*").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins or ["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


class PlaidOnboardRequest(BaseModel):
    agent_job_id: str = Field(min_length=1, max_length=128)
    public_token: str = Field(min_length=1, max_length=256)
    amount: float = Field(gt=0, le=100000)


def _db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000;")
    return conn


def require_api_key(x_api_key: str | None) -> None:
    expected = os.getenv("BLAZE_API_KEY")
    if expected and x_api_key != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key.")


@app.get("/health")
async def health():
    try:
        conn = _db()
        conn.execute("SELECT 1 FROM native_ledger LIMIT 1;").fetchone()
        conn.close()
        return {"status": "ok", "db": "ok"}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"database unavailable: {exc}")


@app.get("/v1/ledger/balances")
async def get_ledger_balances():
    conn = _db()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT account_id, account_type, balance, updated_at FROM native_ledger;"
        )
        native = [
            {"account_id": r[0], "type": r[1], "balance": r[2], "updated_at": r[3]}
            for r in cursor.fetchall()
        ]
        cursor.execute(
            "SELECT job_id, amount, status, bank_tx_id, logged_at "
            "FROM track_ledger ORDER BY logged_at DESC LIMIT 20;"
        )
        track = [
            {
                "job_id": r[0],
                "amount": r[1],
                "status": r[2],
                "bank_tx_id": r[3],
                "logged_at": r[4],
            }
            for r in cursor.fetchall()
        ]
        return {"native_ledger": native, "track_ledger": track}
    finally:
        conn.close()


@app.post("/v1/payments/onboard-and-payout")
async def onboard_and_payout(
    payload: PlaidOnboardRequest,
    background_tasks: BackgroundTasks,
    x_api_key: str | None = Header(default=None),
):
    require_api_key(x_api_key)

    # NOTE: a real Plaid Link exchange must call /item/public_token/exchange.
    # This derivation is a sandbox placeholder and is flagged in the readiness report.
    permanent_access_token = f"access-sandbox-{payload.public_token.split('-')[-1]}"

    if os.getenv("CELERY_BROKER_URL"):
        task = dispatch_autonomous_payout.delay(
            agent_job_id=payload.agent_job_id,
            account_token=permanent_access_token,
            routing_token="rtg_stripe_rail_222",
            amount=payload.amount,
        )
        return {"status": "QUEUED", "mode": "celery", "background_task_id": task.id}

    background_tasks.add_task(
        dispatch_autonomous_payout,
        agent_job_id=payload.agent_job_id,
        account_token=permanent_access_token,
        routing_token="rtg_stripe_rail_222",
        amount=payload.amount,
    )
    return {"status": "QUEUED", "mode": "cloud_async", "agent_job_id": payload.agent_job_id}


@app.post("/v1/webhooks/stripe")
async def handle_stripe_webhook(request: Request):
    raw_body = await request.body()
    sig_header = request.headers.get("stripe-signature")
    webhook_secret = os.getenv("STRIPE_WEBHOOK_SECRET")

    if webhook_secret:
        if not sig_header:
            raise HTTPException(status_code=400, detail="Missing Stripe-Signature header.")
        try:
            event = stripe.Webhook.construct_event(raw_body, sig_header, webhook_secret)
            # construct_event returns a StripeObject, not a dict.
            event = event.to_dict() if hasattr(event, "to_dict") else dict(event)
        except HTTPException:
            raise
        except Exception as err:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=f"Invalid signature: {err}")
    else:
        if os.getenv("BLAZE_ENV", "production").lower() == "production":
            raise HTTPException(
                status_code=503, detail="STRIPE_WEBHOOK_SECRET is not configured."
            )
        event = await request.json()

    event_id = event.get("id")
    event_type = event.get("type")
    obj = event.get("data", {}).get("object", {}) or {}
    payout_id = obj.get("id") or ""
    agent_job_id = (obj.get("metadata") or {}).get("agent_job_id")

    if not event_id:
        raise HTTPException(status_code=400, detail="Webhook event is missing an id.")
    if not payout_id:
        raise HTTPException(status_code=400, detail="Webhook payout object is missing an id.")

    conn = _db()
    cursor = conn.cursor()
    try:
        cursor.execute("BEGIN IMMEDIATE;")

        # Idempotency: Stripe retries webhooks. Claim the event id first.
        cursor.execute(
            "INSERT OR IGNORE INTO webhook_events (event_id, event_type, payout_id, amount) "
            "VALUES (?, ?, ?, ?);",
            (event_id, event_type, payout_id, obj.get("amount", 0) / 100.0),
        )
        if cursor.rowcount == 0:
            conn.rollback()
            return {"status": "DUPLICATE_IGNORED", "event_id": event_id}

        if event_type == "payout.failed":
            # Locate the exact transaction. Never use LIKE '%id%'.
            row = cursor.execute(
                "SELECT job_id, amount, status FROM track_ledger "
                "WHERE (bank_tx_id = ? OR job_id = ?) "
                "AND status IN ('PENDING_BANK_CLEARING', 'SETTLED');",
                (payout_id, agent_job_id),
            ).fetchone()

            if row is None:
                conn.rollback()
                # Unknown or already-reversed payout: acknowledge, do not credit.
                return {"status": "NO_MATCH", "payout_id": payout_id}

            job_id, amount, _status = row
            failure_code = obj.get("failure_code", "UNKNOWN_ERROR")
            cursor.execute(
                "UPDATE track_ledger SET status = ? WHERE job_id = ?;",
                (f"FAILED_BOUNCED ({failure_code})", job_id),
            )
            cursor.execute(
                "UPDATE native_ledger SET balance = balance + ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE account_id = 'pool_01';",
                (amount,),
            )
            conn.commit()
            return {
                "status": "REVERSED",
                "payout_id": payout_id,
                "job_id": job_id,
                "refunded_to_pool": amount,
            }

        if event_type == "payout.paid":
            cursor.execute(
                "UPDATE track_ledger SET status = 'FINAL_SETTLED', bank_tx_id = COALESCE(bank_tx_id, ?) "
                "WHERE (bank_tx_id = ? OR job_id = ?) AND status IN ('SETTLED', 'PENDING_BANK_CLEARING');",
                (payout_id, payout_id, agent_job_id),
            )
            if cursor.rowcount == 0:
                conn.rollback()
                # Not visible yet (payout.paid can race the DB write): let Stripe retry.
                raise HTTPException(status_code=503, detail="Transaction not yet recorded; retry.")
            conn.commit()
            return {"status": "FINAL_SETTLED", "payout_id": payout_id}

        conn.commit()
        return {"status": "IGNORED", "event_type": event_type}
    except HTTPException:
        raise
    except Exception as err:  # noqa: BLE001
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(err))
    finally:
        conn.close()
