import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))


@pytest.fixture(autouse=True)
def fresh_services(tmp_path, monkeypatch):
    for module_name in list(sys.modules):
        if module_name.startswith("app."):
            del sys.modules[module_name]
    monkeypatch.setenv("KAAPI_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("PAYMENT_MODE", "mock")
    monkeypatch.setenv("AGENT_MODE", "mock")


def create_order(skus=("kr-filter-500",)):
    from app.services.mandate_engine import mandate_engine
    from app.services.order_service import order_service

    mandate = mandate_engine.build_mandate("lifecycle", "buyer", list(skus), "test")
    order_service.register_mandate(mandate)
    mandate_engine.confirm_mandate(mandate, "buyer_tap")
    order = order_service.checkout(mandate)
    return mandate, order, order_service


def test_duplicate_confirmation_does_not_duplicate_spend():
    from app.services.mandate_engine import mandate_engine

    mandate, _, _ = create_order()
    assert mandate_engine.get_session_spend("lifecycle") == 0
    assert mandate_engine.confirm_mandate(mandate, "buyer_tap") is mandate
    assert mandate_engine.get_session_spend("lifecycle") == 0


def test_successful_payment_updates_mandate_and_counts_spend_once():
    from app.services.mandate_engine import mandate_engine
    from app.providers.payment.mock import mock_payment_provider

    mandate, order, order_service = create_order()
    webhook = mock_payment_provider.simulate_webhook(order.payment_link_id, "success")
    updated = order_service.handle_webhook_event(webhook["event"], webhook["payload"])
    order_service.handle_webhook_event(webhook["event"], webhook["payload"])

    assert updated.status == "paid"
    assert mandate.status == "paid"
    assert mandate_engine.get_session_spend("lifecycle") == mandate.total_paise


def test_failed_payment_updates_mandate_without_spend():
    from app.services.mandate_engine import mandate_engine
    from app.providers.payment.mock import mock_payment_provider

    mandate, order, order_service = create_order()
    webhook = mock_payment_provider.simulate_webhook(order.payment_link_id, "failure")
    updated = order_service.handle_webhook_event(webhook["event"], webhook["payload"])
    order_service.handle_webhook_event(webhook["event"], webhook["payload"])

    assert updated.status == "payment_failed"
    assert mandate.status == "payment_failed"
    assert mandate_engine.get_session_spend("lifecycle") == 0


def test_paid_and_failed_mandates_cannot_be_confirmed_again():
    from app.services.mandate_engine import GuardrailError, mandate_engine
    from app.providers.payment.mock import mock_payment_provider

    paid_mandate, paid_order, order_service = create_order()
    paid_webhook = mock_payment_provider.simulate_webhook(paid_order.payment_link_id, "success")
    order_service.handle_webhook_event(paid_webhook["event"], paid_webhook["payload"])
    with pytest.raises(GuardrailError):
        mandate_engine.confirm_mandate(paid_mandate, "buyer_tap")

    failed_mandate, failed_order, failed_service = create_order(("kr-frother",))
    failed_webhook = mock_payment_provider.simulate_webhook(failed_order.payment_link_id, "failure")
    failed_service.handle_webhook_event(failed_webhook["event"], failed_webhook["payload"])
    with pytest.raises(GuardrailError):
        mandate_engine.confirm_mandate(failed_mandate, "buyer_tap")