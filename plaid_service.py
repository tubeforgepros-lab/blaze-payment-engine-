"""Real Plaid integration: Link tokens, public-token exchange, balance lookup.

Replaces the fabricated `access-sandbox-<suffix>` tokens used previously.
All functions raise PlaidServiceError with a machine-readable code so the API
layer can return actionable errors instead of silent failures.
"""
import os
import time

import plaid
from plaid.api import plaid_api
from plaid.api_client import ApiClient
from plaid.configuration import Configuration
from plaid.model.accounts_balance_get_request import AccountsBalanceGetRequest
from plaid.model.country_code import CountryCode
from plaid.model.institutions_get_by_id_request import InstitutionsGetByIdRequest
from plaid.model.item_public_token_exchange_request import ItemPublicTokenExchangeRequest
from plaid.model.link_token_create_request import LinkTokenCreateRequest
from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
from plaid.model.products import Products
from plaid.model.sandbox_public_token_create_request import SandboxPublicTokenCreateRequest

_ENV_HOSTS = {
    "sandbox": plaid.Environment.Sandbox,
    "production": plaid.Environment.Production,
}

_client = None


class PlaidServiceError(Exception):
    def __init__(self, code: str, message: str, status: int = 502):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def plaid_env() -> str:
    return (os.getenv("PLAID_ENV") or "sandbox").strip().lower()


def is_configured() -> bool:
    return bool(os.getenv("PLAID_CLIENT_ID")) and bool(os.getenv("PLAID_SECRET"))


def get_client():
    """Build (and cache) a Plaid client. Raises if credentials are missing."""
    global _client
    if _client is not None:
        return _client

    client_id = os.getenv("PLAID_CLIENT_ID")
    secret = os.getenv("PLAID_SECRET")
    if not client_id or not secret:
        raise PlaidServiceError(
            "PLAID_NOT_CONFIGURED",
            "PLAID_CLIENT_ID and PLAID_SECRET must be set.",
            status=503,
        )

    env = plaid_env()
    host = _ENV_HOSTS.get(env)
    if host is None:
        raise PlaidServiceError(
            "PLAID_BAD_ENV",
            f"PLAID_ENV must be 'sandbox' or 'production', got {env!r}.",
            status=500,
        )

    _client = plaid_api.PlaidApi(
        ApiClient(Configuration(host=host, api_key={"clientId": client_id, "secret": secret}))
    )
    return _client


def reset_client() -> None:
    """Drop the cached client (used by tests and after config changes)."""
    global _client
    _client = None


def _wrap(exc: Exception, action: str) -> PlaidServiceError:
    """Translate plaid-python exceptions into actionable API errors."""
    body = getattr(exc, "body", None) or str(exc)
    code = "PLAID_ERROR"
    message = f"{action} failed: {body}"

    if isinstance(body, str):
        lowered = body.lower()
        if "invalid_api_keys" in lowered or "invalid client_id or secret" in lowered:
            code, message = "PLAID_INVALID_CREDENTIALS", "Plaid rejected the configured credentials."
        elif "item_login_required" in lowered:
            code, message = "PLAID_LOGIN_REQUIRED", "The linked bank item requires re-authentication."
        elif "product_not_ready" in lowered:
            code, message = "PLAID_PRODUCT_NOT_READY", "Plaid product not ready for this item."
        elif "invalid_public_token" in lowered:
            code, message = "PLAID_INVALID_PUBLIC_TOKEN", "The public_token is invalid or already used."
        elif "no accounts" in lowered:
            code, message = "PLAID_NO_ACCOUNTS", "No accounts available for this item."

    return PlaidServiceError(code, message)


