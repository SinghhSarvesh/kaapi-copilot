"""
Revenue / growth analytics derived directly from order_service + mandate_engine
state (no separate data pipeline required).
"""
from app.services.order_service import order_service
from app.services.catalog_service import catalog_service

BASELINE_AOV_PAISE = 45000  # assumption: a non-agentic storefront sells one core item, no upsell


class AnalyticsService:
    @staticmethod
    def _has_valid_upsell(order) -> bool:
        mandate = order_service.get_mandate(order.mandate_id)
        if not mandate:
            return False

        order_skus = {item.sku for item in mandate.items}
        return any(
            upsell_sku != sku and upsell_sku in order_skus
            for sku in order_skus
            for upsell_sku in (catalog_service.get_product(sku) or {}).get("upsell_pairs", [])
        )

    def summary(self) -> dict:
        orders = list(order_service._orders.values())
        paid = [o for o in orders if o.status == "paid"]
        failed = [o for o in orders if o.status == "payment_failed"]
        created = [o for o in orders if o.status == "created"]

        mandates = list(order_service._mandates.values())
        blocked = [m for m in mandates if m.status == "blocked"]

        orders_with_upsell = sum(1 for order in paid if self._has_valid_upsell(order))
        attach_rate = (orders_with_upsell / len(paid)) if paid else 0.0

        agent_assisted_aov = (sum(o.total_paise for o in paid) / len(paid)) if paid else 0

        return {
            "orders_paid": len(paid),
            "orders_payment_failed": len(failed),
            "orders_created_awaiting_payment": len(created),
            "mandates_blocked": len(blocked),
            "upsell_attach_rate_pct": round(attach_rate * 100, 1),
            "baseline_aov_paise": BASELINE_AOV_PAISE,
            "agent_assisted_aov_paise": round(agent_assisted_aov),
            "aov_lift_pct": round(((agent_assisted_aov - BASELINE_AOV_PAISE) / BASELINE_AOV_PAISE) * 100, 1)
                            if BASELINE_AOV_PAISE else 0.0,
        }


analytics_service = AnalyticsService()
