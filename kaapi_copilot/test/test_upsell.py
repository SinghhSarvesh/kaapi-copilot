"""
Tests for the deterministic get_upsell_suggestion() gating function.

Covers:
  - No suggestion when cart is empty
  - Budget gate: within_budget flag set correctly for over/under budget
  - Already-offered dedup: no re-suggestion of same SKU
  - Already-in-cart dedup: no suggestion for item already in cart
  - Catalog category allowlist integrity: all upsell pairs resolve to
    allowlisted categories

Run from project root:
    python -m pytest kaapi_copilot/test/ -v
"""
import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))


@pytest.fixture(autouse=True)
def fresh_services(tmp_path, monkeypatch):
    for mod in list(sys.modules):
        if mod.startswith("app."):
            del sys.modules[mod]
    monkeypatch.setenv("KAAPI_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("PAYMENT_MODE", "mock")
    monkeypatch.setenv("AGENT_MODE", "mock")
    monkeypatch.setenv("ALLOWED_ORIGINS", "*")


def test_no_suggestion_when_cart_is_empty():
    from app.services.catalog_service import catalog_service
    assert catalog_service.get_upsell_suggestion(cart_skus=[]) is None


def test_suggestion_over_budget_flagged_within_budget_false():
    from app.services.catalog_service import catalog_service
    # kr-filter-500 (45000p) -> kr-steel-filter (65000p); 45000+65000=110000 > 90000 budget
    result = catalog_service.get_upsell_suggestion(
        cart_skus=["kr-filter-500"],
        session_budget_paise=90000,
    )
    assert result is not None
    assert result["within_budget"] is False
    assert result["sku"] == "kr-steel-filter"


def test_suggestion_within_budget_flagged_true():
    from app.services.catalog_service import catalog_service
    result = catalog_service.get_upsell_suggestion(
        cart_skus=["kr-filter-500"],
        session_budget_paise=200000,
    )
    assert result is not None
    assert result["within_budget"] is True
    assert result["sku"] == "kr-steel-filter"


def test_no_suggestion_when_already_offered():
    from app.services.catalog_service import catalog_service
    result = catalog_service.get_upsell_suggestion(
        cart_skus=["kr-filter-500"],
        already_offered=["kr-steel-filter"],
    )
    assert result is None


def test_no_suggestion_when_already_in_cart():
    from app.services.catalog_service import catalog_service
    result = catalog_service.get_upsell_suggestion(
        cart_skus=["kr-filter-500", "kr-steel-filter"],
    )
    assert result is None


def test_all_upsell_pairs_are_allowlisted_categories():
    from app.services.catalog_service import catalog_service
    from app.data.catalog_data import SEED_PRODUCTS
    ALLOWED = {"powder", "beans", "brew-gear", "subscription", "accessory", "consumable"}
    for product in SEED_PRODUCTS:
        for pair_sku in product.upsell_pairs:
            candidate = catalog_service.get_product(pair_sku)
            assert candidate is not None, f"Upsell pair {pair_sku!r} for {product.sku!r} not found"
            assert candidate["category"] in ALLOWED, (
                f"Upsell pair {pair_sku!r} has category {candidate['category']!r} not in allowlist"
            )
