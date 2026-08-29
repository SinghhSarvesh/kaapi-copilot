"""
MockShoppingAgent — friendly, deterministic conversational brain.
No external API calls. Feels like a real barista, not a chatbot.

Behavioural principles:
  1. Budget is enforced server-side — LLM never does math.
  2. Budget can be set AND cleared from chat.
  3. Upsells are contextual, personal and limited to 1 per turn.
  4. "yes" after checkout question means CHECKOUT, not product discovery.
  5. Replies are warm, short, and emoji-accented.
"""
import re
from typing import Optional
from app.providers.agent.base import ShoppingAgent, AgentResponse
from app.services.catalog_service import catalog_service
from app.services.session_manager import session_manager, ConvState

# ── Intent keywords ──────────────────────────────────────────────────────────
INTENT_KEYWORDS = {
    "kr-filter-500":   ["filter coffee", "filter", "powder", "south indian", "kaapi",
                        "basic", "nothing fancy", "traditional", "everyday", "regular"],
    "kr-arabica-250":  ["arabica", "single origin", "beans", "single-origin", "specialty",
                        "light roast", "fruity", "artisan"],
    "kr-subscription": ["subscription", "monthly", "subscribe", "regular delivery",
                        "every month", "auto", "recurring"],
    "kr-frother":      ["frother", "milk foam", "froth", "latte", "cappuccino", "foam",
                        "creamy", "milk"],
    "kr-filters-100":  ["filter paper", "reusable filter", "paper filter", "filters",
                        "paper", "100 filters"],
    "kr-dripper":      ["dripper", "pour over", "pour-over", "v60", "drip",
                        "slow brew", "manual brew"],
    "kr-steel-filter": ["steel filter", "filter set", "traditional filter", "brass filter",
                        "dabara", "steel", "decoction"],
}

# ── Phrase banks ─────────────────────────────────────────────────────────────
CHECKOUT_PHRASES   = ["checkout", "buy now", "pay", "proceed", "place order",
                      "i'm ready", "im ready", "confirm order", "done", "finish",
                      "complete", "order now", "take my money"]
DECLINE_PHRASES    = ["no thanks", "no thank you", "not now", "skip", "don't need",
                      "dont need", "nah", "no", "nope", "maybe later", "next time",
                      "not interested", "pass", "skip it"]
ACCEPT_PHRASES     = ["yes", "sure", "add it", "sounds good", "okay", "ok", "yeah",
                      "add that", "go ahead", "please", "absolutely", "perfect", "great",
                      "do it", "add", "yep", "yup"]
BARE_REFERENCE_PHRASES = ["it", "that", "that one", "this", "this one", "add it",
                          "add that", "get it", "get that", "i want it", "i want that"]
ORDINAL_PATTERNS = {
    "first": 0, "1st": 0, "one": 0,
    "second": 1, "2nd": 1, "two": 1,
    "third": 2, "3rd": 2, "three": 2,
    "fourth": 3, "4th": 3, "four": 3,
}
BOTH_PHRASES = ["both", "all of them", "all", "the two"]
REMOVE_PHRASES     = ["remove", "discard", "take out", "delete", "drop", "don't want",
                      "dont want", "cancel", "take off", "get rid"]
CLEAR_PHRASES      = ["clear cart", "clear my cart", "empty cart", "start over",
                      "start fresh", "reset cart"]
MENU_PHRASES       = ["show menu", "view menu", "full menu", "what do you have",
                      "what do you sell", "see menu", "show catalog", "product list",
                      "what's available", "show all products", "our menu", "the menu",
                      "catalog", "menu"]
HELP_PHRASES       = ["how does it work", "what can you do", "guide me", "how to use",
                      "instructions", "what can i do", "help me"]
BUDGET_REMOVE_PATTERNS = [
    r"(?:remove|clear|cancel|delete|reset|lift|no)\s+(?:my\s+)?(?:budget|limit|cap|restriction|spending limit)",
    r"(?:no\s+limit|no\s+budget|no\s+cap|unlimited|any\s+budget|remove\s+limit)",
    r"(?:budget|limit)\s+(?:remove|clear|cancel|reset|off)",
    r"(?:don'?t\s+)?(?:have\s+)?(?:a\s+)?(?:budget|limit)\s+(?:anymore|now|set)",
    r"(?:forget|ignore|drop)\s+(?:the\s+)?(?:budget|limit|cap)",
    r"(?:shopping|buy)\s+without\s+(?:limit|budget|cap|restriction)",
]

