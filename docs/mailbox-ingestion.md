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
- Deferred receipts retain their source text and last attempt time privately.
  A bounded scan handles never-attempted receipts first, then the least-recently
  attempted retries, so an unavailable classifier cannot permanently hide later
  supported confirmations. Reclassification reuses the same stored receipt.
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

Every scanned occurrence uses the saved login account namespace, exact folder,
server UIDVALIDITY and UID. Password rotation preserves the namespace; changing
the login account or IMAP endpoint changes it. Gmail host aliases and username
case are normalized; other servers retain username case. The namespace hashes
the endpoint, port and username. Scanning stops when UIDVALIDITY is unavailable.

Gmail's `X-GM-MSGID` binds label copies and mailbox resets to one receipt within
that account. The capability is negotiated after login; a missing identifier
when advertised is a failed fetch. A Gmail endpoint that does not advertise the
extension stops rather than changing identity conventions. Other IMAP servers use an exact RFC message
SHA-256 within the account. Message-ID is a duplicate hint, never identity proof.
Changed content with a previously seen provider identifier, or an unproven
message after a generic IMAP UIDVALIDITY reset, waits for identity review.
Each binding records its occurrence identifiers and content hash. Original RFC
messages are kept in private object storage, including messages held for review.

The migration marks legacy identities unverified without changing old receipt
IDs, evidence, corrections or transaction provenance. A bare UID or old
folder-plus-UID cannot suppress a new scoped receipt. Ambiguous matches wait for
explicit reconciliation in **Settings > Data Connections > Receipt identity
review**. Compare the preserved evidence and ledger before selecting **Confirm
same receipt** or **Confirm separate receipt**. Linking preserves the previous
transaction source identity and canceled/corrected status. Confirming a separate
receipt schedules ordinary classification on the next scoped sync; it does not
post a transaction during review. The decision, time and selected evidence are
retained, and occurrence/provider keys are unique in PostgreSQL. Restore a
pre-upgrade backup to return to a schema without these bindings.

This supports safe identity separation when the saved account changes; it does
not add simultaneous account configuration, expand the saved scope or reconcile
holdings across broker accounts.
Supported template parsing is intentionally limited; unknown documents may need
hosted classification and review. No paid connector or local model is required.

## Reconciliation

Compare source receipts to transaction provenance, then independently replay
settled transactions and compare quantities, lots, and cash. Check receipt gaps,
duplicate candidates, missing opening balances, corrections, corporate actions,
and settlement dates. Do not fill a discrepancy with an invented balancing
transaction. Use the broker snapshot reconciliation flow to compare an actual
statement/export and keep unresolved differences visible for review.
