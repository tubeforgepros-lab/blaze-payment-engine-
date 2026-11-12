# Deployment Readiness Review — blaze-payment-engine

**Repository:** `tubeforgepros-lab/blaze-payment-engine-`
**Branch reviewed:** `main` @ `0a3dc1a`
**Review date:** 2026-11-12
**Scope:** final pre-deployment verification of the API gateway, ledger logic, and hosting config.

---

## Verdict

| Target | Status | Notes |
|---|---|---|
| **Sandbox / staging deploy** | ✅ **GO** | All code-level blockers fixed and verified locally. |
| **Live money movement (production payouts)** | ⛔ **NO-GO** | Four unresolved business/compliance blockers remain — see §5. They cannot be fixed in code alone. |

The application is now technically deployable and no longer corrupts its own ledger. It is **not** safe to point at a live Stripe account until §5 is closed.

---

## 1. Critical defects found (and fixed)

### 1.1 Duplicate route definitions — `server.py`
Commit `0a3dc1a` ("Add cloud ledger audit and webhook routes") appended a second copy of both endpoints instead of editing the existing ones. At runtime FastAPI registered **four** handlers for two paths:

```
POST /v1/payments/onboard-and-payout
POST /v1/webhooks/stripe
POST /v1/payments/onboard-and-payout   <-- duplicate
POST /v1/webhooks/stripe               <-- duplicate
```

The shadow copy was the *worse* implementation (no rollback on error, no signature-missing check). Only the first-registered handler ever executed, so the file was silently misleading and one edit away from swapping in the unsafe version.
**Fix:** single canonical definition of each route; duplicate block removed. Guarded by `test_single_route_definitions`.

### 1.2 Webhook replay created money from nothing — **severity: critical**
Stripe delivers webhooks **at least once** and retries on any non-2xx. The original handler had no idempotency key, so every redelivery of `payout.failed` credited the pool again. Reproduced against the original code:

```
send payout.failed (po_123, $10.00)  ->  {"status":"REVERSED", "refunded_to_pool":10.0}
send payout.failed (po_123, $10.00)  ->  {"status":"REVERSED", "refunded_to_pool":10.0}
pool balance: 100000.00 -> 100020.00   (a $10 payout that was never debited)
```

**Fix:** new `webhook_events` table; the event id is claimed with `INSERT OR IGNORE` inside the same transaction as the ledger mutation. Replays return `DUPLICATE_IGNORED` and touch nothing.

### 1.3 `LIKE '%'` wildcard settled the entire ledger — **severity: critical**
Webhook reconciliation matched with `WHERE job_id LIKE '%' || payout_id || '%'`. A payout object without an `id` produced `LIKE '%%'`, which matches **every row**. Reproduced: a single malformed `payout.paid` event flipped all transactions to `FINAL_SETTLED`.
**Fix:** exact matching on a dedicated `bank_tx_id` column (plus `metadata.agent_job_id` fallback for ordering races). Empty ids are rejected with HTTP 400.

### 1.4 Refund amount trusted the webhook payload
The reversal credited `obj["amount"]` from the request body rather than the amount actually reserved in the ledger. A forged or mismatched event could refund more than was ever debited.
**Fix:** the credit is read from the stored `track_ledger.amount` row. Covered by `test_reversal_uses_stored_amount_not_payload`.

### 1.5 `construct_event` returns a StripeObject, not a dict — **severity: high**
`stripe.Webhook.construct_event()` returns `stripe.Event`, which has no `.get()`. Every *validly signed* webhook raised `AttributeError` → HTTP 500 → Stripe retried forever. This only manifests once `STRIPE_WEBHOOK_SECRET` is set, i.e. exactly in production.
**Fix:** normalise via `.to_dict()`. Covered by `test_signed_webhook_with_stripe_object`.

### 1.6 Non-atomic ledger commit
`execute_dual_ledger_commit` checked the balance, then debited, then inserted the tracking row. A crash between steps left the pool debited with no record. Idempotency also depended entirely on Redis being configured.
**Fix:** `BEGIN IMMEDIATE`; insert the tracking row **first** so the `PRIMARY KEY` on `job_id` is the idempotency guard, then check and debit. Redis is now an optional fast path only. Rollback is conditional on the row still being `PENDING_BANK_CLEARING`, making it idempotent too.

