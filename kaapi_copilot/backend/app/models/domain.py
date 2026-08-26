"""
Core domain models for Kaapi Copilot. Provider-agnostic dataclasses shared by
mock and real payment/agent implementations, and by both Journey A and B.
"""
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional
import uuid


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


@dataclass
class Product:
    sku: str
    name: str
    price_paise: int
    category: str
    description: str = ""
    upsell_pairs: list = field(default_factory=list)  # list of SKUs

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CartItem:
    sku: str
    name: str
    qty: int
    unit_price_paise: int

    @property
    def subtotal_paise(self) -> int:
        return self.qty * self.unit_price_paise

    def to_dict(self) -> dict:
        d = asdict(self)
        d["subtotal_paise"] = self.subtotal_paise
        return d


@dataclass
class Cart:
    session_id: str
    buyer_ref: str
    items: list = field(default_factory=list)  # list[CartItem]
    created_at: str = field(default_factory=_now)
    upsell_offered_skus: list = field(default_factory=list)

    @property
    def total_paise(self) -> int:
        return sum(i.subtotal_paise for i in self.items)

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "buyer_ref": self.buyer_ref,
            "items": [i.to_dict() for i in self.items],
            "total_paise": self.total_paise,
            "created_at": self.created_at,
            "upsell_offered_skus": self.upsell_offered_skus,
        }


@dataclass
class PolicyCheck:
    rule: str
    status: str  # "pass" | "fail"
    limit_paise: Optional[int] = None
    detail: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Confirmation:
    method: str = "pending"  # "buyer_tap" | "mcp_confirm_and_pay" | "pending"
    status: str = "pending"  # "pending" | "confirmed" | "rejected"
    confirmed_at: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PurchaseMandate:
    mandate_id: str
    session_id: str
    buyer_ref: str
    items: list  # list[CartItem]
    currency: str
    total_paise: int
    rationale: str
    policy_checks: list  # list[PolicyCheck]
    status: str  # "pending" | "confirmed" | "blocked" | "paid" | "payment_failed"
    created_at: str = field(default_factory=_now)
    confirmation: Confirmation = field(default_factory=Confirmation)
    order_id: Optional[str] = None
    block_reason: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "mandate_id": self.mandate_id,
            "session_id": self.session_id,
            "created_at": self.created_at,
            "buyer_ref": self.buyer_ref,
            "items": [i.to_dict() for i in self.items],
            "currency": self.currency,
            "total_paise": self.total_paise,
            "rationale": self.rationale,
            "policy_checks": [c.to_dict() for c in self.policy_checks],
            "confirmation": self.confirmation.to_dict(),
            "status": self.status,
            "order_id": self.order_id,
            "block_reason": self.block_reason,
        }


@dataclass
class Order:
    order_id: str
    mandate_id: str
    session_id: str
    total_paise: int
    currency: str
    status: str  # "created" | "paid" | "payment_failed"
    payment_link_id: Optional[str] = None
    payment_link_url: Optional[str] = None
    payment_id: Optional[str] = None
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    failure_reason: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PaymentResult:
    order_id: str
    payment_link_id: str
    payment_link_url: str
    status: str  # "created"
    raw: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class AuditEvent:
    event_id: str
    ts: str
    event_type: str
    session_id: str
    payload: dict
    prev_hash: str
    hash: str

    def to_dict(self) -> dict:
        return asdict(self)


def new_mandate_id() -> str:
    return _id("mnd")


def new_session_id() -> str:
    return _id("sess")


def new_order_id() -> str:
    return _id("order")


def new_event_id() -> str:
    return _id("evt")
