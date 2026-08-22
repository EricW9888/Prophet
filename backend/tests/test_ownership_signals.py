from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

from investos.services.ownership_signals import OwnershipSignalService
from investos.services.review import ReviewService


def test_ownership_signal_parses_lag_value_and_portfolio_priority():
    evidence = SimpleNamespace(
        id=uuid4(),
        source_item_type="congressional_trade_disclosure",
        title="MEMA purchase disclosure",
        url="https://example.com/disclosure",
        event_time=None,
        public_time=None,
        created_at=datetime(2026, 6, 15, tzinfo=timezone.utc),
        metadata_json={
            "ticker": "mema",
            "actor_name": "Public official disclosure",
            "transaction_type": "Purchase",
            "transaction_value": "$1,001 - $15,000",
            "transaction_date": "2026-06-01",
            "disclosure_date": "2026-06-15",
        },
    )
    source = SimpleNamespace(name="Disclosure Tracker", source_type="ownership_tracker")

    signal = OwnershipSignalService.analyze_signal(
        evidence,
        source,
        portfolio_weights={"MEMA": 25.0},
    )

    assert signal is not None
    assert signal.source_kind == "political disclosure"
    assert signal.ticker == "MEMA"
    assert signal.direction == "buy"
    assert signal.transaction_value == 1001.0
    assert signal.disclosure_lag_days == 14.0
    assert signal.is_portfolio_linked is True
    assert signal.portfolio_weight_pct == 25.0
    assert signal.should_surface is True
    assert signal.review_priority > 80
    assert "Verify the disclosure" in signal.review_trigger_reason
    assert "Do not treat it as a trade instruction" in signal.next_test


def test_ownership_signal_ignores_regular_research_items():
    evidence = SimpleNamespace(
        id=uuid4(),
        source_item_type="research_note",
        title="Memory market commentary",
        metadata_json={"ticker": "MEMB"},
    )
    source = SimpleNamespace(name="Research Blog", source_type="web_research")

    assert OwnershipSignalService.analyze_signal(evidence, source) is None


def test_ownership_disclosure_ingest_validation_and_title_are_generic():
    metadata = {
        "ticker": "memb",
        "actor_name": "Reporting owner",
        "transaction_type": "Sale",
    }

    assert (
        OwnershipSignalService._title_from_metadata("insider_disclosure", metadata)
        == "MEMB insider disclosure from Reporting owner"
    )
    assert OwnershipSignalService._validate_source_type("filing") == "filing"
    assert (
        OwnershipSignalService._validate_item_type("institutional_flow")
        == "institutional_flow"
    )

    try:
        OwnershipSignalService._validate_source_type("social_media")
    except ValueError as exc:
        assert str(exc) == "source_type_must_be_filing_or_ownership_tracker"
    else:
        raise AssertionError("Expected invalid source type to fail")

    try:
        OwnershipSignalService._validate_item_type("research_note")
    except ValueError as exc:
        assert str(exc) == "source_item_type_must_be_disclosure_type"
    else:
        raise AssertionError("Expected invalid disclosure type to fail")


def test_ownership_signal_surfaces_large_nonportfolio_disclosure_but_not_noise():
    large = SimpleNamespace(
        id=uuid4(),
        source_item_type="insider_disclosure",
        title="Large insider buy",
        url=None,
        event_time=datetime(2026, 6, 1, tzinfo=timezone.utc),
        public_time=datetime(2026, 6, 2, tzinfo=timezone.utc),
        created_at=datetime(2026, 6, 2, tzinfo=timezone.utc),
        metadata_json={
            "ticker": "ABC",
            "actor_type": "director",
            "transaction_type": "Buy",
            "transaction_value": 2_500_000,
        },
    )
    small = SimpleNamespace(
        id=uuid4(),
        source_item_type="ownership_disclosure",
        title="Small stale ownership update",
        url=None,
        event_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
        public_time=datetime(2026, 6, 1, tzinfo=timezone.utc),
        created_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        metadata_json={
            "ticker": "XYZ",
            "transaction_type": "Ownership update",
            "transaction_value": 500,
        },
    )
    source = SimpleNamespace(name="Filing feed", source_type="filing")

    large_signal = OwnershipSignalService.analyze_signal(
        large, source, portfolio_weights={}
    )
    small_signal = OwnershipSignalService.analyze_signal(
        small, source, portfolio_weights={}
    )

    assert large_signal is not None
    assert large_signal.should_surface is True
    assert small_signal is not None
    assert small_signal.should_surface is False


def test_review_copy_for_ownership_signal_is_concrete_and_cautious():
    service = ReviewService(session=None)
    item = SimpleNamespace(
        item_type="raw_evidence",
        trigger_reason="Ownership signal",
        created_at=datetime(2026, 6, 15, tzinfo=timezone.utc),
        coverage_weakness=0.0,
        contradiction_pressure=0.0,
        thesis_drift=0.0,
        catalyst_proximity=6.0,
    )
    ctx = {
        "label": "MEMA purchase disclosure",
        "ownership_signal": {
            "ticker": "MEMA",
            "issuer": "Memory Alpha Corp.",
            "source_kind": "political disclosure",
            "actor_name": "Public official disclosure",
            "direction": "buy",
            "transaction_value": 1001.0,
            "disclosure_lag_days": 14.0,
            "portfolio_weight_pct": 25.0,
            "is_portfolio_linked": True,
            "next_test": (
                "Check the original political disclosure, normalize the trade date versus disclosure date, "
                "then map the signal to demand, supply, margins, financing, regulation, valuation, or timing."
            ),
        },
    }

    summary = service._why_now_summary(item, ctx)
    next_action = service._next_action(item, ctx)
    tags = service._signal_tags(item, ctx)

    assert "political disclosure on MEMA" in summary
    assert "disclosed about 14 days after the event" in summary
    assert "roughly 25% weight" in summary
    assert "source, timing, mechanism, and later outcome" in summary
    assert "map the signal to demand" in next_action
    assert tags == ["political disclosure", "MEMA", "buy", "14d lag"]
