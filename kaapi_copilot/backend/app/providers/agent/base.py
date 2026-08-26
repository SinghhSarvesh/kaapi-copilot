"""
Shopping agent abstraction. Both the mock (rule-based) and Anthropic-powered
implementations return the same AgentResponse shape, so the API layer and
Journey B MCP tools never branch on which brain is running.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class AgentResponse:
    reply: str
    add_to_cart_skus: list = field(default_factory=list)   # SKUs the agent proposes adding
    upsell_sku: Optional[str] = None                        # at most one, catalog-derived
    upsell_reason: str = ""
    ready_to_checkout: bool = False


class ShoppingAgent(ABC):
    name: str = "base"

    @abstractmethod
    def handle_turn(self, session_state: dict, user_message: str) -> AgentResponse:
        """session_state: {"cart_skus": [...], "upsell_offered": [...], "history": [...]}"""
