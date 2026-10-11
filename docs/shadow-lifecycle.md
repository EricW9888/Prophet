# Shadow experiment lifecycle

An autonomous run owns a planned start and end, a retained paper account, and
append-only transition records. The scheduler considers actionable work before
creation order. A future start or checkpoint waits; an expired end takes priority
over a future checkpoint or pending evidence. A run already executing does not
block another due run.

The end closes the observation window. The service cancels outstanding simulated
orders, releases their reservation and retains unconsumed receipts with an explicit
`window_closed` outcome. It takes a final valuation from current or retained
deterministic marks without requesting a new policy decision or making a late
trade. It records the planned end, actual capture, lateness and valuation basis.
It does not reconstruct missing historical prices or invent checkpoint observations.
Unexpected valuation/storage failures transition the run to failed where storage
is available. Provider failure cannot extend the planned window.

Checkpoint targets stay fixed. Pending evidence can keep a run active before its
deadline, but cannot increase its planned budget indefinitely. Direct execution of
a terminal run is idempotent. An explicit retry creates a new run with a fresh
snapshot and a link to its predecessor; it retains the old result, paper ledger,
checkpoint history and lesson links.

Late valuations remain useful audit observations. A capture more than one configured
shadow refresh interval beyond its planned end is excluded from family calibration.
Measured path statistics still describe their actual capture dates, with unavailable
metrics when measurements cannot support them. Terminal alpha and repeated paper
runs do not establish causal validity or trading readiness.

Family identity combines canonical instrument/subject IDs, mechanism, counterfactual
and numeric horizon. Names, policy prose, model family labels and evidence headlines
are not identity. Discovery receives existing structured identities and must reuse
their exact dimensions for equivalent proposals. A mismatched reuse is rejected;
different mechanisms, counterfactuals or horizons stay independent. A transaction
lock serializes canonical family creation, and a PostgreSQL partial unique index
permits only one queued/pending/running experiment per family.

Structured labels are a declared classification, not proof that two natural-language
hypotheses are equivalent. Older API callers receive exact-policy protection until
their dimensions are established. The migration resolves duplicates within existing
exact family membership, preferring the run with the most checkpoints. It snapshots
the original status/state first and records the retained run and decision basis.
It does not infer equivalence between differently worded legacy families.

Shadow Lab exposes explicit family reconciliation for that remaining review. The
operator supplies a retained run and reason after checking mechanism and
counterfactual. Different subjects or numeric horizons and conflicting structured
dimensions are rejected. Supersession stops current work and closes pending paper
orders/receipts; original family memberships, observations, results and lesson links
stay attached to their historical runs. Future proposals follow the retained family.

Dashboard and Shadow Lab use the same stored/derived count function. Queued and
running are stored states; waiting, due and overdue divide active work. Completed,
failed, skipped, superseded and manual accounts are separate. An expired run cannot
be hidden inside the waiting count.

For frontend validation while the primary app is running, set
`PROPHET_BUILD_VALIDATION=1` for `npm run build`. The build goes to the ignored
`.next-verification` directory rather than replacing the primary app's assets.
Use the same flag if starting that isolated build for a preview.
