# Portfolio daily-risk advisories

The Risk workspace separates the benchmark's existing 30-day trend label from
risk in the invested holdings. A flat broad index can coexist with large,
correlated position moves. Historical stress describes that exposure; it does
not predict the direction or timing of the next session.

An elevated stress statistic can persist across many sessions, including rising
ones. A budget breach therefore calls for exposure review; it does not establish
that a drop is imminent or that conditions just worsened. The watch is one-shot.
Rearming under unchanged above-budget conditions can trigger at the next check.
Paper-policy validation must measure warning frequency, missed losses, lost
upside, response latency and re-entry across independent market episodes rather
than selecting a threshold from one attractive retrospective event.

## Read the assessment

**Check portfolio risk** captures completed daily observations. Background risk
refreshes use the same service when automation and market data are enabled. This
deterministic path needs no LLM, but the host, database and market-data provider
must work. The page shows the last successful check, pauses, stale checks,
missing holdings, matching-history count and captured data cutoff. Failed API
requests retain the previous assessment with an error.

Current ledger quantities and completed unadjusted closes determine position
values. Up to 60 matching provider-adjusted daily returns are applied
simultaneously to those values, preserving observed co-movement. Historical daily
tail stress is the mean loss in the worst 5% of that small sample. The worst
session and its position contributions are shown separately. Neither statistic
is a calibrated VaR, loss bound, probability, causal factor model or forecast.

At least 40 matching returns and complete coverage are required for a budget
verdict. Missing observations never become zero returns. Alignment uses both
endpoints so a two-observation gap cannot be compared with a one-observation
move. Different latest dates, unsupported currencies/payoffs, duplicate dates,
missing adjusted history and stale closes are coverage gaps. Covered-position
count is not value-weighted coverage. Cash is excluded. When enough matching
history exists for a covered subset, its stress and contributors remain visible,
explicitly scoped to that subset; the full-book loss-budget verdict stays
unavailable. Partial stress is not a lower bound on whole-portfolio loss.

The initial calculation supports USD quotes and positive common-stock, ADR or
plain-ETF holdings with multiplier one. It does not infer fund holdings, convert
currencies, reclassify securities, repair quantities or post contingent deals to
the ledger. Review identities and pending receipts independently. Adjusted price
history handles provider-reported splits/dividends; it does not prove the ledger
has been reconciled.

## Choose a watch deliberately

For a verified symbol change of the same instrument, **Correct a listed symbol**
retains the old/new names, effective date, owner reason, reference URL and actual
recording time in the existing append-only knowledge audit. The catalog update
uses an expected-current-symbol guard, serializes competing updates and rejects
conflicting current or historical identities. Existing position/security IDs,
quantities, lots, transactions, cash and original receipts remain intact.
Backfilled broker imports resolve historical names through this audit rather
than creating a second security. Reused or ambiguous symbols require identity
review. These are owner-confirmed metadata corrections, not independently
verified corporate actions or reconstructed historical provider vintages.

`POST /api/portfolio/securities/{id}/symbol` requires an explicit same-instrument
confirmation; `GET /api/portfolio/securities/{id}/symbol-history` exposes its
receipt. Future effective dates cannot change today's catalog. A split, merger,
ADR conversion or other quantity change needs the portfolio reconciliation path.

### Loss budget

Set a positive USD loss budget and maximum completed-close age. The default age
is 96 elapsed hours including weekends, not an exchange-calendar guarantee.
There is no default monetary budget. The generic model watch tool cannot create
or replace an owner risk budget.

A watch triggers once when historical tail stress is at least that budget. Its
decision links the exact assessment/hash and records evaluation and trigger
times. Holding, checking catalysts, reducing overlapping exposure and testing a
smaller shadow position remain owner choices. Proportional reduction-to-budget
arithmetic assumes uniform scaling with cash held; it omits execution, taxes and
changed future shocks, so it is not a trade instruction or guaranteed protection.

Insufficient data leaves the watch armed with an unavailable decision. A breach
disarms it until explicitly rearmed after review. Identical saves and repeated
checks do not create repeated alerts. Disarm cancels a pending watch. This
controls noise but does not alert again on a worsening already-triggered
condition without rearming.

Delivery reuses the opt-in push outbox. The external message is generic, opens
Risk and contains no holdings or budget. Detection, queuing and delivery are
separate. No subscription or disabled push means no delivery record. A `sent`
timestamp records push-service acceptance, not proof the owner saw the message.
Browser background push and owner response add possible latency.

## Inspect the receipt

`GET /api/risk/portfolio/packets/{assessment_id}` returns the frozen packet and
SHA-256 of canonical JSON. It retains version, cutoff, security IDs/current
identity metadata, quantities, provider/basis, retrieval times and daily
completion/availability clocks. Existing `ScenarioAnalysis` records own captured
inputs/results, `ActiveWatcher` owns advisory state and push tables own delivery.
No parallel portfolio ledger is maintained.

Equivalent daily inputs reuse the original packet and detection time. A mutable
`last_checked_at` heartbeat outside the hashed packet records successful polling.
Changed economic inputs, freshness status or freshness policy create a new
assessment. Exact capture retries have a deterministic record identity.

Daily provider labels may identify session open. The adapter uses the provider's
regular-session end when its date matches the bar. Older dates without verified
schedules conservatively complete at the next exchange-local midnight. Unknown
timezone falls back to UTC. This can delay availability and does not reconstruct
historical exchange schedules. Acquisition must also precede the packet cutoff.

Today's Yahoo history is a current revision, not proof of what was retrievable
before a past event. An explicit historical cutoff returns
`historical_capture_required` without fetching today's history; replay a frozen
packet instead. Explicit-cutoff shadow/opportunity contexts reuse the latest
actual earlier portfolio capture, with its hash and data age, when the requested
symbol was captured; absent symbols remain a coverage gap. Live shadow discovery
fixes its cutoff after tape acquisition, includes the matching one-session return
with volume, and omits raw bar arrays from model context.

## Validation boundary

Synthetic regressions exercise shared exposure with a resilient benchmark,
rising/quiet samples, future-data exclusion, stale/missing data, split adjustment,
actual watch/outbox persistence, retry deduplication and explicit rearming. The
deterministic benchmark compares hold, the unchanged monthly benchmark rule and
one declared fractional response. It includes delivery/response latency, opening
gaps, sale/re-entry costs, lost upside, unnecessary alerts and host downtime.
Separate outcome quotes cannot alter decision inputs or backdate fills.

The control maps the unchanged monthly benchmark label to the same declared
hypothetical response. It does not establish that the previous production path
delivered an alert or executed that response.

These are regression and causal-accounting controls, not unseen live outcomes,
a measured false-positive rate or an optimal policy. Synthetic aggregate-book
quotes omit executable bid/ask prices, liquidity, impact and taxes. The same
policy can protect during a decline and lose upside during a rebound.

Intraday catalyst-to-exposure reasoning, effective-dated symbol/corporate-action
revisions, original historical provider vintages, calibrated holdouts and
prospective response evaluation remain in
[Issue #149](https://github.com/EricW9888/Prophet/issues/149). These controls do not
establish that a historical loss was prevented.
