from datetime import UTC, datetime, timedelta

from investos.services.manual_paper_equity import (
    RETAINED_MARKS,
    record_manual_equity,
    supersede_manual_equity,
)


def test_rebuilt_marks_remain_auditable_and_are_not_reused_as_current():
    initial = datetime(2025, 5, 20, tzinfo=UTC)
    details = {}
    state = {"cash": 400, "positions": []}
    record_manual_equity(details, state, {}, captured_at=initial, opening=True)
    rebuilt = initial + timedelta(days=1)
    supersede_manual_equity(details, rebuilt_at=rebuilt.isoformat())
    assert "manual_equity_status" not in details
    assert details["manual_equity_marks"][0]["superseded_at"] == rebuilt.isoformat()
    record_manual_equity(details, state, {}, captured_at=rebuilt)
    old, new = details["manual_equity_marks"]
    assert old["receipt_hash"] == new["receipt_hash"]
    assert old["superseded_at"] != new.get("superseded_at")
    assert new["captured_at"] == rebuilt.isoformat()


def test_only_a_bounded_number_of_observations_is_retained():
    initial = datetime(2025, 5, 20, tzinfo=UTC)
    details = {}
    for index in range(RETAINED_MARKS + 5):
        record_manual_equity(
            details,
            {"cash": 400 + index},
            {},
            captured_at=initial + timedelta(minutes=index),
        )
    assert len(details["manual_equity_marks"]) == RETAINED_MARKS
    assert details["manual_equity_marks"][0]["cash"] == 405
