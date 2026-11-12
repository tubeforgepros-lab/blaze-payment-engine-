import os
import sqlite3
import traceback

import stripe
from celery import Celery
from dotenv import load_dotenv

load_dotenv()

stripe.api_key = os.getenv("STRIPE_SECRET_KEY")

DB_PATH = os.getenv(
    "BLAZE_DB_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "blaze_ledgers.db"),
)

app = Celery(
    "money_engine",
    broker=os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/1"),
    backend=os.getenv("CELERY_RESULT_BACKEND", "redis://localhost:6379/2"),
)

# Optional Redis lock (fast path only). Correctness does NOT depend on it:
# the UNIQUE constraint on track_ledger.job_id is the real idempotency guard.
_redis_client = None


def get_redis():
    global _redis_client
    if _redis_client is None:
        if not os.getenv("REDIS_HOST"):
            return None
        import redis

        _redis_client = redis.Redis(
            host=os.getenv("REDIS_HOST"),
            port=int(os.getenv("REDIS_PORT", "6379")),
            password=os.getenv("REDIS_PASSWORD") or None,
            db=0,
            socket_connect_timeout=2,
        )
    return _redis_client


def _connect():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000;")
    return conn


def verify_external_plaid_balance(access_token: str, required_amount: float) -> bool:
    """Fail-closed pre-flight balance check against Plaid.

    Returns True only when Plaid confirms available balance >= required_amount.
    Set BLAZE_ALLOW_UNVERIFIED_PLAID=true ONLY for local sandbox testing.
    """
    if os.getenv("BLAZE_ALLOW_UNVERIFIED_PLAID", "").lower() == "true":
        print("[PLAID AUDIT] WARNING: verification bypassed (BLAZE_ALLOW_UNVERIFIED_PLAID).")
        return True

    client_id = os.getenv("PLAID_CLIENT_ID")
    secret = os.getenv("PLAID_SECRET")
    if not client_id or not secret:
        print("[PLAID AUDIT] FAIL: PLAID_CLIENT_ID / PLAID_SECRET not configured.")
        return False

    try:
        import plaid
        from plaid.api import plaid_api
        from plaid.api_client import ApiClient
        from plaid.configuration import Configuration
        from plaid.model.accounts_balance_get_request import AccountsBalanceGetRequest

        env = (os.getenv("PLAID_ENV", "sandbox") or "sandbox").lower()
        host = {
            "sandbox": plaid.Environment.Sandbox,
            "production": plaid.Environment.Production,
        }.get(env)
        if host is None:
            print(f"[PLAID AUDIT] FAIL: unsupported PLAID_ENV={env!r}.")
            return False

        api = plaid_api.PlaidApi(
            ApiClient(Configuration(host=host, api_key={"clientId": client_id, "secret": secret}))
        )
        resp = api.accounts_balance_get(AccountsBalanceGetRequest(access_token=access_token))
        available = sum(
            (a.balances.available or 0.0)
            for a in resp["accounts"]
            if (a.balances.iso_currency_code or "USD") == "USD"
        )
        ok = available >= required_amount
        print(
            f"[PLAID AUDIT] available=${available:,.2f} required=${required_amount:,.2f} "
            f"-> {'PASS' if ok else 'FAIL'}"
        )
        return ok
    except Exception as exc:  # noqa: BLE001 - any Plaid error must block the payout
        print(f"[PLAID AUDIT] FAIL: Plaid lookup error: {exc}")
        return False


def execute_dual_ledger_commit(job_id: str, amount: float) -> str:
    """Atomically reserve funds and record the pending payout.

    Returns "OK", "DUPLICATE" or "INSUFFICIENT".
    The INSERT happens first so the PRIMARY KEY enforces idempotency even when
    Redis is unavailable.
    """
    conn = _connect()
    try:
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE;")
        try:
            cursor.execute(
                "INSERT INTO track_ledger (job_id, amount, status) "
                "VALUES (?, ?, 'PENDING_BANK_CLEARING');",
                (job_id, amount),
            )
        except sqlite3.IntegrityError:
            conn.rollback()
            print(f"[LEDGER] Duplicate job_id rejected: {job_id}")
            return "DUPLICATE"

        row = cursor.execute(
            "SELECT balance FROM native_ledger WHERE account_id = 'pool_01';"
        ).fetchone()
        if not row or row[0] < amount:
            conn.rollback()
            print("[LEDGER ERROR] Insufficient internal operational pool allocation.")
            return "INSUFFICIENT"

        cursor.execute(
            "UPDATE native_ledger SET balance = balance - ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE account_id = 'pool_01';",
            (amount,),
        )
        conn.commit()
        return "OK"
    except Exception:
        conn.rollback()
        traceback.print_exc()
        return "INSUFFICIENT"
    finally:
        conn.close()


