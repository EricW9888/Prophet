# Mailbox Ingestion

Gmail/IMAP is an optional receipt input, not a brokerage holdings API. It is
useful for free incremental capture of supported confirmations. A broker
statement or activity export remains necessary for external reconciliation:
emails can omit fees, interest, settlement changes, transfers, and tax-lot detail.
An instant-deposit availability notice is not proof of a settled deposit.

## Import Boundary

- Only the saved folder and explicit sender/domain/subject scope are searched.
  Folder names do not add hidden subject filters.
- Connections verify TLS certificates and select mailboxes read-only. Fetches
  use `BODY.PEEK[]`; reading mail elsewhere does not mark it imported in Prophet.
- Scans select outstanding receipts before applying their processing budget.
  Remaining work and failed messages are reported separately from posted trades.
- A temporary model failure defers unclassified messages without marking them
  imported. Supported templates still run within the scan budget; the same scan
  does not repeatedly call an unavailable model. Deferred messages remain pending
  for the next run. Partial/busy runs are warnings, not completed imports.
- A PostgreSQL advisory lock serializes mailbox scans. Receipt mutations are
  sequential; the final portfolio replay uses transaction dates, not IMAP UID order.
- Stored evidence alone is not an import checkpoint. An operational receipt
  must link to a transaction (including one subsequently corrected or canceled)
  or a durable reconciliation review. Interrupted posting reuses its evidence.
- Supported explicit broker templates can populate validated transaction fields.
  Model-only extractions are review proposals, not authorization to post money or
  shares. Invalid numbers, absent fields, complex corporate actions, and options
  without a unique contract identity require reconciliation.

## What Passing A Scan Does Not Prove

`ok` means the selected scope had no remaining scan work or failed receipts.
It does not mean the account agrees with a broker statement. Receipts awaiting
review may still contain unresolved account activity. A successful model or
mailbox connection test does not establish complete transaction history.

The legacy receipt identity uses folder and UID; it does not yet provide a full
account/UIDVALIDITY migration or Gmail cross-label message identity. Do not
repoint an established import at a different account and assume deduplication is
safe. Multi-account ingestion needs a dedicated identity migration and tests.
Supported template parsing is intentionally limited; unknown documents may need
hosted classification and review. No paid connector or local model is required.

## Reconciliation

Compare source receipts to transaction provenance, then independently replay
settled transactions and compare quantities, lots, and cash. Check receipt gaps,
duplicate candidates, missing opening balances, corrections, corporate actions,
and settlement dates. Do not fill a discrepancy with an invented balancing
transaction. Use the broker snapshot reconciliation flow to compare an actual
statement/export and keep unresolved differences visible for review.
