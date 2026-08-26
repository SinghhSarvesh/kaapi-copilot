"""
RazorpayPaymentProvider — real integration layer using the official `razorpay`
Python SDK against test-mode keys. Implements the same PaymentProvider
interface as the mock, so guardrail/order logic is provider-agnostic.

Activate with:
    PAYMENT_MODE=razorpay
    RAZORPAY_KEY_ID=...
    RAZORPAY_KEY_SECRET=...
"""
from typing import Optional
from app.providers.payment.base import PaymentProvider
from app.core.config import settings


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

    def create_order(self, amount_paise: int, currency: str, receipt: str) -> dict:
        order = self.client.order.create({
            "amount": amount_paise, "currency": currency, "receipt": receipt,
        })
        return {"order_id": order["id"], "status": order["status"]}

    def create_payment_link(self, order_id: str, amount_paise: int, currency: str,
                             description: str, upi_vpa: Optional[str] = None) -> dict:
        payload = {
            "amount": amount_paise, "currency": currency, "description": description,
            "notes": {"order_id": order_id},
        }
        link = self.client.payment_link.create(payload)
        return {"payment_link_id": link["id"], "payment_link_url": link["short_url"],
                "status": link["status"]}

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
    expected = hmac.new(webhook_secret.encode(), body, digestmod=hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")