### 1.7 Fake balance verification
`verify_external_plaid_balance()` was a stub that printed "pre-flight check passed" and unconditionally returned `True`. Every payout proceeded regardless of funding.
**Fix:** real `accounts/balance/get` call via `plaid-python`, summing available USD. **Fail-closed**: missing credentials, unsupported `PLAID_ENV`, or any Plaid error blocks the payout. A `BLAZE_ALLOW_UNVERIFIED_PLAID=true` escape hatch exists for local sandbox testing only and is loudly logged.

---

## 2. Deployment configuration defects (and fixes)

| File | Defect | Impact | Fix |
|---|---|---|---|
| `render.yaml` | `buildCommand: pip install -r backend/requirements.txt`, `startCommand: uvicorn backend.main:app` | **Build fails immediately** — no `backend/` directory exists | Points at root `requirements.txt` / `server:app` |
| `render.yaml` | No `healthCheckPath`, no persistent disk | Render can't gate rollout; SQLite wiped on every deploy | `/health` + 1 GB disk at `/var/data`, `BLAZE_DB_PATH` set |
| `Dockerfile` | Ran as root, no healthcheck, DB inside image layer | Data loss on redeploy; no container health signal | Non-root `blaze` (uid 10001), `HEALTHCHECK` on `/health`, `BLAZE_DB_PATH=/data` |
| `Dockerfile` | `RUN python init_ledgers.py` at build time | Seeded a throwaway DB into the image | Schema created/migrated at app startup instead |
| `index.html` | `<script src="https://plaid.com">`, `<script src="https://jsdelivr.net">` | Both are invalid URLs (no path); neither loads | Removed; axios pinned to a real jsDelivr path |
| `index.html` | Uses `axios` but never loads it | `ReferenceError` on payout click | axios CDN added |
| `index.html` | `RENDER_GATEWAY_URL = "https://onrender.com"` | Every request goes to a placeholder domain | Same-origin default, overridable via `window.BLAZE_API_BASE` |
| `index.html` | Balance hardcoded `$87,999.00`, decremented client-side | UI lied about real liquidity | Reads `/v1/ledger/balances` |
| `requirements.txt` | Fully unpinned | Non-reproducible builds; silent breaking upgrades | Pinned to verified versions |
| — | No `.env.example`, no CI | Config drift; no regression gate | Both added |

---

## 3. Verification performed

**Static**
- `compileall` over the whole tree: clean.
- `render.yaml` parsed and structurally validated (runtime, start command, health path, disk mount, 13 env vars).
- Runtime route table inspected: exactly 4 paths, no duplicates.

**Automated — 12/12 passing** (`pytest tests -q`)

```
test_health
test_single_route_definitions
test_rejects_non_positive_amount
test_webhook_requires_payout_id
test_duplicate_webhook_does_not_double_credit
test_reversal_uses_stored_amount_not_payload
test_unknown_payout_never_credits
test_paid_does_not_settle_unrelated_rows
test_signed_webhook_with_stripe_object
test_duplicate_job_id_rejected
test_failed_stripe_call_rolls_back
test_insufficient_funds_blocks_payout
```

**Live end-to-end against a running uvicorn instance, with real HMAC signature verification:**

```
seeded balance: 990.0
unsigned:            400  Missing Stripe-Signature header
signed #1:           200  REVERSED, refunded_to_pool: 10.0
signed #2 (replay):  200  DUPLICATE_IGNORED
bad signature:       400  No signatures found matching the expected signature
final balance:     1000.0   <- exactly one reversal, no money created
```

**Not verified:** `docker build` — no Docker daemon in this environment. The Dockerfile was reviewed by inspection and mirrors the Render configuration that *was* exercised. Run `docker build -t blaze . && docker run -p 8000:8000 blaze` before the first container deploy.

---

## 4. Behaviour changes to be aware of

1. **Payouts now fail without Plaid credentials.** Previously every payout "passed" verification. Set `PLAID_CLIENT_ID`/`PLAID_SECRET`/`PLAID_ENV`, or set `BLAZE_ALLOW_UNVERIFIED_PLAID=true` in local dev only.
2. **`BLAZE_SEED_BALANCE` defaults to `0.00` in production.** The old `$100,000` mock pool is no longer seeded on Render. Fund `pool_01` through your real treasury process.
3. **`X-API-Key` is enforced when `BLAZE_API_KEY` is set.** It is unset by default so existing sandbox flows keep working — set it before going live.
4. **Unsigned webhooks are rejected when `BLAZE_ENV=production`.** Set `STRIPE_WEBHOOK_SECRET`.
5. **`track_ledger.job_id` is no longer mutated** to embed the bank id; the bank id lives in `bank_tx_id`. A migration backfills existing rows and is safe to run repeatedly.
6. **Amounts are capped at $100,000 per request** (`le=100000`) and must be `> 0`.

