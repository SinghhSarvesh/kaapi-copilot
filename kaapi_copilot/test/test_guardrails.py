"""
pytest suite for the AI Growth & Agentic Commerce guardrail contract. Run from kaapi_copilot/backend:
    pytest ../tests/test_guardrails.py
"""
import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

OVER_CAP_SKUS = ["kr-subscription", "kr-dripper", "kr-frother", "kr-steel-filter", "kr-arabica-250"]  # 318000 paise > 300000 cap


@pytest.fixture(autouse=True)
def fresh_services(tmp_path, monkeypatch):
    """Reload all app modules against a throwaway sqlite db per test."""
    for mod in list(sys.modules):
        if mod.startswith("app."):
            del sys.modules[mod]
    monkeypatch.setenv("KAAPI_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("PAYMENT_MODE", "mock")
    monkeypatch.setenv("AGENT_MODE", "mock")
    yield


def test_price_mismatch_blocks_mandate():
    from app.services.mandate_engine import mandate_engine
    mandate = mandate_engine.build_mandate(
        "sess1", "buyer1", ["kr-filter-500"], "test",
        agent_stated_prices={"kr-filter-500": 1},  # deliberately wrong
    )
    assert mandate.status == "blocked"
    assert "catalog says" in mandate.block_reason


def test_transaction_cap_breach_is_hard_blocked():
    from app.services.mandate_engine import mandate_engine
    mandate = mandate_engine.build_mandate("sess2", "buyer2", OVER_CAP_SKUS, "test")
    assert mandate.status == "blocked"
    checks = {c.rule: c.status for c in mandate.policy_checks}
    assert checks["transaction_spend_cap"] == "fail"


def test_only_confirmed_mandate_can_checkout():
    from app.services.mandate_engine import mandate_engine, GuardrailError
    from app.services.order_service import order_service
    mandate = mandate_engine.build_mandate("sess3", "buyer3", ["kr-filter-500"], "test")
    order_service.register_mandate(mandate)
    with pytest.raises(GuardrailError):
        order_service.checkout(mandate)  # never confirmed


def test_blocked_mandate_cannot_be_confirmed():
    from app.services.mandate_engine import mandate_engine, GuardrailError
    mandate = mandate_engine.build_mandate("sess4", "buyer4", OVER_CAP_SKUS, "test")
    with pytest.raises(GuardrailError):
        mandate_engine.confirm_mandate(mandate, method="buyer_tap")


def test_upi_decline_never_marks_order_paid():
    from app.services.mandate_engine import mandate_engine
    from app.services.order_service import order_service
    from app.providers.payment.mock import mock_payment_provider

    mandate = mandate_engine.build_mandate("sess5", "buyer5", ["kr-frother"], "test")
    order_service.register_mandate(mandate)
    mandate_engine.confirm_mandate(mandate, method="buyer_tap")
    order = order_service.checkout(mandate)

    wh = mock_payment_provider.simulate_webhook(order.payment_link_id, "failure")
    updated = order_service.handle_webhook_event(wh["event"], wh["payload"])

    assert updated.status == "payment_failed"
    assert updated.status != "paid"
    assert order_service.get_held_cart("sess5") is not None


def test_audit_hash_chain_detects_tampering():
    from app.services.audit_trail import audit_trail
    audit_trail.log("event_a", "sessX", {"k": 1})
    audit_trail.log("event_b", "sessX", {"k": 2})
    assert audit_trail.verify_chain()["valid"] is True

    import sqlite3
    conn = sqlite3.connect(audit_trail.db_path)
    conn.execute("UPDATE audit_log SET payload = ? WHERE event_type = ?", ('{"k": 999}', "event_a"))
    conn.commit()
    conn.close()

    result = audit_trail.verify_chain()
    assert result["valid"] is False


def test_empty_cart_is_blocked():
    from app.services.mandate_engine import mandate_engine

    mandate = mandate_engine.build_mandate("sess_empty", "buyer_empty", [], "test")
    assert mandate.status == "blocked"
    checks = {c.rule: c.status for c in mandate.policy_checks}
    assert checks["cart_not_empty"] == "fail"
    assert "empty" in mandate.block_reason.lower()
