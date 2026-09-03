import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))


@pytest.fixture(autouse=True)
def fresh_services(tmp_path, monkeypatch):
    for module_name in list(sys.modules):
        if module_name.startswith("app."):
            del sys.modules[module_name]
    monkeypatch.setenv("KAAPI_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("PAYMENT_MODE", "mock")
    monkeypatch.setenv("AGENT_MODE", "mock")


def test_attach_rate_counts_one_acceptance_per_upsell_offer():
    from app.providers.agent.base import AgentResponse
    from app.services.analytics_service import analytics_service
    from app.services.session_manager import session_manager

    sid = session_manager.create_session("buyer_attach")
    state = session_manager.get_state(sid)

    state["pending_upsell"] = "kr-steel-filter"
    state["upsells_offered_count"] = 1

    # Accept the offered upsell in this turn.
    session_manager.apply_agent_response(
        sid,
        AgentResponse(reply="added", add_to_cart_skus=["kr-steel-filter"]),
    )

    # Next turn, a normal add should NOT re-count the same accepted upsell.
    session_manager.apply_agent_response(
        sid,
        AgentResponse(reply="added again", add_to_cart_skus=["kr-filter-500"]),
    )

    summary = analytics_service.summary()
    assert summary["upsell_attach_rate_pct"] == 100.0
