import os
from fastapi import FastAPI, HTTPException, BackgroundTasks
from pydantic import BaseModel
from tasks import dispatch_autonomous_payout

app = FastAPI(title="Blaze Money Engine Ingestion Gateway")

class PlaidOnboardRequest(BaseModel):
    agent_job_id: str
    public_token: str
    amount: float

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.post("/v1/payments/onboard-and-payout")
async def onboard_and_payout(payload: PlaidOnboardRequest, background_tasks: BackgroundTasks):
    if payload.amount <= 0:
        raise HTTPException(status_code=400, detail="Transaction amount must be greater than $0.00.")
    try:
        permanent_access_token = f"access-sandbox-{payload.public_token.split("-")[-1]}"
        if os.getenv("CELERY_BROKER_URL"):
            task = dispatch_autonomous_payout.delay(
                agent_job_id=payload.agent_job_id,
                account_token=permanent_access_token,
                routing_token="rtg_stripe_rail_222",
                amount=payload.amount
            )
            return {"status": "QUEUED", "mode": "celery", "background_task_id": task.id}
        else:
            background_tasks.add_task(
                dispatch_autonomous_payout,
                agent_job_id=payload.agent_job_id,
                account_token=permanent_access_token,
                routing_token="rtg_stripe_rail_222",
                amount=payload.amount
            )
            return {"status": "QUEUED", "mode": "cloud_async", "agent_job_id": payload.agent_job_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Handshake Broken: {str(e)}")
