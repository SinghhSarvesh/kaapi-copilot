"""
Order Service — the ONLY code path allowed to call the payment provider.
Requires a confirmed mandate; no route from raw agent/LLM output reaches
create_order / create_payment_link directly.
"""
from datetime import datetime, timedelta, timezone
from typing import Optional
from app.core.config import settings
from app.providers.payment.factory import get_payment_provider
from app.services.audit_trail import audit_trail
from app.services.mandate_engine import mandate_engine, GuardrailError
from app.services.session_manager import session_manager
from app.models.domain import Order, PurchaseMandate, new_order_id


class OrderService:
    def __init__(self):
        self._orders = {}          # order_id -> Order
        self._mandates = {}        # mandate_id -> PurchaseMandate
        self._link_to_order = {}   # payment_link_id -> order_id
        self._provider_order_to_order = {}  # provider order id -> internal order id
        self._processed_webhook_ids = set()
        self._held_carts = {}      # session_id -> {"skus": [...], "held_until": iso}

    def register_mandate(self, mandate: PurchaseMandate):
        self._mandates[mandate.mandate_id] = mandate

    def get_mandate(self, mandate_id: str) -> Optional[PurchaseMandate]:
        return self._mandates.get(mandate_id)

    def checkout(self, mandate: PurchaseMandate) -> Order:
        """Only a CONFIRMED mandate may reach here."""
        if mandate.status != "confirmed":
            audit_trail.log("checkout_rejected", mandate.session_id,
                             {"mandate_id": mandate.mandate_id, "reason": f"mandate status is '{mandate.status}', not 'confirmed'"})
            raise GuardrailError(f"Cannot checkout: mandate status is '{mandate.status}', not 'confirmed'")

        provider = get_payment_provider()
        order_resp = provider.create_order(mandate.total_paise, mandate.currency, receipt=mandate.mandate_id)
        link_resp = provider.create_payment_link(
            order_resp["order_id"], mandate.total_paise, mandate.currency,
            description=f"Kaapi Roasters order for {mandate.buyer_ref}",
        )

        order = Order(
            order_id=order_resp["order_id"], mandate_id=mandate.mandate_id, session_id=mandate.session_id,
            total_paise=mandate.total_paise, currency=mandate.currency, status="created",
            payment_link_id=link_resp["payment_link_id"], payment_link_url=link_resp["payment_link_url"],
        )
        mandate.order_id = order.order_id
        self._orders[order.order_id] = order
        self._link_to_order[link_resp["payment_link_id"]] = order.order_id
        self._provider_order_to_order[order_resp["order_id"]] = order.order_id

        audit_trail.log("order_created", mandate.session_id, {
            "order_id": order.order_id, "mandate_id": mandate.mandate_id,
            "payment_link_id": link_resp["payment_link_id"], "payment_link_url": link_resp["payment_link_url"],
            "total_paise": order.total_paise, "provider": provider.name,
        })
        return order

    def get_order(self, order_id: str) -> Optional[Order]:
        return self._orders.get(order_id)

    def handle_webhook_event(self, event: str, payload: dict, event_id: Optional[str] = None) -> Order:
        """Processes payment.captured / payment.failed. Never marks paid except from a verified webhook."""
        payment = payload.get("payment", {}).get("entity", {})
        payment_link = payload.get("payment_link", {}).get("entity", {})
        link_id = payload.get("payment_link_id") or payment.get("payment_link_id") or payment_link.get("id")
        notes = payment.get("notes", {}) or payment_link.get("notes", {})
        provider_order_id = (payload.get("order_id") or payment.get("order_id")
                     or payment_link.get("order_id") or notes.get("order_id"))
        order_id = (self._link_to_order.get(link_id)
                    or self._provider_order_to_order.get(provider_order_id)
                    or provider_order_id)
        order = self._orders.get(order_id)
        if not order:
            audit_trail.log("webhook_order_not_found", "unknown", {"event": event, "payload": payload})
            raise ValueError(f"No order found for webhook payload: {payload}")

        stable_event_id = event_id or payload.get("id") or payment.get("id")
        if stable_event_id and stable_event_id in self._processed_webhook_ids:
            return order
        if stable_event_id:
            self._processed_webhook_ids.add(stable_event_id)

        if event == "payment.captured":
            if order.status == "paid":
                return order
            if order.status == "payment_failed":
                raise ValueError("Cannot capture an order after payment failure; create a new mandate")
            order.status = "paid"
            order.updated_at = datetime.now(timezone.utc).isoformat()
            order.payment_id = payment.get("id") or order.payment_id
            mandate = self._mandates.get(order.mandate_id)
            if mandate:
                mandate.status = "paid"
                mandate_engine.record_successful_spend(mandate)
            session_manager.clear_cart(order.session_id)
            audit_trail.log("payment_captured", order.session_id,
                             {"order_id": order.order_id, "payment": payload.get("payment", {})})
        elif event == "payment.failed":
            if order.status == "payment_failed":
                return order
            if order.status == "paid":
                raise ValueError("Cannot fail an order after payment capture")
            order.status = "payment_failed"
            order.updated_at = datetime.now(timezone.utc).isoformat()
            order.payment_id = payment.get("id") or order.payment_id
            order.failure_reason = payment.get(
                "error_description", "Payment declined.")
            held_until = (datetime.now(timezone.utc) + timedelta(minutes=settings.cart_hold_minutes)).isoformat()
            mandate = self._mandates.get(order.mandate_id)
            cart_skus = [i.sku for i in mandate.items] if mandate else []
            if mandate:
                mandate.status = "payment_failed"
            self._held_carts[order.session_id] = {"skus": cart_skus, "held_until": held_until}
            audit_trail.log("payment_failed", order.session_id, {
                "order_id": order.order_id, "reason": order.failure_reason,
                "cart_held_until": held_until, "recovery_offer": "retry_or_alternate_payment_method",
            })
        else:
            audit_trail.log("webhook_unhandled_event", order.session_id, {"event": event, "payload": payload})
        return order

    def get_held_cart(self, session_id: str) -> Optional[dict]:
        return self._held_carts.get(session_id)


order_service = OrderService()
