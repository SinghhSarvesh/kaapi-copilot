"""
MockShoppingAgent — deterministic, rule-based conversational brain.
No external API calls. Implements the same behavioral safety rules as the
Groq agent: budget checked server-side, remove_from_cart is a real cart
mutation, "yes" after checkout question means checkout (not discovery restart).
"""
import re
from typing import Optional
from app.providers.agent.base import ShoppingAgent, AgentResponse
from app.services.catalog_service import catalog_service
from app.services.session_manager import session_manager, ConvState

INTENT_KEYWORDS = {
    "kr-filter-500":   ["filter coffee", "filter", "powder", "south indian", "kaapi", "basic", "nothing fancy"],
    "kr-arabica-250":  ["arabica", "single origin", "beans", "single-origin"],
    "kr-subscription": ["subscription", "monthly", "subscribe"],
    "kr-frother":      ["frother", "milk foam", "froth"],
    "kr-filters-100":  ["filter paper", "reusable filter", "paper filter"],
    "kr-dripper":      ["dripper", "pour over", "pour-over"],
    "kr-steel-filter": ["steel filter", "filter set", "traditional filter"],
}

CHECKOUT_PHRASES   = ["checkout", "buy now", "pay", "proceed", "place order", "i'm ready", "im ready", "confirm order", "checkout"]
DECLINE_PHRASES    = ["no thanks", "no thank you", "not now", "skip", "don't need", "dont need", "nah", "no"]
ACCEPT_PHRASES     = ["yes", "sure", "add it", "sounds good", "okay", "ok", "yeah", "add that", "go ahead"]
REMOVE_PHRASES     = ["remove", "discard", "take out", "delete", "drop", "don't want", "dont want", "cancel"]
CLEAR_PHRASES      = ["clear cart", "clear my cart", "empty cart", "start over", "start fresh"]

BUDGET_PATTERNS    = [
    r"(?:don['\u2019]?t|do not)\s+spend\s+(?:more\s+than|over)\s+[₹rs\.]*\s*(\d+)",
    r"budget\s+(?:is|=|:)?\s*[₹rs\.]*\s*(\d+)",
    r"(?:limit|cap|maximum|max)\s+(?:is|of|=|:)?\s*[₹rs\.]*\s*(\d+)",
    r"[₹rs\.]*\s*(\d+)\s+(?:is\s+my\s+budget|budget)",
    r"spend\s+(?:only|just|atmost|at\s+most)\s+[₹rs\.]*\s*(\d+)",
]


