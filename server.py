import os
import sqlite3

import stripe
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

import plaid_service
import stripe_service
import token_vault
from init_ledgers import DB_PATH, setup_database
from tasks import dispatch_autonomous_payout

setup_database(DB_PATH)
stripe_service.configure()

app = FastAPI(title="Blaze Money Engine Ingestion Gateway", version="1.1.0")

_origins = [o.strip() for o in os.getenv("BLAZE_ALLOWED_ORIGINS", "*").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins or ["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


class LinkTokenRequest(BaseModel):
    agent_job_id: str = Field(min_length=1, max_length=128)


class ExchangeRequest(BaseModel):
    public_token: str = Field(min_length=1, max_length=512)


class PlaidOnboardRequest(BaseModel):
    agent_job_id: str = Field(min_length=1, max_length=128)
    item_id: str | None = Field(default=None, max_length=128)
    public_token: str | None = Field(default=None, max_length=512)
    amount: float = Field(gt=0, le=100000)


def _db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000;")
    return conn


def require_api_key(x_api_key: str | None) -> None:
    expected = os.getenv("BLAZE_API_KEY")
    if expected and x_api_key != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key.")


def _plaid_http_error(exc: plaid_service.PlaidServiceError) -> HTTPException:
    return HTTPException(
        status_code=exc.status, detail={"code": exc.code, "message": exc.message}
    )


def _store_item(exchange: dict, institution_id: str | None = None) -> dict:
    """Persist an exchanged Plaid item and return its public descriptor."""
    access_token = exchange["access_token"]
    item_id = exchange["item_id"]

    try:
        balances = plaid_service.get_balances(access_token)
    except plaid_service.PlaidServiceError as exc:
        raise _plaid_http_error(exc)

    primary = balances["accounts"][0] if balances["accounts"] else {}
    institution_name = plaid_service.get_institution_name(institution_id or "")

    conn = _db()
    try:
        conn.execute(
            """
            INSERT INTO plaid_items
                (item_id, access_token, institution_id, institution_name,
                 account_id, account_mask, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(item_id) DO UPDATE SET
                access_token = excluded.access_token,
                institution_id = excluded.institution_id,
                institution_name = excluded.institution_name,
                account_id = excluded.account_id,
                account_mask = excluded.account_mask,
                updated_at = CURRENT_TIMESTAMP;
            """,
            (
                item_id,
                token_vault.encrypt(access_token),
                institution_id,
                institution_name,
                primary.get("account_id"),
                primary.get("mask"),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    return {
        "item_id": item_id,
        "institution_name": institution_name,
        "accounts": balances["accounts"],
        "available_total": balances["available_total"],
    }


def _access_token_for(item_id: str) -> str:
    conn = _db()
    try:
        row = conn.execute(
            "SELECT access_token FROM plaid_items WHERE item_id = ?;", (item_id,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "PLAID_ITEM_NOT_FOUND", "message": f"No linked item {item_id!r}."},
        )
    try:
        return token_vault.decrypt(row[0])
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=500,
            detail={"code": "TOKEN_DECRYPT_FAILED", "message": str(exc)},
        )


@app.get("/health")
async def health():
    try:
        conn = _db()
        conn.execute("SELECT 1 FROM native_ledger LIMIT 1;").fetchone()
        conn.close()
        return {"status": "ok", "db": "ok"}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"database unavailable: {exc}")


@app.get("/v1/diagnostics/integrations")
async def diagnostics_integrations():
    """Authenticated health check for both payment integrations."""
    plaid = plaid_service.healthcheck()
    stripe_result = stripe_service.healthcheck()
    return {
        "ready": bool(plaid["ok"] and stripe_result["ok"]),
        "plaid": plaid,
        "stripe": stripe_result,
        "config": {
            "plaid_env": plaid_service.plaid_env(),
            "plaid_configured": plaid_service.is_configured(),
            "stripe_configured": stripe_service.is_configured(),
            "webhook_secret_set": bool(os.getenv("STRIPE_WEBHOOK_SECRET")),
            "api_key_enforced": bool(os.getenv("BLAZE_API_KEY")),
        },
    }


@app.post("/v1/plaid/link-token")
async def create_link_token(
    payload: LinkTokenRequest, x_api_key: str | None = Header(default=None)
):
    require_api_key(x_api_key)
    try:
        return plaid_service.create_link_token(payload.agent_job_id)
    except plaid_service.PlaidServiceError as exc:
        raise _plaid_http_error(exc)


@app.post("/v1/plaid/exchange")
async def exchange_public_token(
    payload: ExchangeRequest, x_api_key: str | None = Header(default=None)
):
    require_api_key(x_api_key)
    try:
        exchange = plaid_service.exchange_public_token(payload.public_token)
        return _store_item(exchange)
    except plaid_service.PlaidServiceError as exc:
        raise _plaid_http_error(exc)


@app.post("/v1/plaid/sandbox-public-token")
async def sandbox_public_token(
    institution_id: str = "ins_109508", x_api_key: str | None = Header(default=None)
):
    """Sandbox only: mint a public_token so the full flow is testable headlessly."""
    require_api_key(x_api_key)
    try:
        return {"public_token": plaid_service.create_sandbox_public_token(institution_id)}
    except plaid_service.PlaidServiceError as exc:
        raise _plaid_http_error(exc)


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


@app.get("/v1/plaid/items")
async def list_items(x_api_key: str | None = Header(default=None)):
    require_api_key(x_api_key)
    conn = _db()
    try:
        rows = conn.execute(
            "SELECT item_id, institution_name, account_mask, updated_at FROM plaid_items "
            "ORDER BY updated_at DESC;"
        ).fetchall()
        return {
            "items": [
                {
                    "item_id": r[0],
                    "institution_name": r[1],
                    "account_mask": r[2],
                    "updated_at": r[3],
                }
                for r in rows
            ]
        }
    finally:
        conn.close()


@app.post("/v1/payments/onboard-and-payout")
async def onboard_and_payout(
    payload: PlaidOnboardRequest,
    background_tasks: BackgroundTasks,
    x_api_key: str | None = Header(default=None),
):
    require_api_key(x_api_key)

    # Resolve a REAL Plaid access token. Either the caller supplies a fresh
    # public_token (exchanged here) or references a previously linked item_id.
    if payload.public_token:
        try:
            exchange = plaid_service.exchange_public_token(payload.public_token)
            item = _store_item(exchange)
        except plaid_service.PlaidServiceError as exc:
            raise _plaid_http_error(exc)
        item_id = item["item_id"]
    elif payload.item_id:
        item_id = payload.item_id
    else:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "MISSING_PLAID_REFERENCE",
                "message": "Provide either item_id or public_token.",
            },
        )

    access_token = _access_token_for(item_id)

    if os.getenv("CELERY_BROKER_URL"):
        task = dispatch_autonomous_payout.delay(
            agent_job_id=payload.agent_job_id,
            account_token=access_token,
            routing_token="rtg_stripe_rail_222",
            amount=payload.amount,
        )
        return {"status": "QUEUED", "mode": "celery", "background_task_id": task.id}

    background_tasks.add_task(
        dispatch_autonomous_payout,
        agent_job_id=payload.agent_job_id,
        account_token=access_token,
        routing_token="rtg_stripe_rail_222",
        amount=payload.amount,
    )
    return {
        "status": "QUEUED",
        "mode": "cloud_async",
        "agent_job_id": payload.agent_job_id,
        "item_id": item_id,
    }


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
            row = cursor.execute(
                "SELECT job_id, amount, status FROM track_ledger "
                "WHERE (bank_tx_id = ? OR job_id = ?) "
                "AND status IN ('PENDING_BANK_CLEARING', 'SETTLED');",
                (payout_id, agent_job_id),
            ).fetchone()

            if row is None:
                conn.rollback()
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
                "UPDATE track_ledger SET status = 'FINAL_SETTLED', "
                "bank_tx_id = COALESCE(bank_tx_id, ?) "
                "WHERE (bank_tx_id = ? OR job_id = ?) "
                "AND status IN ('SETTLED', 'PENDING_BANK_CLEARING');",
                (payout_id, payout_id, agent_job_id),
            )
            if cursor.rowcount == 0:
                conn.rollback()
                raise HTTPException(
                    status_code=503, detail="Transaction not yet recorded; retry."
                )
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
