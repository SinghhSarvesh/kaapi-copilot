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


def add_order(order_id, total_paise, skus, status="paid"):
    from app.models.domain import CartItem, Order, PurchaseMandate
    from app.services.order_service import order_service

    items = [CartItem(sku, sku, 1, 1) for sku in skus]
    mandate = PurchaseMandate(
        mandate_id=f"mandate_{order_id}", session_id=f"session_{order_id}",
        buyer_ref="buyer", items=items, currency="INR", total_paise=total_paise,
        rationale="analytics test", policy_checks=[], status="paid" if status == "paid" else "pending",
    )
    order = Order(
        order_id=order_id, mandate_id=mandate.mandate_id, session_id=mandate.session_id,
        total_paise=total_paise, currency="INR", status=status,
    )
    order_service.register_mandate(mandate)
    order_service._orders[order_id] = order
    return order


def summary():
    from app.services.analytics_service import analytics_service
    return analytics_service.summary()


def test_no_paid_orders_have_zero_attach_rate_and_aov():
    add_order("unpaid", 45000, ["kr-filter-500"], status="created")

    result = summary()

    assert result["orders_paid"] == 0
    assert result["upsell_attach_rate_pct"] == 0.0
    assert result["agent_assisted_aov_paise"] == 0


def test_one_paid_order_without_upsell_has_zero_attach_rate():
    add_order("base", 45000, ["kr-filter-500"])

    assert summary()["upsell_attach_rate_pct"] == 0.0


def test_one_paid_order_with_one_upsell_has_full_attach_rate():
    add_order("one_upsell", 110000, ["kr-filter-500", "kr-steel-filter"])

    assert summary()["upsell_attach_rate_pct"] == 100.0


def test_multiple_upsells_in_one_order_count_as_one_attached_order():
    add_order("multiple_upsells", 247000,
              ["kr-filter-500", "kr-steel-filter", "kr-arabica-250", "kr-dripper"])

    result = summary()

    assert result["orders_paid"] == 1
    assert result["upsell_attach_rate_pct"] == 100.0


@pytest.mark.parametrize(
    ("order_count", "upsell_order_count", "expected_rate"),
    [(2, 1, 50.0), (10, 3, 30.0), (100, 25, 25.0)],
)
def test_attach_rate_is_paid_order_level(order_count, upsell_order_count, expected_rate):
    for index in range(order_count):
        skus = ["kr-filter-500", "kr-steel-filter"] if index < upsell_order_count else ["kr-filter-500"]
        add_order(f"order_{index}", 110000 if index < upsell_order_count else 45000, skus)

    assert summary()["upsell_attach_rate_pct"] == expected_rate


def test_paid_and_unpaid_orders_only_count_paid_orders():
    add_order("paid", 110000, ["kr-filter-500", "kr-steel-filter"])
    add_order("failed", 110000, ["kr-filter-500", "kr-steel-filter"], status="payment_failed")
    add_order("cancelled", 110000, ["kr-filter-500", "kr-steel-filter"], status="cancelled")
    add_order("pending", 110000, ["kr-filter-500", "kr-steel-filter"], status="created")

    result = summary()

    assert result["orders_paid"] == 1
    assert result["upsell_attach_rate_pct"] == 100.0
    assert result["agent_assisted_aov_paise"] == 110000


def test_aov_and_lift_use_all_paid_order_totals():
    add_order("small", 45000, ["kr-filter-500"])
    add_order("large", 110000, ["kr-filter-500", "kr-steel-filter"])

    result = summary()

    assert result["agent_assisted_aov_paise"] == 77500
    assert result["aov_lift_pct"] == 72.2


def test_aov_lift_handles_zero_baseline(monkeypatch):
    import app.services.analytics_service as analytics_module

    add_order("paid", 110000, ["kr-filter-500", "kr-steel-filter"])
    monkeypatch.setattr(analytics_module, "BASELINE_AOV_PAISE", 0)

    result = analytics_module.analytics_service.summary()

    assert result["agent_assisted_aov_paise"] == 110000
    assert result["aov_lift_pct"] == 0.0


def test_duplicate_summary_processing_does_not_inflate_metrics():
    add_order("single", 110000, ["kr-filter-500", "kr-steel-filter"])
    add_order("single", 110000, ["kr-filter-500", "kr-steel-filter"])

    first = summary()
    second = summary()

    assert first == second
    assert second["orders_paid"] == 1
    assert second["upsell_attach_rate_pct"] == 100.0
