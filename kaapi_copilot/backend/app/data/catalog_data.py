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
            "Classic South Indian filter coffee blend — rich, aromatic, bold.",
            ["kr-steel-filter"]),           # Filter powder → Steel filter (complete brewing setup)

    Product("kr-arabica-250", "Single-Origin Arabica Beans, 250g", 38000, "beans",
            "Light-roast single-origin arabica with fruity, floral notes.",
            ["kr-dripper"]),                # Arabica beans → Pour-over dripper (best brewing method)

    Product("kr-steel-filter", "South Indian Steel Filter Set", 65000, "brew-gear",
            "Traditional 3-piece stainless steel filter set — makes authentic decoction.",
            ["kr-filter-500"]),             # Steel filter → Filter powder (needs coffee to brew)

    Product("kr-dripper", "Pour-over Dripper", 90000, "brew-gear",
            "Ceramic pour-over dripper for slow, precise coffee extraction.",
            ["kr-arabica-250"]),            # Dripper → Arabica beans (best beans for pour-over)

    Product("kr-subscription", "Monthly Coffee Subscription (2 bags)", 70000, "subscription",
            "Two freshly roasted bags delivered every month — never run out.",
            ["kr-frother"]),                # Subscription → Frother (elevate the daily ritual)

    Product("kr-frother", "Milk Frother", 55000, "accessory",
            "Handheld milk frother for café-style lattes and cappuccinos at home.",
            ["kr-subscription"]),           # Frother → Subscription (needs regular coffee too)

    Product("kr-filters-100", "Reusable Filter Papers, pack of 100", 25000, "consumable",
            "Eco-friendly reusable filter papers — compatible with most pour-over drippers.",
            ["kr-dripper"]),                # Filter papers → Dripper (complete the setup)
]

CATALOG_BY_SKU = {p.sku: p for p in SEED_PRODUCTS}
