# Investor workflows

Prophet's primary navigation groups recurring investor tasks. Portfolio is the
daily starting point; Research investigates a question; Monitor reviews changed
evidence; Knowledge follows mechanisms and their source support; Review checks
material uncertainty; Simulate observes a paper-only hypothesis. The secondary
menu holds portfolio records, the research library and system activity. Settings
owns setup, connector configuration and presentation preferences.

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

Every surface must distinguish its initial load from an observed empty result.
A failed refresh retains the last loaded state with a visible stale/degraded
message and a retry action. Secondary failures disclose which information is
unavailable while preserving usable primary data. An action failure stays near
the action and does not erase unrelated loaded evidence. Unknown, skipped and
quarantined stages do not receive a completed-work success indicator.

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
