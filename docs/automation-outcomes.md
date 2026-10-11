# Automation outcomes and provider recovery

Agent Desk separates live analyses, cached results, fallback records, deferred
requests and failed requests. Its analysis-record total is the sum of those
categories and unclassified legacy labels. Unknown labels are not silently counted
as successful model work. Independent critique records are visible separately from verification
records; retained token estimates include both reasoning and critique records.
These receipts describe work performed, not proof that an investment conclusion
is correct or that a provider bill reconciles to local estimates.

Evidence work distinguishes fetched source content, enrichment, persisted knowledge,
rejection, intentional extraction skips and provider deferral. A conversation receipt
does not become investment knowledge merely because processing finished. Source
discovery and ingestion share an operation ID, with retained discovery/ingest phases.
Legacy log entries without that ID stay separate; proximity or similar text does
not establish that two requests are the same operation.

Provider health is durable machine-local state, stored atomically beside the runtime
settings. The configuration fingerprint includes provider, endpoint/model and a hash
of the credential; raw credentials, prompts and provider error bodies are not stored.
The state is unobserved, healthy, open or probing. Missing credentials and unreadable
health storage are disclosed separately. A configuration change begins unobserved
without deleting the prior configuration's failures.

After a provider failure, model-dependent jobs defer before starting new analyses or
spending their domain attempt budgets. Deterministic ledger ingestion, market data,
paper maintenance, deadline closure, freshness, integrity and backup jobs continue.
Evidence selection still allows intentional extraction skips while the model is held.
Repeated unchanged deferrals do not manufacture action-feed work.

One shared probe job owns recovery. It waits for persisted backoff, claims a timed
lease and requests a small schema-validated generation from the configured provider.
An HTTP model-list response or CLI help command cannot reopen the circuit. The probe
uses existing provider credentials/rate limits and no implicit paid or local fallback.
The operator can request that same probe in Agent Desk; it honors the shared lease
and backoff. A successful probe reopens work; another failure increases the backoff.
An older request/probe cannot erase a newer failure or probe result.

This coordination supports Prophet's single-primary-process runtime. It is not a
distributed multi-worker lease system. Preserve the ignored provider-health file
across restarts; investigate storage failure rather than deleting a hold to force
requests through. A crashed probe lease expires so a later probe can recover.

Automation status reflects whether the scheduler is actually enabled and running.
Disabled or unobserved jobs do not appear healthy by an empty-list comparison.
Operational scheduler status and completed investment work are separate facts.
