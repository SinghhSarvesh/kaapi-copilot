"""
Kaapi Copilot FastAPI backend. One app, all endpoints:
  Journey A:  /api/chat, /api/mandates/*, /api/checkout, /api/webhooks/razorpay
  Ops:        /api/audit, /api/analytics
  Journey B:  /api/mcp/* (MCP-style tool surface for an external AI buyer agent)

Run with: uvicorn app.api.main:app --reload --port 8000
"""
import asyncio
import json
import logging
import time
from typing import Optional, List, AsyncGenerator
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.core.config import settings
from app.services.catalog_service import catalog_service
from app.services.session_manager import session_manager
from app.services.mandate_engine import mandate_engine, GuardrailError
from app.services.order_service import order_service
from app.services.audit_trail import audit_trail
from app.services.analytics_service import analytics_service
from app.providers.agent.factory import get_shopping_agent
from app.providers.payment.factory import get_payment_provider
from app.providers.payment.mock import mock_payment_provider

app = FastAPI(title="Kaapi Copilot")
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

request_logger = logging.getLogger("kaapi.requests")
request_logger.setLevel(logging.INFO)
if not request_logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(message)s"))
    request_logger.addHandler(_handler)


@app.middleware("http")
async def request_observability_middleware(request: Request, call_next):
    """Structured request/latency logging. Emits one JSON line per request to
    stdout so any host (Railway, etc.) captures it as a log without extra infra.
    Never blocks or mutates the response -- purely observational."""
    start = time.perf_counter()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        return response
    finally:
        duration_ms = round((time.perf_counter() - start) * 1000, 2)
        request_logger.info(json.dumps({
            "event": "http_request",
            "method": request.method,
            "path": request.url.path,
            "status_code": status_code,
            "duration_ms": duration_ms,
        }))

# MCP (Journey B) cart helpers — backed by the session_state SQLite table
# (same db that MandateEngine uses; table is guaranteed created by mandate_engine init).
def _mcp_get_cart(session_id: str) -> list:
    import json, sqlite3
    from app.core.config import settings
    with sqlite3.connect(settings.db_path) as c:
        row = c.execute("SELECT cart_skus FROM session_state WHERE session_id=?",
                        (session_id,)).fetchone()
    return json.loads(row[0]) if row else []


def _mcp_set_cart(session_id: str, skus: list) -> None:
    import json, sqlite3
    from app.core.config import settings
    with sqlite3.connect(settings.db_path) as c:
        c.execute("""INSERT INTO session_state (session_id, spend_paise, cart_skus)
                     VALUES (?, 0, ?)
                     ON CONFLICT(session_id) DO UPDATE SET cart_skus = excluded.cart_skus""",
                  (session_id, json.dumps(skus)))


class ChatRequest(BaseModel):
    session_id: Optional[str] = None
    buyer_ref: str = "buyer_web"
    message: str
    conversation_history: List[dict] = []


class ConfirmRequest(BaseModel):
    mandate_id: str
    method: str = "buyer_tap"


class CheckoutRequest(BaseModel):
    mandate_id: str


class McpCartRequest(BaseModel):
    session_id: str
    sku: str


class McpRemoveRequest(BaseModel):
    session_id: str
    sku: str


class McpMandateRequest(BaseModel):
    session_id: str
    buyer_ref: str


class McpConfirmRequest(BaseModel):
    mandate_id: str


class SetBudgetRequest(BaseModel):
    session_id: str
    amount_inr: float


class BuildMandateRequest(BaseModel):
    session_id: str
    buyer_ref: str = "buyer_web"


@app.get("/")
def root():
    """Root health-check endpoint (used by Railway's health monitor)."""
    return {"status": "ok", "service": "Kaapi Copilot API", **settings.summary()}


@app.get("/api/health")
def health():
    return {"status": "ok", **settings.summary()}