class MockShoppingAgent(ShoppingAgent):
    name = "mock"

    def _match_products(self, text: str) -> list:
        text = text.lower()
        return [sku for sku, keywords in INTENT_KEYWORDS.items()
                if any(kw in text for kw in keywords)]

    def _extract_budget(self, text: str) -> Optional[int]:
        """Extract budget amount in paise from natural language."""
        for pattern in BUDGET_PATTERNS:
            m = re.search(pattern, text.lower())
            if m:
                return int(m.group(1)) * 100  # convert INR to paise
        return None

    def _find_remove_target(self, text: str, cart_skus: list) -> Optional[str]:
        """Find which cart item the user wants to remove."""
        text = text.lower()
        for sku in cart_skus:
            product = catalog_service.get_product(sku)
            if not product:
                continue
            name_words = product["name"].lower().split()
            # Check SKU keywords
            keywords = INTENT_KEYWORDS.get(sku, [])
            if any(kw in text for kw in keywords + name_words):
                return sku
        return None

    def handle_turn(self, session_state: dict, user_message: str,
                    session_id: Optional[str] = None) -> AgentResponse:
        text = user_message.lower().strip()
        cart_skus      = session_state.get("cart_skus", [])
        upsell_offered = session_state.get("upsell_offered", [])
        pending_upsell = session_state.get("pending_upsell")
        conv_state     = session_state.get("conversation_state", ConvState.DISCOVERY)
        decision_log   = []
        remove_skus    = []

        # ── Budget detection (P0: must run before anything else) ────────────
        budget_paise = self._extract_budget(text)
        if budget_paise is not None and session_id:
            session_manager.set_budget(session_id, budget_paise)
            session_state["budget_limit_paise"] = budget_paise
            decision_log.append({"action": "SET_BUDGET", "amount_paise": budget_paise})
            return AgentResponse(
                reply=f"Got it! I'll keep your cart under \u20b9{budget_paise/100:.0f}. What would you like to add?",
                decision_log=decision_log,
            )

        # ── Clear cart ────────────────────────────────────────────────────────
        if any(p in text for p in CLEAR_PHRASES) and session_id:
            result = session_manager.clear_cart(session_id)
            decision_log.append({"action": "CLEAR_CART", "decision": "DONE"})
            return AgentResponse(
                reply="Your cart has been cleared. What would you like to add?",
                decision_log=decision_log,
            )

        # ── Remove-from-cart (P0: contextual — only if item is in cart) ──────
        if any(p in text for p in REMOVE_PHRASES) and cart_skus and session_id:
            # "remove everything above ₹X"
            budget = session_state.get("budget_limit_paise")
            if "everything" in text and budget is not None:
                removed_names = []
                for sku in list(cart_skus):
                    price = catalog_service.get_price_paise(sku)
                    if price and price > budget:
                        res = session_manager.remove_from_cart(session_id, sku)
                        if res.get("removed"):
                            remove_skus.append(sku)
                            p = catalog_service.get_product(sku)
                            removed_names.append(p["name"] if p else sku)
                            decision_log.append({"action": f"REMOVE {sku}", "decision": "DONE"})
                new_cart = session_manager.get_state(session_id)["cart_skus"]
                new_total = session_manager.get_cart_total_paise(session_id)
                if removed_names:
                    return AgentResponse(
                        reply=f"Removed {', '.join(removed_names)} (above \u20b9{budget/100:.0f}). "
                              f"Your cart total is now \u20b9{new_total/100:.0f}.",
                        remove_from_cart_skus=remove_skus, decision_log=decision_log,
                    )
                else:
                    return AgentResponse(reply="All items in your cart are within your budget — nothing to remove.")

            # Remove a specific item
            target_sku = self._find_remove_target(text, cart_skus)
            if target_sku and session_id:
                res = session_manager.remove_from_cart(session_id, target_sku)
                if res.get("removed"):
                    remove_skus.append(target_sku)
                    decision_log.append({"action": f"REMOVE {target_sku}", "decision": "DONE"})
                    new_total = session_manager.get_cart_total_paise(session_id)
                    return AgentResponse(
                        reply=f"Removed {res['product_name']} from your cart. "
                              f"Your cart total is now \u20b9{new_total/100:.0f}.",
                        remove_from_cart_skus=remove_skus, decision_log=decision_log,
                    )

        # ── "yes" after checkout question (P0: context-aware) ────────────────
        if conv_state == ConvState.CHECKOUT_REQUESTED and any(p in text for p in ACCEPT_PHRASES):
            return AgentResponse(
                reply="Proceeding to checkout! Please tap 'Proceed to Pay' to confirm your order.",
                ready_to_checkout=True,
            )

        # ── Upsell response ──────────────────────────────────────────────────
        if pending_upsell and any(p in text for p in ACCEPT_PHRASES):
            if session_id:
                result = session_manager.add_to_cart_validated(session_id, pending_upsell)
                if result.get("added"):
                    product = catalog_service.get_product(pending_upsell)
                    decision_log.append({"action": f"ADD {pending_upsell}", "decision": "ALLOWED"})
                    return AgentResponse(
                        reply=f"Added {product['name']} (\u20b9{product['price_paise']/100:.0f}) to your cart. Ready to checkout whenever you are.",
                        add_to_cart_skus=[pending_upsell], decision_log=decision_log,
                    )
                else:
                    decision_log.append({"action": f"ADD {pending_upsell}", "decision": "BLOCKED", "reason": result.get("reason")})
                    return AgentResponse(reply=result.get("message", "Cannot add that item."), decision_log=decision_log)

        if pending_upsell and any(p in text for p in DECLINE_PHRASES):
            return AgentResponse(reply="No problem, keeping it simple. Let me know when you're ready to checkout.")

        # ── Checkout intent ──────────────────────────────────────────────────
        if any(p in text for p in CHECKOUT_PHRASES) and cart_skus:
            return AgentResponse(
                reply="Here's your order summary — please review and tap 'Proceed to Pay' to confirm.",
                ready_to_checkout=True,
            )

        # ── Product discovery ────────────────────────────────────────────────
        matched = self._match_products(text)
        if not matched:
            return AgentResponse(
                reply="Tell me what you're after — e.g. 'I want good filter coffee, nothing fancy', "
                      "or 'I like single-origin beans'. I can also show the full menu.",
            )

        primary_sku = matched[0]
        product = catalog_service.get_product(primary_sku)

        # Backend-validated add
        add_skus = []
        if session_id:
            result = session_manager.add_to_cart_validated(session_id, primary_sku)
            if result.get("added"):
                add_skus = [primary_sku]
                decision_log.append({"action": f"ADD {primary_sku}", "decision": "ALLOWED",
                                     "new_total_paise": result.get("new_cart_total_paise")})
                reply = f"I'd recommend {product['name']} — \u20b9{product['price_paise']/100:.0f}. {product['description']}"
            else:
                decision_log.append({"action": f"ADD {primary_sku}", "decision": "BLOCKED",
                                     "reason": result.get("reason")})
                reply = result.get("message", f"{product['name']} cannot be added.")
                return AgentResponse(reply=reply, decision_log=decision_log)
        else:
            add_skus = [primary_sku]
            reply = f"I'd recommend {product['name']} — \u20b9{product['price_paise']/100:.0f}. {product['description']}"

        # Upsell (budget-checked)
        upsell_sku, upsell_reason = None, ""
        upsell = catalog_service.get_upsell_for(primary_sku)
        budget = session_state.get("budget_limit_paise")
        if upsell and upsell["sku"] not in upsell_offered and upsell["sku"] not in cart_skus:
            if budget is not None:
                current_total = session_manager.get_cart_total_paise(session_id) if session_id else sum(
                    catalog_service.get_price_paise(s) or 0 for s in cart_skus + add_skus
                )
                if current_total + upsell["price_paise"] > budget:
                    upsell = None  # Upsell would exceed budget — don't propose it
            if upsell:
                upsell_sku = upsell["sku"]
                upsell_reason = (f"Buyers who get {product['name']} usually pair it with "
                                 f"{upsell['name']} (\u20b9{upsell['price_paise']/100:.0f}) — want me to add it?")
                reply += f" {upsell_reason}"

        return AgentResponse(
            reply=reply,
            add_to_cart_skus=add_skus,
            upsell_sku=upsell_sku,
            upsell_reason=upsell_reason,
            decision_log=decision_log,
        )


mock_shopping_agent = MockShoppingAgent()
