import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))


@pytest.fixture(autouse=True)
def fresh_app(tmp_path, monkeypatch):
    for module_name in list(sys.modules):
        if module_name.startswith("app."):
            del sys.modules[module_name]
    monkeypatch.setenv("KAAPI_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("PAYMENT_MODE", "mock")
    monkeypatch.setenv("AGENT_MODE", "mock")


def test_real_style_captured_webhook_maps_order_and_mandate():
    from fastapi.testclient import TestClient
    from app.api.main import app

    client = TestClient(app)
    client.post("/api/mcp/add_to_cart", json={"session_id": "mcp1", "sku": "kr-filter-500"})
    mandate = client.post("/api/mcp/create_checkout_mandate", json={
        "session_id": "mcp1", "buyer_ref": "buyer1",
    }).json()
    result = client.post("/api/mcp/confirm_and_pay", json={"mandate_id": mandate["mandate_id"]}).json()
    order = result["order"]

    webhook = {
        "id": "evt_capture_1",
        "event": "payment.captured",
        "payload": {"payment": {"entity": {
            "id": "pay_real_1", "order_id": order["order_id"], "status": "captured",
        }}},
    }
    response = client.post("/api/webhooks/razorpay", json=webhook)
    assert response.status_code == 200
    assert response.json()["order_status"] == "paid"
    assert result["mandate"]["status"] == "confirmed"


def test_malformed_and_unknown_webhooks_are_controlled():
    from fastapi.testclient import TestClient
    from app.api.main import app

    client = TestClient(app)
    assert client.post("/api/webhooks/razorpay", json={}).status_code == 400
    unknown = client.post("/api/webhooks/razorpay", json={
        "id": "evt_unknown", "event": "payment.captured", "payload": {
            "payment": {"entity": {"id": "pay_unknown", "order_id": "order_unknown"}}
        },
    })
    assert unknown.status_code == 202


def test_mcp_flow_rejects_empty_and_unknown_skus():
    from fastapi.testclient import TestClient
    from app.api.main import app

    client = TestClient(app)
    assert client.post("/api/mcp/create_checkout_mandate", json={
        "session_id": "empty", "buyer_ref": "buyer",
    }).status_code == 400
    assert client.post("/api/mcp/add_to_cart", json={
        "session_id": "bad", "sku": "unknown",
    }).status_code == 404


def test_groq_view_cart_returns_actual_session_state():
    from app.providers.agent.groq_agent import GroqShoppingAgent

    tool_result = GroqShoppingAgent._run_tool(
        object.__new__(GroqShoppingAgent), "view_cart", {},
        {"cart_skus": ["kr-filter-500"]},
    )
    assert tool_result["cart_skus"] == ["kr-filter-500"]
    assert tool_result["items"][0]["sku"] == "kr-filter-500"


def test_chat_with_invalid_session_returns_404():
    from fastapi.testclient import TestClient
    from app.api.main import app

    client = TestClient(app)
    response = client.post("/api/chat", json={
        "session_id": "nonexistent_session_xyz", "buyer_ref": "buyer", "message": "hi",
    })
    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


def test_session_spend_cap_accumulates_across_orders():
    """Two paid orders in the same session must accumulate toward the session spend cap."""
    from fastapi.testclient import TestClient
    from app.api.main import app
    from app.services.mandate_engine import mandate_engine
    from app.providers.payment.mock import mock_payment_provider

    client = TestClient(app)

    def pay_one_item(mcp_session: str, sku: str) -> dict:
        client.post("/api/mcp/add_to_cart", json={"session_id": mcp_session, "sku": sku})
        mandate = client.post("/api/mcp/create_checkout_mandate", json={
            "session_id": mcp_session, "buyer_ref": "buyer_cap_test",
        }).json()
        result = client.post("/api/mcp/confirm_and_pay", json={"mandate_id": mandate["mandate_id"]}).json()
        order = result["order"]
        wh = mock_payment_provider.simulate_webhook(order["payment_link_id"], "success")
        client.post("/api/webhooks/razorpay", json={
            "id": f"evt_{order['order_id']}", "event": wh["event"], "payload": wh["payload"],
        })
        return order

    pay_one_item("sess_cap_a", "kr-filter-500")   # ₹450 — cart cleared after mandate
    pay_one_item("sess_cap_a", "kr-arabica-250")  # ₹380 — fresh cart, only arabica

    spent = mandate_engine.get_session_spend("sess_cap_a")
    # 45000 (filter-500) + 38000 (arabica-250) = 83000 paise
    assert spent == 45000 + 38000