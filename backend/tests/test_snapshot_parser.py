import pytest

from investos.services.portfolio import PortfolioService

parse = PortfolioService.parse_holdings_snapshot


def test_freeform_ticker_qty():
    out = parse("AUTO 10\nMEMA 22.5\nMEMB,15")
    assert out["holdings"] == [
        {"ticker": "AUTO", "quantity": 10.0},
        {"ticker": "MEMA", "quantity": 22.5},
        {"ticker": "MEMB", "quantity": 15.0},
    ]
    assert out["cash"] is None


def test_reversed_and_colon_and_cash():
    out = parse("10 AUTO\nNVDA: 5\nCash: 2,500.50")
    assert {"ticker": "AUTO", "quantity": 10.0} in out["holdings"]
    assert {"ticker": "NVDA", "quantity": 5.0} in out["holdings"]
    assert out["cash"] == 2500.50


def test_csv_with_header():
    csv_text = "Symbol,Quantity,Price\nAUTO,10,250.00\nMEMA,22,90.00"
    out = parse(csv_text)
    assert out["holdings"] == [
        {"ticker": "AUTO", "quantity": 10.0},
        {"ticker": "MEMA", "quantity": 22.0},
    ]


def test_dollar_signs_and_blank_lines_ignored():
    out = parse("\n  \nAAPL  $0\nGLW 1\n")
    assert {"ticker": "GLW", "quantity": 1.0} in out["holdings"]
    assert {"ticker": "AAPL", "quantity": 0.0} in out["holdings"]


@pytest.mark.parametrize(
    "bad_line",
    ["just some prose with no holdings at all", "MEMA missing", "Buying power: 200"],
)
def test_partial_statement_is_rejected_instead_of_silently_comparing(bad_line):
    with pytest.raises(ValueError, match="Statement row 2"):
        parse(f"AUTO 10\n{bad_line}")


def test_csv_preserves_blank_quantity_and_quoted_commas():
    result = parse('Symbol,Quantity,Price\nAUTO,"1,250.50",2\n')
    assert result["holdings"][0]["quantity"] == 1250.5
    with pytest.raises(ValueError, match="Statement row 2"):
        parse("Symbol,Quantity,Price\nAUTO,,2\n")
