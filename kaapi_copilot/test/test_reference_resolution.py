"""
Deterministic reference-resolution regression tests (mock agent), added
after Track 01 audit. Proves "it" / "that" / "the first one" / "both" are
resolved from session state (last_shown_skus, last_single_reference_sku),
never guessed by an LLM.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))


def _reset_modules():
    for mod in list(sys.modules):
        if mod.startswith("app."):
            del sys.modules[mod]


import pytest


@pytest.fixture(autouse=True)
def fresh_services(tmp_path, monkeypatch):
    _reset_modules()
    monkeypatch.setenv("KAAPI_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("PAYMENT_MODE", "mock")
    monkeypatch.setenv("AGENT_MODE", "mock")
    yield


def test_bare_reference_it_adds_last_discussed_product():
    from app.services.session_manager import session_manager
    from app.providers.agent.mock import mock_shopping_agent

    session_id = session_manager.create_session("buyer_ref_test")
    state = session_manager.get_state(session_id)

    mock_shopping_agent.handle_turn(state, "I want good filter coffee, nothing fancy", session_id=session_id)
    state = session_manager.get_state(session_id)
    assert "kr-filter-500" in state["cart_skus"]

    # New session, discuss a product but decline the upsell, then say "add it"
    session_id2 = session_manager.create_session("buyer_ref_test2")
    state2 = session_manager.get_state(session_id2)
    mock_shopping_agent.handle_turn(state2, "tell me about arabica beans", session_id=session_id2)
    resp = mock_shopping_agent.handle_turn(state2, "add it", session_id=session_id2)
    state2 = session_manager.get_state(session_id2)
    assert "kr-arabica-250" in state2["cart_skus"] or "kr-arabica-250" in resp.add_to_cart_skus


def test_ordinal_reference_first_one_resolves_against_last_menu():
    from app.services.session_manager import session_manager
    from app.providers.agent.mock import mock_shopping_agent
    from app.data.catalog_data import SEED_PRODUCTS

    session_id = session_manager.create_session("buyer_ordinal")
    state = session_manager.get_state(session_id)
    mock_shopping_agent.handle_turn(state, "show me your menu", session_id=session_id)

    state = session_manager.get_state(session_id)
    assert state["last_shown_skus"] == [p.sku for p in SEED_PRODUCTS]

    resp = mock_shopping_agent.handle_turn(state, "add the first one", session_id=session_id)
    state = session_manager.get_state(session_id)
    assert SEED_PRODUCTS[0].sku in state["cart_skus"]


def test_both_phrase_adds_all_last_shown_skus():
    from app.services.session_manager import session_manager
    from app.providers.agent.mock import mock_shopping_agent

    session_id = session_manager.create_session("buyer_both")
    state = session_manager.get_state(session_id)
    session_manager.set_last_shown_skus(session_id, ["kr-filter-500", "kr-arabica-250"])

    resp = mock_shopping_agent.handle_turn(state, "add both", session_id=session_id)
    state = session_manager.get_state(session_id)
    assert "kr-filter-500" in state["cart_skus"]
    assert "kr-arabica-250" in state["cart_skus"]
