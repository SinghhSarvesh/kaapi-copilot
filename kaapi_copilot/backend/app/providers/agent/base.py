"""
Shopping agent abstraction. Both the mock (rule-based) and Groq-powered
implementations return the same AgentResponse shape, so the API layer and
Journey B MCP tools never branch on which brain is running.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class AgentResponse:
    reply: str
    add_to_cart_skus: list = field(default_factory=list)        # SKUs successfully added (backend-validated)
    upsell_sku: Optional[str] = None                             # at most one, catalog-derived
    upsell_reason: str = ""
    ready_to_checkout: bool = False
    remove_from_cart_skus: list = field(default_factory=list)   # SKUs removed this turn
    decision_log: list = field(default_factory=list)            # For explainability panel


class ShoppingAgent(ABC):
    name: str = "base"

    @abstractmethod
    def handle_turn(self, session_state: dict, user_message: str,
                    session_id: Optional[str] = None,
                    conversation_history: Optional[list] = None) -> AgentResponse:
        """
        session_state: live dict from session_manager (budget, cart, conv_state, etc.)
        session_id: passed so tool handlers can call session_manager directly for mutations
        conversation_history: optional list of previous turns [{"role": "user"|"assistant", "content": ...}]
        """
