"""
Catalog Service — single source of truth for product data. Exposed to both
the conversational agent (Journey A) and the MCP tool surface (Journey B).
"""
from typing import Optional
from app.data.catalog_data import CATALOG_BY_SKU, SEED_PRODUCTS, MERCHANT
from app.models.domain import Product


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

    def search(self, query: str) -> list:
        q = query.lower()
        return [p.to_dict() for p in SEED_PRODUCTS
                if q in p.name.lower() or q in p.category.lower() or q in p.description.lower()]

    def merchant_info(self) -> dict:
        return MERCHANT


catalog_service = CatalogService()
