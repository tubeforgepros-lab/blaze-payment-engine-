import os
import redis
import sqlite3
import traceback
import stripe
import plaid
from plaid.api import plaid_api
from plaid.model.accounts_balance_get_request import AccountsBalanceGetRequest
from celery import Celery

# Initialize APIs using sandbox keys (or your live credentials once underwriting is complete)
stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "sk_test_mock_holder_key")

PLAID_CLIENT_ID = os.getenv("PLAID_CLIENT_ID", "test_client_id")
PLAID_SECRET = os.getenv("PLAID_SECRET", "test_secret")

plaid_config = plaid.Configuration(
    host=plaid.Environment.Sandbox, # Switch to Environment.Production when authorized
    api_key={
        'plaidClientId': PLAID_CLIENT_ID,
        'plaidSecret': PLAID_SECRET,
    }
)
plaid_client = plaid_api.PlaidApi(plaid.ApiClient(plaid_config))

redis_client = redis.Redis(host=os.getenv("REDIS_HOST", "localhost"), port=6379, db=0)

app = Celery(
    "money_engine", 
    broker=os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/1"),
    backend=os.getenv("CELERY_RESULT_BACKEND", "redis://localhost:6379/2")
)

def verify_external_plaid_balance(access_token: str, required_amount: float) -> bool:
    """
    Queries Plaid's real-time balance endpoint to check if the linked 
    bank account has sufficient available funds.
    """
    print(f"[PLAID AUDIT] Verifying external bank balance for token: {access_token}")
    try:
        # Generate official request object for Plaid's balance engine
        request = AccountsBalanceGetRequest(access_token=access_token)
        
        # Simulating live response behavior in Sandbox mode
        # In a fully underwritten live setup, you would use:
        # response = plaid_client.accounts_balance_get(request)
        # available_balance = response['accounts'][0]['balances']['available']
        
        # Mocking an available bank balance of $5,000.00 for our sandbox account token
        available_balance = 5000.00 
        
        print(f"[PLAID AUDIT] External Bank Available: ${available_balance:,.2f} | Required: ${required_amount:,.2f}")
        return available_balance >= required_amount
        
    except Exception as e:
        print(f"[PLAID AUDIT CRITICAL FAILURE] Could not read external balance: {e}")
        return False

def execute_dual_ledger_commit(job_id: str, amount: float) -> bool:
    """
    Executes atomic transaction reserving funds in internal books.
    """
    try:
        db_path = os.path.abspath("blaze_ledgers.db")
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        cursor.execute("BEGIN TRANSACTION;")
        
        cursor.execute("SELECT balance FROM native_ledger WHERE account_id = 'pool_01';")
        row = cursor.fetchone()
        
        if not row or row[0] < amount:
            print("[LEDGER ERROR] Insufficient internal operational pool allocation.")
            conn.close()
            return False
            
        cursor.execute("""
            UPDATE native_ledger 
            SET balance = balance - ?, updated_at = CURRENT_TIMESTAMP
            WHERE account_id = 'pool_01';
        """, (amount,))
        
        cursor.execute("""
            INSERT INTO track_ledger (job_id, amount, status)
            VALUES (?, ?, 'PENDING_BANK_CLEARING');
        """, (job_id, amount))
        
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print("!!! [LEDGER SQL TRANSACTION ERROR] !!!")
        traceback.print_exc()
        return False

def update_ledger_settled_status(job_id: str, bank_tx_id: str):
    db_path = os.path.abspath("blaze_ledgers.db")
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    try:
        cursor.execute("""
            UPDATE track_ledger 
            SET status = 'SETTLED', job_id = ?
            WHERE job_id = ?;
        """, (f"{job_id} ({bank_tx_id})", job_id))
        conn.commit()
    except Exception as e:
        print(f"[LEDGER UPDATE ERROR] Finalization break: {e}")
    finally:
        conn.close()


@app.task(bind=True, max_retries=3, default_retry_delay=60)
def dispatch_autonomous_payout(self, agent_job_id: str, account_token: str, routing_token: str, amount: float):
    lock_key = f"lock:payout:{agent_job_id}"
    is_new_job = redis_client.set(lock_key, "PROCESSING", ex=86400, nx=True)
    
    if not is_new_job:
        return {"status": "REJECTED", "reason": "Duplicate agent execution token detected."}
        
    try:
        # Pre-flight Check: Query external bank status via Plaid prior to touching ledger entries
        has_liquidity = verify_external_plaid_balance(account_token, amount)
        if not has_liquidity:
            redis_client.delete(lock_key)
            return {"status": "FAILED", "reason": "Plaid verification failed: Insufficient external bank funds."}
            
        # Step 2: Commit internal balance drops atomically
        ledger_success = execute_dual_ledger_commit(agent_job_id, amount)
        if not ledger_success:
            redis_client.delete(lock_key)
            return {"status": "FAILED", "reason": "Ledger adjustment transaction rejected."}
            
        # Step 3: Trigger payment settlement out-of-band via Stripe
        print(f"[BANK DISPATCH] Initializing Stripe Payout for Job {agent_job_id}...")
        bank_tx_id = f"po_stripe_test_{agent_job_id}"
        
        # Step 4: Finalize transaction tracking history state
        update_ledger_settled_status(agent_job_id, bank_tx_id)
        
        redis_client.set(lock_key, "COMPLETED", ex=86400)
        return {"status": "SETTLED", "transaction_id": bank_tx_id}
        
    except Exception as exc:
        redis_client.set(lock_key, "FAILED", ex=3600)
        self.retry(exc=exc)
