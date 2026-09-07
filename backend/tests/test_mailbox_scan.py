from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from investos.services import mailbox as module
from investos.services.mailbox import GmailMailboxService


@pytest.mark.parametrize(
    "statuses, failed, expected",
    [
        (["ok", "ok"], 0, "ok"),
        (["ok", "partial"], 1, "partial"),
        (["ok", "error"], 0, "error"),
        (["ok"], 1, "partial"),
        ([], 0, "error"),
    ],
)
def test_scan_status_does_not_hide_failures(statuses, failed, expected):
    result = GmailMailboxService._combine_scan_results(
        *[{"status": value, "failed_messages": failed} for value in statuses]
    )
    assert result["status"] == expected
    assert result["failed_messages"] == failed * len(statuses)


@pytest.mark.parametrize(
    "failure", [None, "select", "search", "fetch", "commit", "model", "backlog"]
)
async def test_scan_counts_only_committed_messages(monkeypatch, tmp_path, failure):
    class Mailbox:
        def login(self, *_):
            pass

        def select(self, *_, **__):
            return ("NO" if failure == "select" else "OK"), [b"1"]

        def uid(self, action, *_):
            if action == "SEARCH":
                return ("NO" if failure == "search" else "OK"), [
                    b"1 2 3" if failure in {"model", "backlog"} else b"1"
                ]
            if failure == "fetch":
                return "NO", [None]
            return "OK", [
                (b"1", b"Subject: Synthetic confirmation\r\n\r\nSynthetic body")
            ]

        def logout(self):
            pass

    session = AsyncMock()
    session.__aenter__.return_value = session
    if failure == "commit":
        session.commit.side_effect = RuntimeError("synthetic commit failure")
    monkeypatch.setattr("investos.db.async_session_maker", lambda: session)

    def connect(*_, **kwargs):
        import ssl

        assert kwargs["ssl_context"].verify_mode == ssl.CERT_REQUIRED
        assert kwargs["ssl_context"].check_hostname
        assert kwargs["timeout"] == 30
        return Mailbox()

    monkeypatch.setattr(module.imaplib, "IMAP4_SSL", connect)
    monkeypatch.setattr(module, "REPO_ROOT", tmp_path)
    runtime = SimpleNamespace(
        imap_host="mail.example.test",
        imap_port=993,
        username="test",
        password="test-placeholder",
        folder="Statements",
        required_subject_keywords=[],
    )
    monkeypatch.setattr(module, "build_imap_search_query", lambda *_: "ALL")
    monkeypatch.setattr(
        module.RuntimeSettingsStore,
        "load",
        lambda: SimpleNamespace(llm=SimpleNamespace(provider="test")),
    )
    monkeypatch.setattr(
        GmailMailboxService, "_already_ingested", AsyncMock(return_value=False)
    )

    async def preserve_order(self, uids, runtime):
        return uids

    monkeypatch.setattr(GmailMailboxService, "_prioritize_pending_uids", preserve_order)
    if failure == "backlog":

        async def completed(self, uid, runtime=None):
            return uid == "1"

        monkeypatch.setattr(GmailMailboxService, "_already_ingested", completed)
    monkeypatch.setattr(GmailMailboxService, "_is_allowed_message", lambda *_: True)
    monkeypatch.setattr(
        GmailMailboxService,
        "_process_message",
        AsyncMock(return_value={"transaction_created": True}),
    )
    if failure == "model":

        async def process(self, uid, message, runtime=None, *, allow_model=True):
            assert allow_model is (uid == "1")
            return (
                {"transaction_created": True}
                if uid == "2"
                else {"classification_deferred": True}
            )

        monkeypatch.setattr(GmailMailboxService, "_process_message", process)
    monkeypatch.setattr(
        module.PortfolioService, "recalculate_all_positions", AsyncMock()
    )
    service = GmailMailboxService(session)

    if failure in {"select", "search"}:
        with pytest.raises(RuntimeError, match="gmail_"):
            await service._scan_mailbox(runtime=runtime, search_mode="ALL", limit=10)
        session.commit.assert_not_awaited()
        return
    result = await service._scan_mailbox(
        runtime=runtime, search_mode="ALL", limit=1 if failure == "backlog" else 10
    )
    if failure == "backlog":
        assert result["skipped_existing"] == 1
        assert result["transactions_created"] == 1
        assert result["remaining_messages"] == 1
        assert result["status"] == "partial"
        assert GmailMailboxService._process_message.call_args.args[0] == "2"
        return
    if failure == "model":
        assert result["deferred_messages"] == 2
        assert result["failed_messages"] == 0
        assert result["transactions_created"] == 1
        assert result["status"] == "partial"
        return
    assert result["status"] == ("partial" if failure else "ok")
    assert result["failed_messages"] == int(bool(failure))
    assert result["processed_messages"] == int(not failure)
    assert result["transactions_created"] == int(not failure)


