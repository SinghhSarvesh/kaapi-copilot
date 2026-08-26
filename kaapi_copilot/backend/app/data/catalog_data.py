"""
Seed catalog for Kaapi Roasters. Deterministic — same data every run.
Every product carries category + upsell_pairs so recommendations are
data-driven, never improvised by the LLM.
"""
from app.models.domain import Product

MERCHANT = {
    "merchant_id": "kaapi-roasters",
    "name": "Kaapi Roasters",
    "description": "Small D2C filter-coffee brand.",
    "onboarded_at": "2026-08-26T00:00:00Z",
}

SEED_PRODUCTS = [
    Product("kr-filter-500", "Filter Coffee Powder, 500g", 45000, "powder",
            "Classic South Indian filter coffee blend.", ["kr-steel-filter"]),
    Product("kr-arabica-250", "Single-Origin Arabica Beans, 250g", 38000, "beans",
            "Light-roast single-origin arabica.", ["kr-dripper"]),
    Product("kr-steel-filter", "South Indian Steel Filter Set", 65000, "brew-gear",
            "Traditional stainless steel filter set.", []),
    Product("kr-dripper", "Pour-over Dripper", 90000, "brew-gear",
            "Ceramic pour-over dripper.", []),
    Product("kr-subscription", "Monthly Coffee Subscription (2 bags)", 70000, "subscription",
            "Two bags delivered monthly.", []),
    Product("kr-frother", "Milk Frother", 55000, "accessory",
            "Handheld milk frother.", []),
    Product("kr-filters-100", "Reusable Filter Papers, pack of 100", 25000, "consumable",
            "Pack of 100 reusable filter papers.", ["kr-dripper"]),
]

CATALOG_BY_SKU = {p.sku: p for p in SEED_PRODUCTS}
