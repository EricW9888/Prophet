from __future__ import annotations

import asyncio
import email
import imaplib
import json
import re
from datetime import UTC, datetime
from email.header import decode_header, make_header
from email.message import Message
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investos.core.imap_utils import build_imap_search_query
from investos.core.llm import call_llm_json
from investos.core.prompting import bounded_document_excerpt
from investos.models.evidence import RawEvidence
from investos.models.review import ReviewQueueItem
from investos.models.source import Source
from investos.schemas.evidence import RawEvidenceCreate
from investos.schemas.integrations import (
    GmailIntegrationTestRequest,
    GmailIntegrationTestResponse,
)
from investos.schemas.portfolio import TransactionCreate
from investos.services.ingestion import IngestionService
from investos.services.portfolio import PortfolioService
from investos.services.runtime_settings import RuntimeSettingsStore

REPO_ROOT = Path(__file__).resolve().parents[3]

ORDER_CONFIRMATION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "document_type": {
            "type": "string",
            "enum": [
                "order_confirmation",
                "dividend_notice",
                "cash_activity",
                "corporate_action",
                "account_transfer",
                "newsletter",
                "other",
            ],
        },
        "confidence": {
            "type": "number",
            "minimum": 0,
            "maximum": 1,
            "description": "Score from 0.0 to 1.0",
        },
        "ticker": {"type": "string"},
        "action": {"type": "string"},
        "quantity": {"type": "number"},
        "price": {"type": "number"},
        "executed_at": {"type": "string"},
        "notes": {"type": "string"},
        "source_item_type": {"type": "string"},
    },
    "required": [
        "document_type",
        "confidence",
        "ticker",
        "action",
        "quantity",
        "price",
        "executed_at",
        "notes",
        "source_item_type",
    ],
}

GMAIL_OPERATIONAL_SOURCE_NAME = "Gmail Operational Inbox"
ROBINHOOD_DEFAULT_SUBJECT_KEYWORDS = [
    "executed",
    "confirmation",
    "deposit",
    "withdrawal",
    "transfer",
]
RECONCILIATION_DOCUMENT_TYPES = {"account_transfer"}
RECONCILIATION_CORPORATE_ACTIONS = {"merger", "spinoff", "exercise", "assign"}