BUDGET_PATTERNS = [
    r"(?:don[''\u2019]?t|do not)\s+spend\s+(?:more\s+than|over)\s+[₹rs\.]*\s*(\d+)",
    r"budget\s+(?:is|of|=|:)?\s*[₹rs\.]*\s*(\d+)",
    r"(?:limit|cap|maximum|max)\s+(?:is|of|=|:)?\s*[₹rs\.]*\s*(\d+)",
    r"[₹rs\.]*\s*(\d+)\s+(?:is\s+my\s+budget|budget|is\s+(?:my\s+)?limit)",
    r"spend\s+(?:only|just|atmost|at\s+most)\s+[₹rs\.]*\s*(\d+)",
    r"keep\s+(?:it\s+)?(?:under|below|within)\s+[₹rs\.]*\s*(\d+)",
    r"(?:within|under|below)\s+[₹rs\.]*\s*(\d+)",
    r"[₹rs\.]*\s*(\d+)\s+(?:max|maximum|tops?|limit)",
]

# ── Upsell copy — personal, contextual, not generic ──────────────────────────
UPSELL_PITCH = {
    ("kr-filter-500", "kr-steel-filter"):
        "☕ Most filter-coffee lovers also grab the **Steel Filter Set** (₹650) — it makes the decoction richer. Want me to add it?",
    ("kr-arabica-250", "kr-dripper"):
        "🫙 Arabica really shines through a **Pour-over Dripper** (₹900) — unlocks all those fruity notes. Add it?",
    ("kr-filters-100", "kr-dripper"):
        "💧 Those filter papers pair perfectly with our **Pour-over Dripper** (₹900). Together they make the complete setup. Add the dripper?",
    ("kr-filter-500", "kr-subscription"):
        "📦 Love filter coffee? Our **Monthly Subscription** (₹700) sends 2 fresh bags every month — never run out. Add it?",
    ("kr-steel-filter", "kr-filter-500"):
        "🫘 Your Steel Filter needs something to brew! Add **Filter Coffee Powder** (₹450) — it's our house blend. Add it?",
}

PRODUCT_EMOJI = {
    "powder": "🫘", "beans": "☕", "brew-gear": "⚗️",
    "subscription": "📦", "accessory": "🔧", "consumable": "🗂️",
}