def rollback_dual_ledger(job_id: str, amount: float, reason: str) -> None:
    """Reverse a reserved payout. Idempotent: only credits if still PENDING."""
    conn = _connect()
    try:
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE;")
        cursor.execute(
            "UPDATE track_ledger SET status = ? "
            "WHERE job_id = ? AND status = 'PENDING_BANK_CLEARING';",
            (f"FAILED_ROLLED_BACK ({reason})", job_id),
        )
        if cursor.rowcount == 0:
            conn.rollback()
            print(f"[LEDGER] Rollback skipped, job {job_id} not in PENDING state.")
            return
        cursor.execute(
            "UPDATE native_ledger SET balance = balance + ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE account_id = 'pool_01';",
            (amount,),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        traceback.print_exc()
    finally:
        conn.close()


def update_ledger_settled_status(job_id: str, bank_tx_id: str) -> None:
    conn = _connect()
    try:
        conn.execute(
            "UPDATE track_ledger SET status = 'SETTLED', bank_tx_id = ? WHERE job_id = ?;",
            (bank_tx_id, job_id),
        )
        conn.commit()
    finally:
        conn.close()


@app.task(bind=True, max_retries=3, default_retry_delay=60)
def dispatch_autonomous_payout(self, agent_job_id: str, account_token: str, routing_token: str, amount: float):
    if amount is None or amount <= 0:
        return {"status": "REJECTED", "reason": "Amount must be greater than zero."}

    r = get_redis()
    lock_key = f"lock:payout:{agent_job_id}"
    if r is not None:
        try:
            if not r.set(lock_key, "PROCESSING", ex=86400, nx=True):
                return {"status": "REJECTED", "reason": "Duplicate agent execution token detected."}
        except Exception as exc:  # noqa: BLE001
            print(f"[REDIS] lock unavailable, continuing with DB idempotency: {exc}")
            r = None

    # Cheap early duplicate check so we never spend a Plaid call on a replay.
    # The UNIQUE constraint in execute_dual_ledger_commit still closes the race.
    conn = _connect()
    try:
        exists = conn.execute(
            "SELECT 1 FROM track_ledger WHERE job_id = ?;", (agent_job_id,)
        ).fetchone()
    finally:
        conn.close()
    if exists:
        if r is not None:
            r.delete(lock_key)
        return {"status": "REJECTED", "reason": "Duplicate job_id."}

    if not verify_external_plaid_balance(account_token, amount):
        if r is not None:
            r.delete(lock_key)
        return {"status": "FAILED", "reason": "Pre-flight verification failed."}

    claim = execute_dual_ledger_commit(agent_job_id, amount)
    if claim == "DUPLICATE":
        if r is not None:
            r.delete(lock_key)
        return {"status": "REJECTED", "reason": "Duplicate job_id."}
    if claim != "OK":
        if r is not None:
            r.delete(lock_key)
        return {"status": "FAILED", "reason": "Ledger adjustment transaction rejected."}

    try:
        print(f"[BANK DISPATCH] Executing Stripe payout of ${amount:.2f} for Job {agent_job_id}...")
        payout = stripe.Payout.create(
            amount=int(round(amount * 100)),
            currency="usd",
            metadata={"agent_job_id": agent_job_id},
            idempotency_key=f"payout-{agent_job_id}",
        )
        update_ledger_settled_status(agent_job_id, payout.id)
        if r is not None:
            r.set(lock_key, "COMPLETED", ex=86400)
        return {"status": "SETTLED", "transaction_id": payout.id}
    except Exception as exc:  # noqa: BLE001
        err_msg = getattr(exc, "user_message", None) or str(exc)
        print(f"[STRIPE DISPATCH FAILED] Rolling back ledger: {err_msg}")
        rollback_dual_ledger(agent_job_id, amount, err_msg)
        if r is not None:
            r.set(lock_key, "FAILED", ex=3600)
        return {"status": "FAILED", "reason": err_msg}
