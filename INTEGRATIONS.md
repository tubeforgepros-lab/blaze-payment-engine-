# Plaid + Stripe Integration

This document describes how the Blaze Money Engine talks to Plaid and Stripe,
what was broken before, and exactly what an operator must configure for it to
work in production.

## TL;DR — what was broken and what changed

| Area | Before | After |
| --- | --- | --- |
| Plaid access token | **Fabricated**: `f"access-sandbox-{public_token.split('-')[-1]}"`. Never exchanged, so every authenticated Plaid call failed. | Real `POST /item/public_token/exchange` via `plaid_service.exchange_public_token()`. The returned `access_token` is persisted per `item_id`. |
| Plaid bank linking | Frontend used a hardcoded `public-sandbox-99887766-...` placeholder. | Real Plaid Link: backend mints a `link_token`, the widget returns a `public_token`, which is exchanged server-side. |
| Balance verification | Inline SDK code duplicated in `tasks.py`. | Single implementation in `plaid_service.get_balances()`; still **fail-closed**. |
| Stripe payouts | Direct `stripe.Payout.create()` in `tasks.py`. | `stripe_service.create_payout()` with typed error mapping and idempotency keys. |
| Token storage | Plaintext access tokens. | Fernet encryption at rest via `token_vault.py` when `BLAZE_TOKEN_ENCRYPTION_KEY` is set. |
| Diagnostics | None. | `GET /v1/diagnostics/integrations` performs authenticated round-trips against both providers. |

## End-to-end flow

```
Browser                     Backend                         Plaid / Stripe
  |  POST /v1/plaid/link-token |                                  |
  |--------------------------->|  link_token_create ------------->|
  |<---- link_token -----------|                                  |
  |  Plaid Link widget opens   |                                  |
  |  user authenticates bank   |                                  |
  |  onSuccess(public_token)   |                                  |
  |  POST /v1/plaid/exchange   |                                  |
  |--------------------------->|  item/public_token/exchange ---->|
  |                            |<---- access_token + item_id -----|
  |                            |  accounts/balance/get ---------->|
  |                            |  store encrypted token (plaid_items)
  |<---- item_id, accounts ----|                                  |
  |  POST /v1/payments/onboard-and-payout {item_id, amount}       |
  |--------------------------->|  balance pre-flight (fail-closed)|
  |                            |  dual-ledger reserve             |
  |                            |  Payout.create (idempotent) ---->|
  |<---- QUEUED ---------------|                                  |
```

## Required environment variables

| Variable | Purpose | Notes |
| --- | --- | --- |
| `PLAID_CLIENT_ID` | Plaid API client id | Required. |
| `PLAID_SECRET` | Plaid API secret | Required. Must match the environment. |
| `PLAID_ENV` | `sandbox` or `production` | Defaults to `sandbox`. |
| `STRIPE_SECRET_KEY` | Stripe API key | Required. `sk_test_...` or `sk_live_...`. Needs payouts capability. |
| `STRIPE_WEBHOOK_SECRET` | Verifies `/v1/webhooks/stripe` | Required in production. |
| `BLAZE_TOKEN_ENCRYPTION_KEY` | Fernet key for access-token encryption | Generate with `python -c "import token_vault; print(token_vault.generate_key())"`. If unset, tokens are stored in plaintext (sandbox only). |
| `BLAZE_API_KEY` | Shared secret for `X-API-Key` | Strongly recommended in production. |
| `BLAZE_ALLOWED_ORIGINS` | CORS allow-list | Comma-separated. |

## Verifying connectivity

Once credentials are set, hit the diagnostics endpoint:

```bash
curl -s https://<host>/v1/diagnostics/integrations | jq
```

A healthy response looks like:

```json
{
  "ready": true,
  "plaid":  { "ok": true, "code": "OK", "env": "sandbox", "latency_ms": 120.4 },
  "stripe": { "ok": true, "code": "OK", "mode": "test", "latency_ms": 85.1 },
  "config": {
    "plaid_env": "sandbox",
    "plaid_configured": true,
    "stripe_configured": true,
    "webhook_secret_set": true,
    "api_key_enforced": true
  }
}
```

`ready` is `true` only when **both** providers answer an authenticated request.
When credentials are missing the endpoint returns `ready: false` with
`PLAID_NOT_CONFIGURED` / `STRIPE_NOT_CONFIGURED` — it never silently reports
healthy.

## Sandbox smoke test (no Link UI)

With `PLAID_ENV=sandbox` you can exercise the full flow headlessly:

```bash
# 1. Mint a sandbox public_token
curl -s -X POST https://<host>/v1/plaid/sandbox-public-token | jq -r .public_token

# 2. Exchange it (stores the item, returns available balance)
curl -s -X POST https://<host>/v1/plaid/exchange \
  -H 'Content-Type: application/json' \
  -d '{"public_token":"<token>"}' | jq

# 3. Trigger a payout against the stored item
curl -s -X POST https://<host>/v1/payments/onboard-and-payout \
  -H 'Content-Type: application/json' \
  -d '{"agent_job_id":"smoke-1","item_id":"<item_id>","amount":1.00}' | jq
```

## Error codes

| Code | Meaning | Action |
| --- | --- | --- |
| `PLAID_NOT_CONFIGURED` | Missing `PLAID_CLIENT_ID` / `PLAID_SECRET` | Set credentials. |
| `PLAID_INVALID_CREDENTIALS` | Plaid rejected the keys | Check keys and `PLAID_ENV`. |
| `PLAID_LOGIN_REQUIRED` | Item needs re-auth | Re-run Link for that item. |
| `PLAID_INVALID_PUBLIC_TOKEN` | Token invalid/used | Request a fresh Link token. |
| `PLAID_ITEM_NOT_FOUND` | Unknown `item_id` | Link the bank first. |
| `STRIPE_NOT_CONFIGURED` | Missing `STRIPE_SECRET_KEY` | Set the key. |
| `STRIPE_INVALID_CREDENTIALS` | Stripe rejected the key | Check key and mode. |
| `STRIPE_PERMISSION_DENIED` | Key lacks payouts scope | Enable payouts on the key. |
| `STRIPE_RATE_LIMITED` | Too many requests | Retry after backoff. |
| `STRIPE_IDEMPOTENCY_CONFLICT` | Key reused with different params | Use a unique `agent_job_id`. |

## Tests

`tests/test_integrations.py` covers the flow with mocked providers:
link-token success/failure, exchange + persistence, `item_id` resolution,
missing-reference rejection, diagnostics, and token-vault round-trips.