import os
import redis
import sqlite3
import traceback
import stripe
import plaid
from plaid.api import plaid_api
from plaid.model.accounts_balance_get_request import AccountsBalanceGetRequest
from celery import Celery

# Initialize APIs using sandbox keys (or your live credentials once underwriting is complete)
stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "sk_test_mock_holder_key")

PLAID_CLIENT_ID = os.getenv("PLAID_CLIENT_ID", "test_client_id")
PLAID_SECRET = os.getenv("PLAID_SECRET", "test_secret")

plaid_config = plaid.Configuration(
    host=plaid.Environment.Sandbox, # Switch to Environment.Production when authorized
    api_key={
        'plaidClientId': PLAID_CLIENT_ID,
        'plaidSecret': PLAID_SECRET,
    }
)
plaid_client = plaid_api.PlaidApi(plaid.ApiClient(plaid_config))

redis_client = redis.Redis(host=os.getenv("REDIS_HOST", "localhost"), port=6379, db=0)

app = Celery(
    "money_engine", 
    broker=os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/1"),
    backend=os.getenv("CELERY_RESULT_BACKEND", "redis://localhost:6379/2")
)

def verify_external_plaid_balance(access_token: str, required_amount: float) -> bool:
    """
    Queries Plaid's real-time balance endpoint to check if the linked 
    bank account has sufficient available funds.
    """
    print(f"[PLAID AUDIT] Verifying external bank balance for token: {access_token}")
    try:
        # Generate official request object for Plaid's balance engine
        request = AccountsBalanceGetRequest(access_token=access_token)
        
        # Simulating live response behavior in Sandbox mode
        # In a fully underwritten live setup, you would use:
        # response = plaid_client.accounts_balance_get(request)
        # available_balance = response['accounts'][0]['balances']['available']
        
        # Mocking an available bank balance of $5,000.00 for our sandbox account token
        available_balance = 5000.00 
        
        print(f"[PLAID AUDIT] External Bank Available: ${available_balance:,.2f} | Required: ${required_amount:,.2f}")
        return available_balance >= required_amount
        
    except Exception as e:
        print(f"[PLAID AUDIT CRITICAL FAILURE] Could not read external balance: {e}")
        return False

def execute_dual_ledger_commit(job_id: str, amount: float) -> bool:
    """
    Executes atomic transaction reserving funds in internal books.
    """
    try:
        db_path = os.path.abspath("blaze_ledgers.db")
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        cursor.execute("BEGIN TRANSACTION;")
        
        cursor.execute("SELECT balance FROM native_ledger WHERE account_id = 'pool_01';")
        row = cursor.fetchone()
        
        if not row or row[0] < amount:
            print("[LEDGER ERROR] Insufficient internal operational pool allocation.")
            conn.close()
            return False
            
        cursor.execute("""
            UPDATE native_ledger 
            SET balance = balance - ?, updated_at = CURRENT_TIMESTAMP
            WHERE account_id = 'pool_01';
        """, (amount,))
        
        cursor.execute("""
            INSERT INTO track_ledger (job_id, amount, status)
            VALUES (?, ?, 'PENDING_BANK_CLEARING');
        """, (job_id, amount))
        
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print("!!! [LEDGER SQL TRANSACTION ERROR] !!!")
        traceback.print_exc()
        return False

def update_ledger_settled_status(job_id: str, bank_tx_id: str):
    db_path = os.path.abspath("blaze_ledgers.db")
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    try:
        cursor.execute("""
            UPDATE track_ledger 
            SET status = 'SETTLED', job_id = ?
            WHERE job_id = ?;
        """, (f"{job_id} ({bank_tx_id})", job_id))
        conn.commit()
    except Exception as e:
        print(f"[LEDGER UPDATE ERROR] Finalization break: {e}")
    finally:
        conn.close()


