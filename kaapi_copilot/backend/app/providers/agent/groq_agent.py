"""
GroqShoppingAgent -- real conversational brain using Groq's OpenAI-compatible
Chat Completions API with tool calling.

Key safety design:
  - add_to_cart calls session_manager.add_to_cart_validated() — budget checked server-side
  - remove_from_cart calls session_manager.remove_from_cart() — actual cart mutation
  - set_budget calls session_manager.set_budget() — stored outside LLM memory
  - The LLM CANNOT bypass the policy engine; tool results are authoritative

Activate with:
    AGENT_MODE=groq
    GROQ_API_KEY=...
"""
import json
from typing import Optional
from app.providers.agent.base import ShoppingAgent, AgentResponse
from app.services.catalog_service import catalog_service
from app.services.session_manager import session_manager
from app.services.audit_trail import audit_trail
from app.core.config import settings

SYSTEM_PROMPT = """You are Kaapi Copilot, a friendly and expert AI barista for Kaapi Roasters (D2C filter coffee brand).

HARD RULES — never break these:
1. Only mention products/prices returned by search_catalog or confirmed by add_to_cart tool results.
2. Propose AT MOST ONE upsell per turn (via propose_upsell tool), only if it's a catalog-defined pair. Explain warmly why they pair well.
3. When the buyer mentions a spending limit (e.g. "don't spend more than ₹500", "budget is ₹800"), call set_budget immediately.
4. When the buyer asks to remove, lift, or clear their budget limit (e.g. "remove limit", "no budget", "clear spending limit", "unlimited"), call clear_budget immediately.
5. When add_to_cart returns {"added": false, "reason": "ITEM_EXCEEDS_BUDGET"}, explain clearly that the item cannot be added because it exceeds their budget. Do NOT retry or try a different item without asking.
6. When the buyer says "remove", "discard", "take out", or "clear" referring to something IN their cart, call remove_from_cart. Do NOT ask for an order number or email — it is a cart operation, not a subscription cancellation.
7. When the buyer says "yes" or "confirm" after you asked if they want to checkout, call ready_to_checkout immediately.
8. Never claim an item is in the cart unless add_to_cart returned {"added": true}.
9. Use view_cart before making claims about cart contents.
10. For "remove everything above ₹X": call view_cart, then call remove_from_cart for each item above the limit.
"""

