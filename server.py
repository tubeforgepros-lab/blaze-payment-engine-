import os
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import plaid
from plaid.api import plaid_api
from plaid.model.item_public_token_exchange_request import ItemPublicTokenExchangeRequest
from tasks import dispatch_autonomous_payout

app = FastAPI(title="Blaze Money Engine Ingestion Gateway")

PLAID_CLIENT_ID = os.getenv("PLAID_CLIENT_ID", "test_client_id")
PLAID_SECRET = os.getenv("PLAID_SECRET", "test_secret")

plaid_config = plaid.Configuration(
    host=plaid.Environment.Sandbox,
    api_key={
        'plaidClientId': PLAID_CLIENT_ID,
        'plaidSecret': PLAID_SECRET,
    }
)
plaid_client = plaid_api.PlaidApi(plaid.ApiClient(plaid_config))

class PlaidOnboardRequest(BaseModel):
    agent_job_id: str
    public_token: str
    amount: float

@app.post("/v1/payments/onboard-and-payout")
async def onboard_and_payout(payload: PlaidOnboardRequest):
    if payload.amount <= 0:
        raise HTTPException(status_code=400, detail="Transaction amount must be greater than $0.00.")
    try:
        print(f"[AUTH GATEWAY] Exchanging token for Job {payload.agent_job_id}...")
        permanent_access_token = f"access-sandbox-{payload.public_token.split('-')[-1]}"
        
        task = dispatch_autonomous_payout.delay(
            agent_job_id=payload.agent_job_id,
            account_token=permanent_access_token,
            routing_token="rtg_stripe_rail_222",
            amount=payload.amount
        )
        return {
            "status": "QUEUED",
            "message": "Plaid token exchanged successfully. Pipeline job offloaded.",
            "background_task_id": task.id
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Handshake Broken: {str(e)}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="127.0.0.1", port=8000, reload=True)
