# Product Vision

Prophet is a personal investment intelligence system for one investor. Its
purpose is to improve the quality and continuity of investment decisions by
maintaining a durable understanding of portfolio exposures, evidence, views,
uncertainties, and outcomes.

## Purpose And Non-Goals

Prophet should provide the research depth, institutional memory, portfolio
context, skepticism, and continuous monitoring of a small investment firm. It
should help an investor understand what changed, why it matters, what remains
uncertain, and what evidence would change the current view.

Prophet is not a generic chatbot, a feed of disconnected market facts, or a
system that treats activity as progress. It is not an oracle and should not
present uncertain analysis as prediction. It does not place real trades, move
money, or gain authority over investment accounts. Simulation and shadow work
exist to test ideas and improve judgment, not to become an execution path.

## Autonomous But Supervised

Prophet should be able to discover, investigate, connect, challenge, and revisit
important questions without requiring the investor to manually initiate every
step. Chat is one way to direct and inspect the system, not the whole product.
The same research capabilities should support ongoing monitoring, scheduled
review, and follow-up when new evidence changes a material assumption.

Autonomy must remain purposeful. Work should be prioritized by expected
decision value, portfolio relevance, uncertainty, and the cost of being wrong.
The investor remains the supervisor: they can inspect provenance, correct
context, challenge a conclusion, change priorities, and decide what action, if
any, to take. Prophet should surface consequential changes rather than silently
altering important beliefs or source trust.

## Evidence Before Belief

The language model may organize research, form hypotheses, identify missing
angles, and explain competing interpretations. It may not turn fluency into
fact. Retrieved material is a candidate input, not automatically relevant
evidence, and evidence is not automatically an accepted belief.

Important claims should remain traceable to their sources and dates. Their use
should account for source quality, independence, proximity to the underlying
event, corroboration, contradiction, and applicability to the subject. Search
rank, repetition, or apparent consensus must not substitute for relevance.
Exploration may remain provisional; unsupported assertions should not enter
accepted state merely because they fit an existing thesis.

## Time-Aware, Rationale-Preserving Memory

Prophet should preserve what happened, when it became knowable, when it entered
the system, and when it became eligible to affect a view. Historical material
can remain useful, but it must not masquerade as current information.

The system should preserve why it believed something: the evidence,
assumptions, alternatives, challenges, and revisions behind an accepted view.
When knowledge changes, prior state should normally be superseded, deprecated,
or bounded in time rather than silently erased. This makes it possible to
reconstruct decisions honestly, distinguish learning from hindsight, and see
whether a conclusion improved for the right reason.

## Decision-Relevant, Open-Ended Analysis

Research is useful when it clarifies a decision. Prophet should connect new
information to the mechanism that transmits it into a company, market, or
portfolio: what changed relative to expectations, who captures value, how the
effect reaches economics or risk, what the market may already reflect, and what
would confirm or disprove the interpretation.

Analysis should be open-ended rather than constrained to a fixed checklist.
Business models, incentives, competitive dependencies, capital allocation,
capital structure, valuation, externalities, sentiment, positioning, and other
dimensions matter when the situation makes them material. The system should
look both broadly enough to find non-obvious connections and deeply enough to
explain their causal significance. Facts without an investable transmission
route should not be presented as investment conclusions.

## Challenge, History, And Outcome Learning

Prophet should actively search for weak assumptions, alternative explanations,
counterevidence, and falsifiers. Agreement between repeated or dependent
sources is not independent confirmation. A strong view should explain what
could make it wrong and which observations would distinguish competing causes.

Historical episodes should be used as mechanism tests, not templates or
predictions. An analogy is valuable when its causal channel, differences, and
confounders are explicit. Replay and evaluation must respect what was knowable
at the time so that hindsight does not leak into the lesson.

Later outcomes should improve the system's calibration of sources, hypotheses,
decisions, and shadow experiments. It should preserve counterexamples and
distinguish leading signals from lagging confirmation. Repeated success may
strengthen a lesson, but it must never create automatic trading authority.

## Quiet, Inspectable, High-Signal Experience

Prophet should make complex research understandable without flattening it.
Interfaces should lead with significance, provenance, time, uncertainty,
portfolio relevance, and the next useful question. Detail should be available
progressively for inspection rather than overwhelming the primary workflow.

Empty, stale, degraded, and disputed states should be honest and actionable.
Refreshes and background work should preserve the investor's context. The
system should avoid filler, unexplained labels, raw internal traces presented as
answers, and visual density that hides the relationship between evidence and a
decision.

## Document Boundary

The README explains what Prophet is and how to begin. This document owns durable
product direction and principles. `docs/architecture.md` describes how the
current system actually works, and `docs/limitations.md` records current honest
boundaries. GitHub Issues and pull requests own current engineering work.
Private operator or agent guidance remains outside the public repository and is
not a second source of product truth.