TOOLS = [
    {"type": "function", "function": {
        "name": "search_catalog",
        "description": "Search the product catalog by keyword. Always use this before recommending a product.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    }},
    {"type": "function", "function": {
        "name": "add_to_cart",
        "description": "Add a product to the buyer's cart. The backend will check the budget and reject if it would exceed the limit. Check the 'added' field in the response.",
        "parameters": {"type": "object", "properties": {"sku": {"type": "string"}}, "required": ["sku"]},
    }},
    {"type": "function", "function": {
        "name": "remove_from_cart",
        "description": "Remove a product from the buyer's cart. Use this when buyer says 'remove', 'discard', 'take out' about an item currently in their cart.",
        "parameters": {"type": "object", "properties": {"sku": {"type": "string"}}, "required": ["sku"]},
    }},
    {"type": "function", "function": {
        "name": "set_budget",
        "description": "Store the buyer's spending limit. Call this immediately when the buyer mentions any spending limit or budget.",
        "parameters": {"type": "object", "properties": {
            "amount_inr": {"type": "number", "description": "Budget amount in Indian Rupees (e.g. 500 for ₹500)"},
        }, "required": ["amount_inr"]},
    }},
    {"type": "function", "function": {
        "name": "clear_budget",
        "description": "Remove the buyer's spending limit, allowing unconstrained shopping. Call when buyer asks to remove or clear their budget limit.",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "view_cart",
        "description": "View current cart contents with prices and total. Use before checkout or when asked about cart.",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "clear_cart",
        "description": "Remove all items from the cart.",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "propose_upsell",
        "description": "Propose exactly one catalog-defined upsell pairing for a SKU already in cart.",
        "parameters": {"type": "object", "properties": {"sku": {"type": "string"}}, "required": ["sku"]},
    }},
    {"type": "function", "function": {
        "name": "ready_to_checkout",
        "description": "Signal that the buyer is ready to proceed to payment. Call when buyer says 'checkout', 'pay', 'proceed', or 'yes' after being asked about checkout.",
        "parameters": {"type": "object", "properties": {}},
    }},
]


class GroqShoppingAgent(ShoppingAgent):
    name = "groq"

    def __init__(self, api_key: str, model: str):
        try:
            from groq import Groq
        except ImportError as e:
            raise RuntimeError("groq SDK not installed. Run: pip install groq") from e
        self.client = Groq(api_key=api_key)
        self.model = model

    def _run_tool(self, tool_name: str, tool_input: dict, session_state: Optional[dict] = None,
                  session_id: Optional[str] = None) -> dict:
        if tool_name == "search_catalog":
            results = catalog_service.search(tool_input.get("query", ""))
            if session_id:
                audit_trail.log("PRODUCT_SEARCHED", session_id, {"query": tool_input.get("query", ""), "results_count": len(results)})
            return {"results": results}

        if tool_name == "add_to_cart":
            if session_id:
                # Route through backend validation — budget checked here, not by LLM
                return session_manager.add_to_cart_validated(session_id, tool_input["sku"])
            # Fallback (no session context — shouldn't happen in Journey A)
            return {"added": True, "product": catalog_service.get_product(tool_input["sku"])}

        if tool_name == "remove_from_cart":
            if session_id:
                return session_manager.remove_from_cart(session_id, tool_input["sku"])
            return {"removed": False, "reason": "NO_SESSION"}

        if tool_name == "set_budget":
            amount_inr = tool_input.get("amount_inr", 0)
            amount_paise = int(amount_inr * 100)
            if session_id:
                session_manager.set_budget(session_id, amount_paise)
            budget = session_state.get("budget_limit_paise") if session_state else None
            return {
                "budget_set": True,
                "amount_paise": amount_paise,
                "amount_inr": amount_inr,
                "message": f"Budget set to ₹{amount_inr:.0f}. I'll make sure your cart stays within this limit.",
            }

        if tool_name == "clear_budget":
            if session_id:
                session_manager.clear_budget(session_id)
            return {
                "budget_cleared": True,
                "message": "Spending limit removed. The buyer can now add items freely.",
            }

        if tool_name == "view_cart":
            cart_skus = (session_state or {}).get("cart_skus", [])
            items = []
            total = 0
            for sku in cart_skus:
                p = catalog_service.get_product(sku)
                if p:
                    items.append({"sku": sku, "name": p["name"], "price_paise": p["price_paise"]})
                    total += p["price_paise"]
            budget = (session_state or {}).get("budget_limit_paise")
            return {
                "cart_skus": cart_skus,
                "items": items,
                "total_paise": total,
                "budget_limit_paise": budget,
                "remaining_budget_paise": (budget - total) if budget is not None else None,
            }

        if tool_name == "clear_cart":
            if session_id:
                return session_manager.clear_cart(session_id)
            return {"cleared": True, "cart_skus": []}

        if tool_name == "propose_upsell":
            sku = tool_input["sku"]
            upsell = catalog_service.get_upsell_for(sku)
            if upsell and session_id:
                # Check if upsell fits budget
                budget = (session_state or {}).get("budget_limit_paise")
                if budget is not None:
                    current_total = session_manager.get_cart_total_paise(session_id)
                    if current_total + upsell["price_paise"] > budget:
                        return {"upsell": None, "reason": "UPSELL_EXCEEDS_BUDGET",
                                "message": f"The suggested add-on ({upsell['name']}) would exceed your budget."}
            return {"upsell": upsell}

        if tool_name == "ready_to_checkout":
            if session_id:
                from app.services.session_manager import ConvState
                session_manager.set_conversation_state(session_id, ConvState.CHECKOUT_REQUESTED)
            return {"ok": True}

        return {"error": f"unknown tool {tool_name}"}

    def handle_turn(self, session_state: dict, user_message: str,
                    session_id: Optional[str] = None) -> AgentResponse:
        # Build context-aware system message including live budget/cart info
        budget = session_state.get("budget_limit_paise")
        cart_skus = session_state.get("cart_skus", [])
        conv_state = session_state.get("conversation_state", "DISCOVERY")

        context_note = ""
        if budget is not None:
            context_note += f"\nActive buyer budget: \u20b9{budget/100:.0f} (enforce strictly)."
        if cart_skus:
            context_note += f"\nCurrent cart SKUs: {cart_skus}."
        if conv_state == "CHECKOUT_REQUESTED":
            context_note += "\nBuyer previously indicated readiness to checkout. If they say 'yes' or 'confirm', call ready_to_checkout."

        system_content = SYSTEM_PROMPT + context_note

        messages = [
            {"role": "system", "content": system_content},
            {"role": "user", "content": user_message},
        ]
        add_to_cart_skus, remove_from_cart_skus = [], []
        upsell_sku, upsell_reason, ready = None, "", False
        upsell_count = 0
        decision_log = []  # For explainability

        for _ in range(10):  # bounded tool loop
            resp = self.client.chat.completions.create(
                model=self.model, messages=messages, tools=TOOLS, max_tokens=1024,
            )
            msg = resp.choices[0].message
            tool_calls = getattr(msg, "tool_calls", None)

            # Build clean assistant message (no extra fields that break Groq validation)
            assistant_msg = {"role": "assistant"}
            if msg.content:
                assistant_msg["content"] = msg.content
            if tool_calls:
                assistant_msg["tool_calls"] = [
                    {"id": tc.id, "type": "function",
                     "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                    for tc in tool_calls
                ]
            messages.append(assistant_msg)

            if not tool_calls:
                return AgentResponse(
                    msg.content or "", add_to_cart_skus, upsell_sku, upsell_reason, ready,
                    remove_from_cart_skus=remove_from_cart_skus, decision_log=decision_log,
                )

            for call in tool_calls:
                tool_name = call.function.name
                try:
                    tool_input = json.loads(call.function.arguments or "{}")
                except json.JSONDecodeError:
                    tool_input = {}

                result = self._run_tool(tool_name, tool_input, session_state, session_id)

                # Track adds
                if tool_name == "add_to_cart":
                    if result.get("added"):
                        sku = tool_input["sku"]
                        if sku not in add_to_cart_skus:
                            add_to_cart_skus.append(sku)
                        decision_log.append({
                            "action": f"ADD {tool_input['sku']}",
                            "decision": "ALLOWED",
                            "new_total_paise": result.get("new_cart_total_paise"),
                            "budget_paise": result.get("budget_limit_paise"),
                        })
                    else:
                        decision_log.append({
                            "action": f"ADD {tool_input['sku']}",
                            "decision": "BLOCKED",
                            "reason": result.get("reason"),
                            "item_price_paise": result.get("item_price_paise"),
                            "budget_paise": result.get("budget_limit_paise"),
                        })

                # Track removes
                if tool_name == "remove_from_cart" and result.get("removed"):
                    sku = tool_input["sku"]
                    if sku not in remove_from_cart_skus:
                        remove_from_cart_skus.append(sku)
                    decision_log.append({"action": f"REMOVE {sku}", "decision": "DONE"})

                # Track budget sets
                if tool_name == "set_budget":
                    # Refresh session_state budget in this turn
                    session_state["budget_limit_paise"] = result.get("amount_paise")
                    decision_log.append({
                        "action": "SET_BUDGET",
                        "amount_paise": result.get("amount_paise"),
                    })

                # Track upsells
                if tool_name == "propose_upsell" and result.get("upsell") and upsell_count < settings.max_upsells_per_turn:
                    upsell = result["upsell"]
                    upsell_sku = upsell["sku"]
                    upsell_reason = f"Catalog-defined pairing: {upsell['name']}."
                    upsell_count += 1

                if tool_name == "ready_to_checkout":
                    ready = True

                messages.append({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "name": tool_name,
                    "content": json.dumps(result),
                })

        # Final completion after max tool loops
        try:
            final_resp = self.client.chat.completions.create(
                model=self.model,
                messages=messages + [{"role": "user",
                                       "content": "Give a brief, friendly summary of what happened."}],
                max_tokens=512,
            )
            final_text = final_resp.choices[0].message.content or "Here is your cart summary. Ready to proceed whenever you are!"
        except Exception:
            final_text = "Here is your cart summary. Ready to proceed whenever you are!"

        return AgentResponse(
            final_text, add_to_cart_skus, upsell_sku, upsell_reason, ready,
            remove_from_cart_skus=remove_from_cart_skus, decision_log=decision_log,
        )
