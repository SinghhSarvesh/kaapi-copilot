"""
RazorpayPaymentProvider — real integration layer using the official `razorpay`
Python SDK against test-mode keys. Implements the same PaymentProvider
interface as the mock, so guardrail/order logic is provider-agnostic.

Activate with:
    PAYMENT_MODE=razorpay
    RAZORPAY_KEY_ID=...
    RAZORPAY_KEY_SECRET=...

Test-mode quota fallback
------------------------
Razorpay test mode caps payment links at 30 and orders at a similar limit.
When either cap is hit, create_order / create_payment_link transparently fall
back to the mock provider so the demo keeps running without crashing.
The fallback is logged to stderr so it's visible in Railway / Uvicorn logs.
"""
import sys
from typing import Optional
from app.providers.payment.base import PaymentProvider
from app.core.config import settings

_TESTMODE_PHRASES = ("test mode limit", "testmode limit", "test_mode limit")


def _is_testmode_quota(exc) -> bool:
    """True when Razorpay raises because a test-mode object quota is exhausted."""
    msg = str(exc).lower()
    return any(p in msg for p in _TESTMODE_PHRASES)


class RazorpayPaymentProvider(PaymentProvider):
    name = "razorpay"

    def __init__(self, key_id: str, key_secret: str):
        try:
            import razorpay
        except ImportError as e:
            raise RuntimeError(
                "razorpay SDK not installed. Run: pip install razorpay"
            ) from e
        self.client = razorpay.Client(auth=(key_id, key_secret))

    def _mock(self):
        from app.providers.payment.mock import mock_payment_provider
        return mock_payment_provider

    def create_order(self, amount_paise: int, currency: str, receipt: str) -> dict:
        try:
            order = self.client.order.create({
                "amount": amount_paise, "currency": currency, "receipt": receipt,
            })
            return {"order_id": order["id"], "status": order["status"]}
        except Exception as exc:
            if _is_testmode_quota(exc):
                print(
                    f"[razorpay] Test-mode order quota reached — falling back to mock provider. "
                    f"Original error: {exc}",
                    file=sys.stderr,
                )
                return self._mock().create_order(amount_paise, currency, receipt)
            raise

    def create_payment_link(self, order_id: str, amount_paise: int, currency: str,
                             description: str, upi_vpa: Optional[str] = None) -> dict:
        # Razorpay payment link API only accepts: amount, currency, description, notes
        # (order_id is NOT a valid top-level field for payment_link.create)
        payload = {
            "amount": amount_paise,
            "currency": currency,
            "description": description,
            "notes": {"order_id": order_id},
        }
        try:
            link = self.client.payment_link.create(payload)
            return {"payment_link_id": link["id"], "payment_link_url": link["short_url"],
                    "status": link["status"]}
        except Exception as exc:
            if _is_testmode_quota(exc):
                print(
                    f"[razorpay] Test-mode payment_link quota reached — falling back to mock provider. "
                    f"Original error: {exc}",
                    file=sys.stderr,
                )
                return self._mock().create_payment_link(
                    order_id, amount_paise, currency, description, upi_vpa
                )
            raise

    def fetch_payment_link(self, payment_link_id: str) -> dict:
        return self.client.payment_link.fetch(payment_link_id)

    def capture_payment(self, payment_id: str, amount_paise: int) -> dict:
        return self.client.payment.capture(payment_id, amount_paise)

    def fetch_payment(self, payment_id: str) -> dict:
        return self.client.payment.fetch(payment_id)

    def create_refund(self, payment_id: str, amount_paise: int) -> dict:
        return self.client.payment.refund(payment_id, {"amount": amount_paise})

    def simulate_webhook(self, payment_link_id: str, outcome: str) -> dict:
        raise NotImplementedError(
            "Real webhooks come from Razorpay itself via /api/webhooks/razorpay; "
            "use test UPI handles success@razorpay / failure@razorpay to trigger them."
        )


def verify_webhook_signature(body: bytes, signature: str, webhook_secret: str) -> bool:
    """HMAC-SHA256 verification per Razorpay docs. Always run before trusting a payload."""
    import hmac
    import hashlib
    mac = hmac.new(webhook_secret.encode(), body, digestmod=hashlib.sha256)
    expected = mac.hexdigest()
    return hmac.compare_digest(expected, signature or "")
