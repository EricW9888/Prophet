from __future__ import annotations

import imaplib
from typing import Any


def build_imap_search_query(runtime: Any, base_mode: str) -> str:
    parts = [base_mode]

    if runtime.allowed_senders:
        sender_parts = [f'FROM "{sender}"' for sender in runtime.allowed_senders]
        if len(sender_parts) > 1:
            # IMAP OR is binary: OR query1 query2
            # Correct nesting for N parts: OR p1 (OR p2 (OR p3 p4))
            query = sender_parts[-1]
            for next_part in reversed(sender_parts[:-1]):
                query = f"OR ({next_part}) ({query})"
            parts.append(query)
        else:
            parts.append(sender_parts[0])

    if runtime.required_subject_keywords:
        keyword_parts = [f'SUBJECT "{kw}"' for kw in runtime.required_subject_keywords]
        if len(keyword_parts) > 1:
            query = keyword_parts[-1]
            for next_part in reversed(keyword_parts[:-1]):
                query = f"OR ({next_part}) ({query})"
            parts.append(query)
        else:
            parts.append(keyword_parts[0])

    if len(parts) > 1 and base_mode == "ALL":
        parts.pop(0)

    # Combine with spaces (implicit AND)
    return " ".join(parts)
