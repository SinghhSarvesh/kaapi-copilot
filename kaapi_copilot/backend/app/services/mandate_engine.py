"""
Guardrail / Mandate Engine — the hard-gate layer between agent output and any
money-moving call. Plain code, not an LLM instruction: the agent proposes a
cart, this module decides whether it becomes a payment.

Hard constraints implemented here:
  - every line price is re-read from the catalog at mandate-build time
  - a numeric spend cap (session + per-transaction) is checked before any
    Razorpay call; a breach is a hard stop -> blocked mandate, never a soft warning
  - every mandate is written to the audit log BEFORE any Razorpay call
  - only a `confirmed` mandate may trigger create_order / create_payment_link
"""
from datetime import datetime, timezone
from typing import Optional
from app.core.config import settings
from app.services.catalog_service import catalog_service
from app.services.audit_trail import audit_trail
from app.models.domain import (
    PurchaseMandate, PolicyCheck, Confirmation, CartItem,
    new_mandate_id,
)

ALLOWED_CATEGORIES = {"powder", "beans", "brew-gear", "subscription", "accessory", "consumable"}


class GuardrailError(Exception):
    pass


class MandateEngine:
    def __init__(self):
        self._session_spend_paise = {}  # session_id -> cumulative CONFIRMED/paid spend

    def _record_session_spend(self, session_id: str, amount_paise: int):
        self._session_spend_paise[session_id] = self._session_spend_paise.get(session_id, 0) + amount_paise

    def get_session_spend(self, session_id: str) -> int:
        return self._session_spend_paise.get(session_id, 0)

    def build_mandate(self, session_id: str, buyer_ref: str, cart_skus: list,
                       rationale: str, agent_stated_prices: Optional[dict] = None,
                       session_budget_paise: Optional[int] = None) -> PurchaseMandate:
        agent_stated_prices = agent_stated_prices or {}
        items, price_mismatch = [], None

        if not cart_skus:
            mandate = PurchaseMandate(
                mandate_id=new_mandate_id(), session_id=session_id, buyer_ref=buyer_ref,
                items=[], currency="INR", total_paise=0, rationale=rationale,
                policy_checks=[PolicyCheck("cart_not_empty", "fail", detail="Cart is empty; add items before building a mandate.")],
                status="blocked", block_reason="Cart is empty; add items before building a mandate.",
            )
            audit_trail.log("MANDATE_BLOCKED", session_id, mandate.to_dict())
            return mandate

        for sku in cart_skus:
            catalog_price = catalog_service.get_price_paise(sku)
            product = catalog_service.get_product(sku)
            if catalog_price is None or product is None:
                price_mismatch = f"SKU '{sku}' not found in catalog"
                break
            stated = agent_stated_prices.get(sku)
            if stated is not None and stated != catalog_price:
                price_mismatch = f"Agent stated {stated} paise for {sku}, catalog says {catalog_price} paise"
                break
            items.append(CartItem(sku=sku, name=product["name"], qty=1, unit_price_paise=catalog_price))

        total_paise = sum(i.subtotal_paise for i in items)
        checks = []

        if price_mismatch:
            checks.append(PolicyCheck("price_matches_catalog", "fail", detail=price_mismatch))
        else:
            checks.append(PolicyCheck("price_matches_catalog", "pass"))

        checks.append(PolicyCheck(
            "category_allowlist",
            "pass" if all(catalog_service.get_product(i.sku)["category"] in ALLOWED_CATEGORIES for i in items) else "fail",
        ))

        tx_status = "pass" if total_paise <= settings.transaction_spend_cap_paise else "fail"
        checks.append(PolicyCheck("transaction_spend_cap", tx_status, settings.transaction_spend_cap_paise,
                                   detail=f"total_paise={total_paise}"))

        projected_session_spend = self.get_session_spend(session_id) + total_paise
        sess_status = "pass" if projected_session_spend <= settings.session_spend_cap_paise else "fail"
        checks.append(PolicyCheck("session_spend_cap", sess_status, settings.session_spend_cap_paise,
                                   detail=f"projected_session_spend_paise={projected_session_spend}"))

        # P1: Buyer-set budget check (most restrictive — checked last)
        if session_budget_paise is not None:
            budget_status = "pass" if total_paise <= session_budget_paise else "fail"
            checks.append(PolicyCheck(
                "buyer_budget_check", budget_status, session_budget_paise,
                detail=f"total_paise={total_paise}, buyer_budget_paise={session_budget_paise}",
            ))

        blocked = any(c.status == "fail" for c in checks)
        status = "blocked" if blocked else "pending"
        block_reason = "; ".join(c.detail for c in checks if c.status == "fail") if blocked else None

        mandate = PurchaseMandate(
            mandate_id=new_mandate_id(), session_id=session_id, buyer_ref=buyer_ref,
            items=items, currency="INR", total_paise=total_paise, rationale=rationale,
            policy_checks=checks, status=status, block_reason=block_reason,
        )

        # Always written to the audit trail before any Razorpay call is even considered.
        event_name = "MANDATE_BLOCKED" if blocked else "MANDATE_CREATED"
        audit_trail.log(event_name, session_id, mandate.to_dict())
        return mandate


    def confirm_mandate(self, mandate: PurchaseMandate, method: str) -> PurchaseMandate:
        """The only path that may flip a mandate to 'confirmed'. method: 'buyer_tap' | 'mcp_confirm_and_pay'."""
        if mandate.status == "blocked":
            audit_trail.log("mandate_confirmation_rejected", mandate.session_id,
                             {"mandate_id": mandate.mandate_id, "reason": "mandate is blocked"})
            raise GuardrailError(f"Mandate {mandate.mandate_id} is blocked: {mandate.block_reason}")
        if mandate.status == "confirmed":
            return mandate
        if mandate.status == "paid":
            raise GuardrailError(f"Mandate {mandate.mandate_id} is already paid")
        if mandate.status == "payment_failed":
            raise GuardrailError(f"Mandate {mandate.mandate_id} failed; build a new mandate to retry")
        if mandate.status != "pending":
            raise GuardrailError(f"Mandate {mandate.mandate_id} cannot be confirmed from status '{mandate.status}'")
        mandate.confirmation = Confirmation(method=method, status="confirmed",
                                             confirmed_at=datetime.now(timezone.utc).isoformat())
        mandate.status = "confirmed"
        audit_trail.log("mandate_confirmed", mandate.session_id,
                         {"mandate_id": mandate.mandate_id, "method": method, "total_paise": mandate.total_paise})
        return mandate

    def record_successful_spend(self, mandate: PurchaseMandate) -> None:
        self._record_session_spend(mandate.session_id, mandate.total_paise)


mandate_engine = MandateEngine()
