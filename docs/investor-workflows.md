# Investor workflows

Prophet's primary navigation groups recurring investor tasks. Portfolio is the
daily starting point; Research investigates a question; Monitor reviews changed
evidence; Knowledge follows mechanisms and their source support; Review checks
material uncertainty; Simulate observes a paper-only hypothesis. The secondary
menu holds portfolio records, the research library and system activity. Settings
owns setup, connector configuration and presentation preferences.

Workspace navigation and task surfaces use the available window width with a
shared gutter. Reading areas, forms and dialogs can still constrain their own
line length. On wide screens, drag the Knowledge inspector or Research sidebar
divider, or focus it and use the arrow keys. Double-click the divider or press
Enter to restore its default width. Each pane can collapse and keeps its layout
in that browser. On compact screens, the Knowledge inspector stacks below the
map and Research uses a compact workspace drawer.

Research separates **Chats**, **Autonomous**, and **Watches**. Chats are user
conversations and their drafts. Autonomous contains saved background findings
with their trigger and evidence, and opens independently of the current draft.
The latest finding for each subject is visible first; earlier reviews remain searchable.
Discussing a finding prepares a new chat without sending it. Watches are
conditions still being monitored. Activity remains the dated receipt for work,
deferrals, and errors.

| Task | Starting surface | Context and next action |
| --- | --- | --- |
| Establish portfolio truth | Settings, Positions, History | Configure a scoped connector, inspect imported receipts and identity reviews, then reconcile holdings and cash against broker records. Setup readiness does not certify external reconciliation. |
| Review the day | Portfolio, Monitor | Inspect changes, relevance to holdings and review items. Follow the subject or source record before deciding whether more research is needed. Source event/publication time stays distinct from ingestion time. |
| Investigate a question | Research | Keep the subject and conversation context, resolve ambiguity, inspect sources and material gaps, then request deeper work or verification. Model prose does not imply accepted state. |
| Assess a source | Sources, evidence receipt | Inspect origin, source text, processing stage and provenance. Retry a failed stage or review trust. Quarantine retains a receipt while excluding it from investment knowledge. |
| Understand mechanisms | Knowledge, subject profile | Follow focused neighborhoods, source-backed relationships, contradictions and history. System bookkeeping is progressively disclosed. |
| Check conclusions | Review, Decisions | Inspect evidence and verification outcomes before recording a decision. Retained analyses, critiques, accepted views and investment decisions have distinct meanings. |
| Monitor a hypothesis | Research watches, Monitor | Inspect the monitoring condition, evaluation state and source times. Deferred checks remain pending; an unexecuted check is not a successful observation. |
| Learn from a paper outcome | Simulate | Inspect the frozen baseline, policy, paper receipts and measured result. Compare dated observations with the baseline, then inspect retained lessons. A retry starts a new run. |
| Recover unavailable work | Activity, Settings, Agent Desk | Identify whether configuration, provider, storage or a processing stage is unavailable. Deterministic work can continue during model deferral; the shared structured-generation probe owns recovery. |

The Portfolio watchlist includes open holdings and explicitly tracked names,
one row per security. Its order is a workspace preference and does not change
ledger positions, watch conditions, or research priority.

Holdings value history reads retained, same-session closing marks for the
captured book. It excludes cash, shows gaps when holdings are missing or have
different price sessions, and links to each receipt. It is not broker equity or
a cash-flow-adjusted return. Risk's what-if applies an explicit uniform shock
only if the latest capture has full coverage. Shadow shows retained paper
equity at checkpoints. Fill and decision markers use their own dates and do
not imply an unobserved value at the event time.

Every surface must distinguish its initial load from an observed empty result.
A failed refresh retains the last loaded state with a visible stale/degraded
message and a retry action. Secondary failures disclose which information is
unavailable while preserving usable primary data. An action failure stays near
the action and does not erase unrelated loaded evidence. Unknown, skipped and
quarantined stages do not receive a completed-work success indicator.

Sources and Discovery load the active view's reads independently. Settings,
History, Risk, and Simulate retain primary content while secondary reads finish
or report a failure. Knowledge builds each scoped view in one backend read,
then calculates node positions off the browser's main thread. Selecting a node
loads its inspector separately. Resizing the inspector changes the visible
canvas without rebuilding the graph or resetting its camera.

Knowledge lists all open holdings, including those without a saved research
profile. A broad portfolio update checks recent stored evidence by holding and
makes bounded external checks of the market, relevant industries and material
holdings. The answer's coverage receipt distinguishes the holdings roster,
explicit links in the selected evidence, and provisional external candidates.
An issuer label with a ticker resolves to a catalog entity only when the name
and symbol agree. The audited entity cleanup uses the same check for old labels.
When identities are consolidated, equivalent links keep their canonical
relationship and audit the retired link; links that would point back to the same
subject are recorded in the audit instead of creating new graph connections.
The live work log records observable stages. Text attachments are read-only
inputs for one turn; Prophet retains filenames and the answer, not the input
files themselves.