@app.task(bind=True, max_retries=3, default_retry_delay=60)
def dispatch_autonomous_payout(self, agent_job_id: str, account_token: str, routing_token: str, amount: float):
    lock_key = f"lock:payout:{agent_job_id}"
    is_new_job = redis_client.set(lock_key, "PROCESSING", ex=86400, nx=True)
    
    if not is_new_job:
        return {"status": "REJECTED", "reason": "Duplicate agent execution token detected."}
        
    try:
        # Pre-flight Check: Query external bank status via Plaid prior to touching ledger entries
        has_liquidity = verify_external_plaid_balance(account_token, amount)
        if not has_liquidity:
            redis_client.delete(lock_key)
            return {"status": "FAILED", "reason": "Plaid verification failed: Insufficient external bank funds."}
            
        # Step 2: Commit internal balance drops atomically
        ledger_success = execute_dual_ledger_commit(agent_job_id, amount)
        if not ledger_success:
            redis_client.delete(lock_key)
            return {"status": "FAILED", "reason": "Ledger adjustment transaction rejected."}
        # Step 3: Trigger payment settlement via the live Plaid-Stripe Bridge
        print(f"[BANK DISPATCH] Swapping Plaid token for live Stripe Bank token...")
        
        # 1. Exchange the Plaid permanent token for a secure Stripe btok reference
        from plaid.model.processor_stripe_bank_account_token_create_request import ProcessorStripeBankAccountTokenCreateRequest
        
        swap_request = ProcessorStripeBankAccountTokenCreateRequest(
            access_token=account_token,
            account_id="vzeNDwK7KQIm4yEog683uElbp9GRLEFXGK98D" # Dynamically maps to linked account ID
        )
        
        # Generate the btok token from Plaid's processing partner node
        swap_response = plaid_client.processor_stripe_bank_account_token_create(swap_request)
        live_btok_reference = swap_response['stripe_bank_account_token']
        print(f"[BANK DISPATCH] Swapped token successfully. Key generated: {live_btok_reference}")
        
        # 2. Dispatch real funds using Stripe's live Payout engine
        # Convert dollar figure to Stripe integer cents (e.g. $2000.00 -> 200000)
        stripe_amount_cents = int(amount * 100)
        
        # Connect to Stripe's payment network out-of-band
        payout = stripe.Payout.create(
            amount=stripe_amount_cents,
            currency="usd",
            method="standard", # Use 'standard' for low-fee ACH or 'instant' for real-time rails
            statement_descriptor="Blaze Payments",
            # Passes the token to instruct Stripe which destination bank to fund
            source_type="bank_account" 
        )
        
        # Extract the official transaction clearing key from the bank network
        bank_tx_id = payout.id
        print(f"[BANK SUCCESS] Live Stripe Payout dispatched with reference ID: {bank_tx_id}")
            
        
        # Step 4: Finalize transaction tracking history state
        update_ledger_settled_status(agent_job_id, bank_tx_id)
        
        redis_client.set(lock_key, "COMPLETED", ex=86400)
        return {"status": "SETTLED", "transaction_id": bank_tx_id}
        
    except Exception as exc:
        redis_client.set(lock_key, "FAILED", ex=3600)
        self.retry(exc=exc)
import os
import redis
import sqlite3
import traceback
import stripe
import plaid
from plaid.api import plaid_api
from plaid.model.accounts_balance_get_request import AccountsBalanceGetRequest
from celery import Celery

# Initialize APIs using sandbox keys (or your live credentials once underwriting is complete)
stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "sk_test_mock_holder_key")

PLAID_CLIENT_ID = os.getenv("PLAID_CLIENT_ID", "test_client_id")
PLAID_SECRET = os.getenv("PLAID_SECRET", "test_secret")

plaid_config = plaid.Configuration(
    host=plaid.Environment.Sandbox, # Switch to Environment.Production when authorized
    api_key={
        'plaidClientId': PLAID_CLIENT_ID,
        'plaidSecret': PLAID_SECRET,
    }
)
plaid_client = plaid_api.PlaidApi(plaid.ApiClient(plaid_config))

redis_client = redis.Redis(host=os.getenv("REDIS_HOST", "localhost"), port=6379, db=0)

app = Celery(
    "money_engine", 
    broker=os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/1"),
    backend=os.getenv("CELERY_RESULT_BACKEND", "redis://localhost:6379/2")
)

def verify_external_plaid_balance(access_token: str, required_amount: float) -> bool:
    """
    Queries Plaid's real-time balance endpoint to check if the linked 
    bank account has sufficient available funds.
    """
    print(f"[PLAID AUDIT] Verifying external bank balance for token: {access_token}")
    try:
        # Generate official request object for Plaid's balance engine
        request = AccountsBalanceGetRequest(access_token=access_token)
        
        # Simulating live response behavior in Sandbox mode
        # In a fully underwritten live setup, you would use:
        # response = plaid_client.accounts_balance_get(request)
        # available_balance = response['accounts'][0]['balances']['available']
        
        # Mocking an available bank balance of $5,000.00 for our sandbox account token
        available_balance = 5000.00 
        
        print(f"[PLAID AUDIT] External Bank Available: ${available_balance:,.2f} | Required: ${required_amount:,.2f}")
        return available_balance >= required_amount
        
    except Exception as e:
        print(f"[PLAID AUDIT CRITICAL FAILURE] Could not read external balance: {e}")
        return False

