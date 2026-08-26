"""
MockPaymentProvider — deterministic, in-memory simulation of Razorpay's
order / payment-link / webhook flow. Recognizes the same demo UPI test
handles Razorpay itself documents: success@razorpay, failure@razorpay.
"""
import hashlib
import time
from typing import Optional
from app.providers.payment.base import PaymentProvider


class MockPaymentProvider(PaymentProvider):
    name = "mock"

    def __init__(self):
        self._orders = {}
        self._links = {}
        self._payments = {}
        self._counter = 0

    def _next_id(self, prefix: str) -> str:
        self._counter += 1
        raw = f"{prefix}-{self._counter}-{time.time()}"
        return f"{prefix}_{hashlib.sha1(raw.encode()).hexdigest()[:14]}"

    def create_order(self, amount_paise: int, currency: str, receipt: str) -> dict:
        order_id = self._next_id("order")
        self._orders[order_id] = {
            "order_id": order_id, "amount_paise": amount_paise,
            "currency": currency, "receipt": receipt, "status": "created",
        }
        return {"order_id": order_id, "status": "created"}

    def create_payment_link(self, order_id: str, amount_paise: int, currency: str,
                             description: str, upi_vpa: Optional[str] = None) -> dict:
        link_id = self._next_id("plink")
        url = f"https://mock-razorpay.test/pay/{link_id}"
        self._links[link_id] = {
            "payment_link_id": link_id, "order_id": order_id,
            "amount_paise": amount_paise, "currency": currency,
            "description": description, "upi_vpa": upi_vpa or "pending",
            "status": "created", "payment_id": None,
        }
        return {"payment_link_id": link_id, "payment_link_url": url, "status": "created"}

    def fetch_payment_link(self, payment_link_id: str) -> dict:
        return self._links.get(payment_link_id, {"status": "not_found"})

    def capture_payment(self, payment_id: str, amount_paise: int) -> dict:
        p = self._payments.get(payment_id)
        if not p:
            return {"status": "not_found"}
        p["status"] = "captured"
        return p

    def fetch_payment(self, payment_id: str) -> dict:
        return self._payments.get(payment_id, {"status": "not_found"})

    def create_refund(self, payment_id: str, amount_paise: int) -> dict:
        refund_id = self._next_id("rfnd")
        return {"refund_id": refund_id, "payment_id": payment_id,
                "amount_paise": amount_paise, "status": "processed"}

    def simulate_webhook(self, payment_link_id: str, outcome: str, order_id: Optional[str] = None, amount_paise: int = 0) -> dict:
        """outcome: 'success' -> payment.captured, 'failure' -> payment.failed"""
        link = self._links.get(payment_link_id)
        if not link:
            # Fallback for real Razorpay mode or untracked links: synthesize link record
            link = {
                "payment_link_id": payment_link_id,
                "order_id": order_id or payment_link_id,
                "amount_paise": amount_paise,
                "status": "created",
            }
            self._links[payment_link_id] = link

        payment_id = self._next_id("pay")
        vpa = "success@razorpay" if outcome == "success" else "failure@razorpay"
        if outcome == "success":
            link["status"] = "paid"
            self._payments[payment_id] = {
                "payment_id": payment_id, "order_id": link["order_id"],
                "amount_paise": link["amount_paise"], "status": "captured", "vpa": vpa,
            }
            event = "payment.captured"
        else:
            link["status"] = "failed"
            self._payments[payment_id] = {
                "payment_id": payment_id, "order_id": link["order_id"],
                "amount_paise": link["amount_paise"], "status": "failed", "vpa": vpa,
                "error_description": "Payment declined by UPI simulator (failure@razorpay).",
            }
            event = "payment.failed"
        link["payment_id"] = payment_id
        return {
            "event": event,
            "payload": {
                "payment": {"entity": self._payments[payment_id]},
                "order_id": link["order_id"],
                "payment_link_id": payment_link_id,
            },
        }


mock_payment_provider = MockPaymentProvider()
