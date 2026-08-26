"""
Shopping agent factory -- selects Mock or Groq based on settings.agent_mode.
"""
from app.core.config import settings
from app.providers.agent.mock import mock_shopping_agent


def get_shopping_agent():
    if settings.agent_mode == "groq":
        from app.providers.agent.groq_agent import GroqShoppingAgent
        return GroqShoppingAgent(settings.groq_api_key, settings.groq_model)
    return mock_shopping_agent
