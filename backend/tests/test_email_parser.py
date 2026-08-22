from investos.services.mailbox import GmailMailboxService

parse = GmailMailboxService._parse_robinhood_extended


def test_forward_split_ratio():
    r = parse("AAPL underwent a 4-for-1 stock split. Your shares were adjusted.")
    assert r["action"] == "split"
    assert r["ticker"] == "AAPL"
    assert abs(r["quantity"] - 4.0) < 1e-9
    assert r["document_type"] == "corporate_action"


def test_reverse_split_ratio_below_one():
    r = parse("GME had a 1-for-10 reverse split applied to your position.")
    assert r["action"] == "split"
    assert abs(r["quantity"] - 0.1) < 1e-9


def test_option_expiration():
    r = parse("Your AAPL $190 Call expired and was removed from your account.")
    assert r["action"] == "expire"
    assert r["ticker"] == "AAPL"


def test_interest_credit_maps_to_deposit():
    r = parse("You earned $1.23 in interest this month.")
    assert r["action"] == "deposit"
    assert r["ticker"] == "CASH"
    assert abs(r["price"] - 1.23) < 1e-9


def test_account_transfer_flags_for_reconciliation():
    r = parse("Your account transfer is complete. Shares have been received.")
    assert r["document_type"] == "account_transfer"
    assert r["action"] in {"transfer_in", "transfer_out"}
    assert r["confidence"] < 0.6
    assert GmailMailboxService._classification_requires_reconciliation(r)


def test_spinoff_routes_to_reconciliation_without_fabricated_terms():
    r = parse("MEMA shares were spun off from MEMH and are now in your account.")
    assert r["document_type"] == "corporate_action"
    assert r["action"] == "spinoff"
    assert r["quantity"] == 0.0
    assert r["price"] is None
    assert GmailMailboxService._classification_requires_reconciliation(r)


def test_option_assignment_routes_to_reconciliation():
    r = parse("Your AUTO $300 Call option was assigned.")
    assert r["document_type"] == "corporate_action"
    assert r["action"] == "assign"
    assert r["ticker"] == "AUTO"
    assert GmailMailboxService._classification_requires_reconciliation(r)


def test_split_and_expiry_remain_directly_replayable():
    assert not GmailMailboxService._classification_requires_reconciliation(
        parse("AAPL underwent a 4-for-1 stock split. Your shares were adjusted.")
    )
    assert not GmailMailboxService._classification_requires_reconciliation(
        parse("Your AAPL $190 Call expired and was removed from your account.")
    )


def test_broadened_withdrawal():
    r = parse("We transferred $500.00 to your bank account.")
    assert r["action"] == "withdrawal"
    assert abs(r["price"] - 500.0) < 1e-9


def test_broadened_deposit():
    r = parse("We received your $2,000.00 deposit.")
    assert r["action"] == "deposit"
    assert abs(r["price"] - 2000.0) < 1e-9


def test_unmatched_returns_none():
    assert parse("Here are this week's market movers and a hot stock tip.") is None


def test_spcx_order_execution_without_extra_status_words():
    service = GmailMailboxService.__new__(GmailMailboxService)
    result = service._parse_robinhood_deterministic(
        "Your order to buy 3 shares of ORBT executed at an average price of $22.14.",
        subject="Your order has been executed",
    )
    assert result["document_type"] == "order_confirmation"
    assert result["action"] == "buy"
    assert result["ticker"] == "ORBT"
    assert result["quantity"] == 3.0
    assert result["price"] == 22.14


def test_combined_gmail_scan_result_preserves_ok_status():
    result = GmailMailboxService._combine_scan_results(
        {
            "status": "ok",
            "processed_messages": 2,
            "transactions_created": 1,
            "skipped_existing": 3,
            "skipped_irrelevant": 4,
            "detail": "ok mode=UNSEEN matched=10 target=5",
        },
        {
            "status": "ok",
            "processed_messages": 5,
            "transactions_created": 2,
            "skipped_existing": 7,
            "skipped_irrelevant": 8,
            "detail": "ok mode=ALL matched=20 target=10",
        },
    )

    assert result["status"] == "ok"
    assert result["processed_messages"] == 7
    assert result["transactions_created"] == 3
    assert result["detail"].startswith("ok mode=UNSEEN")
