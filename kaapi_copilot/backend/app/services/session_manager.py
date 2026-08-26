"""
In-memory session state for Journey A conversational turns. Tracks the cart,
which upsell (if any) is currently pending a buyer response, and which
upsells have already been offered (so we never re-offer the same one).
"""
from app.models.domain import new_session_id


class SessionManager:
    def __init__(self):
        self._sessions = {}  # session_id -> state dict

    def create_session(self, buyer_ref: str) -> str:
        session_id = new_session_id()
        self._sessions[session_id] = {
            "buyer_ref": buyer_ref, "cart_skus": [], "upsell_offered": [],
            "pending_upsell": None, "history": [], "upsells_accepted": 0, "upsells_offered_count": 0,
        }
        return session_id

    def get_state(self, session_id: str) -> dict:
        if session_id not in self._sessions:
            raise KeyError(f"Unknown session {session_id}")
        return self._sessions[session_id]

    def apply_agent_response(self, session_id: str, agent_response) -> None:
        state = self.get_state(session_id)
        for sku in agent_response.add_to_cart_skus:
            if sku not in state["cart_skus"]:
                state["cart_skus"].append(sku)
            if state.get("pending_upsell") == sku:
                state["upsells_accepted"] += 1
        if agent_response.upsell_sku:
            state["pending_upsell"] = agent_response.upsell_sku
            state["upsell_offered"].append(agent_response.upsell_sku)
            state["upsells_offered_count"] += 1
        elif state.get("pending_upsell") not in agent_response.add_to_cart_skus:
            # The pending upsell was not accepted in this turn — clear it.
            state["pending_upsell"] = None


session_manager = SessionManager()
