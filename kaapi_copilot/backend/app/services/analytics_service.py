"""
Revenue / growth analytics derived directly from order_service + mandate_engine
+ session_manager state (no separate data pipeline required).
"""
from app.services.order_service import order_service
from app.services.session_manager import session_manager

BASELINE_AOV_PAISE = 45000  # assumption: a non-agentic storefront sells one core item, no upsell


class AnalyticsService:
    def summary(self) -> dict:
        orders = list(order_service._orders.values())
        paid = [o for o in orders if o.status == "paid"]
        failed = [o for o in orders if o.status == "payment_failed"]
        created = [o for o in orders if o.status == "created"]

        mandates = list(order_service._mandates.values())
        blocked = [m for m in mandates if m.status == "blocked"]

        sessions = session_manager._sessions
        total_offered = sum(s["upsells_offered_count"] for s in sessions.values())
        total_accepted = sum(s["upsells_accepted"] for s in sessions.values())
        attach_rate = (total_accepted / total_offered) if total_offered else 0.0

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
                            if agent_assisted_aov else 0.0,
        }


analytics_service = AnalyticsService()