class MockShoppingAgent(ShoppingAgent):
    name = "mock"

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _match_products(self, text: str) -> list:
        text = text.lower()
        return [sku for sku, keywords in INTENT_KEYWORDS.items()
                if any(kw in text for kw in keywords)]

    def _extract_budget(self, text: str) -> Optional[int]:
        for pattern in BUDGET_PATTERNS:
            m = re.search(pattern, text.lower())
            if m:
                return int(m.group(1)) * 100
        return None

    def _wants_budget_removed(self, text: str) -> bool:
        text = text.lower()
        return any(re.search(p, text) for p in BUDGET_REMOVE_PATTERNS)

    def _find_remove_target(self, text: str, cart_skus: list) -> Optional[str]:
        text = text.lower()
        for sku in cart_skus:
            product = catalog_service.get_product(sku)
            if not product:
                continue
            name_words = product["name"].lower().split()
            keywords = INTENT_KEYWORDS.get(sku, [])
            if any(kw in text for kw in keywords + name_words):
                return sku
        return None

    def _fmt_price(self, paise: int) -> str:
        return f"₹{paise // 100}"

    def _product_line(self, sku: str) -> str:
        p = catalog_service.get_product(sku)
        if not p:
            return sku
        emoji = PRODUCT_EMOJI.get(p["category"], "🛍️")
        return f"{emoji} **{p['name']}** — {self._fmt_price(p['price_paise'])}"

    def _menu_text(self) -> str:
        lines = ["Here's what we've got at **Kaapi Roasters** ☕\n"]
        for product in catalog_service.list_products():
            emoji = PRODUCT_EMOJI.get(product["category"], "🛍️")
            lines.append(
                f"{emoji} **{product['name']}** — {self._fmt_price(product['price_paise'])}\n"
                f"   _{product['description']}_"
            )
        lines.append("\nJust tell me which one catches your eye!")
        return "\n".join(lines)

    def _get_upsell_pitch(self, primary_sku: str, upsell_sku: str) -> str:
        key = (primary_sku, upsell_sku)
        if key in UPSELL_PITCH:
            return UPSELL_PITCH[key]
        upsell = catalog_service.get_product(upsell_sku)
        if upsell:
            return f"💡 Customers also love our **{upsell['name']}** ({self._fmt_price(upsell['price_paise'])}). Want me to add it?"
        return ""

    # ── Main turn handler ─────────────────────────────────────────────────────

    def handle_turn(self, session_state: dict, user_message: str,
                    session_id: Optional[str] = None,
                    conversation_history: Optional[list] = None) -> AgentResponse:
        text = user_message.lower().strip()
        cart_skus      = session_state.get("cart_skus", [])
        upsell_offered = session_state.get("upsell_offered", [])
        pending_upsell = session_state.get("pending_upsell")
        conv_state     = session_state.get("conversation_state", ConvState.DISCOVERY)
        budget         = session_state.get("budget_limit_paise")
        decision_log   = []
        remove_skus    = []

        # ── 0. Budget REMOVAL (must run before SET so "remove budget ₹500" → remove) ──
        if self._wants_budget_removed(text) and session_id:
            if budget is None:
                return AgentResponse(
                    reply="You don't have a spending limit set right now — feel free to browse! 🛍️",
                    decision_log=decision_log,
                )
            session_manager.clear_budget(session_id)
            session_state["budget_limit_paise"] = None
            budget = None
            decision_log.append({"action": "CLEAR_BUDGET", "decision": "DONE"})
            return AgentResponse(
                reply="Done! Your spending limit has been removed. Shop freely! 🛍️",
                decision_log=decision_log,
            )

        # ── 1. Budget SET ────────────────────────────────────────────────────
        budget_paise = self._extract_budget(text)
        if budget_paise is not None and session_id:
            session_manager.set_budget(session_id, budget_paise)
            session_state["budget_limit_paise"] = budget_paise
            budget = budget_paise
            decision_log.append({"action": "SET_BUDGET", "amount_paise": budget_paise})
            current_total = session_manager.get_cart_total_paise(session_id)
            remaining = budget_paise - current_total
            cart_note = f" You have **{self._fmt_price(remaining)}** left to spend." if current_total > 0 else ""
            return AgentResponse(
                reply=f"Got it! I'll keep your cart under **{self._fmt_price(budget_paise)}**.{cart_note} What would you like to add?",
                decision_log=decision_log,
            )

        # ── 2. Menu / catalog request ────────────────────────────────────────
        if any(p in text for p in MENU_PHRASES):
            if session_id:
                session_manager.set_last_shown_skus(session_id, [p["sku"] for p in catalog_service.list_products()])
            return AgentResponse(reply=self._menu_text(), decision_log=decision_log)

        # ── 3. Help ──────────────────────────────────────────────────────────
        if any(p in text for p in HELP_PHRASES):
            return AgentResponse(
                reply=(
                    "I'm your AI barista ☕ Here's what I can do:\n\n"
                    "• **Browse**: *'Show me your menu'* or *'what filter coffee do you have?'*\n"
                    "• **Set a budget**: *'Keep it under ₹1000'* or *'budget is ₹500'*\n"
                    "• **Remove limit**: *'Remove my budget limit'*\n"
                    "• **Remove items**: *'Remove the steel filter'*\n"
                    "• **Clear cart**: *'Start over'* or *'Clear my cart'*\n"
                    "• **Checkout**: *'Proceed to pay'* or *'I'm ready'*\n\n"
                    "What would you like to do?"
                ),
                decision_log=decision_log,
            )

        # ── 4. Clear cart ────────────────────────────────────────────────────
        if any(p in text for p in CLEAR_PHRASES) and session_id:
            session_manager.clear_cart(session_id)
            decision_log.append({"action": "CLEAR_CART", "decision": "DONE"})
            return AgentResponse(
                reply="Your cart has been cleared 🗑️ Fresh start! What would you like to add?",
                decision_log=decision_log,
            )

        # ── 5. Remove item from cart ─────────────────────────────────────────
        if any(p in text for p in REMOVE_PHRASES) and cart_skus and session_id:
            # "remove everything above my limit" — budget prune
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
                new_total = session_manager.get_cart_total_paise(session_id)
                if removed_names:
                    return AgentResponse(
                        reply=f"Removed **{', '.join(removed_names)}** (over {self._fmt_price(budget)}) ✓\n"
                              f"Your cart is now **{self._fmt_price(new_total)}**.",
                        remove_from_cart_skus=remove_skus, decision_log=decision_log,
                    )
                return AgentResponse(
                    reply=f"All your items are already within **{self._fmt_price(budget)}** — nothing to remove! 👍",
                )

            # Remove a specific named item
            target_sku = self._find_remove_target(text, cart_skus)
            if target_sku:
                res = session_manager.remove_from_cart(session_id, target_sku)
                if res.get("removed"):
                    remove_skus.append(target_sku)
                    decision_log.append({"action": f"REMOVE {target_sku}", "decision": "DONE"})
                    new_total = session_manager.get_cart_total_paise(session_id)
                    state_after = session_manager.get_state(session_id)
                    remaining_items = state_after["cart_skus"]
                    cart_summary = (
                        f" Cart: {', '.join(self._product_line(s) for s in remaining_items)}" if remaining_items
                        else " Your cart is now empty."
                    )
                    return AgentResponse(
                        reply=f"Removed **{res['product_name']}** ✓{cart_summary}\n"
                              f"Cart total: **{self._fmt_price(new_total)}**",
                        remove_from_cart_skus=remove_skus, decision_log=decision_log,
                    )

        # ── 6. "yes" after checkout prompt ───────────────────────────────────
        if conv_state == ConvState.CHECKOUT_REQUESTED and any(p in text for p in ACCEPT_PHRASES):
            return AgentResponse(
                reply="Heading to checkout! 🎉 Tap **'Proceed to Pay'** below to confirm your order.",
                ready_to_checkout=True,
            )

        # ── 7. Upsell accept / decline ───────────────────────────────────────
        if pending_upsell:
            if any(p in text for p in ACCEPT_PHRASES):
                if session_id:
                    result = session_manager.add_to_cart_validated(session_id, pending_upsell)
                    if result.get("added"):
                        product = catalog_service.get_product(pending_upsell)
                        new_total = result.get("new_cart_total_paise", 0)
                        decision_log.append({"action": f"ADD {pending_upsell}", "decision": "ALLOWED"})
                        cart_info = f" Cart total: **{self._fmt_price(new_total)}**."
                        budget_info = (
                            f" (Remaining: **{self._fmt_price(result['remaining_budget_paise'])}**)"
                            if result.get("remaining_budget_paise") is not None else ""
                        )
                        return AgentResponse(
                            reply=f"Added **{product['name']}** ({self._fmt_price(product['price_paise'])}) ✓{cart_info}{budget_info}\n"
                                  f"Ready to checkout? Or looking for anything else?",
                            add_to_cart_skus=[pending_upsell], decision_log=decision_log,
                        )
                    else:
                        decision_log.append({"action": f"ADD {pending_upsell}", "decision": "BLOCKED",
                                             "reason": result.get("reason")})
                        return AgentResponse(
                            reply=f"⚠️ {result.get('message', 'Cannot add that item.')} Anything else I can help with?",
                            decision_log=decision_log,
                        )

            if any(p in text for p in DECLINE_PHRASES):
                total = session_manager.get_cart_total_paise(session_id) if session_id else 0
                return AgentResponse(
                    reply=f"No problem! 👍 Your cart total is **{self._fmt_price(total)}**. Ready to checkout when you are!",
                )

        # ── 8. Checkout intent ───────────────────────────────────────────────
        if any(p in text for p in CHECKOUT_PHRASES):
            if not cart_skus:
                return AgentResponse(
                    reply="Your cart is empty! Add something first. Try: *'Show me your menu'* 🛍️",
                )
            total = session_manager.get_cart_total_paise(session_id) if session_id else 0
            items_text = "\n".join(f"  {self._product_line(s)}" for s in cart_skus)
            return AgentResponse(
                reply=f"Here's your order summary 🧾\n\n{items_text}\n\n"
                      f"**Total: {self._fmt_price(total)}**\n\n"
                      f"Tap **'Proceed to Pay'** to confirm!",
                ready_to_checkout=True,
            )

        # ── 9. Product discovery ─────────────────────────────────────────────
        matched = self._match_products(text)

        if not matched and session_id:
            last_shown = session_manager.get_last_shown_skus(session_id)
            last_single = session_manager.get_last_single_reference(session_id)

            # "add both" / "all of them" — apply to the most recently shown listing
            if any(p in text for p in BOTH_PHRASES) and last_shown:
                added_names, added_skus = [], []
                for sku in last_shown:
                    result = session_manager.add_to_cart_validated(session_id, sku)
                    if result.get("added"):
                        added_skus.append(sku)
                        p = catalog_service.get_product(sku)
                        added_names.append(p["name"] if p else sku)
                        decision_log.append({"action": f"ADD {sku}", "decision": "ALLOWED"})
                    else:
                        decision_log.append({"action": f"ADD {sku}", "decision": "BLOCKED",
                                             "reason": result.get("reason")})
                if added_skus:
                    new_total = session_manager.get_cart_total_paise(session_id)
                    return AgentResponse(
                        reply=f"Added **{', '.join(added_names)}** ✓\nCart total: **{self._fmt_price(new_total)}**.",
                        add_to_cart_skus=added_skus, decision_log=decision_log,
                    )
                return AgentResponse(
                    reply="I couldn't add those — check if they fit your budget or are already in your cart.",
                    decision_log=decision_log,
                )

            # Ordinal reference — "the first one" / "the second one" against the last listing shown
            for word, idx in ORDINAL_PATTERNS.items():
                if word in text and last_shown and idx < len(last_shown):
                    matched = [last_shown[idx]]
                    break

            # Bare reference — "it" / "that" / "add it" -> most recently discussed product
            if not matched and last_single and any(p in text for p in BARE_REFERENCE_PHRASES):
                matched = [last_single]

        if not matched:
            # Friendly fallback with gentle prompts
            greetings = ["hi", "hello", "hey", "namaste", "hola", "good morning",
                         "good evening", "good afternoon", "howdy"]
            if any(g in text for g in greetings):
                return AgentResponse(
                    reply=(
                        "Hey there! ☕ Welcome to Kaapi Roasters!\n\n"
                        "I can help you find the perfect coffee. What are you in the mood for?\n\n"
                        "• *'Show me your full menu'*\n"
                        "• *'I want good filter coffee'*\n"
                        "• *'Something for pour-over brewing'*\n"
                        "• *'Keep it under ₹500'*"
                    ),
                )
            return AgentResponse(
                reply=(
                    "Hmm, I didn't catch that ☕ Here are some things you can try:\n\n"
                    "• *'Show me your menu'* — see everything we have\n"
                    "• *'I want filter coffee'* — our bestseller\n"
                    "• *'Something for pour-over'* — specialty brewing\n"
                    "• *'Keep my cart under ₹800'* — set a budget\n"
                    "• *'Help'* — full guide"
                ),
            )

        primary_sku = matched[0]
        product = catalog_service.get_product(primary_sku)
        emoji = PRODUCT_EMOJI.get(product["category"], "🛍️")

        # Backend-validated add
        add_skus = []
        if session_id:
            result = session_manager.add_to_cart_validated(session_id, primary_sku)
            if result.get("added"):
                add_skus = [primary_sku]
                new_total = result.get("new_cart_total_paise", 0)
                decision_log.append({"action": f"ADD {primary_sku}", "decision": "ALLOWED",
                                     "new_total_paise": new_total})
                session_manager.set_last_single_reference(session_id, primary_sku)
                budget_note = ""
                if result.get("remaining_budget_paise") is not None:
                    budget_note = f" You have **{self._fmt_price(result['remaining_budget_paise'])}** left in your budget."
                reply = (
                    f"{emoji} Added **{product['name']}** ({self._fmt_price(product['price_paise'])}) to your cart ✓\n"
                    f"_{product['description']}_\n\n"
                    f"Cart total: **{self._fmt_price(new_total)}**.{budget_note}"
                )
            else:
                decision_log.append({"action": f"ADD {primary_sku}", "decision": "BLOCKED",
                                     "reason": result.get("reason")})
                session_manager.set_last_single_reference(session_id, primary_sku)
                pname = product["name"]
                return AgentResponse(
                    reply=f"⚠️ {result.get('message', f'{pname} cannot be added.')}",
                    decision_log=decision_log,
                )
        else:
            add_skus = [primary_sku]
            reply = (
                f"{emoji} **{product['name']}** — {self._fmt_price(product['price_paise'])}\n"
                f"_{product['description']}_"
            )
            if session_id:
                session_manager.set_last_single_reference(session_id, primary_sku)

        # ── 10. Upsell — budget-checked, contextual, with proper copy ────────
        upsell_sku, upsell_reason = None, ""
        upsell = catalog_service.get_upsell_for(primary_sku)
        if upsell and upsell["sku"] not in upsell_offered and upsell["sku"] not in cart_skus:
            # Check budget compatibility
            current_total = session_manager.get_cart_total_paise(session_id) if session_id else 0
            if budget is not None and current_total + upsell["price_paise"] > budget:
                upsell = None  # Over budget — don't offer
            if upsell:
                upsell_sku = upsell["sku"]
                upsell_reason = self._get_upsell_pitch(primary_sku, upsell_sku)
                reply += f"\n\n{upsell_reason}"
                if session_id:
                    session_manager.set_last_single_reference(session_id, upsell_sku)

        return AgentResponse(
            reply=reply,
            add_to_cart_skus=add_skus,
            upsell_sku=upsell_sku,
            upsell_reason=upsell_reason,
            decision_log=decision_log,
        )


mock_shopping_agent = MockShoppingAgent()
