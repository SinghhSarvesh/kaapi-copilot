"""
In-memory session state for Journey A conversational turns.

Tracks:
  - cart_skus and their authoritative catalog prices
  - active buyer budget (set deterministically, enforced server-side)
  - conversation state machine (DISCOVERY → CHECKOUT_REQUESTED → etc.)
  - upsell offer history (so we never re-offer the same product)

Key design principle: The LLM is NEVER the source of truth for any of these
values. The LLM may request actions; this module decides whether they are
allowed and executes them.
"""
from typing import Optional
from app.models.domain import new_session_id
from app.services.catalog_service import catalog_service
from app.services.audit_trail import audit_trail


# Conversation state machine values
class ConvState:
    DISCOVERY          = "DISCOVERY"
    CART_BUILDING      = "CART_BUILDING"
    CHECKOUT_REQUESTED = "CHECKOUT_REQUESTED"
    MANDATE_PENDING    = "MANDATE_PENDING"
    PAYMENT_PENDING    = "PAYMENT_PENDING"
    PAYMENT_SUCCESS    = "PAYMENT_SUCCESS"
    PAYMENT_FAILED     = "PAYMENT_FAILED"


class SessionManager:
    def __init__(self):
        self._sessions = {}  # session_id -> state dict

    def create_session(self, buyer_ref: str) -> str:
        session_id = new_session_id()
        self._sessions[session_id] = {
            "buyer_ref": buyer_ref,
            "cart_skus": [],
            "upsell_offered": [],
            "pending_upsell": None,
            "history": [],
            "upsells_accepted": 0,
            "upsells_offered_count": 0,
            # P0 additions
            "budget_limit_paise": None,    # None = no limit set
            "conversation_state": ConvState.DISCOVERY,
        }
        return session_id

    def get_state(self, session_id: str) -> dict:
        if session_id not in self._sessions:
            raise KeyError(f"Unknown session {session_id}")
        return self._sessions[session_id]

    # ── Budget ─────────────────────────────────────────────────────────────

    def set_budget(self, session_id: str, amount_paise: int) -> None:
        """Store the buyer's authoritative budget. Called from tool handler — never from LLM output directly."""
        state = self.get_state(session_id)
        state["budget_limit_paise"] = amount_paise
        audit_trail.log("BUDGET_SET", session_id, {"budget_limit_paise": amount_paise})

    def get_budget(self, session_id: str) -> Optional[int]:
        return self.get_state(session_id).get("budget_limit_paise")

    # ── Cart total (authoritative — catalog prices, not LLM-stated) ────────

    def get_cart_total_paise(self, session_id: str) -> int:
        state = self.get_state(session_id)
        total = 0
        for sku in state["cart_skus"]:
            price = catalog_service.get_price_paise(sku)
            if price:
                total += price
        return total

    # ── Cart mutations ──────────────────────────────────────────────────────

    def add_to_cart_validated(self, session_id: str, sku: str) -> dict:
        """
        Add an item to the cart after verifying:
          1. SKU exists in catalog
          2. new_total <= budget_limit (if a limit is active)
        Returns a structured result the LLM can use to explain the outcome.
        """
        state = self.get_state(session_id)

        # Validate SKU
        product = catalog_service.get_product(sku)
        if product is None:
            return {"added": False, "reason": "INVALID_SKU",
                    "message": f"SKU '{sku}' is not in the catalog."}

        item_price = product["price_paise"]
        current_total = self.get_cart_total_paise(session_id)
        new_total = current_total + item_price
        budget = state.get("budget_limit_paise")

        if budget is not None and new_total > budget:
            audit_trail.log("BUDGET_CHECK_FAILED", session_id, {
                "sku": sku,
                "item_price_paise": item_price,
                "current_cart_total_paise": current_total,
                "new_total_paise": new_total,
                "budget_limit_paise": budget,
                "decision": "BLOCKED",
                "reason": "ITEM_EXCEEDS_BUDGET",
            })
            return {
                "added": False,
                "reason": "ITEM_EXCEEDS_BUDGET",
                "product_name": product["name"],
                "item_price_paise": item_price,
                "current_cart_total_paise": current_total,
                "new_total_paise": new_total,
                "budget_limit_paise": budget,
                "message": (
                    f"{product['name']} costs \u20b9{item_price/100:.0f}, "
                    f"which would bring your cart to \u20b9{new_total/100:.0f}, "
                    f"exceeding your \u20b9{budget/100:.0f} spending limit."
                ),
            }

        # All checks passed — mutate cart
        if sku not in state["cart_skus"]:
            state["cart_skus"].append(sku)

        # Update pending-upsell acceptance tracking
        if state.get("pending_upsell") == sku:
            state["upsells_accepted"] += 1

        # Update conversation state
        if state["conversation_state"] == ConvState.DISCOVERY:
            state["conversation_state"] = ConvState.CART_BUILDING

        audit_trail.log("ITEM_ADDED", session_id, {
            "sku": sku,
            "item_price_paise": item_price,
            "new_cart_total_paise": new_total,
            "budget_limit_paise": budget,
            "decision": "ALLOWED",
        })
        return {
            "added": True,
            "product": product,
            "new_cart_total_paise": new_total,
            "budget_limit_paise": budget,
            "remaining_budget_paise": (budget - new_total) if budget else None,
        }

    def remove_from_cart(self, session_id: str, sku: str) -> dict:
        """Remove a specific SKU from the cart. Audit-logged."""
        state = self.get_state(session_id)
        if sku not in state["cart_skus"]:
            return {"removed": False, "reason": "NOT_IN_CART",
                    "message": f"'{sku}' was not in your cart."}
        state["cart_skus"].remove(sku)
        # If removed item was pending upsell, clear it
        if state.get("pending_upsell") == sku:
            state["pending_upsell"] = None
        new_total = self.get_cart_total_paise(session_id)
        product = catalog_service.get_product(sku)
        audit_trail.log("ITEM_REMOVED", session_id, {
            "sku": sku,
            "new_cart_total_paise": new_total,
        })
        return {
            "removed": True,
            "sku": sku,
            "product_name": product["name"] if product else sku,
            "cart_skus": state["cart_skus"],
            "new_cart_total_paise": new_total,
        }

    def clear_cart(self, session_id: str) -> dict:
        """Clear all items from the cart. Audit-logged."""
        if session_id not in self._sessions:
            return {"cleared": False}
        state = self._sessions[session_id]
        prev_skus = list(state["cart_skus"])
        state["cart_skus"] = []
        state["pending_upsell"] = None
        audit_trail.log("CART_CLEARED", session_id, {"removed_skus": prev_skus})
        return {"cleared": True, "cart_skus": []}

    # ── Conversation state ──────────────────────────────────────────────────

    def set_conversation_state(self, session_id: str, new_state: str) -> None:
        state = self.get_state(session_id)
        state["conversation_state"] = new_state

    # ── Agent response application ──────────────────────────────────────────

    def apply_agent_response(self, session_id: str, agent_response) -> None:
        """
        Apply the structured parts of an agent response to session state.
        Note: add_to_cart_skus here have already been validated by the tool
        handler — we just need to ensure they are recorded.
        """
        state = self.get_state(session_id)
        for sku in agent_response.add_to_cart_skus:
            if sku not in state["cart_skus"]:
                state["cart_skus"].append(sku)
            if state.get("pending_upsell") == sku:
                state["upsells_accepted"] += 1

        if agent_response.upsell_sku:
            state["pending_upsell"] = agent_response.upsell_sku
            if agent_response.upsell_sku not in state["upsell_offered"]:
                state["upsell_offered"].append(agent_response.upsell_sku)
            state["upsells_offered_count"] += 1
        elif state.get("pending_upsell") not in agent_response.add_to_cart_skus:
            # The pending upsell was not accepted in this turn — clear it.
            state["pending_upsell"] = None

        if agent_response.ready_to_checkout:
            state["conversation_state"] = ConvState.CHECKOUT_REQUESTED


session_manager = SessionManager()
