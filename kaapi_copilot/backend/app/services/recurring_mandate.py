"""
Recurring mandate simulation for subscription SKUs (e.g. kr-subscription).
A mandate is authorized once; run_cycle() re-uses that authorization to
create a fresh order/payment-link per billing cycle, gated by the same
guardrail checks each time (re-verifies price + spend caps per cycle).
"""
from datetime import datetime, timezone
from app.services.mandate_engine import mandate_engine, GuardrailError
from app.services.order_service import order_service
from app.services.audit_trail import audit_trail


class RecurringMandateService:
    def __init__(self):
        self._recurring = {}  # mandate_id -> {"session_id":..., "buyer_ref":..., "skus":[...], "cycles_run": int}

    def authorize(self, session_id: str, buyer_ref: str, skus: list) -> dict:
        mandate = mandate_engine.build_mandate(
            session_id, buyer_ref, skus,
            rationale="Buyer authorized a recurring monthly subscription mandate.",
        )
        order_service.register_mandate(mandate)
        mandate_engine.confirm_mandate(mandate, method="buyer_tap")
        self._recurring[mandate.mandate_id] = {
            "session_id": session_id, "buyer_ref": buyer_ref, "skus": skus, "cycles_run": 0,
        }
        audit_trail.log("recurring_mandate_authorized", session_id,
                         {"mandate_id": mandate.mandate_id, "skus": skus})
        return {"mandate_id": mandate.mandate_id, "status": mandate.status}

    def run_cycle(self, mandate_id: str) -> dict:
        """Re-verifies price/caps and creates a new order+link for this billing cycle."""
        info = self._recurring.get(mandate_id)
        if not info:
            raise GuardrailError(f"No recurring authorization found for {mandate_id}")

        cycle_mandate = mandate_engine.build_mandate(
            info["session_id"], info["buyer_ref"], info["skus"],
            rationale=f"Recurring billing cycle #{info['cycles_run'] + 1} under mandate {mandate_id}.",
        )
        order_service.register_mandate(cycle_mandate)
        mandate_engine.confirm_mandate(cycle_mandate, method="mcp_confirm_and_pay")
        order = order_service.checkout(cycle_mandate)
        info["cycles_run"] += 1
        audit_trail.log("recurring_cycle_billed", info["session_id"], {
            "parent_mandate_id": mandate_id, "cycle_mandate_id": cycle_mandate.mandate_id,
            "order_id": order.order_id, "cycle_number": info["cycles_run"],
        })
        return {"cycle_number": info["cycles_run"], "order": order.to_dict()}


recurring_mandate_service = RecurringMandateService()