@pytest.mark.parametrize("allow_model", [False, True])
async def test_unavailable_classifier_defers_without_posting_unknown_mail(
    monkeypatch, allow_model
):
    from email.message import EmailMessage

    from investos.core.llm import LLMProviderCooldownError

    session = AsyncMock()
    service = GmailMailboxService(session)
    classifier = AsyncMock(side_effect=LLMProviderCooldownError("Synthetic cooldown"))
    monkeypatch.setattr(service, "_classify_message", classifier)
    source = AsyncMock()
    monkeypatch.setattr(service, "_get_or_create_email_source", source)
    defer = AsyncMock(return_value={"classification_deferred": True})
    monkeypatch.setattr(service, "_preserve_deferred_receipt", defer)
    message = EmailMessage()
    message["From"] = "sender@example.test"
    message.set_content("Synthetic unknown message")
    result = await service._process_message("1", message, allow_model=allow_model)
    assert result == {"classification_deferred": True}
    assert classifier.await_count == int(allow_model)
    defer.assert_awaited_once()
    source.assert_not_awaited()
    session.commit.assert_not_awaited()


@pytest.mark.parametrize(
    "linked, state, complete",
    [(None, None, False), ("txn", None, True), (None, "needs_review", True)],
)
async def test_evidence_alone_does_not_prove_a_posted_transaction(
    monkeypatch, linked, state, complete
):
    session = AsyncMock()
    session.scalar.return_value = linked
    service = GmailMailboxService(session)
    monkeypatch.setattr(
        service,
        "_existing_receipt",
        AsyncMock(
            return_value=SimpleNamespace(
                id="receipt",
                metadata_json={"operational_mailbox": True, "mailbox_status": state},
            )
        ),
    )
    assert await service._already_ingested("1") is complete


@pytest.mark.parametrize(
    "sender, allowed",
    [
        ("Broker <statements@broker.example>", True),
        ("broker.example <attacker@evil.example>", False),
        ("attacker@broker.example.evil.example", False),
        ("statements@broker.example", True),
    ],
)
def test_sender_scope_checks_address_not_display_name(sender, allowed):
    from email.message import EmailMessage

    message = EmailMessage()
    message["From"] = sender
    message["Subject"] = "Dividend receipt"
    runtime = SimpleNamespace(
        allowed_senders=["statements@broker.example"],
        allowed_domains=["broker.example"],
        required_subject_keywords=[],
    )
    assert (
        GmailMailboxService.__new__(GmailMailboxService)._is_allowed_message(
            message, runtime
        )
        is allowed
    )


def test_explicit_scope_does_not_add_subject_filter():
    from email.message import EmailMessage

    message = EmailMessage()
    message["Subject"] = "An investment in Marketing Corp paid a dividend"
    message["From"] = "notice@broker.example"
    runtime = SimpleNamespace(
        folder="Robinhood",
        allowed_senders=[],
        allowed_domains=[],
        required_subject_keywords=[],
    )
    service = GmailMailboxService.__new__(GmailMailboxService)
    assert service._is_allowed_message(message, runtime)


@pytest.mark.parametrize(
    "overrides",
    [
        {"ticker": ""},
        {"quantity": 0},
        {"price": 0},
        {"quantity": float("nan")},
        {"price": float("inf")},
        {"ticker": "CASH"},
        {"action": "expire"},
    ],
)
def test_incomplete_or_invalid_transaction_fields_require_review(overrides):
    from datetime import UTC, datetime

    payload = {"action": "buy", "ticker": "SYNTH", "quantity": 2, "price": 10}
    payload.update(overrides)
    assert not GmailMailboxService._has_postable_fields(payload, datetime.now(UTC))


async def test_concurrent_scan_reports_busy(monkeypatch):
    connection = AsyncMock()
    connection.__aenter__.return_value = connection
    connection.scalar.return_value = False
    monkeypatch.setattr(
        "investos.db.engine", SimpleNamespace(connect=lambda: connection)
    )
    service = GmailMailboxService(AsyncMock())
    scan = AsyncMock()
    monkeypatch.setattr(service, "_scan_mailbox", scan)
    result = await service._run_mailbox_scan(runtime=None, search_mode="ALL", limit=20)
    assert result["status"] == "busy"
    scan.assert_not_awaited()


@pytest.mark.parametrize("failed", [False, True])
async def test_scan_lock_is_released_on_success_and_failure(monkeypatch, failed):
    connection = AsyncMock()
    connection.__aenter__.return_value = connection
    connection.scalar.return_value = True
    monkeypatch.setattr(
        "investos.db.engine", SimpleNamespace(connect=lambda: connection)
    )
    service = GmailMailboxService(AsyncMock())
    scan = AsyncMock(return_value={"status": "ok"})
    if failed:
        scan.side_effect = RuntimeError("synthetic")
    monkeypatch.setattr(service, "_scan_mailbox", scan)
    if failed:
        with pytest.raises(RuntimeError, match="synthetic"):
            await service._run_mailbox_scan(runtime=None, search_mode="ALL", limit=20)
    else:
        await service._run_mailbox_scan(runtime=None, search_mode="ALL", limit=20)
    assert "pg_advisory_unlock" in str(connection.execute.call_args.args[0])


@pytest.mark.parametrize("size", [0, 3, 20000])
async def test_retry_order_is_bounded_and_stable_for_large_folders(size):
    session = AsyncMock()
    session.execute.return_value = SimpleNamespace(all=lambda: [])
    service = GmailMailboxService(session)
    uids = [str(i).encode() for i in range(size)]
    assert (
        await service._prioritize_pending_uids(
            uids, SimpleNamespace(folder="Synthetic")
        )
        == uids
    )
    assert session.execute.await_count == (size * 2 + 999) // 1000
    for call in session.execute.call_args_list:
        params = call.args[0].compile().params
        assert (
            sum(len(value) for value in params.values() if isinstance(value, list))
            <= 2000
        )
