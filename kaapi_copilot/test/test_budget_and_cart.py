"""
P0 budget enforcement, remove-from-cart, and conversation state regression tests.
Covers exact failure cases from the FINAL CORRECTION PROMPT requirements document.

Run from project root:
    python -m pytest kaapi_copilot/test/ -v
"""
import os
import sys
import pytest

# Make sure the backend package is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))


@pytest.fixture(autouse=True)
def fresh_services(tmp_path, monkeypatch):
    """Reload all app modules with a throwaway SQLite DB and mock providers per test."""
    for mod in list(sys.modules):
        if mod.startswith("app."):
            del sys.modules[mod]
    monkeypatch.setenv("KAAPI_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("PAYMENT_MODE", "mock")
    monkeypatch.setenv("AGENT_MODE", "mock")
    monkeypatch.setenv("ALLOWED_ORIGINS", "*")


# ── Helpers ────────────────────────────────────────────────────────────────────

def get_client():
    from fastapi.testclient import TestClient
    from app.api.main import app
    return TestClient(app)


def new_session(client, message="hello", buyer_ref="test_buyer"):
    r = client.post("/api/chat", json={"message": message, "buyer_ref": buyer_ref})
    assert r.status_code == 200, r.text
    return r.json()["session_id"]


def set_budget_api(client, session_id, amount_inr):
    r = client.post("/api/session/set_budget", json={
        "session_id": session_id, "amount_inr": amount_inr,
    })
    assert r.status_code == 200, r.text
    return r.json()


def mcp_add(client, session_id, sku):
    return client.post("/api/mcp/add_to_cart", json={"session_id": session_id, "sku": sku})


def mcp_remove(client, session_id, sku):
    return client.post("/api/mcp/remove_from_cart", json={"session_id": session_id, "sku": sku})


def mcp_cart(client, session_id):
    return client.get(f"/api/mcp/get_cart?session_id={session_id}").json()


def get_session_manager():
    from app.services.session_manager import session_manager
    return session_manager


# ── Budget enforcement ─────────────────────────────────────────────────────────

def test_add_product_under_budget():
    """Add a ₹450 product with ₹500 budget — must be allowed."""
    c = get_client()
    sess = new_session(c)
    set_budget_api(c, sess, 500)  # ₹500

    # kr-filter-500 = ₹450 → under ₹500 → ALLOWED
    r = mcp_add(c, sess, "kr-filter-500")
    assert r.status_code == 200, r.text
    assert "kr-filter-500" in r.json()["cart_skus"]


def test_add_product_exactly_at_budget():
    """Add a ₹450 product with exactly ₹450 budget — boundary: ≤ not <, so ALLOWED."""
    c = get_client()
    sess = new_session(c)
    set_budget_api(c, sess, 450)  # exactly ₹450

    # kr-filter-500 = ₹450 = exactly at limit → ALLOWED
    r = mcp_add(c, sess, "kr-filter-500")
    assert r.status_code == 200, r.text


def test_add_product_above_budget_is_blocked():
    """EXACT REGRESSION: ₹700 subscription with ₹500 budget must be BLOCKED."""
    c = get_client()
    sess = new_session(c)
    set_budget_api(c, sess, 500)  # ₹500

    # kr-subscription = ₹700 → exceeds ₹500 limit → BLOCKED
    r = mcp_add(c, sess, "kr-subscription")
    assert r.status_code == 400, f"Expected 400, got {r.status_code}: {r.text}"
    assert "ITEM_EXCEEDS_BUDGET" in r.text or "exceeding" in r.text.lower() or "exceed" in r.text.lower()


def test_add_product_causes_cumulative_total_above_budget():
    """Add ₹450 item, then ₹250 item with ₹500 budget — second must be blocked (cumulative ₹700 > ₹500)."""
    c = get_client()
    sess = new_session(c)
    set_budget_api(c, sess, 500)  # ₹500

    # First: kr-filter-500 = ₹450 → total ₹450 ≤ ₹500 → ALLOWED
    r1 = mcp_add(c, sess, "kr-filter-500")
    assert r1.status_code == 200, r1.text

    # Second: kr-filters-100 = ₹250 → total would be ₹700 > ₹500 → BLOCKED
    r2 = mcp_add(c, sess, "kr-filters-100")
    assert r2.status_code == 400, f"Expected 400, got {r2.status_code}: {r2.text}"

    # Cart must only have the first item
    cart = mcp_cart(c, sess)
    assert cart["cart_skus"] == ["kr-filter-500"]


def test_cart_unchanged_after_budget_rejection():
    """After a budget rejection, cart must be identical to before the attempt."""
    c = get_client()
    sess = new_session(c)
    set_budget_api(c, sess, 500)  # ₹500

    mcp_add(c, sess, "kr-filter-500")  # ₹450 — ok
    cart_before = mcp_cart(c, sess)

    mcp_add(c, sess, "kr-subscription")  # ₹700 — blocked
    cart_after = mcp_cart(c, sess)

    assert cart_before["cart_skus"] == cart_after["cart_skus"]
    assert cart_before["total_paise"] == cart_after["total_paise"]


def test_budget_audit_event_written_on_rejection():
    """BUDGET_CHECK_FAILED must appear in audit trail when an add is blocked."""
    c = get_client()
    sess = new_session(c)
    set_budget_api(c, sess, 500)  # ₹500

    mcp_add(c, sess, "kr-subscription")  # ₹700 — blocked

    audit = c.get(f"/api/audit?session_id={sess}").json()
    event_types = [e["event_type"] for e in audit["events"]]
    assert "BUDGET_CHECK_FAILED" in event_types, f"Events found: {event_types}"


def test_budget_set_audit_event():
    """BUDGET_SET must appear in audit trail when budget is set."""
    c = get_client()
    sess = new_session(c)
    set_budget_api(c, sess, 500)

    audit = c.get(f"/api/audit?session_id={sess}").json()
    event_types = [e["event_type"] for e in audit["events"]]
    assert "BUDGET_SET" in event_types, f"Events: {event_types}"


def test_budget_persists_across_turns():
    """Budget set in turn 1 must still be enforced in turn 3 — stored outside LLM."""
    c = get_client()
    sess = new_session(c)
    set_budget_api(c, sess, 500)  # ₹500

    # Turn 1: add ₹450 item — ok
    mcp_add(c, sess, "kr-filter-500")

    # Turn 2: chat turn — budget must remain in session
    c.post("/api/chat", json={"session_id": sess, "message": "What else do you have?"})

    # Verify budget still in session
    sm = get_session_manager()
    assert sm.get_state(sess)["budget_limit_paise"] == 50000, "Budget should persist after chat turn"

    # Turn 3: add ₹700 item — still blocked
    r = mcp_add(c, sess, "kr-subscription")
    assert r.status_code == 400, f"Budget should still block after chat turn. Got: {r.status_code}"


def test_no_budget_set_allows_any_item():
    """With no budget set, any valid product must be addable."""
    c = get_client()
    sess = new_session(c)
    # No budget set — subscription (₹700) should be addable
    r = mcp_add(c, sess, "kr-subscription")
    assert r.status_code == 200, r.text


# ── Remove from cart ─────────────────────────────────────────────────────────

def test_remove_cart_item():
    """EXACT REGRESSION: remove_from_cart must actually mutate the cart."""
    c = get_client()
    sess = new_session(c)

    mcp_add(c, sess, "kr-filter-500")
    mcp_add(c, sess, "kr-steel-filter")

    cart_before = mcp_cart(c, sess)
    assert len(cart_before["cart_skus"]) == 2

    r = mcp_remove(c, sess, "kr-steel-filter")
    assert r.status_code == 200, r.text
    assert r.json()["removed"] is True

    cart_after = mcp_cart(c, sess)
    assert "kr-steel-filter" not in cart_after["cart_skus"]
    assert "kr-filter-500" in cart_after["cart_skus"]
    assert len(cart_after["cart_skus"]) == 1


def test_remove_subscription_from_cart():
    """EXACT REGRESSION: 'discard subscription' in cart = remove, not subscription cancel."""
    c = get_client()
    sess = new_session(c)

    mcp_add(c, sess, "kr-filter-500")
    mcp_add(c, sess, "kr-subscription")

    cart = mcp_cart(c, sess)
    assert "kr-subscription" in cart["cart_skus"]

    r = mcp_remove(c, sess, "kr-subscription")
    assert r.status_code == 200, r.text
    assert r.json()["removed"] is True

    cart_after = mcp_cart(c, sess)
    assert "kr-subscription" not in cart_after["cart_skus"]
    assert cart_after["total_paise"] == 45000  # Only ₹450 filter coffee remains


def test_remove_nonexistent_item_returns_error():
    """Removing an item not in cart must return a clear error."""
    c = get_client()
    sess = new_session(c)
    mcp_add(c, sess, "kr-filter-500")

    r = mcp_remove(c, sess, "kr-subscription")  # not in cart
    assert r.status_code == 400, r.text


def test_remove_item_audit_event():
    """ITEM_REMOVED must appear in audit trail after removal."""
    c = get_client()
    sess = new_session(c)

    mcp_add(c, sess, "kr-filter-500")
    mcp_remove(c, sess, "kr-filter-500")

    audit = c.get(f"/api/audit?session_id={sess}").json()
    event_types = [e["event_type"] for e in audit["events"]]
    assert "ITEM_REMOVED" in event_types, f"Events: {event_types}"


def test_item_added_audit_event():
    """ITEM_ADDED must appear in audit trail when an item is added."""
    c = get_client()
    sess = new_session(c)
    mcp_add(c, sess, "kr-filter-500")

    audit = c.get(f"/api/audit?session_id={sess}").json()
    event_types = [e["event_type"] for e in audit["events"]]
    assert "ITEM_ADDED" in event_types, f"Events: {event_types}"


def test_bulk_remove_via_mock_agent():
    """'Remove everything above my limit' via chat — only over-budget items removed."""
    c = get_client()
    sess = new_session(c)
    set_budget_api(c, sess, 500)  # ₹500 budget

    # Directly inject items into cart (bypass add validation)
    sm = get_session_manager()
    sm.get_state(sess)["cart_skus"] = ["kr-filter-500", "kr-subscription"]  # ₹450 + ₹700

    # Chat: ask to remove everything above the limit
    r = c.post("/api/chat", json={
        "session_id": sess,
        "message": "Remove everything above my limit",
    })
    assert r.status_code == 200, r.text

    cart = mcp_cart(c, sess)
    assert "kr-subscription" not in cart["cart_skus"]   # ₹700 — exceeds ₹500 → removed
    assert "kr-filter-500" in cart["cart_skus"]         # ₹450 — within ₹500 → kept
    assert cart["total_paise"] == 45000


# ── Mandate budget check ───────────────────────────────────────────────────────

def test_mandate_blocked_when_cart_exceeds_buyer_budget():
    """Build mandate when cart total > buyer budget — mandate must be blocked with buyer_budget_check."""
    c = get_client()
    sess = new_session(c)

    # Force items into cart directly
    sm = get_session_manager()
    state = sm.get_state(sess)
    state["cart_skus"] = ["kr-subscription"]   # ₹700
    state["budget_limit_paise"] = 50000         # ₹500 budget

    r = c.post("/api/mandates/build", json={"session_id": sess, "buyer_ref": "buyer"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "blocked", f"Expected blocked, got: {body['status']}"

    policy_names = [p["rule"] for p in body["policy_checks"]]
    assert "buyer_budget_check" in policy_names

    fail_checks = [p for p in body["policy_checks"] if p["status"] == "fail"]
    assert any(p["rule"] == "buyer_budget_check" for p in fail_checks)


# ── Checkout conversation state ────────────────────────────────────────────────

def test_checkout_yes_confirmation_not_restarted():
    """EXACT REGRESSION: 'yes' after checkout question must not restart product discovery."""
    c = get_client()
    sess = new_session(c)

    # Add item to cart
    mcp_add(c, sess, "kr-filter-500")

    # Set conversation state to CHECKOUT_REQUESTED
    sm = get_session_manager()
    from app.services.session_manager import ConvState
    sm.set_conversation_state(sess, ConvState.CHECKOUT_REQUESTED)

    r = c.post("/api/chat", json={"session_id": sess, "message": "yes"})
    assert r.status_code == 200, r.text
    body = r.json()

    # Must signal ready_to_checkout, and reply must not be "what would you like to add"
    assert body["ready_to_checkout"] is True
    reply_lower = body["reply"].lower()
    assert "what would you like" not in reply_lower


# ── MCP Journey B guardrail bypass prevention ─────────────────────────────────

def test_mcp_cannot_bypass_budget_guardrail():
    """External AI agent (Journey B) must not be able to add items that exceed the budget."""
    c = get_client()
    sess = new_session(c)
    set_budget_api(c, sess, 500)  # ₹500

    # MCP agent attempts to add ₹700 subscription
    r = mcp_add(c, sess, "kr-subscription")
    assert r.status_code == 400, f"Expected MCP to be blocked. Got {r.status_code}: {r.text}"

    # Cart must remain empty
    cart = mcp_cart(c, sess)
    assert "kr-subscription" not in cart["cart_skus"]


def test_mcp_remove_from_cart_endpoint_works():
    """Journey B must have a working POST /api/mcp/remove_from_cart endpoint."""
    c = get_client()
    sess = new_session(c)

    mcp_add(c, sess, "kr-filter-500")
    mcp_add(c, sess, "kr-arabica-250")

    r = mcp_remove(c, sess, "kr-arabica-250")
    assert r.status_code == 200, r.text
    assert r.json()["removed"] is True
    assert r.json()["updated_total_paise"] == 45000  # Only ₹450 filter coffee remains


# ── Payment failure stays unpaid ──────────────────────────────────────────────

def test_payment_failure_order_stays_unpaid():
    """Payment failure must never mark an order as paid."""
    c = get_client()
    sess = new_session(c)
    mcp_add(c, sess, "kr-filter-500")

    mandate_r = c.post("/api/mandates/build", json={"session_id": sess, "buyer_ref": "buyer"})
    assert mandate_r.status_code == 200, mandate_r.text
    mandate_id = mandate_r.json()["mandate_id"]

    c.post("/api/mandates/confirm", json={"mandate_id": mandate_id, "method": "buyer_tap"})
    order_r = c.post("/api/checkout", json={"mandate_id": mandate_id})
    assert order_r.status_code == 200, order_r.text
    order_id = order_r.json()["order_id"]

    fail_r = c.post(f"/api/demo/trigger-webhook?order_id={order_id}&outcome=failure")
    assert fail_r.status_code == 200, fail_r.text
    assert fail_r.json()["status"] == "payment_failed"
    assert fail_r.json()["status"] != "paid"
