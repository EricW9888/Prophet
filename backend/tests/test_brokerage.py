from investos.services.brokerage import BrokerageService


def test_plaid_holdings_snapshot_combines_positions_and_separates_cash():
    result = BrokerageService._normalize_holdings_response(
        {
            "accounts": [{"account_id": "acct-1"}],
            "securities": [
                {
                    "security_id": "sec-1",
                    "ticker_symbol": "MEMB",
                    "is_cash_equivalent": False,
                },
                {
                    "security_id": "sec-2",
                    "ticker_symbol": "MEMB",
                    "is_cash_equivalent": False,
                },
                {
                    "security_id": "cash-1",
                    "ticker_symbol": None,
                    "is_cash_equivalent": True,
                },
                {
                    "security_id": "unknown",
                    "ticker_symbol": None,
                    "name": "Private asset",
                },
            ],
            "holdings": [
                {"security_id": "sec-1", "quantity": 2.5, "institution_value": 250},
                {"security_id": "sec-2", "quantity": 1.5, "institution_value": 150},
                {"security_id": "cash-1", "quantity": 1, "institution_value": 1250},
                {"security_id": "unknown", "quantity": 3, "institution_value": 300},
            ],
        },
        item_id="item-1",
    )

    assert result["holdings"] == [{"ticker": "MEMB", "quantity": 4.0}]
    assert result["cash"] == 1250.0
    assert result["account_count"] == 1
    assert result["item_id"] == "item-1"
    assert result["ignored"][0]["reason"] == "missing_ticker_or_quantity"
