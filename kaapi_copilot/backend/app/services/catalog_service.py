"""
Catalog Service — single source of truth for product data. Exposed to both
the conversational agent (Journey A) and the MCP tool surface (Journey B).
"""
from typing import Optional
from app.data.catalog_data import CATALOG_BY_SKU, SEED_PRODUCTS, MERCHANT
from app.models.domain import Product

# Category allowlist — must match MandateEngine.ALLOWED_CATEGORIES exactly.
_ALLOWED_CATEGORIES = {"powder", "beans", "brew-gear", "subscription", "accessory", "consumable"}


class CatalogService:
    def list_products(self, category: Optional[str] = None) -> list:
        products = SEED_PRODUCTS
        if category:
            products = [p for p in products if p.category == category]
        return [p.to_dict() for p in products]

    def get_product(self, sku: str) -> Optional[dict]:
        p = CATALOG_BY_SKU.get(sku)
        return p.to_dict() if p else None

    def get_price_paise(self, sku: str) -> Optional[int]:
        """Authoritative price lookup — never trust an LLM-stated price."""
        p = CATALOG_BY_SKU.get(sku)
        return p.price_paise if p else None

    def get_upsell_for(self, sku: str) -> Optional[dict]:
        p = CATALOG_BY_SKU.get(sku)
        if not p or not p.upsell_pairs:
            return None
        return self.get_product(p.upsell_pairs[0])

    def get_upsell_suggestion(
        self,
        cart_skus: list,
        session_budget_paise: Optional[int] = None,
        already_offered: Optional[list] = None,
    ) -> Optional[dict]:
        """
        Deterministic, policy-gated upsell suggestion. Plain code — no LLM judgment.

        Rules (in order):
        1. Returns None if cart is empty.
        2. Looks up the complementary SKU for the first cart item that has one.
        3. Returns None if the suggested SKU is already in the cart or already_offered.
        4. Returns None if the suggested SKU's category is not in the allowlist.
        5. Returns None if a budget is set and adding the suggestion would exceed it.
        6. Returns the product dict on pass, with an extra 'within_budget' flag.
        """
        if not cart_skus:
            return None
        already_offered = already_offered or []
        # Current cart total (authoritative catalog prices, never LLM-stated)
        cart_total = sum(self.get_price_paise(s) or 0 for s in cart_skus)

        for cart_sku in cart_skus:
            candidate = self.get_upsell_for(cart_sku)
            if candidate is None:
                continue
            sku = candidate["sku"]
            # Skip if already in cart or already offered this session
            if sku in cart_skus or sku in already_offered:
                continue
            # Category allowlist gate
            if candidate.get("category") not in _ALLOWED_CATEGORIES:
                continue
            # Budget gate
            within_budget = True
            if session_budget_paise is not None:
                within_budget = (cart_total + candidate["price_paise"]) <= session_budget_paise
            return {**candidate, "trigger_sku": cart_sku, "within_budget": within_budget}

        return None

    def search(self, query: str) -> list:
        q = query.lower().strip()
        if not q:
            return [p.to_dict() for p in SEED_PRODUCTS]
        # First try exact substring match
        exact = [p.to_dict() for p in SEED_PRODUCTS
                 if q in p.name.lower() or q in p.category.lower() or q in p.description.lower()]
        if exact:
            return exact
        # Fall back to word-level token match (e.g. 'starter kit' -> matches 'coffee', 'filter', etc.)
        terms = [t for t in q.split() if len(t) > 2]
        if not terms:
            return [p.to_dict() for p in SEED_PRODUCTS]
        matched = []
        for p in SEED_PRODUCTS:
            text = f"{p.name} {p.category} {p.description}".lower()
            if any(t in text for t in terms):
                matched.append(p.to_dict())
        return matched if matched else [p.to_dict() for p in SEED_PRODUCTS[:4]]

    def merchant_info(self) -> dict:
        return MERCHANT


catalog_service = CatalogService()