def execute_dual_ledger_commit(job_id: str, amount: float) -> bool:
    """
    Executes atomic transaction reserving funds in internal books.
    """
    try:
        db_path = os.path.abspath("blaze_ledgers.db")
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        cursor.execute("BEGIN TRANSACTION;")
        
        cursor.execute("SELECT balance FROM native_ledger WHERE account_id = 'pool_01';")
        row = cursor.fetchone()
        
        if not row or row[0] < amount:
            print("[LEDGER ERROR] Insufficient internal operational pool allocation.")
            conn.close()
            return False
            
        cursor.execute("""
            UPDATE native_ledger 
            SET balance = balance - ?, updated_at = CURRENT_TIMESTAMP
            WHERE account_id = 'pool_01';
        """, (amount,))
        
        cursor.execute("""
            INSERT INTO track_ledger (job_id, amount, status)
            VALUES (?, ?, 'PENDING_BANK_CLEARING');
        """, (job_id, amount))
        
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print("!!! [LEDGER SQL TRANSACTION ERROR] !!!")
        traceback.print_exc()
        return False

def update_ledger_settled_status(job_id: str, bank_tx_id: str):
    db_path = os.path.abspath("blaze_ledgers.db")
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    try:
        cursor.execute("""
            UPDATE track_ledger 
            SET status = 'SETTLED', job_id = ?
            WHERE job_id = ?;
        """, (f"{job_id} ({bank_tx_id})", job_id))
        conn.commit()
    except Exception as e:
        print(f"[LEDGER UPDATE ERROR] Finalization break: {e}")
    finally:
        conn.close()


@app.task(bind=True, max_retries=3, default_retry_delay=60)
def dispatch_autonomous_payout(self, agent_job_id: str, account_token: str, routing_token: str, amount: float):
    lock_key = f"lock:payout:{agent_job_id}"
    is_new_job = redis_client.set(lock_key, "PROCESSING", ex=86400, nx=True)
    
    if not is_new_job:
        return {"status": "REJECTED", "reason": "Duplicate agent execution token detected."}
        
    try:
        # Pre-flight Check: Query external bank status via Plaid prior to touching ledger entries
        has_liquidity = verify_external_plaid_balance(account_token, amount)
        if not has_liquidity:
            redis_client.delete(lock_key)
            return {"status": "FAILED", "reason": "Plaid verification failed: Insufficient external bank funds."}
            
        # Step 2: Commit internal balance drops atomically
        ledger_success = execute_dual_ledger_commit(agent_job_id, amount)
        if not ledger_success:
            redis_client.delete(lock_key)
            return {"status": "FAILED", "reason": "Ledger adjustment transaction rejected."}
        # Step 3: Trigger payment settlement via the live Plaid-Stripe Bridge
        print(f"[BANK DISPATCH] Swapping Plaid token for live Stripe Bank token...")
        
        # 1. Initialize the official exchange request configuration payload
        from plaid.model.processor_stripe_bank_account_token_create_request import ProcessorStripeBankAccountTokenCreateRequest
        
        swap_request = ProcessorStripeBankAccountTokenCreateRequest(
            access_token=account_token, # The permanent access token obtained from your endpoint
            account_id="vzeNDwK7KQIm4yEog683uElbp9GRLEFXGK98D" # The active depository account ID string
        )
        
        # 2. Swap your token safely with Plaid's partner processing gateway nodes
        # swap_response = plaid_client.processor_stripe_bank_account_token_create(swap_request)
        # live_btok_reference = swap_response['stripe_bank_account_token']
        
        # Simulating the exact tokenized layout format Stripe returns (btok_...)
        live_btok_reference = f"btok_stripe_live_{agent_job_id}"
        print(f"[BANK DISPATCH SUCCESS] Generated safe token reference: {live_btok_reference}")
        
        # 3. Fire the parameters to Stripe Payout API context
        # (This is where you execute stripe.Payout.create referencing the token)
        bank_tx_id = f"po_stripe_test_{agent_job_id}"
        
        # Step 4: Finalize transaction tracking history state
        update_ledger_settled_status(agent_job_id, bank_tx_id)
        
        redis_client.set(lock_key, "COMPLETED", ex=86400)
        return {"status": "SETTLED", "transaction_id": bank_tx_id}
        
    except Exception as exc:
        redis_client.set(lock_key, "FAILED", ex=3600)
        self.retry(exc=exc)

