"""Stripe integration helpers: configuration, health check, payout dispatch."""
import os
import time

import stripe


class StripeServiceError(Exception):
    def __init__(self, code: str, message: str, status: int = 502):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def configure() -> None:
    """Apply the configured secret key to the stripe module."""
    stripe.api_key = os.getenv("STRIPE_SECRET_KEY")


def is_configured() -> bool:
    return bool(os.getenv("STRIPE_SECRET_KEY"))


def _mode() -> str:
    key = os.getenv("STRIPE_SECRET_KEY") or ""
    if key.startswith("sk_live_") or key.startswith("rk_live_"):
        return "live"
    if key.startswith("sk_test_") or key.startswith("rk_test_"):
        return "test"
    return "unknown"


def _wrap(exc: Exception, action: str) -> StripeServiceError:
    code = "STRIPE_ERROR"
    status = 502
    message = f"{action} failed: {exc}"

    if isinstance(exc, stripe.AuthenticationError):
        code, message, status = "STRIPE_INVALID_CREDENTIALS", "Stripe rejected the API key.", 502
    elif isinstance(exc, stripe.PermissionError):
        code, message = "STRIPE_PERMISSION_DENIED", "The API key lacks the payouts capability."
    elif isinstance(exc, stripe.RateLimitError):
        code, message, status = "STRIPE_RATE_LIMITED", "Stripe rate limit hit; retry shortly.", 429
    elif isinstance(exc, stripe.IdempotencyError):
        code, message = "STRIPE_IDEMPOTENCY_CONFLICT", "Idempotency key reused with different parameters."
    elif isinstance(exc, stripe.InvalidRequestError):
        code, message, status = "STRIPE_INVALID_REQUEST", str(exc), 400
    elif isinstance(exc, stripe.APIConnectionError):
        code, message, status = "STRIPE_UNREACHABLE", "Could not reach Stripe.", 503

    return StripeServiceError(code, message, status)


def healthcheck() -> dict:
    """Authenticated round-trip: balance retrieve."""
    if not is_configured():
        return {
            "ok": False,
            "code": "STRIPE_NOT_CONFIGURED",
            "mode": _mode(),
            "latency_ms": None,
        }

    configure()
    started = time.perf_counter()
    try:
        balance = stripe.Balance.retrieve()
        latency = round((time.perf_counter() - started) * 1000, 1)

        available = [
            {"currency": b["currency"], "amount": b["amount"] / 100.0}
            for b in balance.get("available", [])
        ]
        pending = [
            {"currency": b["currency"], "amount": b["amount"] / 100.0}
            for b in balance.get("pending", [])
        ]
        return {
            "ok": True,
            "code": "OK",
            "mode": _mode(),
            "livemode": bool(getattr(balance, "livemode", False)),
            "available": available,
            "pending": pending,
            "latency_ms": latency,
        }
    except Exception as exc:  # noqa: BLE001
        err = _wrap(exc, "Balance.retrieve")
        return {
            "ok": False,
            "code": err.code,
            "message": err.message,
            "mode": _mode(),
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
        }


def create_payout(amount: float, agent_job_id: str, idempotency_key: str | None = None) -> dict:
    """Create a real Stripe payout. Raises StripeServiceError on failure."""
    configure()
    if not is_configured():
        raise StripeServiceError(
            "STRIPE_NOT_CONFIGURED", "STRIPE_SECRET_KEY is not set.", status=503
        )
    try:
        payout = stripe.Payout.create(
            amount=int(round(amount * 100)),
            currency="usd",
            metadata={"agent_job_id": agent_job_id},
            idempotency_key=idempotency_key or f"payout-{agent_job_id}",
        )
        return {
            "id": payout.id,
            "status": payout.status,
            "amount": payout.amount / 100.0,
            "arrival_date": payout.get("arrival_date"),
            "livemode": bool(getattr(payout, "livemode", False)),
        }
    except Exception as exc:  # noqa: BLE001
        raise _wrap(exc, "Payout.create")
