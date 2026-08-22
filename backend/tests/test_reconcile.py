from decimal import Decimal

from investos.services.portfolio import PortfolioService


def test_in_sync_when_book_matches_snapshot():
    book = [{"ticker": "AAPL", "quantity": 10}, {"ticker": "msft", "quantity": 5}]
    snap = [{"ticker": "aapl", "quantity": 10}, {"ticker": "MSFT", "quantity": 5}]
    assert PortfolioService.diff_positions(book, snap) == []


def test_detects_missing_extra_and_mismatch():
    book = [{"ticker": "AAPL", "quantity": 10}, {"ticker": "GME", "quantity": 3}]
    snap = [{"ticker": "AAPL", "quantity": 8}, {"ticker": "NVDA", "quantity": 4}]
    diffs = {d["ticker"]: d for d in PortfolioService.diff_positions(book, snap)}

    assert diffs["AAPL"]["kind"] == "quantity_mismatch"
    assert diffs["AAPL"]["delta"] == Decimal("-2")  # broker has 2 fewer
    assert diffs["GME"]["kind"] == "extra_in_book"  # we have it, broker doesn't
    assert diffs["NVDA"]["kind"] == "missing_in_book"  # broker has it, we don't


def test_tolerance_ignores_tiny_float_drift():
    book = [{"ticker": "AAPL", "quantity": 10.00001}]
    snap = [{"ticker": "AAPL", "quantity": 10.0}]
    assert PortfolioService.diff_positions(book, snap) == []


def test_folds_duplicate_tickers():
    # Two lots/rows for the same ticker should sum before comparison.
    book = [{"ticker": "AAPL", "quantity": 4}, {"ticker": "AAPL", "quantity": 6}]
    snap = [{"ticker": "AAPL", "quantity": 10}]
    assert PortfolioService.diff_positions(book, snap) == []