class GmailMailboxService:
    def __init__(self, session: AsyncSession):
        self.session = session
        self.ingestion = IngestionService(session)
        self.portfolio = PortfolioService(session)

    async def sync_recent_messages(self, limit: int | None = None) -> dict[str, Any]:
        runtime = RuntimeSettingsStore.load().gmail
        issue = self._configuration_issue(
            runtime, require_enabled=True, require_scope=True
        )
        if issue:
            return {
                "status": issue,
                "processed_messages": 0,
                "transactions_created": 0,
                "skipped_existing": 0,
                "skipped_irrelevant": 0,
                "detail": issue,
            }
        if runtime.only_unseen:
            unseen = await self._run_mailbox_scan(
                runtime=runtime,
                search_mode="UNSEEN",
                limit=(limit or runtime.fetch_limit),
            )
            all_recent = await self._run_mailbox_scan(
                runtime=runtime,
                search_mode="ALL",
                limit=(limit or runtime.fetch_limit),
            )
            return self._combine_scan_results(unseen, all_recent)

        return await self._run_mailbox_scan(
            runtime=runtime,
            search_mode="ALL",
            limit=(limit or runtime.fetch_limit),
        )

    async def backfill_scoped_label(self, limit: int = 5000) -> dict[str, Any]:
        """
        Deep scan of a specific label/folder.
        Defaults to a large limit (5000) for historical backfill.
        """
        runtime = RuntimeSettingsStore.load().gmail
        if not self._is_scope_ready(runtime):
            return {"error": "gmail_scope_required"}

        return await self._run_mailbox_scan(
            runtime=runtime,
            search_mode="ALL",
            limit=limit,
        )

    async def _run_mailbox_scan(
        self,
        *,
        runtime,
        search_mode: str,
        limit: int,
    ) -> dict[str, Any]:
        processed = 0
        transactions_created = 0
        skipped_existing = 0
        skipped_irrelevant = 0

        mailbox = imaplib.IMAP4_SSL(runtime.imap_host, runtime.imap_port)
        try:
            mailbox.login(runtime.username, runtime.password)

            # Resolve folder
            actual_folder = runtime.folder
            try:
                mailbox.select(runtime.folder, readonly=True)
            except Exception:
                found = False
                _, all_folders = mailbox.list()
                for f in all_folders:
                    name = f.decode("utf-8").split(' "/" ')[-1].strip('"')
                    if runtime.folder.lower() in name.lower():
                        actual_folder = name
                        mailbox.select(actual_folder, readonly=True)
                        found = True
                        break
                if not found:
                    raise ValueError(f"Mailbox folder '{runtime.folder}' not found.")

            if (
                not runtime.required_subject_keywords
                and runtime.folder.lower() == "robinhood"
            ):
                runtime.required_subject_keywords = ROBINHOOD_DEFAULT_SUBJECT_KEYWORDS
            search_query = build_imap_search_query(runtime, search_mode)
            log_path = REPO_ROOT / "data" / "backfill_status.log"
            with open(log_path, "a") as f:
                f.write(
                    f"[{datetime.now().isoformat()}] Searching folder={actual_folder} "
                    f"mode={search_mode} query={search_query}\n"
                )

            # Use UID for stable tracking across sessions
            _, data = mailbox.uid("SEARCH", None, search_query)
            uids = data[0].split()

            # We want to process the newest limit messages in chronological order (oldest first)
            # to ensure BUY transactions are processed before dependent SELL transactions.
            targeted_uids = uids[-limit:]
            with open(log_path, "a") as f:
                f.write(
                    f"[{datetime.now().isoformat()}] Search matched {len(uids)} UID(s); "
                    f"processing {len(targeted_uids)} newest.\n"
                )

            # Process in batches of 3 to respect rate limits
            log_path = REPO_ROOT / "data" / "backfill_status.log"
            batch_size = 3

            from investos.db import async_session_maker

            for i in range(0, len(targeted_uids), batch_size):
                batch = targeted_uids[i : i + batch_size]

                async def _process_uid(uid_bytes):
                    nonlocal processed, transactions_created, skipped_existing, skipped_irrelevant
                    async with async_session_maker() as session:
                        # Each parallel task must have its own session to avoid concurrency errors
                        local_service = GmailMailboxService(session)

                        uid = uid_bytes.decode("utf-8")
                        from investos.services.runtime_settings import (
                            RuntimeSettingsStore,
                        )

                        llm_provider = RuntimeSettingsStore.load().llm.provider

                        with open(log_path, "a") as f:
                            f.write(
                                f"[{datetime.now().isoformat()}] Scanning UID {uid} using {llm_provider}...\n"
                            )

                        if await local_service._already_ingested(uid, runtime=runtime):
                            skipped_existing += 1
                            return

                        _, raw_data = mailbox.uid("FETCH", uid, "(RFC822)")
                        if not raw_data or not raw_data[0]:
                            return

                        message = email.message_from_bytes(raw_data[0][1])
                        if not local_service._is_allowed_message(message, runtime):
                            await local_service._record_skip(
                                uid, message, runtime=runtime
                            )
                            skipped_irrelevant += 1
                            return

                        try:
                            result = await local_service._process_message(
                                uid, message, runtime=runtime
                            )
                            if result.get("skipped_irrelevant"):
                                skipped_irrelevant += 1
                            else:
                                processed += 1
                                if result.get("transaction_created"):
                                    transactions_created += 1
                                    meta = result.get("metadata", {})
                                    source_tag = (
                                        "[DETERMINISTIC]"
                                        if meta.get("confidence") == 1.0
                                        else "[LLM]"
                                    )
                                    details = f"{source_tag} {str(meta.get('action')).upper()} {meta.get('quantity')} {meta.get('ticker')} @ ${meta.get('price')}"
                                    with open(log_path, "a") as f:
                                        f.write(
                                            f"[{datetime.now().isoformat()}] SUCCESS: UID {uid} -> {details}\n"
                                        )

                            # Persist immediately during backfill to show progress
                            await session.commit()
                        except Exception as exc:
                            with open(log_path, "a") as f:
                                f.write(
                                    f"[{datetime.now().isoformat()}] FAILED: UID {uid} - {str(exc)}\n"
                                )

                # Run the batch in parallel
                await asyncio.gather(*[_process_uid(u) for u in batch])

                # Yield to event loop
                await asyncio.sleep(0.1)

            # Rebuild all positions and cash ledger to ensure perfect FIFO matching and balances
            await self.portfolio.recalculate_all_positions()

        finally:
            try:
                mailbox.logout()
            except Exception:
                pass

        return {
            "status": "ok",
            "processed_messages": processed,
            "transactions_created": transactions_created,
            "skipped_existing": skipped_existing,
            "skipped_irrelevant": skipped_irrelevant,
            "detail": f"ok mode={search_mode} matched={len(uids)} target={len(targeted_uids)}",
        }

    @staticmethod
    def _combine_scan_results(*results: dict[str, Any]) -> dict[str, Any]:
        statuses = [str(result.get("status") or "") for result in results]
        return {
            "status": "ok" if all(status == "ok" for status in statuses) else "error",
            "processed_messages": sum(
                int(result.get("processed_messages") or 0) for result in results
            ),
            "transactions_created": sum(
                int(result.get("transactions_created") or 0) for result in results
            ),
            "skipped_existing": sum(
                int(result.get("skipped_existing") or 0) for result in results
            ),
            "skipped_irrelevant": sum(
                int(result.get("skipped_irrelevant") or 0) for result in results
            ),
            "detail": " + ".join(
                str(result.get("detail") or "ok") for result in results
            ),
        }

    async def test_connection(
        self, payload: GmailIntegrationTestRequest
    ) -> GmailIntegrationTestResponse:
        stored = RuntimeSettingsStore.load().gmail
        password = payload.password or stored.password
        if not password:
            raise ValueError("A Gmail app password is required to test the connection.")

        mailbox = imaplib.IMAP4_SSL(payload.imap_host, payload.imap_port)
        scanned = 0
        matched = 0
        sample_subjects: list[str] = []
        runtime = payload.model_dump()
        runtime["password"] = password
        try:
            mailbox.login(payload.username, password)
            mailbox.select(payload.folder)
            search_mode = "UNSEEN" if payload.only_unseen else "ALL"
            _, data = mailbox.search(None, search_mode)
            message_ids = data[0].split()
            recent_ids = message_ids[-payload.fetch_limit :]

            for msg_id in recent_ids:
                _, raw_data = mailbox.fetch(msg_id, "(RFC822)")
                if not raw_data or not raw_data[0]:
                    continue
                scanned += 1
                message = email.message_from_bytes(raw_data[0][1])
                if self._is_allowed_message(message, type("Runtime", (), runtime)()):
                    matched += 1
                    sample_subjects.append(
                        self._decode_header(message.get("Subject", "(no subject)"))
                    )
                    if len(sample_subjects) >= 5:
                        break
            return GmailIntegrationTestResponse(
                ok=True,
                detail="Connection succeeded.",
                matched_messages=matched,
                scanned_messages=scanned,
                sample_subjects=sample_subjects,
            )
        finally:
            try:
                mailbox.close()
            except Exception as exc:
                import logging

                logging.getLogger(__name__).exception("Masked failure caught")
                pass
            mailbox.logout()

    def is_configured(
        self, *, require_enabled: bool = True, require_scope: bool = False
    ) -> bool:
        runtime = RuntimeSettingsStore.load().gmail
        return (
            self._configuration_issue(
                runtime, require_enabled=require_enabled, require_scope=require_scope
            )
            is None
        )

    def _is_scope_ready(self, runtime) -> bool:
        return bool(
            runtime.folder.strip().upper() != "INBOX"
            or runtime.allowed_senders
            or runtime.allowed_domains
            or runtime.required_subject_keywords
        )

    def _configuration_issue(
        self,
        runtime,
        *,
        require_enabled: bool,
        require_scope: bool,
    ) -> str | None:
        if require_enabled and not runtime.enabled:
            return "gmail_sync_disabled"
        if not runtime.username or not runtime.password:
            return "gmail_credentials_missing"
        if require_scope and not self._is_scope_ready(runtime):
            return "gmail_scope_required"
        return None

    def _external_id_for_uid(self, uid: str, runtime=None) -> str:
        if runtime is None:
            return uid
        folder = str(getattr(runtime, "folder", "") or "INBOX").strip() or "INBOX"
        return f"gmail:{folder}:{uid}"

    async def _already_ingested(self, uid: str, runtime=None) -> bool:
        external_id = self._external_id_for_uid(uid, runtime)
        external_ids = [uid] if external_id == uid else [external_id, uid]
        existing = (
            await self.session.execute(
                select(RawEvidence).where(RawEvidence.external_id.in_(external_ids))
            )
        ).scalar_one_or_none()
        return existing is not None

    def _parse_robinhood_deterministic(
        self, body: str, subject: str = ""
    ) -> dict[str, Any] | None:
        """
        High-performance regex parser for standard Robinhood execution emails.
        Bypasses LLM for 100% accuracy on known templates.
        """
        import re

        subj_lower = subject.lower()
        body_lower = body.lower()

        # 0. Detect and Skip Marketing, Statements, and Alerts (Ignored / Irrelevant)
        is_skip = False
        skip_reason = ""

        if "confirmations are available" in subj_lower:
            is_skip = True
            skip_reason = "Statement/confirmation notification"
        elif (
            "statement is available" in subj_lower
            or "statements are available" in subj_lower
        ):
            is_skip = True
            skip_reason = "Statement notification"
        elif "action required" in subj_lower or "action required" in body_lower:
            is_skip = True
            skip_reason = "Action required / pending action notice"
        elif "pending deposit" in body_lower or "pending deposit" in subj_lower:
            is_skip = True
            skip_reason = "Pending deposit notice"
        elif ("transfer" in subj_lower and "bonus" in subj_lower) or (
            "transfer" in body_lower and "bonus" in body_lower
        ):
            is_skip = True
            skip_reason = "Transfer bonus marketing"
        elif "deposit match" in subj_lower or "deposit match" in body_lower:
            is_skip = True
            skip_reason = "Deposit match marketing"
        elif "retirement transfer" in subj_lower or "retirement transfer" in body_lower:
            is_skip = True
            skip_reason = "Retirement transfer marketing"
        elif "cash bonus" in subj_lower or "cash bonus" in body_lower:
            is_skip = True
            skip_reason = "Cash bonus marketing"
        elif "margin balance" in subj_lower:
            is_skip = True
            skip_reason = "Margin transfer bonus warning"
        elif "crypto transfer" in subj_lower:
            is_skip = True
            skip_reason = "Crypto transfer marketing"
        elif "is now on robinhood" in subj_lower:
            is_skip = True
            skip_reason = "New asset announcement"
        elif (
            "vote in the" in subj_lower
            or "proxy" in subj_lower
            or "annual meeting" in subj_lower
            or "special meeting" in subj_lower
        ):
            is_skip = True
            skip_reason = "Proxy vote/shareholder notice"
        elif "learn, open, fund" in subj_lower or "learn and trade" in subj_lower:
            is_skip = True
            skip_reason = "Learn promo"
        elif "recent login" in subj_lower:
            is_skip = True
            skip_reason = "Security login alert"
        elif (
            "order has been canceled" in subj_lower
            or "order has been cancelled" in subj_lower
        ):
            is_skip = True
            skip_reason = "Canceled order notice"
        elif "privacy" in subj_lower or "agreement" in subj_lower:
            is_skip = True
            skip_reason = "Legal/Privacy notice"
        elif "dividends, paid early" in subj_lower:
            is_skip = True
            skip_reason = "Early dividends promo"

        if is_skip:
            return {
                "action": "other",
                "quantity": 0.0,
                "ticker": "CASH",
                "price": 0.0,
                "confidence": 1.0,
                "document_type": "other",
                "notes": f"Deterministic parse: Skip ({skip_reason})",
            }

        # 1. Standard Order Executions
        # Supports: "1 share", "10 shares", "1.5 shares", "EXMPL", etc.
        order_match = re.search(
            r"order to (buy|sell) ([\d,]+(?:\.\d+)?) share[s]? of ([A-Z0-9]+)(?: .*?)? (?:has been )?executed at an average price of \$([\d,]+(?:\.\d+)?)",
            body,
            re.IGNORECASE | re.DOTALL,
        )
        if order_match:
            action, qty, ticker, price = order_match.groups()
            return {
                "action": action.lower(),
                "quantity": float(qty.replace(",", "")),
                "ticker": ticker.upper(),
                "price": float(price.replace(",", "")),
                "confidence": 1.0,
                "document_type": "order_confirmation",
                "notes": f"Deterministic parse: {action.upper()} {qty} {ticker} @ ${price}",
            }

        # 2. Dividends
        # Example: "You received a dividend of $0.13 from EXMPL"
        div_match = re.search(
            r"received a dividend of \$([\d,.]+) from ([A-Z]+)", body, re.IGNORECASE
        )
        if div_match:
            amount, ticker = div_match.groups()
            return {
                "action": "dividend",
                "quantity": 0,  # Dividend quantity is usually 0 (cash)
                "ticker": ticker.upper(),
                "price": float(amount.replace(",", "")),
                "confidence": 1.0,
                "document_type": "dividend_notice",
                "notes": f"Deterministic parse: Dividend of ${amount} from {ticker}",
            }

        # 3. Deposits/Withdrawals
        # Example: "Your individual account deposit is complete ... Amount: $2,000.00"
        cash_match = re.search(
            r"(deposit|withdrawal) is complete.*?Amount:\s+\$([\d,.]+)",
            body,
            re.IGNORECASE | re.DOTALL,
        )
        if cash_match:
            action_type, amount = cash_match.groups()
            return {
                "action": (
                    "deposit" if "deposit" in action_type.lower() else "withdrawal"
                ),
                "quantity": 1.0,
                "ticker": "CASH",
                "price": float(amount.replace(",", "")),
                "confidence": 1.0,
                "document_type": "cash_activity",
                "notes": f"Deterministic parse: {action_type.capitalize()} of ${amount}",
            }

        # 4. Deposit completion (alternate layout, e.g. "Your deposit of $5,000.00 ... has completed")
        cash_match_alt = re.search(
            r"your deposit of \$([\d,.]+).*?has completed",
            body,
            re.IGNORECASE | re.DOTALL,
        )
        if cash_match_alt:
            amount = cash_match_alt.group(1)
            return {
                "action": "deposit",
                "quantity": 1.0,
                "ticker": "CASH",
                "price": float(amount.replace(",", "")),
                "confidence": 1.0,
                "document_type": "cash_activity",
                "notes": f"Deterministic parse: Deposit of ${amount} (alt template)",
            }

        # 5+. Corporate actions, transfers, options, interest, broadened cash.
        extended = self._parse_robinhood_extended(body)
        if extended:
            return extended

        return None

    @staticmethod
    def _parse_robinhood_extended(body: str) -> dict[str, Any] | None:
        """Deterministic patterns for the rest of the account lifecycle.

        Kept as a pure static method (no DB, no self) so it can be unit-tested
        against representative email bodies. Robinhood emails are templated, so
        deterministic extraction is safe — and far more trustworthy than letting
        the LLM guess a split ratio or a transferred share count.

        NOTE: the exact phrasings below are best-effort against documented
        Robinhood templates; validate against real samples and extend as needed.
        Unmatched lifecycle emails fall through to the LLM classifier.
        """
        import re

        # Stock split (forward or reverse): "... 4-for-1 stock split ..." /
        # "1-for-10 reverse split". Ratio is post/pre = first/second.
        split_match = re.search(
            r"\b([\d.]+)[\s-]for[\s-]([\d.]+)\b.{0,40}?(?:stock\s+)?(?:reverse\s+)?split",
            body,
            re.IGNORECASE | re.DOTALL,
        )
        if not split_match:
            split_match = re.search(
                r"split.{0,40}?\b([\d.]+)[\s-]for[\s-]([\d.]+)\b",
                body,
                re.IGNORECASE | re.DOTALL,
            )
        if split_match:
            ticker_match = re.search(
                r"\b([A-Z]{1,6})\b\s+(?:underwent|stock split|had a)", body
            )
            ticker = ticker_match.group(1) if ticker_match else None
            num = float(split_match.group(1))
            den = float(split_match.group(2)) or 1.0
            ratio = num / den
            return {
                "action": "split",
                "quantity": ratio,  # replay convention: post/pre ratio
                "ticker": (ticker or ""),
                "price": None,
                "confidence": 1.0 if ticker else 0.6,
                "document_type": "corporate_action",
                "notes": f"Deterministic parse: {split_match.group(1)}-for-{split_match.group(2)} split (ratio {ratio:g})",
            }

        # Option expiration worthless: "Your EXMPL $100 Call expired"
        # Ticker must be a real uppercase symbol (no IGNORECASE on the symbol).
        opt_exp = re.search(
            r"\b([A-Z]{1,6})\b\s*\$?[\d.,]*\s*(?:[Cc]all|[Pp]ut)\b.{0,30}?expired",
            body,
            re.DOTALL,
        )
        if opt_exp:
            return {
                "action": "expire",
                "quantity": 0.0,
                "ticker": opt_exp.group(1).upper(),
                "price": None,
                "confidence": 0.9,
                "document_type": "corporate_action",
                "notes": "Deterministic parse: option expired worthless",
            }

        # Complex corporate actions need a target security, conversion ratio,
        # strike, cash component, or tax-lot treatment before replay. Detect the
        # event deterministically, then send it to reconciliation instead of
        # guessing book mutations.
        complex_actions = (
            (
                "spinoff",
                re.compile(r"\b(?:spin[ -]?off|spun off)\b", re.IGNORECASE),
            ),
            (
                "merger",
                re.compile(
                    r"\b(?:merger|merged with|acquisition (?:closed|completed)|was acquired)\b",
                    re.IGNORECASE,
                ),
            ),
            (
                "exercise",
                re.compile(
                    r"\b(?:option|call|put|warrant)s?\b.{0,45}\bexercised\b|\bexercised\b.{0,45}\b(?:option|call|put|warrant)s?\b",
                    re.IGNORECASE | re.DOTALL,
                ),
            ),
            (
                "assign",
                re.compile(
                    r"\b(?:option|call|put)s?\b.{0,45}\bassigned\b|\bassigned\b.{0,45}\b(?:option|call|put)s?\b",
                    re.IGNORECASE | re.DOTALL,
                ),
            ),
        )
        for action, pattern in complex_actions:
            if not pattern.search(body):
                continue
            ticker_match = re.search(
                r"\b([A-Z]{1,6})\b(?=.{0,35}\b(?:option|call|put|warrant|shares?|stock|merger|spin[ -]?off))",
                body,
                re.DOTALL,
            )
            ticker = ticker_match.group(1) if ticker_match else ""
            return {
                "action": action,
                "quantity": 0.0,
                "ticker": ticker,
                "price": None,
                "confidence": 0.7 if ticker else 0.55,
                "document_type": "corporate_action",
                "notes": (
                    f"Deterministic parse: {action} detected; target security, ratio, cash component, "
                    "and lot treatment require reconciliation"
                ),
            }

        # Interest payment (cash credit). Map to a deposit-type cash entry.
        interest = re.search(
            r"(?:interest payment of|earned)\s+\$([\d,.]+)\s+in interest|interest payment of \$([\d,.]+)",
            body,
            re.IGNORECASE,
        )
        if interest:
            amount = next(g for g in interest.groups() if g)
            return {
                "action": "deposit",
                "quantity": 1.0,
                "ticker": "CASH",
                "price": float(amount.replace(",", "")),
                "confidence": 1.0,
                "document_type": "cash_activity",
                "notes": f"Deterministic parse: interest credit of ${amount}",
            }

        # Account transfer (ACATS) in/out: shares move without a buy/sell, so
        # cost basis is unknown. Don't fabricate it — flag for reconciliation.
        if re.search(
            r"account transfer.{0,40}?(?:complete|received|initiated)",
            body,
            re.IGNORECASE | re.DOTALL,
        ) or re.search(r"\bACATS\b", body):
            outgoing = bool(
                re.search(r"out of|to your other|transfer out", body, re.IGNORECASE)
            )
            return {
                "action": "transfer_out" if outgoing else "transfer_in",
                "quantity": 0.0,
                "ticker": "",
                "price": None,
                "confidence": 0.5,
                "document_type": "account_transfer",
                "notes": "Deterministic parse: account transfer detected (cost basis unknown — needs reconciliation)",
            }

        # Broadened withdrawal phrasings.
        wd = re.search(
            r"(?:withdrawal of|withdrew|transferred)\s+\$([\d,.]+)\s+to your bank",
            body,
            re.IGNORECASE,
        )
        if wd:
            amount = wd.group(1)
            return {
                "action": "withdrawal",
                "quantity": 1.0,
                "ticker": "CASH",
                "price": float(amount.replace(",", "")),
                "confidence": 1.0,
                "document_type": "cash_activity",
                "notes": f"Deterministic parse: withdrawal of ${amount}",
            }

        # Broadened deposit phrasings ("We received your $X deposit").
        dep = re.search(
            r"received your \$([\d,.]+) deposit|\$([\d,.]+) deposit was successful",
            body,
            re.IGNORECASE,
        )
        if dep:
            amount = next(g for g in dep.groups() if g)
            return {
                "action": "deposit",
                "quantity": 1.0,
                "ticker": "CASH",
                "price": float(amount.replace(",", "")),
                "confidence": 1.0,
                "document_type": "cash_activity",
                "notes": f"Deterministic parse: deposit of ${amount}",
            }

        return None

    async def _process_message(
        self, uid: str, message: Message, runtime=None
    ) -> dict[str, Any]:
        subject = self._decode_header(message.get("Subject", ""))
        sender = self._decode_header(message.get("From", ""))
        body = self._extract_text_body(message)
        public_time = self._parse_email_datetime(message.get("Date"))
        external_id = self._external_id_for_uid(uid, runtime)

        # 1. Try Deterministic Parsers first (Fast, Accurate, Free)
        classification = None
        if "robinhood" in sender.lower() or "robinhood" in subject.lower():
            classification = self._parse_robinhood_deterministic(body, subject=subject)

        # 2. Fallback to LLM if no deterministic match found
        if not classification:
            classification = await self._classify_message(
                subject=subject, sender=sender, body=body
            )

        source = await self._get_or_create_email_source()

        allowed_types = [
            "order_confirmation",
            "dividend_notice",
            "cash_activity",
            "corporate_action",
            "account_transfer",
        ]
        requires_reconciliation = self._classification_requires_reconciliation(
            classification
        )
        confidence_floor = 0.4 if requires_reconciliation else 0.6
        if (
            classification["document_type"] not in allowed_types
            or classification.get("confidence", 0) < confidence_floor
        ):
            # We record the evidence even if skipped to prevent re-scanning the same UID in future backfills
            evidence = RawEvidence(
                external_id=external_id,
                source_id=source.id,
                source_item_type="email",
                title=subject,
                is_processed=True,
                public_time=public_time,
                event_time=public_time,
                metadata_json={
                    "classification": classification,
                    "skipped": True,
                    "reason": "irrelevant_content",
                    "uid": uid,
                },
            )
            self.session.add(evidence)
            await self.session.flush()
            return {
                "transaction_created": False,
                "classification": classification["document_type"],
                "skipped_irrelevant": True,
            }

        evidence = await self.ingestion.ingest_text(
            RawEvidenceCreate(
                title=subject,
                source_id=source.id,
                source_item_type="email_order_confirmation",
                author=sender,
                public_time=public_time,
                metadata_json={
                    "content_type": "text/html",
                    "sender": sender,
                    "uid": uid,
                    "external_id": external_id,
                    "mailbox_classification": classification,
                    "skip_extraction": True,
                    "operational_mailbox": True,
                },
                content=f"From: {sender}\nSubject: {subject}\n\n{body}",
            ),
            process_now=False,
        )
        evidence.external_id = external_id
        evidence.is_processed = True
        # Time-honest: stamp when the event actually happened (email send date as
        # the best baseline; refined to the parsed transaction date below).
        evidence.event_time = public_time
        await self.session.commit()

        # Transfers and complex corporate actions cannot safely mutate the book
        # without authoritative target-security/ratio/lot details. Preserve the
        # email and queue one durable review item instead of silently discarding
        # it or fabricating a transaction.
        if requires_reconciliation:
            await self._queue_reconciliation_review(
                external_id=external_id,
                subject=subject,
                classification=classification,
            )
            await self.session.commit()
            return {
                "transaction_created": False,
                "evidence_id": str(evidence.id),
                "metadata": classification,
                "needs_reconciliation": True,
                "skipped_irrelevant": False,
            }

        if (
            classification["ticker"]
            and classification["action"]
            and (classification["quantity"] is not None)
        ):
            executed_at = self._parse_datetime_fallback(
                classification.get("executed_at"),
                public_time,
            )
            # The trade's execution date is the true event_time for this evidence.
            if executed_at is not None:
                evidence.event_time = executed_at
            await self.portfolio.add_transaction_by_ticker(
                ticker=classification["ticker"],
                txn_data=TransactionCreate(
                    action=classification["action"],
                    quantity=float(classification["quantity"]),
                    price=float(classification["price"] or 0.0),
                    executed_at=executed_at,
                    notes=classification.get("notes")
                    or f"Ingested from email: {subject}",
                    lot_type="broker_confirmation",
                    provenance_json={
                        "source": "gmail",
                        "source_type": "email_order_confirmation",
                        "source_label": "Broker confirmation email",
                        "raw_evidence_id": str(evidence.id),
                        "source_id": str(source.id),
                        "external_id": external_id,
                        "uid": uid,
                        "sender": sender,
                        "subject": subject,
                        "public_time": public_time.isoformat() if public_time else None,
                        "executed_at": executed_at.isoformat() if executed_at else None,
                        "document_type": classification.get("document_type"),
                        "confidence": classification.get("confidence"),
                        "parser": "mailbox_ingestion",
                    },
                ),
            )
            await self.session.commit()
            return {
                "transaction_created": True,
                "evidence_id": str(evidence.id),
                "metadata": classification,
                "skipped_irrelevant": False,
            }

        return {
            "transaction_created": False,
            "evidence_id": str(evidence.id),
            "metadata": classification,
            "skipped_irrelevant": False,
        }

    @staticmethod
    def _classification_requires_reconciliation(classification: dict[str, Any]) -> bool:
        document_type = str(classification.get("document_type") or "").strip().lower()
        action = str(classification.get("action") or "").strip().lower()
        return (
            document_type in RECONCILIATION_DOCUMENT_TYPES
            or action in RECONCILIATION_CORPORATE_ACTIONS
        )

    async def _queue_reconciliation_review(
        self,
        *,
        external_id: str,
        subject: str,
        classification: dict[str, Any],
    ) -> None:
        action = str(classification.get("action") or "corporate_action").strip().lower()
        document_type = (
            str(classification.get("document_type") or "corporate_action")
            .strip()
            .lower()
        )
        item_id = uuid5(NAMESPACE_URL, f"mailbox-reconciliation:{external_id}")
        reason = (
            f"{document_type.replace('_', ' ').title()} detected via broker email ({action}). "
            "Authoritative target-security, ratio, cash component, and cost-basis details are required "
            f"before changing the portfolio. Subject: {subject}"
        )
        existing = (
            await self.session.execute(
                select(ReviewQueueItem)
                .where(
                    ReviewQueueItem.item_id == item_id,
                    ReviewQueueItem.status.in_(["pending", "in_review"]),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if existing is not None:
            existing.trigger_reason = reason
            existing.priority_score = max(float(existing.priority_score or 0), 70.0)
            return
        self.session.add(
            ReviewQueueItem(
                item_type=(
                    "account_transfer"
                    if document_type == "account_transfer"
                    else "corporate_action"
                ),
                item_id=item_id,
                priority_score=70.0,
                trigger_reason=reason,
            )
        )

    async def _classify_message(
        self, *, subject: str, sender: str, body: str, model: str | None = None
    ) -> dict[str, Any]:
        # Prophet Backfill Guard: Use a semaphore to prevent bombarding the LLM during historical deep scans
        if not hasattr(self, "_llm_semaphore"):
            # Limit to 1 concurrent classification to respect rate limits
            self._llm_semaphore = asyncio.Semaphore(1)

        async with self._llm_semaphore:
            res = await call_llm_json(
                system_prompt=(
                    "Classify Gmail messages for Prophet. "
                    "This Gmail path is only for portfolio-truth backfill and broker confirmations. "
                    "CRITICAL: DO NOT invent trade data. If an email is a promotion, newsletter, product update, or news alert, "
                    "you MUST set confidence=0.0 and ticker=null. NEVER provide a placeholder like 'EXMPL' if it is not explicitly "
                    "stated as a trade execution in the text. "
                    "Promotions, bonuses, newsletters, product updates, market-mover emails, and generic account notices "
                    "are not order confirmations and should not be ingested into the research graph. "
                    "If the message is a broker order confirmation, a dividend notice, or a cash deposit/withdrawal notice, extract the transaction fields exactly. "
                    "For 'confidence', provide a score from 0.0 to 1.0 based on how certain you are this is a real transaction confirmation. "
                    "For deposits/withdrawals, set document_type='cash_activity', ticker='CASH', action='deposit' or 'withdrawal', quantity=1.0, and price=amount. "
                    "For dividends, set document_type='dividend_notice', action='dividend', quantity=0.0, and price=amount. "
                    "For a STOCK SPLIT, set document_type='corporate_action', action='split', ticker=the symbol, and quantity=the post/pre ratio (e.g. a 4-for-1 split is 4.0, a 1-for-10 reverse split is 0.1). Only do this if the ratio is explicitly stated. "
                    "For an ACCOUNT TRANSFER / ACATS (shares moving in or out of the account), set document_type='account_transfer'; do NOT invent share counts or prices — leave them empty. "
                    "If a field like ticker, quantity, or price is unknown, provide an empty string for strings or 0 for numbers. Do not omit fields."
                    "Otherwise classify it as newsletter or other."
                ),
                user_prompt=json.dumps(
                    {
                        "subject": subject,
                        "sender": sender,
                        "body": bounded_document_excerpt(
                            body, head_chars=2200, tail_chars=700
                        ),
                    },
                    ensure_ascii=True,
                ),
                schema=ORDER_CONFIRMATION_SCHEMA,
                model=model,
            )
            # Post-call cooldown of 1.5s to respect rate limits
            await asyncio.sleep(1.5)
            return res

    async def _get_or_create_email_source(self) -> Source:
        existing = (
            await self.session.execute(
                select(Source).where(Source.name == GMAIL_OPERATIONAL_SOURCE_NAME)
            )
        ).scalar_one_or_none()
        if existing:
            if existing.is_trusted:
                existing.apply_learned_trust(
                    False,
                    reason=(
                        "Operational brokerage email is portfolio input, not an "
                        "independent research source."
                    ),
                )
            return existing
        source = Source(
            name=GMAIL_OPERATIONAL_SOURCE_NAME,
            source_type="email",
            description="Operational Gmail inbox source used only for broker-confirmation backfill and sync.",
            is_trusted=False,
        )
        self.session.add(source)
        await self.session.flush()
        return source

    def _decode_header(self, value: str) -> str:
        return str(make_header(decode_header(value))).strip()

    def _extract_text_body(self, message: Message) -> str:
        """Extract text from the message, preferring plain text but falling back to stripped HTML."""
        if message.is_multipart():
            for part in message.walk():
                content_type = part.get_content_type()
                disposition = str(part.get("Content-Disposition", ""))
                if content_type == "text/plain" and "attachment" not in disposition:
                    payload = part.get_payload(decode=True) or b""
                    charset = part.get_content_charset() or "utf-8"
                    content = payload.decode(charset, errors="ignore")
                    if (
                        "<html>" in content.lower()
                        or "<!doctype" in content.lower()
                        or "<div" in content.lower()
                    ):
                        return self._clean_html(content)
                    return content
            for part in message.walk():
                if part.get_content_type() == "text/html":
                    payload = part.get_payload(decode=True) or b""
                    charset = part.get_content_charset() or "utf-8"
                    return self._clean_html(payload.decode(charset, errors="ignore"))

        payload = message.get_payload(decode=True) or b""
        charset = message.get_content_charset() or "utf-8"
        content = payload.decode(charset, errors="ignore")
        if (
            message.get_content_type() == "text/html"
            or "<html>" in content.lower()
            or "<!doctype" in content.lower()
            or "<div" in content.lower()
        ):
            return self._clean_html(content)
        return re.sub(r"\s+", " ", content).strip()

    def _clean_html(self, html: str) -> str:
        """Strip tags and hidden elements from HTML."""
        # Strip <style> and <script> blocks completely, as they waste ~97% of character length
        html = re.sub(
            r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL | re.IGNORECASE
        )
        html = re.sub(
            r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE
        )
        # Remove hidden elements (broker mail can include a fake ticker preheader in a display:none div)
        html = re.sub(
            r"<div[^>]*display:\s*none[^>]*>.*?</div>",
            "",
            html,
            flags=re.DOTALL | re.IGNORECASE,
        )
        # Strip all tags
        text = re.sub(r"<[^>]+>", " ", html)
        # Collapse multiple spaces
        return re.sub(r"\s+", " ", text).strip()

    def _parse_email_datetime(self, value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            parsed = email.utils.parsedate_to_datetime(value)
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=UTC)
            return parsed.astimezone(UTC)
        except Exception as exc:
            import logging

            logging.getLogger(__name__).exception("Masked failure caught")
            return None

    def _parse_datetime_fallback(
        self,
        value: str | None,
        fallback: datetime | None,
    ) -> datetime:
        if value:
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    return parsed.replace(tzinfo=UTC)
                return parsed.astimezone(UTC)
            except Exception as exc:
                import logging

                logging.getLogger(__name__).exception("Masked failure caught")
                pass
        return fallback or datetime.now(UTC)

    def _is_allowed_message(self, message: Message, runtime) -> bool:
        sender = self._decode_header(message.get("From", ""))
        subject = self._decode_header(message.get("Subject", ""))
        normalized_sender = sender.lower()
        normalized_subject = subject.lower()

        sender_match = True
        if runtime.allowed_senders:
            sender_match = any(
                allowed.lower() in normalized_sender
                for allowed in runtime.allowed_senders
            )

        domain_match = True
        if runtime.allowed_domains:
            domain_match = any(
                normalized_sender.endswith(f"@{domain.lower()}>")
                or normalized_sender.endswith(f"@{domain.lower()}")
                or f"@{domain.lower()}" in normalized_sender
                for domain in runtime.allowed_domains
            )

        subject_match = True
        if runtime.required_subject_keywords:
            subject_match = any(
                keyword.lower() in normalized_subject
                for keyword in runtime.required_subject_keywords
            )

        # Naive Prophet Filter: Skip obvious marketing that waste LLM tokens and trigger 429s
        # Removed 'summary' and 'weekly' as they often contain brokerage context.
        blacklist = [
            "newsletter",
            "digest",
            "marketing",
            "promotion",
            "invite",
            "webinar",
        ]
        is_newsletter = any(word in normalized_subject for word in blacklist)

        return sender_match and domain_match and subject_match and not is_newsletter

    async def _record_skip(self, uid: str, message: Message, runtime=None) -> None:
        """Mark a message as skipped in the database so we don't scan it again."""
        source = await self._get_or_create_email_source()
        subject = self._decode_header(message.get("Subject", "(no subject)"))
        external_id = self._external_id_for_uid(uid, runtime)
        evidence = RawEvidence(
            external_id=external_id,
            source_id=source.id,
            source_item_type="email_skip",
            title=subject,
            is_processed=True,
            metadata_json={"skipped": True, "reason": "filter_rule", "uid": uid},
        )
        self.session.add(evidence)
        # Flush but don't commit here, as it's part of a larger scan transaction
        await self.session.flush()