---

## 5. Remaining blockers — live money movement (NO-GO)

These are **not code bugs**; they are product, security, and compliance gaps that require decisions and accounts I cannot provision.

1. **Plaid onboarding is fabricated.** `onboard-and-payout` derives an access token with `f"access-sandbox-{public_token.split('-')[-1]}"`. No `/item/public_token/exchange` call is ever made, so the "linked bank account" does not exist and the balance check queries a non-existent item. A real Plaid Link + exchange flow is required before the verification step means anything.
2. **No caller authentication on the payout endpoint** beyond an optional static API key. There is no per-tenant identity, no scopes, and `/v1/ledger/balances` is fully public. Anyone who learns the URL can attempt payouts.
3. **No KYC/AML, sanctions screening, or transaction limits.** This is a money-movement product; operating it without these is a regulatory exposure in essentially every jurisdiction.
4. **SQLite is single-writer.** It is fine for staging and low volume, but `BEGIN IMMEDIATE` serialises all writes and the file lives on one Render disk. Concurrent payout volume will queue or fail. Migrate to Postgres before real throughput; the ledger logic is already transaction-shaped and ports cleanly.
5. **No audit trail beyond `track_ledger`.** No immutable append-only log, no actor attribution, no reconciliation report against Stripe's balance transactions.
6. **Stripe payouts require an approved platform account** with a funded balance and the `payouts` capability enabled. `stripe.Payout.create` will fail on a standard account until that is granted.

**Recommendation:** deploy to staging now, run the smoke test in `test_production_cloud.py` against it, and treat §5 as the gate for production.

---

## 6. Deploy procedure

**Render (blueprint)**
1. Push this branch and merge to `main`.
2. In the Render dashboard: **New → Blueprint**, select the repo. `render.yaml` is picked up automatically.
3. Set the `sync: false` secrets: `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `PLAID_CLIENT_ID`, `PLAID_SECRET`, `BLAZE_API_KEY`, `BLAZE_ALLOWED_ORIGINS`.
4. Deploy. Render gates the rollout on `GET /health` returning `{"status":"ok","db":"ok"}`.
5. Register the webhook in Stripe: `https://<service>.onrender.com/v1/webhooks/stripe`, events `payout.paid` and `payout.failed`. Copy the signing secret into `STRIPE_WEBHOOK_SECRET` and redeploy.
6. Smoke test: `BLAZE_URL=https://<service>.onrender.com BLAZE_API_KEY=... python test_production_cloud.py`

**Docker**
```bash
docker build -t blaze-payment-engine .
docker run -p 8000:8000 --env-file .env -v blaze-data:/data blaze-payment-engine
```

**Rollback:** redeploy the previous Render release. The ledger disk is preserved across deploys, so no financial state is lost. Schema migrations are additive (`ADD COLUMN`, `CREATE TABLE IF NOT EXISTS`) and backward-compatible with the previous release.

---

## 7. Files changed

| File | Change |
|---|---|
| `server.py` | Rewritten — single routes, idempotent webhooks, exact matching, auth, CORS, health |
| `tasks.py` | Rewritten — fail-closed Plaid check, atomic claim-then-debit, idempotent rollback, Stripe idempotency key |
| `init_ledgers.py` | Rewritten — `bank_tx_id`, `webhook_events`, migrations, env-configurable path/seed |
| `webhook_receiver.py` | Fixed `StripeObject` handling |
| `render.yaml` | Rewritten — correct paths, health check, persistent disk, secrets |
| `Dockerfile` | Rewritten — non-root, healthcheck, `/data` volume |
| `index.html` | Fixed script tags, axios, API base, real balance |
| `requirements.txt` | Pinned |
| `requirements-dev.txt`, `.env.example`, `.gitignore`, `.github/workflows/ci.yml` | Added |
| `tests/conftest.py`, `tests/test_money_safety.py`, `tests/manual_webhook_check.py` | Added |
| `test_production_cloud.py` | Env-driven URL/key |
| `DEPLOYMENT_READINESS.md` | This report |
