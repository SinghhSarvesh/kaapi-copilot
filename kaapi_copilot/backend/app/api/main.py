"""
Kaapi Copilot FastAPI backend. One app, all endpoints:
  Journey A:  /api/chat, /api/mandates/*, /api/checkout, /api/webhooks/razorpay
  Ops:        /api/audit, /api/analytics
  Journey B:  /api/mcp/* (MCP-style tool surface for an external AI buyer agent)

Run with: uvicorn app.api.main:app --reload --port 8000
"""
from typing import Optional
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
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

# In-memory cart store for the MCP (Journey B) tool surface, keyed by mcp session id
_mcp_carts: dict = {}


class ChatRequest(BaseModel):
    session_id: Optional[str] = None
    buyer_ref: str = "buyer_web"
    message: str


class ConfirmRequest(BaseModel):
    mandate_id: str
    method: str = "buyer_tap"


class CheckoutRequest(BaseModel):
    mandate_id: str


class McpCartRequest(BaseModel):
    session_id: str
    sku: str


class McpMandateRequest(BaseModel):
    session_id: str
    buyer_ref: str


class McpConfirmRequest(BaseModel):
    mandate_id: str


@app.get("/")
def root():
    """Root health-check endpoint (used by Railway's health monitor)."""
    return {"status": "ok", "service": "Kaapi Copilot API", **settings.summary()}


@app.get("/api/health")
def health():
    return {"status": "ok", **settings.summary()}


# ---------------- Journey A: conversational chat ----------------
@app.post("/api/chat")
def chat(req: ChatRequest):
    session_id = req.session_id or session_manager.create_session(req.buyer_ref)
    try:
        state = session_manager.get_state(session_id)
    except KeyError:
        raise HTTPException(404, f"Session '{session_id}' not found. Start a new session by omitting session_id.")
    audit_trail.log("chat_message_received", session_id, {"buyer_ref": req.buyer_ref, "message": req.message})

    agent = get_shopping_agent()
    response = agent.handle_turn(state, req.message)
    session_manager.apply_agent_response(session_id, response)
    audit_trail.log("agent_turn", session_id, {
        "reply": response.reply, "added": response.add_to_cart_skus,
        "upsell_sku": response.upsell_sku, "ready_to_checkout": response.ready_to_checkout,
    })

    return {
        "session_id": session_id, "reply": response.reply,
        "cart_skus": state["cart_skus"], "upsell_sku": response.upsell_sku,
        "upsell_reason": response.upsell_reason, "ready_to_checkout": response.ready_to_checkout,
    }


@app.post("/api/mandates/build")
def build_mandate(req: ChatRequest):
    if not req.session_id:
        raise HTTPException(400, "session_id required")
    try:
        state = session_manager.get_state(req.session_id)
    except KeyError:
        raise HTTPException(404, f"Session '{req.session_id}' not found.")
    mandate = mandate_engine.build_mandate(
        req.session_id, req.buyer_ref, state["cart_skus"],
        rationale=f"Buyer-confirmed cart built via Journey A chat for {req.buyer_ref}.",
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
    wh = mock_payment_provider.simulate_webhook(order.payment_link_id, outcome)
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
    if catalog_service.get_product(req.sku) is None:
        raise HTTPException(404, "sku not found")
    cart = _mcp_carts.setdefault(req.session_id, [])
    if req.sku not in cart:
        cart.append(req.sku)
    audit_trail.log("mcp_add_to_cart", req.session_id, {"sku": req.sku})
    return {"cart_skus": cart}


@app.get("/api/mcp/get_cart")
def mcp_get_cart(session_id: str):
    cart = _mcp_carts.get(session_id, [])
    items = [catalog_service.get_product(s) for s in cart]
    # Filter out any None items (SKU removed from catalog after being added to cart)
    valid_items = [i for i in items if i is not None]
    total = sum(i["price_paise"] for i in valid_items)
    return {"cart_skus": cart, "items": valid_items, "total_paise": total}


@app.post("/api/mcp/create_checkout_mandate")
def mcp_create_checkout_mandate(req: McpMandateRequest):
    cart = _mcp_carts.get(req.session_id, [])
    if not cart:
        raise HTTPException(400, "cart is empty")
    mandate = mandate_engine.build_mandate(
        req.session_id, req.buyer_ref, cart,
        rationale="External AI buyer agent (Journey B) requested checkout via MCP tools.",
    )
    order_service.register_mandate(mandate)
    # Clear the cart after the mandate is captured so the next add_to_cart starts fresh.
    _mcp_carts[req.session_id] = []
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