# ---------------- Shared chat helper ----------------
def _execute_chat(req: ChatRequest):
    """Core chat logic shared between /api/chat and /api/chat/stream."""
    session_id = req.session_id or session_manager.create_session(req.buyer_ref)
    try:
        state = session_manager.get_state(session_id)
    except KeyError:
        raise HTTPException(404, f"Session '{session_id}' not found. Start a new session by omitting session_id.")
    audit_trail.log("chat_message_received", session_id, {"buyer_ref": req.buyer_ref, "message": req.message})

    # Retrieve server-side history; if client sent history use it as fallback for new sessions
    server_history = session_manager.get_history(session_id, max_turns=20)
    history = server_history if server_history else req.conversation_history

    agent = get_shopping_agent()
    response = agent.handle_turn(state, req.message, session_id=session_id,
                                  conversation_history=history)

    # Persist this turn to server-side history
    session_manager.append_to_history(session_id, "user", req.message)
    session_manager.append_to_history(session_id, "assistant", response.reply)

    session_manager.apply_agent_response(session_id, response)
    audit_trail.log("agent_turn", session_id, {
        "reply": response.reply,
        "added": response.add_to_cart_skus,
        "removed": response.remove_from_cart_skus,
        "upsell_sku": response.upsell_sku,
        "ready_to_checkout": response.ready_to_checkout,
        "decision_log": response.decision_log,
    })

    # Log UPSELL_SUGGESTED when the agent proposes a complementary product.
    # This event is the canonical source for the upsell_attach_rate dashboard metric.
    if response.upsell_sku:
        state_for_upsell = session_manager.get_state(session_id)
        budget_for_upsell = state_for_upsell.get("budget_limit_paise")
        upsell_price = catalog_service.get_price_paise(response.upsell_sku)
        cart_total_for_upsell = session_manager.get_cart_total_paise(session_id)
        within_budget = (
            budget_for_upsell is None
            or (upsell_price is not None and (cart_total_for_upsell + upsell_price) <= budget_for_upsell)
        )
        audit_trail.log("UPSELL_SUGGESTED", session_id, {
            "suggested_sku": response.upsell_sku,
            "trigger_skus": response.add_to_cart_skus,
            "within_budget": within_budget,
            "upsell_price_paise": upsell_price,
            "budget_limit_paise": budget_for_upsell,
        })

    # Re-read authoritative state after all mutations
    state = session_manager.get_state(session_id)
    cart_total = session_manager.get_cart_total_paise(session_id)
    budget = state.get("budget_limit_paise")

    return {
        "session_id": session_id,
        "reply": response.reply,
        "cart_skus": state["cart_skus"],
        "cart_total_paise": cart_total,
        "budget_limit_paise": budget,
        "remaining_budget_paise": (budget - cart_total) if budget is not None else None,
        "upsell_sku": response.upsell_sku,
        "upsell_reason": response.upsell_reason,
        "ready_to_checkout": response.ready_to_checkout,
        "conversation_state": state.get("conversation_state"),
        "decision_log": response.decision_log,
    }


# ---------------- Journey A: conversational chat ----------------
@app.post("/api/chat")
def chat(req: ChatRequest):
    return _execute_chat(req)


@app.post("/api/chat/stream")
async def chat_stream(req: ChatRequest):
    """SSE streaming endpoint. Computes the full response synchronously (preserving
    all guardrail logic), then streams the reply text token-by-token so the frontend
    can render words as they arrive. Ends with a [DONE] event containing full payload."""

    async def event_generator() -> AsyncGenerator[str, None]:
        try:
            # Run sync agent logic in a thread to avoid blocking the event loop
            loop = asyncio.get_event_loop()
            data = await loop.run_in_executor(None, _execute_chat, req)
        except HTTPException as exc:
            err = json.dumps({"type": "error", "message": exc.detail, "status": exc.status_code})
            yield f"data: {err}\n\n"
            return
        except Exception as exc:
            err = json.dumps({"type": "error", "message": str(exc)})
            yield f"data: {err}\n\n"
            return

        # Stream reply text word-by-word with small delays for perceived streaming
        reply_text = data.get("reply", "")
        words = reply_text.split(" ")
        chunk = ""
        for i, word in enumerate(words):
            chunk += word
            if i < len(words) - 1:
                chunk += " "
            # Emit every 2 words to reduce overhead while still feeling streamed
            if (i % 2 == 1) or (i == len(words) - 1):
                token_event = json.dumps({"type": "token", "text": chunk})
                yield f"data: {token_event}\n\n"
                chunk = ""
                await asyncio.sleep(0.022)  # ~22ms between chunks → ~45 chunks/sec

        # Final event with complete structured payload (cart, guardrail data, etc.)
        done_payload = {**data, "type": "done"}
        yield f"data: {json.dumps(done_payload)}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",   # disable Nginx buffering
            "Connection": "keep-alive",
        },
    )


@app.post("/api/session/set_budget")
def set_budget(req: SetBudgetRequest):
    """Explicitly set (or clear if amount_inr=0) a spending budget for a session."""
    try:
        session_manager.get_state(req.session_id)
    except KeyError:
        raise HTTPException(404, f"Session '{req.session_id}' not found.")
    if req.amount_inr <= 0:
        session_manager.clear_budget(req.session_id)
        return {"budget_set": False, "budget_cleared": True, "amount_inr": 0}
    amount_paise = int(req.amount_inr * 100)
    session_manager.set_budget(req.session_id, amount_paise)
    return {"budget_set": True, "amount_paise": amount_paise, "amount_inr": req.amount_inr}


