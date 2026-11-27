"""Graph labels are words, not stored codes."""

from types import SimpleNamespace

from investos.services.graph import GraphService
from investos.services.ownership_signals import OwnershipSignalService


def test_a_conclusion_without_a_summary_is_named_by_its_stance_in_words():
    service = GraphService.__new__(GraphService)
    for stance, label in [("no_view", "No view yet"), ("bullish", "Bullish view")]:
        node = SimpleNamespace(current_thesis_summary=None, current_stance=stance)
        assert service._node_label("conclusion", node) == label
    summarized = SimpleNamespace(
        current_thesis_summary="Margins recover", current_stance="bullish"
    )
    assert service._node_label("conclusion", summarized) == "Margins recover"


async def test_an_ownership_signal_is_named_by_its_own_sentence(monkeypatch):
    created = {}

    class FakeSetups:
        def __init__(self, session):
            pass

        async def create_signal(self, **fields):
            created.update(fields)

    monkeypatch.setattr(
        "investos.services.market_setup.MarketSetupSignalService", FakeSetups
    )
    service = OwnershipSignalService(session=None)

    async def no_weights():
        return {}

    monkeypatch.setattr(service, "portfolio_weights_by_ticker", no_weights)
    evidence = SimpleNamespace(
        id="e1",
        source_item_type="insider_disclosure",
        title="Doe Jane (director) sold 5,000 SYNQ shares at an average $102.40",
        metadata_json={
            "ticker": "SYNQ",
            "transaction_code": "S",
            "transaction_type": "sold",
        },
        url=None,
        event_time=None,
        public_time=None,
    )
    await service._store_market_setup_signal(
        evidence, SimpleNamespace(source_type="filing")
    )
    assert created["signal_name"] == evidence.title
