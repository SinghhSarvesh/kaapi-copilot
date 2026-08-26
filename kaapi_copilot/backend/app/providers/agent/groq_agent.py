"""
GroqShoppingAgent -- real conversational brain using Groq's OpenAI-compatible
Chat Completions API with tool calling. Implements the same ShoppingAgent
interface as the mock.

System prompt hard constraints:
  - at most ONE upsell suggestion per turn, and only if catalog upsell_pairs says so
  - never state a price/product not returned by a tool call

Activate with:
    AGENT_MODE=groq
    GROQ_API_KEY=...
"""
import json
from typing import Optional
from app.providers.agent.base import ShoppingAgent, AgentResponse
from app.services.catalog_service import catalog_service
from app.core.config import settings

SYSTEM_PROMPT = """You are Kaapi Copilot, the shopping assistant for Kaapi Roasters (D2C filter coffee).
Rules you must never break:
- Only use products/prices returned by the search_catalog or add_to_cart tools. Never invent a product or price.
- Propose AT MOST ONE upsell per turn, and only a catalog-defined upsell pair (via propose_upsell tool), never a guess.
- Keep replies short and plain-language. When the buyer signals they are ready to pay, call ready_to_checkout.
"""

TOOLS = [
    {"type": "function", "function": {"name": "search_catalog", "description": "Search the product catalog by keyword.",
     "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {"name": "add_to_cart", "description": "Add a product SKU to the buyer's cart.",
     "parameters": {"type": "object", "properties": {"sku": {"type": "string"}}, "required": ["sku"]}}},
    {"type": "function", "function": {"name": "propose_upsell", "description": "Propose exactly one catalog-defined upsell for a SKU already in cart.",
     "parameters": {"type": "object", "properties": {"sku": {"type": "string"}}, "required": ["sku"]}}},
    {"type": "function", "function": {"name": "view_cart", "description": "View current cart contents.",
     "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "ready_to_checkout", "description": "Signal the buyer wants to proceed to payment.",
     "parameters": {"type": "object", "properties": {}}}},
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

    def _run_tool(self, tool_name: str, tool_input: dict, session_state: Optional[dict] = None) -> dict:
        if tool_name == "search_catalog":
            return {"results": catalog_service.search(tool_input.get("query", ""))}
        if tool_name == "add_to_cart":
            return {"product": catalog_service.get_product(tool_input["sku"])}
        if tool_name == "propose_upsell":
            return {"upsell": catalog_service.get_upsell_for(tool_input["sku"])}
        if tool_name == "view_cart":
            cart_skus = (session_state or {}).get("cart_skus", [])
            return {"cart_skus": cart_skus,
                    "items": [catalog_service.get_product(sku) for sku in cart_skus]}
        if tool_name == "ready_to_checkout":
            return {"ok": True}
        return {"error": f"unknown tool {tool_name}"}

    def handle_turn(self, session_state: dict, user_message: str) -> AgentResponse:
        messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user_message}]
        add_to_cart_skus, upsell_sku, upsell_reason, ready = [], None, "", False
        upsell_count = 0

        for _ in range(8):  # bounded tool loop
            resp = self.client.chat.completions.create(
                model=self.model, messages=messages, tools=TOOLS, max_tokens=1024,
            )
            msg = resp.choices[0].message
            tool_calls = getattr(msg, "tool_calls", None)

            # Build clean assistant message
            assistant_msg = {"role": "assistant"}
            if msg.content:
                assistant_msg["content"] = msg.content
            if tool_calls:
                assistant_msg["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in tool_calls
                ]
            messages.append(assistant_msg)

            if not tool_calls:
                return AgentResponse(msg.content or "", add_to_cart_skus, upsell_sku, upsell_reason, ready)

            for call in tool_calls:
                tool_name = call.function.name
                try:
                    tool_input = json.loads(call.function.arguments or "{}")
                except json.JSONDecodeError:
                    tool_input = {}
                result = self._run_tool(tool_name, tool_input, session_state)
                if tool_name == "add_to_cart" and result.get("product"):
                    sku = tool_input["sku"]
                    if sku not in add_to_cart_skus:
                        add_to_cart_skus.append(sku)
                if tool_name == "propose_upsell" and result.get("upsell") and upsell_count < settings.max_upsells_per_turn:
                    upsell_sku = result["upsell"]["sku"]
                    upsell_reason = f"Catalog-defined pair for this purchase: {result['upsell']['name']}."
                    upsell_count += 1
                if tool_name == "ready_to_checkout":
                    ready = True
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "name": tool_name,
                    "content": json.dumps(result),
                })

        # One final completion to explain the recommendations
        try:
            final_resp = self.client.chat.completions.create(
                model=self.model,
                messages=messages + [{"role": "user", "content": "Please give a brief, friendly summary of what you recommended and added to the cart."}],
                max_tokens=512,
            )
            final_text = final_resp.choices[0].message.content or "Here is your cart summary. Ready to proceed whenever you are!"
        except Exception:
            final_text = "Here is your cart summary. Ready to proceed whenever you are!"

        return AgentResponse(final_text, add_to_cart_skus, upsell_sku, upsell_reason, ready)
