import os, redis, traceback, plaid
from dotenv import load_dotenv
from plaid.api import plaid_api
from plaid.model.transfer_authorization_create_request import TransferAuthorizationCreateRequest
from plaid.model.transfer_create_request import TransferCreateRequest
from plaid.model.transfer_type import TransferType
from plaid.model.transfer_network import TransferNetwork
from plaid.model.ach_class import ACHClass
from plaid.model.transfer_authorization_user_in_request import TransferAuthorizationUserInRequest
from celery import Celery
from db import get_conn, q

load_dotenv()

plaid_env = plaid.Environment.Production if os.getenv("PLAID_ENV") == "production" else plaid.Environment.Sandbox
plaid_config = plaid.Configuration(
    host=plaid_env,
    api_key={"clientId": os.getenv("PLAID_CLIENT_ID"), "secret": os.getenv("PLAID_SECRET")}
)
plaid_client = plaid_api.PlaidApi(plaid.ApiClient(plaid_config))

redis_client = redis.Redis(host=os.getenv("REDIS_HOST", "localhost"), port=6379, db=0)
app = Celery(
    "money_engine",
    broker=os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/1"),
    backend=os.getenv("CELERY_RESULT_BACKEND", "redis://localhost:6379/2")
)

def execute_dual_ledger_commit(job_id: str, amount: float) -> bool:
    try:
        conn = get_conn()
        cursor = conn.cursor()
        cursor.execute(q("BEGIN TRANSACTION;"))
        cursor.execute(q("SELECT balance FROM native_ledger WHERE account_id = 'pool_01';"))
        row = cursor.fetchone()
        if not row or row < amount:
            conn.close()
            return False
        cursor.execute(q("UPDATE native_ledger SET balance = balance - ?, updated_at = CURRENT_TIMESTAMP WHERE account_id = 'pool_01';"), (amount,))
        cursor.execute(q("INSERT INTO track_ledger (job_id, amount, status) VALUES (?, ?, 'PENDING_PLAID_CLEARING');"), (job_id, amount))
        conn.commit()
        conn.close()
        return True
    except Exception:
        traceback.print_exc()
        return False

def rollback_dual_ledger(job_id: str, amount: float, reason: str):
    conn = get_conn()
    cursor = conn.cursor()
    try:
        cursor.execute(q("BEGIN TRANSACTION;"))
        cursor.execute(q("UPDATE native_ledger SET balance = balance + ?, updated_at = CURRENT_TIMESTAMP WHERE account_id = 'pool_01';"), (amount,))
        cursor.execute(q("UPDATE track_ledger SET status = ? WHERE job_id = ?;"), (f"FAILED_ROLLED_BACK ({reason[:180]})", job_id))
        conn.commit()
    finally:
        conn.close()

def update_ledger_settled_status(job_id: str, bank_tx_id: str):
    conn = get_conn()
    cursor = conn.cursor()
    try:
        cursor.execute(q("UPDATE track_ledger SET status = 'SETTLED', job_id = ? WHERE job_id = ?;"), (f"{job_id} ({bank_tx_id})", job_id))
        conn.commit()
    finally:
        conn.close()

@app.task(bind=True, max_retries=3, default_retry_delay=60)
def dispatch_autonomous_payout(self, agent_job_id: str, account_token: str, routing_token: str, amount: float):
    lock_key = f"lock:payout:{agent_job_id}"
    if os.getenv("REDIS_HOST") and not redis_client.set(lock_key, "PROCESSING", ex=86400, nx=True):
        return {"status": "REJECTED", "reason": "Duplicate agent execution token detected."}

    if not execute_dual_ledger_commit(agent_job_id, amount):
        if os.getenv("REDIS_HOST"): redis_client.delete(lock_key)
        return {"status": "FAILED", "reason": "Ledger adjustment transaction rejected."}

    try:
        access_token = os.getenv("PLAID_ACCESS_TOKEN", account_token)
        account_id = os.getenv("PLAID_ACCOUNT_ID", routing_token)
        amt_str = f"{amount:.2f}"

        auth_req = TransferAuthorizationCreateRequest(
            access_token=access_token,
            account_id=account_id,
            type=TransferType("credit"),
            network=TransferNetwork("ach"),
            amount=amt_str,
            ach_class=ACHClass("ppd"),
            user=TransferAuthorizationUserInRequest(legal_name=os.getenv("PLAID_LEGAL_NAME", "TubeForge Digital"))
        )
        auth_res = plaid_client.transfer_authorization_create(auth_req).to_dict()
        auth_id = auth_res["authorization"]["id"]
        decision = auth_res["authorization"]["decision"]
        if decision != "approved":
            raise RuntimeError(f"Plaid authorization {decision}: {auth_res['authorization'].get('decision_rationale')}")

        tx_req = TransferCreateRequest(
            access_token=access_token,
            account_id=account_id,
            authorization_id=auth_id,
            description="BlazePay"
        )
        tx_res = plaid_client.transfer_create(tx_req).to_dict()
        bank_tx_id = tx_res["transfer"]["id"]

        update_ledger_settled_status(agent_job_id, bank_tx_id)
        if os.getenv("REDIS_HOST"): redis_client.set(lock_key, "COMPLETED", ex=86400)
        return {"status": "SETTLED", "transaction_id": bank_tx_id}
    except Exception as exc:
        err_msg = getattr(exc, "body", None) or str(exc)
        rollback_dual_ledger(agent_job_id, amount, str(err_msg))
        if os.getenv("REDIS_HOST"): redis_client.set(lock_key, "FAILED", ex=3600)
        return {"status": "FAILED", "reason": str(err_msg)}