Activity keeps successful routine price, risk and backup refreshes behind
**Include routine refreshes**, so those receipts cannot push research,
deferrals or failures out of the default catch-up feed.

Monitor shows a claim's target date separately from when its review became
eligible. Late discovery can inform outcome learning using dated evidence
without backdating Prophet's knowledge. A pasted broker statement can be
previewed before adding discrepancies to Review. Paper simulation measures each
account against its own starting value and excludes a run that began after the
planned close from lesson calibration.

Diagnostics, provider details and provenance belong behind disclosure controls
when they are not needed for the immediate decision. Source links retain evidence
identity; subject and time context must remain explicit when crossing surfaces.
Translations are labeled, versioned presentation derivatives with an unchanged
original, as defined in [Presentation language](presentation-language.md).

The daily Portfolio surface puts its attention queue and holdings before
research activity and system details. Operational receipts, research-question
history and paper-run counts remain available in disclosure panels. An empty
queue means no reviews are currently queued; it does not establish complete
coverage or an absence of portfolio risk.

Review producers record a purpose separately from the subject and queue owner:
`investment` for evidence, questions, views and paper outcomes; `portfolio_data`
for receipt identity, posting holds and broker reconciliation. A position may
have both kinds of concern. Portfolio shows investment attention and a visible
counted link to portfolio data reviews; those holds remain unresolved until their
own review completes. This separation never lowers priority, links a receipt,
posts a transaction or certifies balances.

Review offers All, Investment and Portfolio data views. The API filters by
`domain` before deterministic pagination and returns counts for both domains;
omitting the filter still returns all active reviews. Exact concern lookup is
independent of the filter, and changing views preserves a prepared challenge.
Dashboard `review_queue` and `review_queue_total` describe investment work;
`portfolio_data_review_total` counts the separately visible data reviews.
Migration `d9f6b3c1a854` backfills recognized receipt types and the existing
position-reconciliation producer format without changing identities, priorities,
statuses or evidence. Unknown legacy concerns remain investment-visible rather
than disappearing through an inferred classification. New producers own the
stored domain; runtime presentation never classifies a review by its prose.

Refreshing the queue replaces only pending computed reviews owned by that
refresher. Connector, reconciliation and unclassified legacy concerns retain
their IDs, and a review already in progress takes precedence over a newly
computed duplicate. Migration `c8e5a2b0f743` adds ownership without deleting
records; it recognizes existing generator type/format pairs and conservatively
retains unknown legacy reviews.

Review records carry resolved subject, profile, original-receipt or paper-run
identities through the API. Queue IDs, position IDs and profile IDs are distinct;
missing records retain their concern without an invented destination. A position
review uses its own profile when available, then its security's entity profile.
Its research and verification actions keep the original position subject even
when the linked reading profile belongs to the underlying company.
Mailbox reconciliation reviews store an explicit original-evidence reference
separately from their deduplication key. Their title and receipt action use that
reference; the review ID, posting hold and ledger remain unchanged. Migration
`e0a7c4d2b965` backfills direct receipt IDs and exact unique legacy producer keys.
Conflicting or missing legacy receipt mappings remain unresolved, with no guessed
account or original.
The Review surface progressively loads concerns in priority order with an
explicit total, including concerns beyond the first API page. A linked concern
is resolved independently of that page. Mailbox identity concerns explain
the posting hold and direct the owner to receipt comparison in Data Connections.
Holdings and tracked names open fresh, unsent research drafts for their positions.
Profile facts and claims link through their source item to the unchanged receipt;
nodes without that provenance say so explicitly.

Investigate opens an unsent draft with the concern and its subject attached.
Prepare a challenge selects that concern's subject and question; verification
runs only when the user submits the form. Opening either link does not start
research or verification. Research conversation links open the specified session,
and an existing session takes precedence over an unrelated subject parameter.
Users can release a draft's subject context or start a new unscoped chat. A late
history or context-preview response cannot overwrite a newer selection.
If computed reviews refresh between a click and navigation, a challenge link
retains its original subject and unsent question only when that subject still
has a current profile. The UI discloses that the concern is no longer queued;
missing context never silently selects a different subject.

The interaction choices draw on subject-scoped alert navigation in
[Koyfin](https://www.koyfin.com/help/release-notes/v3-66-desktop-alerts/) and
source traversal alongside research in [Quartr](https://quartr.com/features/ai-chat).
These references inform presentation; Prophet's evidence and state contracts
remain authoritative.

Representative browser verification covers setup and imported receipts; daily
review through Monitor to a source; research through evidence and verification;
question retirement/renewal; paper lifecycle history/retry; provider failure and
recovery; and language preference/translation/original traversal. Exercise desktop
and compact widths, inspect overflow and control visibility, and distinguish
observed runtime proof from unit/API/build checks. [Issue #41](https://github.com/EricW9888/Prophet/issues/41)
tracks completion of that product-wide browser acceptance.