@app.post("/api/session/clear_budget")
def clear_budget_endpoint(session_id: str):
    """Remove the active spending limit from a session."""
    try:
        session_manager.get_state(session_id)
    except KeyError:
        raise HTTPException(404, f"Session '{session_id}' not found.")
    session_manager.clear_budget(session_id)
    return {"budget_cleared": True}


@app.post("/api/cart/remove")
def remove_from_cart(session_id: str, sku: str):
    """Explicitly remove an item from the cart (can also be done via chat)."""
    try:
        session_manager.get_state(session_id)
    except KeyError:
        raise HTTPException(404, f"Session '{session_id}' not found.")
    result = session_manager.remove_from_cart(session_id, sku)
    if not result.get("removed"):
        raise HTTPException(400, result.get("reason", "Item not in cart"))
    total = session_manager.get_cart_total_paise(session_id)
    return {**result, "cart_total_paise": total}


@app.post("/api/cart/clear")
def clear_cart(session_id: str):
    """Clear all items from the cart."""
    try:
        session_manager.get_state(session_id)
    except KeyError:
        raise HTTPException(404, f"Session '{session_id}' not found.")
    return session_manager.clear_cart(session_id)


@app.post("/api/mandates/build")
def build_mandate(req: BuildMandateRequest):
    if not req.session_id:
        raise HTTPException(400, "session_id required")
    try:
        state = session_manager.get_state(req.session_id)
    except KeyError:
        raise HTTPException(404, f"Session '{req.session_id}' not found.")
    mandate = mandate_engine.build_mandate(
        req.session_id, req.buyer_ref, state["cart_skus"],
        rationale=f"Buyer-confirmed cart built via Journey A chat for {req.buyer_ref}.",
        session_budget_paise=state.get("budget_limit_paise"),
    )
    order_service.register_mandate(mandate)
    return mandate.to_dict()


@app.post("/api/mandates/confirm")
def confirm_mandate(req: ConfirmRequest):
    mandate = order_service.get_mandate(req.mandate_id)
    if not mandate:
        raise HTTPException(404, "mandate not found")
    try:
        mandate = mandate_engine.confirm_mandate(mandate, method=req.method)
    except GuardrailError as e:
        raise HTTPException(400, str(e))
    return mandate.to_dict()


@app.post("/api/checkout")
def checkout(req: CheckoutRequest):
    mandate = order_service.get_mandate(req.mandate_id)
    if not mandate:
        raise HTTPException(404, "mandate not found")
    try:
        order = order_service.checkout(mandate)
    except GuardrailError as e:
        raise HTTPException(400, str(e))
    return order.to_dict()


# ---------------- Webhooks ----------------
@app.post("/api/webhooks/razorpay")
async def razorpay_webhook(request: Request):
    body = await request.body()
    signature = request.headers.get("X-Razorpay-Signature", "")
    if settings.payment_mode == "razorpay":
        from app.providers.payment.razorpay_provider import verify_webhook_signature
        if not verify_webhook_signature(body, signature, settings.razorpay_webhook_secret):
            audit_trail.log("webhook_signature_invalid", "unknown", {"signature": signature})
            raise HTTPException(400, "invalid signature")
    try:
        payload = await request.json()
        event = payload["event"]
        event_payload = payload["payload"]
    except (ValueError, KeyError, TypeError):
        audit_trail.log("webhook_malformed", "unknown", {})
        raise HTTPException(400, "malformed webhook payload")
    try:
        order = order_service.handle_webhook_event(event, event_payload, payload.get("id"))
    except ValueError:
        raise HTTPException(202, "webhook received for unknown order")
    return {"status": "processed", "order_id": order.order_id, "order_status": order.status}


@app.post("/api/demo/trigger-webhook")
def trigger_demo_webhook(order_id: str, outcome: str = "success"):
    """Demo-only: simulate a Razorpay webhook without a real payment."""
    order = order_service.get_order(order_id)
    if not order:
        raise HTTPException(404, "order not found")
    if order.payment_link_id is None:
        raise HTTPException(500, "order has no payment link")
    wh = mock_payment_provider.simulate_webhook(
        order.payment_link_id, outcome, order_id=order.order_id, amount_paise=order.total_paise
    )
    updated = order_service.handle_webhook_event(wh["event"], wh["payload"])
    return updated.to_dict()


# ---------------- Ops: audit + analytics ----------------
@app.get("/api/audit")
def get_audit(session_id: Optional[str] = None, limit: int = 200):
    return {"events": audit_trail.list_events(session_id, limit), "chain": audit_trail.verify_chain()}


@app.get("/api/analytics")
def get_analytics():
    return analytics_service.summary()


