from __future__ import annotations

import email
import imaplib
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from investos.core.llm import call_llm_json
from investos.services.runtime_settings import RuntimeSettingsStore

DISCOVERY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "potential_brokers": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "name": {"type": "string"},
                    "sender": {"type": "string"},
                    "pattern_found": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": ["name", "sender", "pattern_found", "confidence"],
            },
        }
    },
    "required": ["potential_brokers"],
}


class GmailDiscoveryService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def discover_potential_brokers(self, limit: int = 50) -> dict[str, Any]:
        runtime = RuntimeSettingsStore.load().gmail
        if not runtime.username or not runtime.password:
            return {"ok": False, "error": "Gmail credentials missing"}

        mailbox = imaplib.IMAP4_SSL(runtime.imap_host, runtime.imap_port)
        try:
            mailbox.login(runtime.username, runtime.password)
            mailbox.select("INBOX")  # Discovery always starts at INBOX

            # Search for common broker keywords in headers to be fast
            keywords = [
                "confirmation",
                "order",
                "executed",
                "trade",
                "fidelity",
                "schwab",
                "robinhood",
                "vanguard",
                "e-trade",
            ]
            found_headers = []

            for kw in keywords:
                _, data = mailbox.search(None, f'SUBJECT "{kw}"')
                ids = data[0].split()[-10:]  # Last 10 per keyword
                for msg_id in ids:
                    _, raw_header = mailbox.fetch(
                        msg_id, "(BODY[HEADER.FIELDS (SUBJECT FROM DATE)])"
                    )
                    if raw_header and raw_header[0]:
                        msg = email.message_from_bytes(raw_header[0][1])
                        found_headers.append(
                            {
                                "subject": str(msg.get("Subject")),
                                "from": str(msg.get("From")),
                                "date": str(msg.get("Date")),
                            }
                        )

            if not found_headers:
                return {
                    "ok": True,
                    "potential_brokers": [],
                    "detail": "No broker patterns found in recent INBOX headers.",
                }

            # Use LLM to identify actual brokers from headers
            analysis = await call_llm_json(
                system_prompt="Identify financial brokers or investment platforms from these email headers. Return potential sender addresses to whitelist.",
                user_prompt=str(found_headers[:30]),
                schema=DISCOVERY_SCHEMA,
            )

            return {
                "ok": True,
                "potential_brokers": analysis.get("potential_brokers", []),
            }

        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        finally:
            try:
                mailbox.close()
                mailbox.logout()
            except (imaplib.IMAP4.error, OSError):
                pass
