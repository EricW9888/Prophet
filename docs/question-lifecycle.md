# Research questions and unfinished work

A fetched source does not answer a question. Prophet assesses processed, relevant
evidence before recording a resolution. Irrelevant evidence returns the question
to research; a supported partial answer retains its summary and residual
uncertainty. Missing or degraded extraction waits for extraction, while a provider
failure waits for a provider retry without consuming a completed assessment.

Active questions have an owner, next action, review date and last material
outcome. Automatic research and assessment each have a three-attempt budget per
question. Unfinished research retries after 24 hours; an extraction wait ends
after seven days or when extraction becomes terminal. Exhausted work is abandoned
with its provenance retained. These are conservative automation limits, not a
claim that three searches exhaust the available evidence.

Questions use the canonical company subject behind a position or security, plus
structured mechanism, time horizon and decision relevance. Coverage audits see
existing uncertainties and reuse their IDs and dimensions for paraphrases.
Matching dimensions converge on a database-enforced unique key. Distinct
mechanisms, horizons and decisions remain separate even when their wording is
identical. This relies on the structured assessment correctly identifying the
uncertainty; it is not a guarantee of perfect natural-language equivalence.
Legacy or manually supplied prompts remain explicitly unclassified until those
dimensions are established. Merged questions retain their original rows,
resolution links and pointers to the canonical question.

The dashboard's Research work panel separates eligible active questions, work
due now, all retained questions and material outcomes in the last 24 hours.
Automated eligibility covers the portfolio and companies, securities and
positions represented by holdings, watchlist or considering entries. Eligibility
and review dates are checked before limiting scheduler candidates. Broader
untracked uncertainties remain historical rather than silently consuming the
portfolio research queue.

Inspect a question's history to see preserved prompts, evidence outcomes and
owner decisions. Renew research budget schedules a new bounded search for a
tracked subject. Retire to history removes the scheduled work while preserving
the question and its resolutions. Both actions require a reason, which is retained
with the previous attempt counts. Coverage refreshes may reprioritize archived
uncertainties, but do not silently renew exhausted budgets or reopen answered
questions. Provider failures do not replace the backlog with fallback questions.

Migration `9f4b8d2e5c30` records every original question state before changing its
work state. Legacy fetched questions receive an assessment action; open questions
without evidence older than 30 days become unprioritized history. The migration
does not guess semantic identities or change old resolution/evidence links.
Downgrading requires restoring a pre-upgrade backup because discarding lifecycle
history is not supported.