# ---------------- Journey B: MCP-style tool surface ----------------
@app.get("/api/mcp/list_products")
def mcp_list_products(category: Optional[str] = None):
    return {"products": catalog_service.list_products(category)}


@app.get("/api/mcp/get_product")
def mcp_get_product(sku: str):
    product = catalog_service.get_product(sku)
    if not product:
        raise HTTPException(404, "sku not found")
    return product


@app.post("/api/mcp/add_to_cart")
def mcp_add_to_cart(req: McpCartRequest):
    """
    MCP add-to-cart with full backend budget enforcement.
    External AI agents cannot bypass the policy engine here.
    """
    # If session doesn't exist in session_manager, create a new one
    if req.session_id not in session_manager._sessions:
        session_manager._sessions[req.session_id] = {
            "buyer_ref": "external_ai_agent",
            "cart_skus": [],
            "upsell_offered": [],
            "pending_upsell": None,
            "history": [],
            "upsells_accepted": 0,
            "upsells_offered_count": 0,
            "budget_limit_paise": None,
            "conversation_state": "DISCOVERY",
        }
    # Never overwrite an existing session — it may have a budget set

    result = session_manager.add_to_cart_validated(req.session_id, req.sku)
    if not result.get("added"):
        raise HTTPException(400, result.get("message", result.get("reason", "Cannot add item")))

    state = session_manager.get_state(req.session_id)
    _mcp_set_cart(req.session_id, state["cart_skus"])
    return {
        "cart_skus": state["cart_skus"],
        "new_cart_total_paise": result.get("new_cart_total_paise"),
        "budget_limit_paise": result.get("budget_limit_paise"),
    }


@app.post("/api/mcp/remove_from_cart")
def mcp_remove_from_cart(req: McpRemoveRequest):
    """
    MCP remove-from-cart. Journey B agents can now remove items.
    """
    try:
        session_manager.get_state(req.session_id)
    except KeyError:
        raise HTTPException(404, f"Session '{req.session_id}' not found.")
    result = session_manager.remove_from_cart(req.session_id, req.sku)
    if not result.get("removed"):
        raise HTTPException(400, result.get("message", "Item not in cart"))
    state = session_manager.get_state(req.session_id)
    _mcp_set_cart(req.session_id, state["cart_skus"])
    total = session_manager.get_cart_total_paise(req.session_id)
    return {
        "removed": True,
        "sku": req.sku,
        "updated_cart": state["cart_skus"],
        "updated_total_paise": total,
    }


@app.get("/api/mcp/get_cart")
def mcp_get_cart(session_id: str):
    try:
        state = session_manager.get_state(session_id)
        cart_skus = state["cart_skus"]
    except KeyError:
        cart_skus = _mcp_get_cart(session_id)
    items = [catalog_service.get_product(s) for s in cart_skus]
    valid_items = [i for i in items if i is not None]
    total = sum(i["price_paise"] for i in valid_items)
    budget = None
    try:
        budget = session_manager.get_state(session_id).get("budget_limit_paise")
    except KeyError:
        pass
    return {
        "cart_skus": cart_skus,
        "items": valid_items,
        "total_paise": total,
        "budget_limit_paise": budget,
        "remaining_budget_paise": (budget - total) if budget is not None else None,
    }


@app.post("/api/mcp/create_checkout_mandate")
def mcp_create_checkout_mandate(req: McpMandateRequest):
    try:
        state = session_manager.get_state(req.session_id)
        cart = state["cart_skus"]
        budget = state.get("budget_limit_paise")
    except KeyError:
        cart = _mcp_get_cart(req.session_id)
        budget = None
    if not cart:
        raise HTTPException(400, "cart is empty")
    mandate = mandate_engine.build_mandate(
        req.session_id, req.buyer_ref, cart,
        rationale="External AI buyer agent (Journey B) requested checkout via MCP tools.",
        session_budget_paise=budget,
    )
    order_service.register_mandate(mandate)
    # Clear the cart after mandate is captured
    try:
        session_manager.clear_cart(req.session_id)
    except KeyError:
        pass
    _mcp_set_cart(req.session_id, [])
    return mandate.to_dict()


@app.post("/api/mcp/confirm_and_pay")
def mcp_confirm_and_pay(req: McpConfirmRequest):
    """The MCP equivalent of the buyer's tap — explicit confirmation with no human present."""
    mandate = order_service.get_mandate(req.mandate_id)
    if not mandate:
        raise HTTPException(404, "mandate not found")
    try:
        mandate = mandate_engine.confirm_mandate(mandate, method="mcp_confirm_and_pay")
        order = order_service.checkout(mandate)
    except GuardrailError as e:
        raise HTTPException(400, str(e))
    return {"mandate": mandate.to_dict(), "order": order.to_dict()}
