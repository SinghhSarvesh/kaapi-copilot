"""
MockShoppingAgent — deterministic, rule-based conversational brain. No external
API calls. Matches buyer intent against the catalog via keywords, proposes
exactly one catalog-defined upsell, and never invents a product or price.
"""
import re
from app.providers.agent.base import ShoppingAgent, AgentResponse
from app.services.catalog_service import catalog_service

INTENT_KEYWORDS = {
    "kr-filter-500": ["filter coffee", "filter", "powder", "south indian", "kaapi", "basic", "nothing fancy"],
    "kr-arabica-250": ["arabica", "single origin", "beans", "single-origin"],
    "kr-subscription": ["subscription", "monthly", "subscribe"],
    "kr-frother": ["frother", "milk foam", "froth"],
    "kr-filters-100": ["filter paper", "reusable filter", "paper filter"],
    "kr-dripper": ["dripper", "pour over", "pour-over"],
    "kr-steel-filter": ["steel filter", "filter set", "traditional filter"],
}

CHECKOUT_PHRASES = ["checkout", "buy now", "pay", "proceed", "place order", "i'm ready", "im ready", "confirm order"]
DECLINE_PHRASES = ["no thanks", "no thank you", "not now", "skip", "don't need", "dont need", "nah"]
ACCEPT_PHRASES = ["yes", "sure", "add it", "sounds good", "okay", "ok", "yeah", "add that"]


class MockShoppingAgent(ShoppingAgent):
    name = "mock"

    def _match_products(self, text: str) -> list:
        text = text.lower()
        matched = []
        for sku, keywords in INTENT_KEYWORDS.items():
            if any(kw in text for kw in keywords):
                matched.append(sku)
        return matched

    def handle_turn(self, session_state: dict, user_message: str) -> AgentResponse:
        text = user_message.lower().strip()
        cart_skus = session_state.get("cart_skus", [])
        upsell_offered = session_state.get("upsell_offered", [])
        pending_upsell = session_state.get("pending_upsell")

        # Buyer responding to a previously offered upsell
        if pending_upsell and any(p in text for p in ACCEPT_PHRASES):
            product = catalog_service.get_product(pending_upsell)
            return AgentResponse(
                reply=f"Great — added {product['name']} (₹{product['price_paise']/100:.0f}) to your cart. Ready to checkout whenever you are.",
                add_to_cart_skus=[pending_upsell],
                ready_to_checkout=False,
            )
        if pending_upsell and any(p in text for p in DECLINE_PHRASES):
            return AgentResponse(
                reply="No problem, keeping it simple. Let me know when you're ready to checkout.",
                ready_to_checkout=False,
            )

        # Explicit checkout intent
        if any(p in text for p in CHECKOUT_PHRASES) and cart_skus:
            return AgentResponse(
                reply="Here's your order summary — please review and tap 'Proceed to Pay' to confirm.",
                ready_to_checkout=True,
            )

        # Product discovery
        matched = self._match_products(text)
        if not matched:
            return AgentResponse(
                reply="Tell me what you're after — e.g. 'I want good filter coffee, nothing fancy', "
                      "or 'I like single-origin beans'. I can also show the full menu.",
            )

        primary_sku = matched[0]
        product = catalog_service.get_product(primary_sku)
        reply = f"I'd recommend {product['name']} — ₹{product['price_paise']/100:.0f}. {product['description']}"

        upsell = catalog_service.get_upsell_for(primary_sku)
        upsell_sku, upsell_reason = None, ""
        if upsell and upsell["sku"] not in upsell_offered and upsell["sku"] not in cart_skus:
            upsell_sku = upsell["sku"]
            upsell_reason = (f"Buyers who get {product['name']} usually pair it with "
                              f"{upsell['name']} (₹{upsell['price_paise']/100:.0f}) — want me to add it?")
            reply += f" {upsell_reason}"

        return AgentResponse(
            reply=reply,
            add_to_cart_skus=[primary_sku],
            upsell_sku=upsell_sku,
            upsell_reason=upsell_reason,
        )


mock_shopping_agent = MockShoppingAgent()
