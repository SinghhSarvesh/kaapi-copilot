"""
Payment provider abstraction. Business logic (guardrail engine, order service)
only ever talks to this interface — never to Razorpay or mock internals directly.
"""
from abc import ABC, abstractmethod
from typing import Optional


class PaymentProvider(ABC):
    name: str = "base"

    @abstractmethod
    def create_order(self, amount_paise: int, currency: str, receipt: str) -> dict:
        """Returns {"order_id": ...}"""

    @abstractmethod
    def create_payment_link(self, order_id: str, amount_paise: int, currency: str,
                             description: str, upi_vpa: Optional[str] = None) -> dict:
        """Returns {"payment_link_id": ..., "payment_link_url": ...}"""

    @abstractmethod
    def fetch_payment_link(self, payment_link_id: str) -> dict:
        """Returns current status of a payment link."""

    @abstractmethod
    def capture_payment(self, payment_id: str, amount_paise: int) -> dict:
        """Captures an authorized payment."""

    @abstractmethod
    def fetch_payment(self, payment_id: str) -> dict:
        """Returns payment details."""

    @abstractmethod
    def create_refund(self, payment_id: str, amount_paise: int) -> dict:
        """Issues a refund."""

    @abstractmethod
    def simulate_webhook(self, payment_link_id: str, outcome: str) -> dict:
        """Demo-only helper: mock providers can synthesize a webhook payload.
        Real provider raises NotImplementedError (webhooks come from Razorpay itself)."""