def create_link_token(agent_job_id: str, client_user_id: str | None = None) -> dict:
    """Create a Plaid Link token for the frontend widget."""
    api = get_client()
    try:
        req = LinkTokenCreateRequest(
            user=LinkTokenCreateRequestUser(client_user_id=client_user_id or agent_job_id),
            client_name="Blaze Payment Engine",
            products=[Products("auth"), Products("balance")],
            country_codes=[CountryCode("US")],
            language="en",
        )
        resp = api.link_token_create(req)
        return {
            "link_token": resp["link_token"],
            "expiration": resp.get("expiration"),
            "request_id": resp.get("request_id"),
        }
    except PlaidServiceError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _wrap(exc, "link_token_create")


def exchange_public_token(public_token: str) -> dict:
    """Exchange a Link public_token for a permanent access_token."""
    api = get_client()
    try:
        resp = api.item_public_token_exchange(
            ItemPublicTokenExchangeRequest(public_token=public_token)
        )
        return {
            "access_token": resp["access_token"],
            "item_id": resp["item_id"],
            "request_id": resp.get("request_id"),
        }
    except PlaidServiceError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _wrap(exc, "item_public_token_exchange")


def get_institution_name(institution_id: str) -> str | None:
    if not institution_id:
        return None
    api = get_client()
    try:
        resp = api.institutions_get_by_id(
            InstitutionsGetByIdRequest(
                institution_id=institution_id, country_codes=[CountryCode("US")]
            )
        )
        return resp["institution"]["name"]
    except Exception:  # noqa: BLE001 - cosmetic only, never block a payout
        return None


def get_balances(access_token: str) -> dict:
    """Return total available USD plus the per-account breakdown."""
    api = get_client()
    try:
        resp = api.accounts_balance_get(AccountsBalanceGetRequest(access_token=access_token))
    except PlaidServiceError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _wrap(exc, "accounts_balance_get")

    accounts = []
    total = 0.0
    for acct in resp["accounts"]:
        bal = acct.get("balances") or {}
        currency = bal.get("iso_currency_code") or "USD"
        available = bal.get("available")
        if available is None:
            available = bal.get("current") or 0.0
        if currency != "USD":
            continue
        total += float(available)
        accounts.append(
            {
                "account_id": acct.get("account_id"),
                "name": acct.get("name"),
                "mask": acct.get("mask"),
                "type": str(acct.get("type")),
                "subtype": str(acct.get("subtype")),
                "available": float(available),
                "currency": currency,
            }
        )
    return {"available_total": total, "accounts": accounts}


def create_sandbox_public_token(institution_id: str = "ins_109508") -> str:
    """Sandbox-only helper so the full flow can be exercised without the Link UI."""
    if plaid_env() != "sandbox":
        raise PlaidServiceError(
            "PLAID_NOT_SANDBOX",
            "Sandbox token creation is only available when PLAID_ENV=sandbox.",
            status=400,
        )
    api = get_client()
    try:
        resp = api.sandbox_public_token_create(
            SandboxPublicTokenCreateRequest(
                institution_id=institution_id,
                initial_products=[Products("auth"), Products("balance")],
            )
        )
        return resp["public_token"]
    except PlaidServiceError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _wrap(exc, "sandbox_public_token_create")


def healthcheck() -> dict:
    """Authenticated round-trip used by /v1/diagnostics/integrations."""
    if not is_configured():
        return {
            "ok": False,
            "code": "PLAID_NOT_CONFIGURED",
            "env": plaid_env(),
            "latency_ms": None,
        }
    started = time.perf_counter()
    try:
        from plaid.model.institutions_get_request import InstitutionsGetRequest

        api = get_client()
        api.institutions_get(
            InstitutionsGetRequest(count=1, offset=0, country_codes=[CountryCode("US")])
        )
        return {
            "ok": True,
            "code": "OK",
            "env": plaid_env(),
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
        }
    except Exception as exc:  # noqa: BLE001
        err = _wrap(exc, "institutions_get")
        return {
            "ok": False,
            "code": err.code,
            "message": err.message,
            "env": plaid_env(),
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
        }
